"""One-shot read audit through a fresh local MCP stdio session.

No credentials, tokens or raw API responses are persisted. Only projected MCP
results are saved, with discovered serial numbers masked before writing.
"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if __package__:
    from .deye_mcp_server import configure_diagnostics
else:
    from deye_mcp_server import configure_diagnostics


def mask_serial(sn):
    return "***" + sn[-4:] if len(sn) > 4 else "***"


def redact(value, serials):
    if isinstance(value, dict):
        return {key: redact(item, serials) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, serials) for item in value]
    if isinstance(value, str):
        for sn in sorted(serials, key=len, reverse=True):
            value = value.replace(sn, mask_serial(sn))
    return value


async def audit(output: Path):
    root = Path(__file__).resolve().parents[1]
    end = int(datetime.now(timezone.utc).timestamp())
    window = {"start_timestamp": end - 7 * 86400, "end_timestamp": end}
    report = {"observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "source": "deye_local MCP stdio 0.4.0", "read_only": True,
              "alarm_window": window, "operations": [], "limitations": []}
    serials = set()
    params = StdioServerParameters(command=sys.executable,
        args=["-B", "-u", str(root / "scripts/deye_mcp_server.py")], cwd=str(root))
    # The server emits fixed diagnostics only. Do not echo dependency errors.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
        async with stdio_client(params, errlog=diagnostics) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                last_call = 0.0
                rate_limited = False

                async def call(name, args):
                    nonlocal last_call, rate_limited
                    # Reserve room for authentication plus a read per call.
                    await asyncio.sleep(max(0, 2.1 - (time.monotonic() - last_call)))
                    if rate_limited:
                        payload = {"ok": False, "error": "SKIPPED_AFTER_RATE_LIMIT"}
                    else:
                        last_call = time.monotonic()
                        result = await session.call_tool(name, args)
                        payload = result.structuredContent
                    if not isinstance(payload, dict):
                        payload = {"ok": False, "error": "INVALID_MCP_RESULT"}
                    if payload.get("error") == "RATE_LIMITED":
                        rate_limited = True
                    report["operations"].append({"tool": name, "arguments": args, "result": payload})
                    return payload

                async def alerts(name, args):
                    total = None
                    received = 0
                    seen_alerts = set()
                    for page in range(1, 11):
                        result = await call(name, {**args, "page": page, "size": 100})
                        if not result.get("ok"):
                            report["limitations"].append(name + ":INCOMPLETE")
                            return
                        data = result["data"]
                        items = data["alertList" if name == "deye_device_alerts" else "stationAlertItems"]
                        if total is None:
                            total = data["total"]
                        page_ids = [item["alertId"] for item in items if item["alertId"] is not None]
                        if (total != data["total"] or seen_alerts.intersection(page_ids)
                                or len(set(page_ids)) != len(page_ids)):
                            break
                        seen_alerts.update(page_ids)
                        received += len(items)
                        if not data["has_more"]:
                            if received == total:
                                return
                            break
                    report["limitations"].append(name + ":PAGINATION_INCOMPLETE")

                status = await call("deye_connection_status", {})
                stations = await call("deye_list_stations", {"page": 1, "size": 50})
                if status.get("ok") and stations.get("ok") and stations.get("total") == 1:
                    station_id = stations["stations"][0]["id"]
                    await call("deye_station_latest", {"station_id": station_id})
                    devices, seen, complete = [], set(), False
                    for page in range(1, 11):
                        result = await call("deye_list_station_devices",
                                            {"station_id": station_id, "page": page, "size": 100})
                        if not result.get("ok"):
                            break
                        data = result["data"]
                        items = data["deviceListItems"]
                        page_sns = {item["deviceSn"] for item in items}
                        if seen & page_sns:
                            report["limitations"].append("DEVICE_PAGINATION_REPEATED")
                            break
                        seen.update(page_sns)
                        devices.extend(items)
                        if not data["has_more"]:
                            complete = len(devices) == data["total"]
                            break
                    if not complete:
                        report["limitations"].append("DEVICE_INVENTORY_INCOMPLETE")
                    serials.update(device["deviceSn"] for device in devices)
                    await alerts("deye_station_alerts", {"station_id": station_id, **window})
                    # A bounded one-shot audit; no polling or automatic retries.
                    if len(devices) > 100:
                        report["limitations"].append("DETAIL_AUDIT_LIMIT_100_DEVICES")
                    devices = devices[:100]
                    for offset in range(0, len(devices), 10):
                        await call("deye_device_latest", {
                            "device_sns": [item["deviceSn"] for item in devices[offset:offset + 10]]})
                    for device in devices:
                        sn, kind = device["deviceSn"], device["deviceType"]
                        args = {"device_sn": sn}
                        if kind == "INVERTER":
                            await call("deye_device_measure_points", args)
                        if kind in {"INVERTER", "PCS", "MICRO_STORAGE_IN_ONE"}:
                            for tool in ("deye_read_battery_settings", "deye_read_system_settings", "deye_read_tou"):
                                await call(tool, args)
                        await alerts("deye_device_alerts", {**args, **window})
                else:
                    report["limitations"].append("AUTH_FAILED_OR_NOT_EXACTLY_ONE_STATION")
    # Mask even serials appearing only in alarm or telemetry results.
    def collect(value):
        if isinstance(value, dict):
            sn = value.get("deviceSn")
            if isinstance(sn, str) and sn:
                serials.add(sn)
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(report)
    safe = redact(report, serials)
    output.write_text(json.dumps(safe, ensure_ascii=True, indent=2), encoding="utf-8")
    print("Auditoria MCP local guardada con numeros de serie ocultos.")
    return safe


def main():
    configure_diagnostics()
    output = Path(__file__).resolve().parents[1] / "docs/deye-audit-readonly.json"
    try:
        asyncio.run(audit(output))
        return 0
    except Exception:
        print("No se pudo completar la auditoria MCP local; sin detalles sensibles.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
