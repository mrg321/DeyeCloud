"""At most six diagnostic reads remaining from the phase 4B budget of 44."""

import asyncio
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if __package__:
    from .audit_deye_readonly import redact
    from .audit_deye_history import GROUPS, MAX_TOOL_CALLS
    from .deye_history_quality import day_bounds, extract_device_samples, analyze_samples
    from .deye_mcp_server import configure_diagnostics
else:
    from audit_deye_readonly import redact
    from audit_deye_history import GROUPS, MAX_TOOL_CALLS
    from deye_history_quality import day_bounds, extract_device_samples, analyze_samples
    from deye_mcp_server import configure_diagnostics


async def collect_followup(session, prior, *, pace=2.1):
    budget = min(6, max(0, MAX_TOOL_CALLS - len(prior["operations"])))
    report = {"phase": "4B-diagnostics", "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True, "prior_tool_calls": len(prior["operations"]),
        "max_additional_tool_calls": budget, "operations": [], "limitations": []}
    serials, samples, last_call, stopped = set(), [], 0.0, False

    async def call(name, args):
        nonlocal last_call, stopped
        if len(report["operations"]) >= budget or stopped:
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

    station_id = next(o["result"]["stations"][0]["id"] for o in prior["operations"]
                      if o["tool"] == "deye_list_stations" and o["result"].get("ok"))
    inventory = await call("deye_list_station_devices", {"station_id": station_id, "size": 100})
    if not inventory.get("ok") or inventory["data"]["has_more"]:
        report["limitations"].append("INVENTORY_UNAVAILABLE_OR_INCOMPLETE")
        return report
    devices = inventory["data"]["deviceListItems"]
    serials.update(item["deviceSn"] for item in devices)
    inverters = [item for item in devices if item["deviceType"] == "INVERTER"]
    if len(inverters) != 1:
        return redact(report, serials)
    sn = inverters[0]["deviceSn"]
    days = []
    for operation in prior["operations"]:
        if operation["tool"] == "deye_device_daily_history" and operation["result"].get("ok"):
            for row in operation["result"]["data"].get("dataList") or []:
                try:
                    day = date.fromisoformat(row["time"])
                except (ValueError, TypeError, KeyError):
                    continue
                if prior["requested_window"]["first_date"] <= str(day) <= prior["requested_window"]["last_date"]:
                    days.append(str(day))
    days = sorted(set(days))[-2:]
    if days:
        left, right = day_bounds(days[-1])
        right = min(right, int(datetime.now(timezone.utc).timestamp()))
        await call("deye_station_power_history", {"station_id": station_id,
            "start_timestamp": left, "end_timestamp": right - 1})
    available = set(next(o["result"]["data"]["measurePoints"] for o in prior["operations"]
                        if o["tool"] == "deye_device_measure_points" and o["result"].get("ok")))
    for day in days:
        for group in GROUPS[:2]:
            points = [key for key in group if key in available]
            if not points:
                continue
            result = await call("deye_device_intraday_history", {"device_sn": sn,
                "date": day, "measure_points": points})
            if result.get("ok"):
                parsed, issues = extract_device_samples(result["data"], set(points), prior.get("station_timezone"), day)
                samples.extend(parsed)
                if issues:
                    report["limitations"].append({"date": day, "parse_issues": issues})
    start, _ = day_bounds(prior["requested_window"]["first_date"])
    _, end = day_bounds(prior["requested_window"]["last_date"])
    end = min(end, int(datetime.now(timezone.utc).timestamp()))
    report["device_quality"] = analyze_samples(samples, start, end)
    report["total_phase4b_tool_calls"] = report["prior_tool_calls"] + len(report["operations"])
    report["diagnostic_dates"] = days
    return redact(report, serials)


async def run(root):
    prior = json.loads((root / "docs/deye-phase4b-results.json").read_text(encoding="utf-8"))
    params = StdioServerParameters(command=sys.executable,
        args=["-B", "-u", str(root / "scripts/deye_mcp_server.py")], cwd=str(root))
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        async with stdio_client(params, errlog=diagnostics) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                report = await collect_followup(session, prior)
    (root / "docs/deye-phase4b-followup.json").write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print("Diagnostico 4B guardado; presupuesto total maximo de 44 llamadas MCP.")


def main():
    configure_diagnostics()
    try:
        asyncio.run(run(Path(__file__).resolve().parents[1]))
        return 0
    except Exception:
        print("Diagnostico 4B no completado; error local saneado.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
