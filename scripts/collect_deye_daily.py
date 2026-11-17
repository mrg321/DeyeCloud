"""Standalone scheduled read collector; production writes only a verified private repo."""

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import http.client
import logging
import os
import re

if __package__:
    from .deye_client import DeyeClient, DeyeClientError
    from .deye_history_quality import MADRID, day_bounds, numeric, sample_time, analyze_samples
    from .deye_private_store import GitHubPrivateStore, canonical
else:
    from deye_client import DeyeClient, DeyeClientError
    from deye_history_quality import MADRID, day_bounds, numeric, sample_time, analyze_samples
    from deye_private_store import GitHubPrivateStore, canonical


class CollectionError(RuntimeError):
    pass


ENERGY = {"Production": "generationValue", "Consumption": "consumptionValue",
          "ElectricityPurchasing": "purchaseValue", "GridFeed-in": "gridValue",
          "ChargingCapacity": "chargeValue", "DischargingCapacity": "dischargeValue"}
POINT_GROUPS = [("SOC", "BMSSOC", "DC Temperature", "AC Temperature"),
                ("TotalDCInputPower", "TotalConsumptionPower", "TotalGridPower", "BatteryPower")]
OPTIONAL_ERRORS = {"UNSUPPORTED_CONFIG_POINT", "UNSUPPORTED_DEVICE", "DEVICE_OFFLINE", "ACCESS_DENIED"}
SENSITIVE_KEYS = {"accesstoken", "refreshtoken", "token", "appid", "appsecret", "email", "password", "authorization",
                  "headers", "credential", "credentials", "msg", "requestid"}


def clean_original(value, secrets=()):
    """Archive successful read payloads, never authentication/envelope messages or secrets."""
    if type(value) is dict:
        return {key: clean_original(item, secrets) for key, item in value.items()
                if re.sub(r"[^a-z]", "", key.lower()) not in SENSITIVE_KEYS}
    if type(value) is list:
        return [clean_original(item, secrets) for item in value]
    if type(value) is str:
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def validate_config(config):
    if (type(config) is not dict or set(config) != {
            "station_id", "device_sn", "installation_start", "timezone"}
            or type(config["station_id"]) is not int or not 1 <= config["station_id"] <= 2**63 - 1
            or not isinstance(config["device_sn"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", config["device_sn"])
            or config["timezone"] != "Europe/Madrid"):
        raise CollectionError("PRIVATE_CONFIG_INVALID")
    try:
        first = date.fromisoformat(config["installation_start"])
        if str(first) != config["installation_start"]:
            raise ValueError()
    except (TypeError, ValueError):
        raise CollectionError("PRIVATE_CONFIG_INVALID") from None
    return config


def collection_days(config, now, end_date=None):
    latest = now.astimezone(MADRID).date() - timedelta(days=1)
    try:
        end = date.fromisoformat(end_date) if end_date else latest
        if end_date and str(end) != end_date:
            raise ValueError()
    except (TypeError, ValueError):
        raise CollectionError("DATE_INVALID") from None
    if end > latest:
        raise CollectionError("INCOMPLETE_DAY_FORBIDDEN")
    first = max(date.fromisoformat(config["installation_start"]), end - timedelta(days=3))
    return [str(first + timedelta(days=i)) for i in range(max(0, (end - first).days + 1))]


def validate_timezone(source_zone, days):
    from zoneinfo import ZoneInfo
    try:
        zone = ZoneInfo(source_zone)
        for day in days:
            left, right = day_bounds(day)
            for seconds in range(left, right + 1, 3600):
                stamp = datetime.fromtimestamp(seconds, timezone.utc)
                if stamp.astimezone(zone).utcoffset() != stamp.astimezone(MADRID).utcoffset():
                    raise ValueError()
    except (TypeError, ValueError, KeyError):
        raise CollectionError("STATION_TIMEZONE_MISMATCH") from None


def station_daily(data, days):
    if data.get("quality", {}).get("complete_by_total") is False:
        raise CollectionError("INCOMPLETE_STATION_RESPONSE")
    found = {}
    for row in data.get("stationDataItems") or []:
        try:
            day = str(date(row["year"], row["month"], row["day"]))
        except (ValueError, TypeError, KeyError):
            raise CollectionError("INVALID_DAILY_DATE") from None
        if day not in days or day in found:
            raise CollectionError("DUPLICATE_OR_UNREQUESTED_DAY")
        found[day] = {}
        for key in ENERGY.values():
            value = row.get(key)
            if value is not None and (numeric(value) is None or value < 0):
                raise CollectionError("INVALID_DAILY_VALUE")
            found[day][key] = value
    return found


def device_daily(data, days):
    found = {}
    for row in data.get("dataList") or []:
        day = row.get("time")
        if day not in days or day in found:
            raise CollectionError("DUPLICATE_OR_UNREQUESTED_DAY")
        values = dict.fromkeys(ENERGY)
        seen = set()
        for item in row.get("itemList") or []:
            key = item.get("key")
            if key not in ENERGY:
                continue
            if key in seen:
                raise CollectionError("DUPLICATE_MEASURE")
            seen.add(key)
            raw = item.get("value")
            value = numeric(raw)
            if raw is not None and (value is None or value < 0 or item.get("unit") != "kWh"):
                raise CollectionError("INVALID_DAILY_UNIT_OR_VALUE")
            values[key] = value
        found[day] = values
    return found


def intraday_samples(data, points, day, source_zone):
    left, right = day_bounds(day)
    result, seen, units = [], set(), {}
    for row in data.get("dataList") or []:
        stamp, error = sample_time(row.get("time"), source_zone, day)
        if error or stamp is None or not left <= stamp.timestamp() < right:
            raise CollectionError("INVALID_SAMPLE_TIME")
        for item in row.get("itemList") or []:
            key = item.get("key")
            if key not in points:
                continue
            identity = (stamp.isoformat(), key)
            if identity in seen:
                raise CollectionError("DUPLICATE_SAMPLE")
            seen.add(identity)
            raw, unit = item.get("value"), item.get("unit")
            value = numeric(raw)
            if raw is None:
                continue  # Missing never overwrites a good historical observation.
            allowed = ({"%"} if key in {"SOC", "BMSSOC"} else
                       {"℃", "°C"} if "Temperature" in key else {"W", "kW"})
            if value is None or unit not in allowed or (key in units and units[key] != unit):
                raise CollectionError("INVALID_SAMPLE_UNIT_OR_VALUE")
            if key in {"SOC", "BMSSOC"} and not 0 <= value <= 100:
                raise CollectionError("SOC_RANGE_INVALID")
            units[key] = unit
            result.append({"timestamp": stamp.isoformat(), "key": key, "value": value, "unit": unit})
    return result


def merge_energy(current, previous):
    merged, retained = dict(current), []
    for key, value in previous.items():
        if key in merged and merged[key] is None and value is not None:
            merged[key] = value
            retained.append(key)
    return merged, sorted(retained)


def collect(client, store, *, now=None, end_date=None, originals=None):
    """Build a full in-memory transaction, then publish once. No local data writes."""
    config = validate_config(store.read("config/installation.json"))
    now = now or datetime.now(timezone.utc)
    days = collection_days(config, now, end_date)
    if not days:
        return False
    station_id, sn = config["station_id"], config["device_sn"]
    changes, source_hashes = {}, set()
    originals = originals if originals is not None else []

    def query(name, args):
        client.ensure_authenticated()
        data = client.query_read(name, args)
        # Only the observer receives successful authenticated read payloads.
        while originals:
            original = originals.pop(0)
            digest = hashlib.sha256(canonical(original)).hexdigest()
            changes["originals/" + digest + ".json"] = original
            source_hashes.add(digest)
        return data

    inventory = query("deye_list_station_devices", {"station_id": station_id, "size": 100})
    if inventory.get("has_more") or sum(item["deviceSn"] == sn and item["deviceType"] == "INVERTER"
                                       for item in inventory["deviceListItems"]) != 1:
        raise CollectionError("CONFIGURED_DEVICE_NOT_VERIFIED")
    metadata = query("deye_station_metadata", {"station_id": station_id})
    source_zone = (metadata.get("station") or {}).get("regionTimezone")
    validate_timezone(source_zone, days)
    measures = query("deye_device_measure_points", {"device_sn": sn})
    supported = set(measures.get("measurePoints") or [])
    dates = {"start_date": days[0], "end_date": days[-1]}
    station = station_daily(query("deye_station_daily_history", {"station_id": station_id, **dates}), days)
    device = device_daily(query("deye_device_daily_history", {"device_sn": sn, **dates}), days)
    for day in days:
        old = store.read("daily/" + day + ".json") or {}
        old_energy = old.get("energy", {})
        device_values, retained_device = merge_energy(device.get(day, dict.fromkeys(ENERGY)), old_energy.get("device", {}))
        station_values, retained_station = merge_energy(station.get(day, dict.fromkeys(ENERGY.values())), old_energy.get("station", {}))
        fresh = []
        for group in POINT_GROUPS:
            points = [key for key in group if key in supported]
            if points:
                data = query("deye_device_intraday_history", {"device_sn": sn, "date": day, "measure_points": points})
                fresh.extend(intraday_samples(data, points, day, source_zone))
        old_series = store.read("intraday/" + day + ".json") or {}
        merged = {(item["timestamp"], item["key"]): item for item in old_series.get("samples", [])}
        merged.update({(item["timestamp"], item["key"]): item for item in fresh})
        series = [merged[key] for key in sorted(merged)]
        left, right = day_bounds(day)
        quality = analyze_samples(series, left, right)
        quality.pop("hours", None)
        daily = {"schema_version": 1, "date": day, "timezone": "Europe/Madrid", "energy_unit": "kWh",
            "energy": {"device": device_values, "station": station_values},
            "quality": {"device_day_returned": day in device, "station_day_returned": day in station,
                "retained_device_fields": retained_device, "retained_station_fields": retained_station,
                "missing_device_fields": [key for key, value in device_values.items() if value is None],
                "missing_station_fields": [key for key, value in station_values.items() if value is None],
                "fresh_samples": len(fresh), "intraday": quality}}
        changes["daily/" + day + ".json"] = daily
        changes["intraday/" + day + ".json"] = {"schema_version": 1, "date": day, "samples": series}
        changes["consumption/" + day + ".json"] = {"date": day, "unit": "kWh",
            "consumption": device_values["Consumption"], "purchase_reported": device_values["ElectricityPurchasing"],
            "export_reported": device_values["GridFeed-in"],
            "retained_fields": retained_device, "day_returned": day in device}
    parameters = {}
    old_parameters = store.read("parameters/latest.json") or {}
    for name in ("deye_read_system_settings", "deye_read_battery_settings", "deye_read_tou"):
        try:
            parameters[name] = {"status": "available", "data": query(name, {"device_sn": sn})}
        except DeyeClientError as error:
            if error.code not in OPTIONAL_ERRORS:
                raise
            parameters[name] = {"status": error.code,
                "last_available": old_parameters.get(name, {}).get("data") or old_parameters.get(name, {}).get("last_available")}
    changes["parameters/latest.json"] = parameters
    changes["state/collection.json"] = {"schema_version": 1, "last_target_date": days[-1],
        "queried_dates": days, "original_hashes": sorted(source_hashes),
        "backfill": end_date is not None, "timezone": "Europe/Madrid"}
    return store.publish(changes)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Daily read-only collection to a private data repository.")
    parser.add_argument("--end-date", default=None, help="Optional closed date YYYY-MM-DD; reads it and three earlier dates.")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    http.client.HTTPConnection.debuglevel = 0
    store = client = None
    try:
        if os.environ.get("DEYE_AUTOMATION_ENABLED") != "true":
            raise CollectionError("AUTOMATION_NOT_AUTHORIZED")
        store = GitHubPrivateStore(os.environ.get("DEYE_DATA_REPOSITORY"), os.environ.get("DEYE_DATA_TOKEN"),
                                   public_repository=os.environ.get("GITHUB_REPOSITORY"))
        originals = []
        secrets = [os.environ.get(key, "") for key in ("DEYE_APP_ID", "DEYE_APP_SECRET", "DEYE_EMAIL", "DEYE_PASSWORD", "DEYE_DATA_TOKEN")]
        def observer(path, payload):
            originals.append({"path": path, "payload": clean_original(payload, secrets + [client._token or ""])})
        client = DeyeClient(auth_mode="environment", read_observer=observer, min_interval=2.1, max_requests=32)
        collect(client, store, end_date=args.end_date or os.environ.get("DEYE_END_DATE") or None, originals=originals)
        print("Daily collection completed; private data only.")
        return 0
    except Exception:
        print("Daily collection failed; sensitive details suppressed.")
        return 1
    finally:
        for resource in (client, store):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass  # Cleanup must never render a raw exception in public logs.


if __name__ == "__main__":
    raise SystemExit(main())
