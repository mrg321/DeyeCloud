"""Record decisions about private Deye recommendations. No inverter client."""

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from deye_history_quality import MADRID
from deye_private_store import GitHubPrivateStore, canonical


COMMAND = re.compile(r"/deye (approve|reject|cancel) (\d{4}-\d{2}-\d{2}) ([a-f0-9]{64})")
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


def issue_text(payload, identity, report):
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
        f"**Producción FV:** {value(forecast, 'pv_kwh', ' kWh')}",
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
    if payload["actionable"]:
        body += ["Comenta exactamente uno de estos comandos:", "~~~text",
                 *(f"/deye {command} {payload['date']} {identity}" for command in DECISIONS), "~~~"]
    else:
        body.append("Informe informativo. Esta recomendación no solicita un cambio.")
    return "\n\n".join(body[:12]) + "\n\n" + "\n".join(body[12:])


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
        if not path.startswith("approvals/proposals/"):
            continue
        _, _, day, filename = path.split("/")
        identity = filename.removesuffix(".json")
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


def notify(store, day, cutoff, now, reviewers):
    if day != str(now.astimezone(MADRID).date() + timedelta(days=1)):
        return "STALE_DISPATCH"
    report = store.read(f"recommendations/{day}.json")
    if report is None or report["date"] != day:
        return "NO_RECOMMENDATION"
    deadline = expiry(day, cutoff)
    result = report["recommendation"]
    targets = result.get("current_targets")
    target = result.get("recommended_soc")
    actionable = result["decision"] == "recommend" and now < deadline
    if actionable and (type(target) is not int or not 0 <= target <= 100
                       or type(targets) is not list or len(targets) != 2
                       or any(type(item) is not int or not 0 <= item <= 100 for item in targets)
                       or targets == [target, target]):
        raise ValueError("INVALID_PROPOSED_TARGET")
    current_path = f"approvals/current/{day}.json"
    current = store.read(current_path) or {}
    report_hash = digest(report)
    if current.get("report_sha256") == report_hash:
        return "ALREADY_NOTIFIED"
    payload = {"schema_version": 1, "date": day, "report_sha256": report_hash,
               "expires_at": deadline.isoformat(), "current_targets": targets,
               "slots_madrid": ["00:00–05:00", "05:00–08:00"],
               "proposed_targets": [target, target] if actionable else None, "actionable": actionable}
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
        status = "expired" if result["decision"] == "recommend" else "no_change"
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
    if (issue["state"] != "open" or issue["body"] != issue_text(payload, identity, report)
            or issue["title"] != f"Deye · {day} · {identity[:16]}"):
        return "ISSUE_CHANGED_OR_CLOSED"
    if created > now or created < datetime.fromisoformat(record["created_at"]).replace(microsecond=0):
        return "INVALID_EVENT_TIME"
    status = "expired" if max(created, now) >= deadline else DECISIONS[command]
    store.publish({path: terminal(day, identity, status, now, actor_id=actor["id"],
                                  actor_login=actor["login"], command=command, comment_id=original["id"],
                                  commented_at=created.isoformat(), issue=record["issue"])})
    close_issue(store, record["issue"])
    return status.upper()


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
        store = store_from_event()
        try:
            if event_name == "issue_comment":
                result = decide(store, event, reviewers, now)
            else:
                if reconcile_expiry(store, now):
                    store.close()
                    store = store_from_event()
                result = notify(store, event["client_payload"]["date"],
                                os.environ.get("DEYE_APPROVAL_CUTOFF") or "23:00", now, reviewers)
        finally:
            store.close()
    except Exception:
        print("Approval event failed. Private details suppressed.")
        return 1
    print("Approval event:", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
