"""Phase 4B: bounded read-only history audit through local MCP, no cloud writes."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if __package__:
    from .audit_deye_readonly import redact
    from .deye_mcp_server import configure_diagnostics
    from .deye_history_quality import MADRID, day_bounds, extract_device_samples, analyze_samples, sample_time, numeric
else:
    from audit_deye_readonly import redact
    from deye_mcp_server import configure_diagnostics
    from deye_history_quality import MADRID, day_bounds, extract_device_samples, analyze_samples, sample_time, numeric


MAX_TOOL_CALLS = 44  # At most 88 HTTP requests including reauthentication.
GROUPS = [
    ["SOC", "BMSSOC", "DC Temperature", "AC Temperature"],
    ["TotalDCInputPower", "TotalConsumptionPower", "TotalGridPower", "BatteryPower"],
    ["PVDailyPowerGenerationActive", "DailyConsumption", "DailyEnergyPurchased",
     "DailyGridFeedIn", "DailyChargingEnergy", "DailyDischargingEnergy"],
]


async def collect(session, *, now=None, pace=2.1):
    now = now or datetime.now(timezone.utc)
    report = {"phase": "4B", "source": "deye_local MCP 0.4.1", "read_only": True,
        "observed_at_utc": now.isoformat(), "display_timezone": "Europe/Madrid",
        "max_tool_calls": MAX_TOOL_CALLS, "operations": [], "limitations": []}
    serials, last_call, stopped = set(), 0.0, False

    async def call(name, args):
        nonlocal last_call, stopped
        if len(report["operations"]) >= MAX_TOOL_CALLS or stopped:
            report["limitations"].append("REQUEST_BUDGET_OR_RATE_LIMIT")
            return {"ok": False, "error": "LOCAL_BUDGET_STOP"}
        await asyncio.sleep(max(0, pace - (time.monotonic() - last_call)))
        last_call = time.monotonic()
        result = await session.call_tool(name, args)
        payload = result.structuredContent
        if not isinstance(payload, dict):
            payload = {"ok": False, "error": "INVALID_MCP_RESULT"}
        if payload.get("error") in {"RATE_LIMITED", "AUTH_REJECTED", "ACCESS_DENIED"}:
            stopped = True
        report["operations"].append({"tool": name, "arguments": args, "result": payload})
        return payload

    await call("deye_connection_status", {})
    stations = await call("deye_list_stations", {"size": 50})
    if not stations.get("ok") or stations.get("total") != 1:
        report["limitations"].append("NOT_EXACTLY_ONE_ACCESSIBLE_STATION")
        return report
    station_id = stations["stations"][0]["id"]
    metadata = await call("deye_station_metadata", {"station_id": station_id})
    station = (metadata.get("data") or {}).get("station") or {}
    source_zone = station.get("regionTimezone")
    if source_zone:
        try:
            ZoneInfo(source_zone)
        except (KeyError, ValueError):
            source_zone = None
    if not source_zone:
        report["limitations"].append("STATION_TIMEZONE_UNAVAILABLE")
    report["station_timezone"] = source_zone
    devices = await call("deye_list_station_devices", {"station_id": station_id, "size": 100})
    if not devices.get("ok") or devices["data"]["has_more"]:
        report["limitations"].append("INCOMPLETE_DEVICE_INVENTORY")
        return report
    inventory = devices["data"]["deviceListItems"]
    serials.update(item["deviceSn"] for item in inventory)
    inverters = [item for item in inventory if item["deviceType"] == "INVERTER"]
    if len(inverters) != 1:
        report["limitations"].append("NOT_EXACTLY_ONE_INVERTER")
        return redact(report, serials)
    sn = inverters[0]["deviceSn"]
    latest = await call("deye_device_latest", {"device_sns": [sn]})
    measures = await call("deye_device_measure_points", {"device_sn": sn})
    supported = set((measures.get("data") or {}).get("measurePoints") or [])
    await call("deye_station_latest", {"station_id": station_id})
    await call("deye_read_tou", {"device_sn": sn})
    records = (latest.get("data") or {}).get("deviceDataList") or []
    latest_time = next((item.get("collectionTime") for item in records if item.get("deviceSn") == sn), None)
    anchor = now.astimezone(MADRID).date()
    if type(latest_time) is int and 1000000000 <= latest_time <= int(now.timestamp()):
        anchor = min(anchor, datetime.fromtimestamp(latest_time, MADRID).date())
    first = anchor - timedelta(days=6)
    start, _ = day_bounds(first)
    _, end = day_bounds(anchor)
    # The current day is explicitly partial. Never request future time.
    end = min(end, int(now.timestamp()))
    report["requested_window"] = {"first_date": str(first), "last_date": str(anchor),
        "start_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "end_utc_exclusive": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "last_day_partial": anchor == now.astimezone(MADRID).date()}
    offsets_match = source_zone is not None
    if source_zone:
        # Check all boundaries, including both sides of a DST transition.
        for hours in range(0, int((end - start) // 3600) + 1):
            instant = datetime.fromtimestamp(start + hours * 3600, timezone.utc)
            if instant.astimezone(ZoneInfo(source_zone)).utcoffset() != instant.astimezone(MADRID).utcoffset():
                offsets_match = False
                break
    report["source_offsets_match_madrid_for_window"] = offsets_match
    if not offsets_match:
        report["limitations"].append("DAILY_BUCKETS_NOT_VERIFIED_AS_MADRID_DAYS")
    daily_args = {"start_date": str(first), "end_date": str(anchor)}
    await call("deye_station_daily_history", {"station_id": station_id, **daily_args})
    device_days = await call("deye_device_daily_history", {"device_sn": sn, **daily_args})
    available_device_dates = {row.get("time") for row in
        (device_days.get("data") or {}).get("dataList") or []}
    all_device_samples, station_samples = [], []
    report["parse_issues"] = []
    for index in range(7):
        day = first + timedelta(days=index)
        left, right = day_bounds(day)
        right = min(right, end)
        if left >= right:
            continue
        # API endpoint end inclusivity is unspecified. Exclude next midnight on input,
        # and apply a half-open interval again when analyzing returned samples.
        result = await call("deye_station_power_history", {
            "station_id": station_id, "start_timestamp": left, "end_timestamp": right - 1})
        if result.get("ok"):
            for row in result["data"].get("stationDataItems") or []:
                stamp, issue = sample_time(row.get("timeStamp"), source_zone, str(day))
                if stamp is not None and not left <= stamp.timestamp() < right:
                    stamp, issue = None, "OUTSIDE_REQUESTED_INTERVAL"
                if issue:
                    report["parse_issues"].append({"source": "station", "date": str(day), "issue": issue})
                for key in ("generationPower", "consumptionPower", "purchasePower", "gridPower",
                            "chargePower", "dischargePower", "batterySOC"):
                    station_samples.append({"timestamp": stamp.isoformat() if stamp else None,
                        "key": key, "value": numeric(row.get(key)), "unit": None})
        # Daily inventory determines which earlier days warrant device detail.
        # Always probe the latest day: its daily rollup may not exist yet.
        if str(day) not in available_device_dates and day != anchor:
            continue
        for group in GROUPS:
            points = [key for key in group if key in supported]
            if not points:
                continue
            result = await call("deye_device_intraday_history", {
                "device_sn": sn, "date": str(day), "measure_points": points})
            if result.get("ok"):
                samples, issues = extract_device_samples(result["data"], set(points), source_zone, str(day))
                all_device_samples.extend(samples)
                if issues:
                    report["parse_issues"].append({"source": "device", "date": str(day), "issues": issues})
    report["device_quality"] = analyze_samples(all_device_samples, start, end)
    report["station_quality"] = analyze_samples(station_samples, start, end)
    report["calls_used"] = len(report["operations"])
    # Raw sample values remain available in their projected operation results.
    report["limitations"].extend([
        "NO_GAP_FILLING_OR_POWER_TO_ENERGY_INTEGRATION",
        "STATION_POWER_UNITS_UNSPECIFIED_IN_OFFICIAL_SCHEMA",
        "DEVICE_COUNTER_SPANS_ARE_NOT_COMPLETE_HOURLY_ENERGY",
        "SEVEN_CALENDAR_DAYS_REQUESTED_NOT_SEVEN_GUARANTEED_DAYS_WITH_DATA",
    ])
    return redact(report, serials)


async def run_audit(root):
    params = StdioServerParameters(command=sys.executable,
        args=["-B", "-u", str(root / "scripts/deye_mcp_server.py")], cwd=str(root))
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        async with stdio_client(params, errlog=diagnostics) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                report = await collect(session)
    (root / "docs/deye-phase4b-results.json").write_text(
        json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print("Fase 4B guardada: resultados MCP saneados y resumen horario; sin cambios en Deye Cloud.")


def main():
    configure_diagnostics()
    try:
        asyncio.run(run_audit(Path(__file__).resolve().parents[1]))
        return 0
    except Exception:
        print("Fase 4B no completada; error local saneado.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
