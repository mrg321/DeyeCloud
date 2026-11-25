"""Record decisions about private Deye recommendations. No inverter client."""

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from collect_deye_daily import CollectionError
from deye_history_quality import MADRID
from deye_private_store import GitHubPrivateStore, canonical
from deye_tou import read_periods
from deye_tou_write import apply_approved, execution_binding
from deye_control import DeyeControlClient


COMMAND = re.compile(r"/deye (approve|reject|cancel) (\d{4}-\d{2}-\d{2}) ([a-f0-9]{64})")
PROPOSAL_PATH = re.compile(r"approvals/proposals/(\d{4}-\d{2}-\d{2})/([a-f0-9]{64})\.json")
DECISIONS = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}
UTC = timezone.utc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def proposal_path(day, identity):
    return f"approvals/proposals/{day}/{identity}.json"


def decision_path(day, identity):
    return f"approvals/decisions/{day}/{identity}.json"


def expiry(day, cutoff):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cutoff):
        raise ValueError("INVALID_CUTOFF")
    local_day = date.fromisoformat(day) - timedelta(days=1)
    local = datetime.combine(local_day, time.fromisoformat(cutoff), MADRID)
    # Reject ambiguous or skipped wall times rather than guessing a DST boundary.
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        raise ValueError("AMBIGUOUS_CUTOFF")
    return local


def reviewer_ids(raw):
    identities = json.loads(raw)
    if (type(identities) is not list or not 1 <= len(identities) <= 10
            or any(type(value) is not int or value <= 0 for value in identities)
            or len(set(identities)) != len(identities)):
        raise ValueError("INVALID_REVIEWERS")
    return set(identities)


def issue_text(payload, identity, report, *, sort_write_keys=True):
    if payload["schema_version"] == 2:
        return tou_issue_text(payload, identity, report["tou_simulation"], report["forecast"],
                              sort_write_keys=sort_write_keys)
    result, forecast = report["recommendation"], report["forecast"]

    def value(record, key, suffix=""):
        number = record.get(key)
        return f"{number:.2f}{suffix}" if type(number) in (float, int) else "No disponible"

    targets = payload["current_targets"]
    baseline = " / ".join(f"{target} %" for target in targets) if targets else "No disponible"
    explanation = str(result.get("explanation") or "").replace("@", "＠").replace("<", "&lt;")
    body = [
        f"**Fecha objetivo:** {payload['date']}",
        f"**SOC actual:** {value(result, 'current_soc', ' %')}",
        f"**TOU actual a las 00:00 / 05:00:** {baseline}",
        f"**SOC recomendado:** {value(result, 'recommended_soc', ' %')}",
        f"**Sol efectivo:** {value(forecast, 'sunshine_hours', ' h')}",
        f"**Producción FV:** {value(forecast, 'pv_kwh', ' kWh')}" + pv_forecast_explanation(forecast),
        f"**Consumo previsto:** {value(result, 'expected_load_kwh', ' kWh')}",
        f"**Aporte de batería 08:00–24:00:** {value(result, 'expected_battery_output_08_24_kwh', ' kWh')}",
        f"**Ahorro estimado:** {value(result, 'estimated_saving_eur', ' EUR')}",
        f"**Confianza:** {result['confidence']}",
        f"**Hora límite (Madrid):** {payload['expires_at']}",
        explanation,
    ]
    if result.get("gross_saving_eur") is not None:
        body.insert(12, f"Mejor SOC por coste: {value(result, 'optimal_soc', ' %')}. "
                    f"Ahorro bruto: {result['gross_saving_eur']:.6f} EUR. "
                    f"Mínimo exigido: {result['minimum_saving_eur']:.6f} EUR.")
    if "lower_targets_verified" in result:
        body.insert(12, f"**Reserva operativa:** {value(result, 'reserve_soc', ' %')} · "
                    f"**Mínimo nocturno permitido:** {value(result, 'minimum_allowed_soc', ' %')}.")
    if "night_soc" in result:
        body.insert(12, night_soc_text(result["night_soc"]))
    if payload["actionable"]:
        body += ["Comenta exactamente uno de estos comandos:", "~~~text",
                 *(f"/deye {command} {payload['date']} {identity}" for command in DECISIONS), "~~~"]
    else:
        body.append("Informe informativo. Esta recomendación no solicita un cambio.")
    return "\n\n".join(body[:12]) + "\n\n" + "\n".join(body[12:])


def pv_forecast_explanation(forecast):
    # Older reports must still render exactly as they did when their issue was created.
    model = forecast.get("pv_model")
    if model is None:
        return ""
    minutes = round(forecast["sunshine_hours"] * 60)
    hours, minutes = divmod(minutes, 60)
    irradiation = f"{forecast['irradiation_kwh_m2']:.4f}".replace(".", ",")
    return (
        f"\n\n**Sol previsto para el día objetivo:** {hours} h {minutes:02d} min. "
        f"Open-Meteo prevé {irradiation} kWh/m² de irradiación diaria sobre los paneles "
        f"({model['tilt']:g}° de inclinación y {model['azimuth']:g}° de acimut). "
        f"Esa irradiación equivale a {irradiation} horas solares pico al dividirla "
        f"por la irradiancia de referencia de 1 kW/m². "
        f"La producción FV del día se calcula multiplicando esas horas por la potencia "
        f"nominal de {model['pv_kwp']:g} kWp y por el factor (1 − {model['pv_losses']:.2%}), "
        f"lo que da **{forecast['pv_kwh']:.2f} kWh**."
    )


def night_soc_text(night):
    band = {"valley": "valle", "flat": "llano", "peak": "punta"}.get(night["next_band"], "sin tarifa disponible")
    hour = "00:00 del día siguiente" if night["hour"] == 24 else f"{night['hour']:02d}:00"
    message = (f"**SOC a las {hour} (tarifa {band}):** mínimo {night['minimum_soc']:g} % · "
               f"vigente {night['current_soc']:.1f} % · alternativa {night['proposed_soc']:.1f} %.")
    if night["status"] == "unreachable":
        return message + " Ninguna alternativa evaluada alcanza el mínimo en todos los escenarios."
    if night["status"] == "requires_change":
        return message + " El plan vigente no alcanza el mínimo en todos los escenarios."
    return message


def tou_issue_text(payload, identity, result, forecast, *, sort_write_keys=True):
    execution = payload.get("execution_enabled") is True
    title = "Aplicación TOU" if execution else "Simulación TOU"
    description = ("Aprobar autoriza el envío del JSON mostrado al inversor indicado."
                   if execution else
                   "La aprobación registra una revisión de la simulación. La ejecución está deshabilitada.")
    lines = [f"**{title} · {payload['date']}**", "", description,
             f"Caduca {payload['expires_at']} · confianza {result['confidence']}"]
    if "night_soc" in result:
        lines += ["", night_soc_text(result["night_soc"])]
    strategies = result.get("strategies")
    if strategies:
        lines += ["", f"SOC medido {result['current_soc']:.1f} % · "
                  f"SOC proyectado a medianoche {result['estimated_midnight_soc']:.1f} % · "
                  f"horizonte {result['horizon']['hours']} h",
                  "", "| Estrategia | Coste neto | Carga red | Compra punta | SOC 18 h | SOC 22 h | SOC final |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for key, name in (("current", "Vigente"), ("night", "Nocturna"), ("full", "Completa")):
            row = strategies[key]
            lines.append(f"| {name} | {row['net_cost_eur']:.2f} € | {row['grid_charge_kwh']:.2f} kWh | "
                         f"{row['peak_import_kwh']:.2f} kWh | {row['soc_at_18']:.1f} % | "
                         f"{row['soc_at_22']:.1f} % | {row['final_soc']:.1f} % |")
        full, current = strategies["full"], strategies["current"]
        evening = result["evening"]
        lines += ["", f"Consumo estimado {full['load_kwh']:.2f} kWh · FV estimada {full['pv_kwh']:.2f} kWh"
                  + pv_forecast_explanation(forecast),
                  f"Ahorro neto estimado {full['avoided_net_cost_eur']:.2f} € · "
                  f"ahorro ajustado por SOC final {full['avoided_objective_eur']:.2f} €",
                  f"Ahorro frente a la estrategia nocturna {full['saving_vs_night_eur']:.2f} € · "
                  f"compras en punta evitadas {full['avoided_peak_import_kwh']:.2f} kWh",
                  f"De 18:00 a 22:00 se prevén {evening['load_kwh']:.2f} kWh de consumo y "
                  f"{evening['pv_kwh']:.2f} kWh de FV. El SOC teórico inicial para cubrir el déficit es "
                  f"{evening['required_start_soc']:.1f} %.",
                  "", "| Periodo | Vigente | Propuesto | Campos que cambian |", "|---|---|---|---|"]
        changes = {row["period"]: row["reason"] for row in result["changes"]}
        for i, (before, after) in enumerate(zip(current["periods"], full["periods"])):
            def describe(periods, row):
                start, end = row["time"], periods[(i + 1) % 6]["time"]
                charge = "ON" if row["enableGridCharge"] else "OFF"
                return f"{start[:2]}:{start[2:]}–{end[:2]}:{end[2:]} · {row['soc']} % · {charge}"

            lines.append(f"| {i + 1} | {describe(current['periods'], before)} | "
                         f"{describe(full['periods'], after)} | {changes.get(i + 1, 'Igual')} |")
        lines += ["", "<details><summary>Curvas horarias estimadas</summary>", ""]
        for key, title in (("soc_end", "SOC"), ("load_kwh", "Consumo"), ("pv_kwh", "FV"),
                           ("import_kwh", "Compra"), ("price", "Precio")):
            values = [row[key] for row in full["hours"]]
            low, high = min(values), max(values)
            bars = "▁▂▃▄▅▆▇█"
            graph = "".join(bars[round(7 * (value - low) / (high - low))] if high > low else bars[0]
                            for value in values)
            lines.extend([f"{title} · {low:.3f} a {high:.3f}", "", f"`{graph}`", ""])
        lines += ["</details>"]
    reason = result["decision_reason"]
    if reason == "TOU_TARIFF_ALIGNMENT_REQUIRED":
        lines += ["", "El horario vigente cruza límites tarifarios. La propuesta corrige "
                  "el encaje aunque no alcance el umbral de ahorro."]
    elif reason == "TOU_TARIFF_ALIGNMENT_UNREACHABLE":
        lines += ["", "El horario vigente cruza límites tarifarios y no se encontró "
                  "un plan alineado que cumpla las comprobaciones operativas."]
    lines += ["", reason]
    if execution:
        lines += ["", "~~~json", json.dumps(payload["write_body"], indent=2,
                                          sort_keys=sort_write_keys), "~~~"]
    if payload["actionable"]:
        lines += ["", "~~~text", *(f"/deye {command} {payload['date']} {identity}" for command in DECISIONS), "~~~"]
    return "\n".join(lines)


def issue_body_matches(store, body, payload, identity, report):
    if body == issue_text(payload, identity, report):
        return True
    if payload.get("execution_enabled") is not True:
        return False
    # Issues emitted before stable rendering used the order in which the
    # binding built the request. Recover that exact text for pending Issues.
    try:
        binding = execution_binding(store, payload, report)
    except (CollectionError, KeyError, TypeError, ValueError):
        return False
    if any(payload.get(key) != value for key, value in binding.items()):
        return False
    legacy = {**payload, "write_body": binding["write_body"]}
    return body == issue_text(legacy, identity, report, sort_write_keys=False)


def tou_preflight(payload, snapshot):
    """Compare a fresh read supplied by the caller. No command is ever emitted."""
    try:
        periods = [period.record() for period in read_periods(snapshot)]
        if (digest(snapshot["parameters"]) != payload["snapshot_sha256"]
                or periods != payload["current_periods"]):
            return "CONFIGURATION_CHANGED"
    except (ValueError, KeyError, TypeError):
        return "CONFIGURATION_UNAVAILABLE"
    return "EXECUTION_DISABLED"


def terminal(day, identity, status, now, **actor):
    return {"schema_version": 1, "date": day, "proposal_id": identity,
            "status": status, "recorded_at": now.isoformat(), **actor}


def close_issue(store, number):
    store.verify_private()
    store._request("PATCH", f"/issues/{number}", {"state": "closed"})


def reconcile_expiry(store, now):
    changes, issues = {}, []
    next_day = now.astimezone(MADRID).date() + timedelta(days=1)
    for path in sorted(store.entries):
        proposal_file = PROPOSAL_PATH.fullmatch(path)
        if not proposal_file:
            continue
        day, identity = proposal_file.groups()
        decision = decision_path(day, identity)
        if decision in store.entries or date.fromisoformat(day) > next_day:
            continue
        proposal = store.read(path)
        if now >= datetime.fromisoformat(proposal["payload"]["expires_at"]):
            changes[decision] = terminal(day, identity, "expired", now, issue=proposal["issue"])
            issues.append(proposal["issue"])
        if len(issues) == 10:
            break
    if changes:
        store.publish(changes)
        for number in issues:
            close_issue(store, number)
        # The store is a snapshot. Reopen it before publishing anything else.
        return True
    return False


def notify(store, day, cutoff, now, reviewers, *, execution_enabled=False):
    if day != str(now.astimezone(MADRID).date() + timedelta(days=1)):
        return "STALE_DISPATCH"
    report = store.read(f"recommendations/{day}.json")
    if report is None or report["date"] != day:
        return "NO_RECOMMENDATION"
    deadline = expiry(day, cutoff)
    result = report["recommendation"]
    phase3 = report.get("tou_simulation")
    targets = result.get("current_targets")
    target = result.get("recommended_soc")
    wants_review = (phase3["decision"] == "review" if phase3 is not None else result["decision"] == "recommend")
    actionable = wants_review and now < deadline
    if phase3 is None and actionable and (type(target) is not int or not 0 <= target <= 100
                       or type(targets) is not list or len(targets) != 2
                       or any(type(item) is not int or not 0 <= item <= 100 for item in targets)
                       or targets == [target, target]):
        raise ValueError("INVALID_PROPOSED_TARGET")
    current_path = f"approvals/current/{day}.json"
    current = store.read(current_path) or {}
    report_hash = digest(report)
    executable = phase3 is not None and actionable and execution_enabled
    if current.get("report_sha256") == report_hash:
        previous = store.read(proposal_path(day, current["proposal_id"])) or {}
        if bool(previous.get("payload", {}).get("execution_enabled")) == executable:
            return "ALREADY_NOTIFIED"
    payload = {"schema_version": 1, "date": day, "report_sha256": report_hash,
               "expires_at": deadline.isoformat(), "current_targets": targets,
               "slots_madrid": ["00:00–05:00", "05:00–08:00"],
               "proposed_targets": [target, target] if actionable else None, "actionable": actionable}
    if phase3 is not None:
        strategies = phase3.get("strategies", {})
        payload = {"schema_version": 2, "date": day, "report_sha256": report_hash,
                   "expires_at": deadline.isoformat(), "actionable": actionable,
                   "execution_enabled": executable, "snapshot_sha256": phase3.get("snapshot_sha256"),
                   "current_periods": strategies.get("current", {}).get("periods"),
                   "proposed_periods": strategies.get("full", {}).get("periods")}
        if executable:
            payload.update(execution_binding(store, payload, report))
    identity = digest(payload)
    if proposal_path(day, identity) in store.entries:
        return "PREVIOUS_PROPOSAL"
    title = f"Deye · {day} · {identity[:16]}"
    body = issue_text(payload, identity, report)
    # Recover an issue created before an interrupted Git commit. Only the Actions bot
    # and an exact body can match. The retained issue number is checked on every vote.
    store.verify_private()
    candidates = store._request("GET", "/issues?state=all&sort=created&direction=desc&per_page=100")
    issue = next((item for item in candidates if item.get("title") == title and item.get("body") == body
                  and item["user"]["login"] == "github-actions[bot]" and item["user"]["type"] == "Bot"), None)
    if issue is None:
        users = [store._request("GET", f"/users/{login}") for login in reviewers.values()]
        if any(user["id"] != identity or user["type"] != "User" for identity, user in zip(reviewers, users)):
            raise ValueError("REVIEWER_ID_MISMATCH")
        store.verify_private()
        issue = store._request("POST", "/issues", {"title": title, "body": body,
                                                 "assignees": [user["login"] for user in users]})
    if {assignee["id"] for assignee in issue["assignees"]} != set(reviewers):
        raise ValueError("ISSUE_ASSIGNMENT_FAILED")
    number = issue["number"]
    changes = {proposal_path(day, identity): {"payload": payload, "issue": number, "created_at": now.isoformat()},
               current_path: {"proposal_id": identity, "report_sha256": report_hash}}
    close = []
    previous = current.get("proposal_id")
    if previous and decision_path(day, previous) not in store.entries:
        previous_issue = store.read(proposal_path(day, previous))["issue"]
        changes[decision_path(day, previous)] = terminal(
            day, previous, "superseded", now, replacement=identity, issue=previous_issue)
        close.append(previous_issue)
    if not actionable:
        status = "expired" if wants_review else "no_change"
        changes[decision_path(day, identity)] = terminal(day, identity, status, now, issue=number)
        close.append(number)
    store.publish(changes)
    for number in close:
        close_issue(store, number)
    return "NOTIFIED"


def decide(store, event, reviewers, now):
    original = event["comment"]
    actor = event["sender"]
    match = COMMAND.fullmatch(original["body"].strip())
    if (not match or event.get("action") != "created" or "pull_request" in event["issue"]
            or actor["id"] not in reviewers or actor["type"] != "User"
            or original["user"]["id"] != actor["id"]):
        return "IGNORED"
    command, day, identity = match.groups()
    if type(original.get("id")) is not int or original["id"] <= 0:
        return "INVALID_COMMENT_ID"
    record = store.read(proposal_path(day, identity))
    if record is None or record["issue"] != event["issue"]["number"]:
        return "UNKNOWN_PROPOSAL"
    payload = record["payload"]
    if digest(payload) != identity or payload["date"] != day or not payload["actionable"]:
        return "INVALID_PROPOSAL"
    current = store.read(f"approvals/current/{day}.json") or {}
    if current.get("proposal_id") != identity:
        return "SUPERSEDED"
    path = decision_path(day, identity)
    if path in store.entries:
        return "ALREADY_DECIDED"
    comment = store._request("GET", f"/issues/comments/{original['id']}")
    if (comment["user"]["id"] != actor["id"] or comment["body"] != original["body"]
            or comment["created_at"] != comment["updated_at"]
            or not comment["issue_url"].endswith(f"/issues/{record['issue']}")):
        return "COMMENT_CHANGED"
    created = datetime.fromisoformat(comment["created_at"])
    deadline = datetime.fromisoformat(payload["expires_at"])
    report = store.read(f"recommendations/{day}.json")
    if report is None or digest(report) != payload["report_sha256"]:
        return "REPORT_CHANGED"
    issue = store._request("GET", f"/issues/{record['issue']}")
    if (issue["state"] != "open" or issue["title"] != f"Deye · {day} · {identity[:16]}"
            or not issue_body_matches(store, issue["body"], payload, identity, report)):
        return "ISSUE_CHANGED_OR_CLOSED"
    if created > now or created < datetime.fromisoformat(record["created_at"]).replace(microsecond=0):
        return "INVALID_EVENT_TIME"
    status = "expired" if max(created, now) >= deadline else DECISIONS[command]
    if payload["schema_version"] == 2 and status == "approved":
        snapshot_day = date.fromisoformat(day) - timedelta(days=1)
        snapshot = store.read(f"evening/{snapshot_day}.json") or {}
        preflight = tou_preflight(payload, snapshot)
        latest_parameters = store.read("parameters/latest.json")
        if (preflight == "EXECUTION_DISABLED" and latest_parameters is not None
                and digest(latest_parameters) != payload["snapshot_sha256"]):
            preflight = "CONFIGURATION_CHANGED"
        status = {"EXECUTION_DISABLED": "approved" if payload.get("execution_enabled") is True else "simulation_approved",
                  "CONFIGURATION_CHANGED": "configuration_changed",
                  "CONFIGURATION_UNAVAILABLE": "configuration_unavailable"}[preflight]
    store.publish({path: terminal(day, identity, status, now, actor_id=actor["id"],
                                  actor_login=actor["login"], command=command, comment_id=original["id"],
                                  commented_at=created.isoformat(), issue=record["issue"])})
    close_issue(store, record["issue"])
    return status.upper()


def execute_event(factory, event, reviewers, clock, *, client_factory=DeyeControlClient):
    original = event["comment"]
    actor = event["sender"]
    match = COMMAND.fullmatch(original["body"].strip())
    if (not match or match[1] != "approve" or event.get("action") != "created"
            or "pull_request" in event["issue"] or actor["type"] != "User"
            or actor["id"] not in reviewers or original["user"]["id"] != actor["id"]
            or type(original.get("id")) is not int or original["id"] <= 0):
        return "NO_EXECUTABLE_APPROVAL"
    _, day, identity = match.groups()

    def authorize(store, now):
        record = store.read(proposal_path(day, identity)) or {}
        payload = record.get("payload") or {}
        decision = store.read(decision_path(day, identity)) or {}
        current = store.read(f"approvals/current/{day}.json") or {}
        if (payload.get("schema_version") != 2 or payload.get("execution_enabled") is not True
                or payload.get("actionable") is not True or payload.get("date") != day
                or digest(payload) != identity or current.get("proposal_id") != identity
                or current.get("report_sha256") != payload.get("report_sha256")
                or decision.get("status") != "approved" or decision.get("command") != "approve"
                or decision.get("date") != day or decision.get("proposal_id") != identity
                or decision.get("comment_id") != original["id"] or decision.get("actor_id") != actor["id"]
                or decision.get("issue") != event["issue"]["number"] or record.get("issue") != decision["issue"]):
            return None
        deadline = datetime.fromisoformat(payload["expires_at"])
        commented = datetime.fromisoformat(decision["commented_at"])
        recorded = datetime.fromisoformat(decision["recorded_at"])
        if not commented <= recorded <= now < deadline:
            return None
        comment = store._request("GET", f"/issues/comments/{original['id']}")
        if (comment["user"]["id"] != actor["id"] or comment["user"]["type"] != "User"
                or comment["body"] != original["body"] or comment["created_at"] != comment["updated_at"]
                or datetime.fromisoformat(comment["created_at"]) != commented
                or not comment["issue_url"].endswith(f"/issues/{record['issue']}")):
            return None
        report = store.read(f"recommendations/{day}.json")
        if not report or digest(report) != payload["report_sha256"]:
            return None
        binding = execution_binding(store, payload, report)
        if any(payload.get(key) != value for key, value in binding.items()):
            return None
        return payload

    return apply_approved(factory, client_factory, day, identity, authorize, clock)


def main():
    try:
        repository = os.environ["GITHUB_REPOSITORY"]
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        event_name = os.environ["GITHUB_EVENT_NAME"]
        if (event["repository"]["full_name"] != repository or event["repository"]["private"] is not True
                or os.environ["GITHUB_REF"] != "refs/heads/main"):
            raise ValueError("INVALID_EVENT_REPOSITORY")
        if event_name not in {"repository_dispatch", "issue_comment"}:
            raise ValueError("INVALID_EVENT")
        ids = reviewer_ids(os.environ["DEYE_APPROVER_IDS"])
        accounts = json.loads(os.environ["DEYE_APPROVER_LOGINS"])
        reviewers = {int(key): login for key, login in accounts.items()}
        if ids != reviewers.keys() or any(not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) for login in reviewers.values()):
            raise ValueError("INVALID_REVIEWER_LOGINS")
        now = datetime.now(UTC)
        if event_name == "repository_dispatch" and event.get("action") != "deye-recommendation":
            raise ValueError("INVALID_DISPATCH")
        def store_from_event():
            return GitHubPrivateStore(repository, os.environ["GH_TOKEN"],
                                      public_repository=os.environ["DEYE_APPROVAL_CODE_REPOSITORY"])
        enabled = os.environ.get("DEYE_TOU_WRITE_ENABLED") == "true"
        if os.environ.get("DEYE_APPROVAL_STAGE") == "execute":
            if not enabled or event_name != "issue_comment":
                return 0
            result = execute_event(store_from_event, event, reviewers, lambda: datetime.now(UTC))
            print("Approval execution:", result)
            return 0 if result in {"VERIFIED", "NO_EXECUTABLE_APPROVAL", "ALREADY_ATTEMPTED"} else 1
        store = store_from_event()
        try:
            if event_name == "issue_comment":
                result = decide(store, event, reviewers, now)
            else:
                if reconcile_expiry(store, now):
                    store.close()
                    store = store_from_event()
                result = notify(store, event["client_payload"]["date"],
                                os.environ.get("DEYE_APPROVAL_CUTOFF") or "23:00", now, reviewers,
                                execution_enabled=enabled)
        finally:
            store.close()
    except Exception:
        print("Approval event failed. Private details suppressed.")
        return 1
    print("Approval event:", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
