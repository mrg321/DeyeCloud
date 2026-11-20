"""Forecast and simulate TOU targets from the collector's private read snapshots."""

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from statistics import median
from time import monotonic

import requests

from deye_history_quality import MADRID, day_bounds, numeric, sample_time
from deye_private_store import ApprovalWorkflowMissing, GitHubPrivateStore, canonical
from deye_workbook import add_recommendation_sheet


UTC = timezone.utc
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_FIELDS = {"global_tilted_irradiance": "W/m²", "sunshine_duration": "s", "cloud_cover": "%"}
TOU_TIMES = ("0000", "0500", "0800", "1300", "1700", "2100")
ENERGY_FIELDS = ("Consumption", "Production", "ElectricityPurchasing", "GridFeed-in",
                 "ChargingCapacity", "DischargingCapacity")


def settings(override=None, *, require_installation=False):
    config = json.loads((Path(__file__).resolve().parents[1] / "config/recommendation.example.json")
                        .read_text(encoding="utf-8"))
    override = dict(override or {})
    for legacy, current in (("nominal_kwh", "battery_nominal_kwh"), ("usable_kwh", "battery_usable_kwh")):
        if legacy in override:
            value = override.pop(legacy)
            if current in override and override[current] != value:
                raise ValueError("CONFLICTING_BATTERY_CAPACITY_SETTINGS")
            override.setdefault(current, value)
    required = {"latitude", "longitude", "pv_kwp", "tilt", "azimuth",
                "battery_nominal_kwh", "battery_usable_kwh", "grid_kw"}
    if require_installation and not required <= override.keys():
        raise ValueError("PRIVATE_RECOMMENDATION_CONFIG_MISSING")
    if override.keys() - config.keys():
        raise ValueError("UNKNOWN_RECOMMENDATION_SETTING")
    config.update(override)

    def bounded(value, low, high):
        return type(value) in (int, float) and low <= value <= high

    limits = {
        "latitude": (-90, 90), "longitude": (-180, 180), "pv_kwp": (0.01, 100),
        "tilt": (0, 90), "azimuth": (-180, 180), "pv_losses": (0, 0.9),
        "battery_nominal_kwh": (0.1, 500), "battery_usable_kwh": (0.1, 500), "reserve_soc": (0, 99),
        "charge_efficiency": (0.1, 1), "discharge_efficiency": (0.1, 1),
        "grid_kw": (0.1, 100), "battery_kw": (0.1, 100), "export_price": (0, 10),
        "max_soc_age_minutes": (1, 240), "pv_uncertainty": (0, 0.9),
        "load_uncertainty": (0, 0.9), "minimum_saving_eur": (0, 100),
    }
    if not all(bounded(config[key], *bounds) for key, bounds in limits.items()):
        raise ValueError("INVALID_RECOMMENDATION_SETTING")
    targets, prices = config["targets"], config["prices"]
    if (config["battery_usable_kwh"] > config["battery_nominal_kwh"] or type(targets) is not list or not targets
            or not all(type(x) is int and config["reserve_soc"] < x <= 100 for x in targets)
            or targets != sorted(set(targets)) or config["fallback"] not in ("hold", "abstain")
            or set(prices) != {"valley", "flat", "peak"}
            or not all(bounded(value, 0, 10) for value in prices.values())
            or type(config["history_days"]) is not int or not 1 <= config["history_days"] <= 366
            or type(config["min_profiles"]) is not int or not 1 <= config["min_profiles"] <= 12):
        raise ValueError("INVALID_ENERGY_SETTINGS")
    end = 0
    for first, last, period in config["weekday_periods"]:
        if type(first) is not int or type(last) is not int or first != end or not first < last <= 24 or period not in prices:
            raise ValueError("INVALID_TARIFF_PERIODS")
        end = last
    if end != 24:
        raise ValueError("INCOMPLETE_TARIFF_PERIODS")
    for year, holidays in config["holidays"].items():
        if len(year) != 4 or not year.isascii() or not year.isdigit() or type(holidays) is not list:
            raise ValueError("INVALID_HOLIDAY_CALENDAR")
        for holiday in holidays:
            if date.fromisoformat(year + "-" + holiday).isoformat()[5:] != holiday:
                raise ValueError("INVALID_HOLIDAY_DATE")
    return config


def hour_starts(day):
    return range(*day_bounds(day), 3600)


def forecast(day, config, *, get=requests.get):
    hours = hour_starts(day)
    # Radiation and sunshine are preceding-hour aggregates. Request their UTC end times.
    end_times = [stamp + 3600 for stamp in hours]
    utc = [datetime.fromtimestamp(stamp, UTC) for stamp in end_times]
    response = get(WEATHER_URL, timeout=(5, 15), allow_redirects=False, params={
        "latitude": config["latitude"], "longitude": config["longitude"],
        "tilt": config["tilt"], "azimuth": config["azimuth"],
        "timezone": "GMT", "timeformat": "unixtime",
        "start_date": str(min(datetime.fromtimestamp(hours.start, UTC).date(), day)),
        "end_date": str(max(utc[-1].date(), day)),
        "hourly": ",".join(WEATHER_FIELDS), "daily": "daylight_duration",
    })
    if response.status_code != 200:
        raise ValueError("WEATHER_HTTP_ERROR")
    data = response.json()
    hourly = data["hourly"]
    if any(data["hourly_units"].get(key) != unit for key, unit in WEATHER_FIELDS.items()):
        raise ValueError("WEATHER_UNITS")
    if len(set(hourly["time"])) != len(hourly["time"]):
        raise ValueError("DUPLICATE_WEATHER_HOUR")
    index = {stamp: i for i, stamp in enumerate(hourly["time"])}
    records = []
    for start, end in zip(hours, end_times):
        gti, sun, cloud = (numeric(hourly[key][index[end if key != "cloud_cover" else start]])
                           for key in WEATHER_FIELDS)
        if (gti is None or not 0 <= gti <= 1600 or sun is None or not 0 <= sun <= 3600
                or cloud is None or not 0 <= cloud <= 100):
            raise ValueError("INCOMPLETE_WEATHER_HOUR")
        records.append({"hour": datetime.fromtimestamp(start, MADRID).isoformat(),
                        "irradiation_kwh_m2": gti / 1000, "sunshine_seconds": sun,
                        "cloud_cover_pct": cloud,
                        "pv_kwh": gti / 1000 * config["pv_kwp"] * (1 - config["pv_losses"])})
    civil_day = int(datetime.combine(day, datetime.min.time(), UTC).timestamp())
    daily = data["daily"]
    daylight = numeric(daily["daylight_duration"][daily["time"].index(civil_day)])
    if data["daily_units"]["daylight_duration"] != "s" or daylight is None or not 0 <= daylight <= 86400:
        raise ValueError("INVALID_DAYLIGHT")
    return {"source": "Open-Meteo", "daylight_hours": daylight / 3600,
            "sunshine_hours": sum(row["sunshine_seconds"] for row in records) / 3600,
            "irradiation_kwh_m2": sum(row["irradiation_kwh_m2"] for row in records),
            "cloud_cover_mean_pct": sum(row["cloud_cover_pct"] for row in records) / len(records),
            "pv_kwh": sum(row["pv_kwh"] for row in records), "hours": records,
            "confidence": "medium", "issues": []}


def day_type(day, config):
    holidays = config["holidays"].get(str(day.year))
    if holidays is None:
        return None
    if str(day)[5:] in holidays:
        return "holiday"
    return "weekend" if day.weekday() >= 5 else "weekday"


def load_profile(samples, day, consumption):
    """Integrate valid short power spans, then normalize to measured household kWh."""
    hours = hour_starts(day)
    if len(hours) != 24:  # Use ordinary historical days to learn each wall-clock hour.
        return None
    start, end = day_bounds(day)
    points = {}
    for row in samples:
        if row.get("key") != "TotalConsumptionPower":
            continue
        stamp, _ = sample_time(row.get("timestamp"), None)
        value, unit = numeric(row.get("value")), row.get("unit")
        if (stamp is None or not start <= stamp.timestamp() < end or stamp.timestamp() in points
                or unit not in ("W", "kW") or value is None or value < 0):
            return None
        points[stamp.timestamp()] = value / 1000 if unit == "W" else value
    energy, duration = [0.0] * 24, [0.0] * 24
    ordered = sorted(points.items())
    for (left, a), (right, b) in zip(ordered, ordered[1:]):
        if right - left > 600:
            continue
        slope = (b - a) / (right - left)
        origin = left
        while left < right:
            hour = int((left - start) // 3600)
            stop = min(right, start + (hour + 1) * 3600)
            mean_power = a + slope * ((left - origin) + (stop - origin)) / 2
            energy[hour] += mean_power * (stop - left) / 3600
            duration[hour] += stop - left
            left = stop
    if min(duration) < 2700:
        return None
    estimated = [value * 3600 / covered for value, covered in zip(energy, duration)]
    total = sum(estimated)
    if total <= 0 or not 0.65 <= total / consumption <= 1.35:
        return None
    return [value * consumption / total for value in estimated]


def history(store, target, config):
    season = (target.month % 12) // 3
    kind = day_type(target, config)
    days = [target - timedelta(days=offset) for offset in range(2, config["history_days"] + 2)]
    days = [day for day in days if day_type(day, config) is not None
            and f"daily/{day}.json" in store.entries and f"intraday/{day}.json" in store.entries]
    days.sort(key=lambda day: (day_type(day, config) != kind,
                               (day.month % 12) // 3 != season, target - day))
    selected = []
    for day in days[:36]:
        daily = store.read(f"daily/{day}.json") or {}
        series = store.read(f"intraday/{day}.json") or {}
        if daily.get("date") != str(day) or series.get("date") != str(day) or daily.get("energy_unit") != "kWh":
            continue
        device = daily.get("energy", {}).get("device", {})
        station = daily.get("energy", {}).get("station", {})
        station_fields = dict(zip(ENERGY_FIELDS, ("consumptionValue", "generationValue", "purchaseValue",
                                                  "gridValue", "chargeValue", "dischargeValue")))
        totals = {}
        for key in ENERGY_FIELDS:
            value = numeric(device.get(key))
            if value is None or value < 0:
                value = numeric(station.get(station_fields[key]))
            totals[key] = value if value is not None and value >= 0 else None
        consumption = totals["Consumption"]
        profile = load_profile(series.get("samples", []), day, consumption) if consumption else None
        if profile is not None:
            selected.append((day, profile, totals))
        if len(selected) == 12:
            break
    # Use weaker matches only when the stronger group cannot meet the minimum.
    groups = ([row for row in selected if day_type(row[0], config) == kind
               and (row[0].month % 12) // 3 == season],
              [row for row in selected if day_type(row[0], config) == kind], selected)
    return next((rows for rows in groups if len(rows) >= config["min_profiles"]), selected)


def evening_state(snapshot, now, config):
    soc = targets = soc_time = None
    captured, _ = sample_time(snapshot.get("captured_at"), None)
    if captured is None or not timedelta(0) <= now - captured <= timedelta(minutes=config["max_soc_age_minutes"]):
        return soc, targets, soc_time
    readings = []
    for key in ("SOC", "BMSSOC"):
        reading = snapshot.get("latest_soc", {}).get(key) or {}
        value = numeric(reading.get("value"))
        stamp, _ = sample_time(reading.get("timestamp"), None)
        if (value is not None and 0 <= value <= 100 and reading.get("unit") == "%" and stamp
                and stamp <= captured and now - stamp <= timedelta(minutes=config["max_soc_age_minutes"])):
            readings.append((stamp, value))
    if readings:
        stamp, soc = max(readings)
        soc_time = stamp.isoformat()
    if len(readings) == 2 and abs(readings[0][1] - readings[1][1]) > 10:
        soc = soc_time = None
    tou = snapshot.get("parameters", {}).get("deye_read_tou", {})
    data = tou.get("data") or {}
    slots = data.get("timeUseSettingItems") or []
    if (tou.get("status") == "available" and data.get("touAction") == "on"
            and tuple(slot.get("time") for slot in slots) == TOU_TIMES
            and all(slot.get("enableGeneration") is False and slot.get("enableGridCharge") is (i < 2)
                    and type(slot.get("soc")) is int and 0 <= slot["soc"] <= 100
                    for i, slot in enumerate(slots))
            and all(slot["soc"] == config["reserve_soc"] for slot in slots[2:])
            and all(slot["soc"] >= config["reserve_soc"] for slot in slots[:2])):
        targets = [slot["soc"] for slot in slots[:2]]
    return soc, targets, soc_time


def simulate(hours, load, pv, initial, targets, kind, config):
    capacity = config["battery_usable_kwh"]
    battery = initial * capacity / 100
    charge_eff, discharge_eff = config["charge_efficiency"], config["discharge_efficiency"]
    power = config["battery_kw"]
    cost = imports = exports = grid_charge = discharge = daytime_discharge = direct_pv = pv_charge = 0.0
    for hour, demand, production in zip(hours, load, pv):
        direct_pv += min(demand, production)
        deficit = max(0, demand - production)
        surplus = max(0, production - demand)
        solar_input = min(surplus, power, (capacity - battery) / charge_eff)
        battery += solar_input * charge_eff
        pv_charge += solar_input
        period = "valley" if kind != "weekday" else next(
            name for start, end, name in config["weekday_periods"] if start <= hour < end)
        floor = config["reserve_soc"]
        grid_input = 0.0
        if hour < 8:
            floor = targets[0 if hour < 5 else 1]
            grid_input = min(max(0, (floor * capacity / 100 - battery) / charge_eff),
                             power - solar_input, max(0, config["grid_kw"] - deficit))
            battery += grid_input * charge_eff
        delivered = min(deficit, power, max(0, battery - floor * capacity / 100) * discharge_eff)
        battery -= delivered / discharge_eff
        # Keep numerical drift from producing negative charge headroom.
        battery = min(capacity, battery)
        bought, sold = deficit - delivered + grid_input, surplus - solar_input
        imports += bought
        exports += sold
        grid_charge += grid_input
        discharge += delivered
        if hour >= 8:
            daytime_discharge += delivered
        cost += bought * config["prices"][period] - sold * config["export_price"]
    return {"cost_eur": cost, "grid_import_kwh": imports, "grid_export_kwh": exports,
            "grid_to_battery_kwh": grid_charge, "pv_to_battery_kwh": pv_charge,
            "direct_pv_self_consumption_kwh": direct_pv, "battery_output_kwh": discharge,
            "battery_output_08_24_kwh": daytime_discharge, "end_soc": battery / capacity * 100}


def recommendation(target, now, weather, selected, snapshot, config):
    soc, baseline, soc_time = evening_state(snapshot, now, config)
    kind = day_type(target, config)
    hours = [datetime.fromtimestamp(stamp, MADRID).hour for stamp in hour_starts(target)]
    profile = [median(row[1][h] for row in selected) for h in range(24)] if selected else None
    load = [profile[h] for h in hours] if profile else None
    historical = {}
    for key in ENERGY_FIELDS:
        values = [row[2][key] for row in selected if row[2][key] is not None]
        historical[key] = median(values) if values else None
    reasons = []
    if not weather.get("hours"):
        reasons.append("WEATHER_UNAVAILABLE")
    if len(selected) < config["min_profiles"]:
        reasons.append("INSUFFICIENT_HOUSEHOLD_PROFILES")
    if soc is None:
        reasons.append("INVALID_OR_STALE_SOC")
    if baseline is None:
        reasons.append("TOU_UNAVAILABLE_OR_UNEXPECTED")
    if kind is None:
        reasons.append("MISSING_HOLIDAY_CALENDAR")
    report = {"day_type": kind, "current_soc": soc, "soc_measured_at": soc_time,
              "current_targets": baseline, "recommended_soc": None,
              "decision": "hold" if baseline and config["fallback"] == "hold" else "abstain",
              "confidence": "low", "issues": reasons, "historic_days": [str(row[0]) for row in selected],
              "historical_medians_kwh": historical, "battery_nominal_kwh": config["battery_nominal_kwh"],
              "battery_usable_kwh": config["battery_usable_kwh"],
              "battery_available_above_reserve_kwh": config["battery_usable_kwh"] * (1 - config["reserve_soc"] / 100),
              "expected_load_kwh": sum(load) if load else None,
              "expected_load_08_24_kwh": sum(value for hour, value in zip(hours, load) if hour >= 8) if load else None,
              "forecast_pv_kwh": weather.get("pv_kwh"), "expected_cost_eur": None,
              "estimated_saving_eur": None, "expected_battery_output_08_24_kwh": None}
    if reasons:
        if report["decision"] == "hold" and baseline[0] == baseline[1]:
            report["recommended_soc"] = baseline[0]
        report["explanation"] = "Datos insuficientes o inconsistentes. Se descarta proponer un cambio de SOC."
        return report
    pv = [row["pv_kwh"] for row in weather["hours"]]
    # Estimate evening discharge until midnight with the historical load and no PV credit.
    local = datetime.fromisoformat(soc_time).astimezone(MADRID)
    remaining = profile[local.hour] * (3600 - local.minute * 60 - local.second) / 3600
    remaining += sum(profile[local.hour + 1:])
    initial = max(min(soc, config["reserve_soc"]), soc - (
        remaining / config["discharge_efficiency"] / config["battery_usable_kwh"] * 100))
    scenarios = [(1, 1)] + [(1 + load_sign * config["load_uncertainty"],
                             1 + pv_sign * config["pv_uncertainty"])
                            for load_sign in (-1, 1) for pv_sign in (-1, 1)]
    outcomes = {}
    for setting in dict.fromkeys([tuple(baseline)] + [(x, x) for x in config["targets"]]):
        outcomes[setting] = [simulate(hours, [x * lf for x in load], [x * pf for x in pv],
                                      initial, setting, kind, config) for lf, pf in scenarios]
    reference = outcomes[tuple(baseline)]
    options = []
    for setting, outcomes_by_scenario in outcomes.items():
        savings = [base["cost_eur"] - option["cost_eur"]
                   for base, option in zip(reference, outcomes_by_scenario)]
        options.append({"targets": list(setting), **outcomes_by_scenario[0],
                        "saving_eur": savings[0], "minimum_scenario_saving_eur": min(savings)})
    viable = [row for row in options[1:] if row["minimum_scenario_saving_eur"] > 0
              and row["minimum_scenario_saving_eur"] >= config["minimum_saving_eur"]]
    best = min(viable, key=lambda row: (row["cost_eur"], row["targets"])) if viable else options[0]
    targets = best["targets"]
    comparable_season = (target.month % 12) // 3
    same_type_season = sum(day_type(row[0], config) == kind and (row[0].month % 12) // 3 == comparable_season
                           for row in selected)
    report.update({"decision": "recommend" if viable else "hold",
                   "confidence": "medium" if same_type_season >= config["min_profiles"] else "low",
                   "same_type_and_season_profiles": same_type_season,
                   "recommended_soc": targets[0] if targets[0] == targets[1] else None,
                   "estimated_midnight_soc": initial, "options": options,
                   "expected_cost_eur": best["cost_eur"], "estimated_saving_eur": best["saving_eur"],
                   "expected_battery_output_08_24_kwh": best["battery_output_08_24_kwh"],
                   "explanation": (f"Consumo {sum(load):.2f} kWh y FV {sum(pv):.2f} kWh. "
                                   f"Coste estimado {best['cost_eur']:.2f} EUR frente a "
                                   f"{reference[0]['cost_eur']:.2f} EUR con el TOU actual. "
                                   f"Ahorro mínimo simulado {best['minimum_scenario_saving_eur']:.2f} EUR.")})
    return report


def run(store, now, *, dry_run=False, fetch=forecast):
    today = now.astimezone(MADRID).date()
    target = today + timedelta(days=1)
    path = f"recommendations/{target}.json"
    workbook_path = f"reports/combined/{today}.xlsx"
    if not dry_run and path in store.entries:
        if workbook_path in store.entries:
            workbook = store.read_workbook(workbook_path)
            updated = add_recommendation_sheet(workbook, store.read(path))
            if updated != workbook:
                store.publish({workbook_path: updated})
                return "WORKBOOK_UPDATED"
        return "ALREADY_RECORDED"
    config = settings(store.read("config/recommendation.json"), require_installation=True)
    snapshot = store.read(f"evening/{today}.json") or {}
    selected = history(store, target, config)
    try:
        weather = fetch(target, config)
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
        weather = {"source": "Open-Meteo", "confidence": "unavailable", "issues": ["INCOMPLETE_OR_FAILED_FORECAST"]}
    weather.update({"queried_at": now.astimezone(MADRID).isoformat(), "forecast_date": str(target)})
    result = {"schema_version": 1, "date": str(target), "timezone": "Europe/Madrid",
              "settings_sha256": hashlib.sha256(canonical(config)).hexdigest(),
              "history_commit": store.head, "forecast": weather,
              "recommendation": recommendation(target, now, weather, selected, snapshot, config)}
    if dry_run:
        return "DRY_RUN_COMPLETED"
    changes = {path: result}
    if workbook_path in store.entries:
        workbook = store.read_workbook(workbook_path)
        changes[workbook_path] = add_recommendation_sheet(workbook, result)
    store.publish(changes)
    return "RECORDED_" + result["recommendation"]["decision"].upper()


def dispatch_approval(repository, data_token, public_repository, day):
    workflow_token = os.environ.get("DEYE_APPROVAL_WORKFLOW_TOKEN")
    values = {name: os.environ.get(name, "") for name in ("DEYE_APPROVER_IDS", "DEYE_APPROVER_LOGINS")}
    if not workflow_token or not all(values.values()):
        raise ApprovalWorkflowMissing("APPROVAL_CONFIGURATION_MISSING")
    values.update(DEYE_APPROVAL_ENABLED="true", DEYE_APPROVAL_CUTOFF=os.environ.get("DEYE_APPROVAL_CUTOFF") or "23:00")
    template = (Path(__file__).resolve().parents[1] / "config/deye-approval.workflow.example.json").read_bytes()
    installer = GitHubPrivateStore(repository, workflow_token, public_repository=public_repository)
    try:
        installer.ensure_approval_variables(values)
        installer.install_approval_workflow(template)
    finally:
        installer.close()

    # Refresh the branch snapshot and dispatch with the ordinary data credential.
    refreshed = GitHubPrivateStore(repository, data_token, public_repository=public_repository)
    try:
        refreshed.dispatch_approval(day)
    finally:
        refreshed.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Compute without publishing a private report.")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or os.environ.get("DEYE_RECOMMENDATION_DRY_RUN") == "true"
    start = monotonic()
    now = datetime.now(MADRID)
    if not dry_run and os.environ.get("DEYE_RECOMMENDATION_ENABLED") != "true":
        return 0
    try:
        repository = os.environ.get("DEYE_DATA_REPOSITORY")
        data_token = os.environ.get("DEYE_DATA_TOKEN")
        public_repository = os.environ.get("GITHUB_REPOSITORY")
        store = GitHubPrivateStore(repository, data_token, public_repository=public_repository)
        try:
            result = run(store, now, dry_run=dry_run)
            if not dry_run and os.environ.get("DEYE_APPROVAL_ENABLED") == "true":
                dispatch_approval(repository, data_token, public_repository,
                                  str(now.date() + timedelta(days=1)))
        finally:
            store.close()
    except ApprovalWorkflowMissing:
        print("Recommendation retained. Verify the approval variables and DEYE_APPROVAL_WORKFLOW_TOKEN in DeyeCloud, then rerun.")
        return 1
    except Exception:
        print(f"Recommendation failed after {monotonic() - start:.1f} s. Private details suppressed.")
        return 1
    status = "RECORDED" if result.startswith("RECORDED_") else result
    print(f"Deye recommendation {status} in {monotonic() - start:.1f} s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
