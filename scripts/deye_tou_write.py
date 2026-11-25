"""Prepare and execute the six TOU periods bound to an approved recommendation."""

from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import re
import time

from collect_deye_daily import validate_config
from deye_client import DeyeClientError
from deye_control import (
    VERIFY_ATTEMPTS, VERIFY_INTERVAL_SECONDS, comparable_settings, tou_differences, wait_for_order,
)
from deye_private_store import canonical
from deye_tou import read_periods
from deye_write_lock import InverterBusy, InverterLock


def execution_binding(store, payload, report):
    snapshot_day = date.fromisoformat(payload["date"]) - timedelta(days=1)
    snapshot = store.read(f"evening/{snapshot_day}.json") or {}
    current = [period.record() for period in read_periods(snapshot)]
    if (current != payload["current_periods"]
            or hashlib.sha256(canonical(snapshot["parameters"])).hexdigest() != payload["snapshot_sha256"]
            or report["tou_simulation"]["strategies"]["full"]["periods"] != payload["proposed_periods"]):
        raise ValueError("TOU_BASELINE_CHANGED")
    installation = validate_config(store.read("config/installation.json"))
    parameters = snapshot["parameters"]
    baseline = comparable_settings(parameters["deye_read_tou"]["data"])
    battery = parameters.get("deye_read_battery_settings") or {}
    battery_data = battery.get("data") or {}
    shutdown, low = (battery_data.get(key) for key in ("battShutDownCapacity", "battLowCapacity"))
    floor = report["tou_simulation"].get("minimum_soc")
    proposed = payload["proposed_periods"]
    if (battery.get("status") != "available"
            or type(shutdown) is not int or type(low) is not int or not 0 <= shutdown <= low < 100
            or type(floor) not in (int, float) or not low < floor <= 100
            or type(proposed) is not list or len(proposed) != 6 or proposed == current):
        raise ValueError("UNSAFE_TOU_PLAN")
    rows = []
    for old, new in zip(baseline["timeUseSettingItems"], proposed):
        if (type(new) is not dict or set(new) != {"time", "soc", "power", "enableGridCharge"}
                or not isinstance(new["time"], str)
                or re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9]", new["time"]) is None
                or type(new["soc"]) is not int or not floor <= new["soc"] <= 100
                or type(new["power"]) is not int or not 0 < new["power"] <= 2**31 - 1
                or type(new["enableGridCharge"]) is not bool):
            raise ValueError("INVALID_TOU_PERIOD")
        start = new["time"]
        row = {**new, "time": f"{start[:2]}:{start[2:]}", "enableGeneration": old["enableGeneration"]}
        voltage = old.get("voltage")
        if voltage is not None:
            if type(voltage) is not int or not 0 < voltage <= 2**31 - 1:
                raise ValueError("INVALID_TOU_VOLTAGE")
            row["voltage"] = voltage
        rows.append(row)
    starts = [row["time"] for row in rows]
    if starts != sorted(set(starts)):
        raise ValueError("UNORDERED_TOU_PERIODS")
    return {
        "installation": installation, "tou_baseline": baseline, "battery_baseline": battery_data,
        "write_body": {"deviceSn": installation["device_sn"], "timeUseSettingItems": rows},
    }


def read_dynamic_tou(client, serial, *, on_order=None):
    order_id = client.start_dynamic_read(serial)
    if type(order_id) is not int or not 0 < order_id < 2**63:
        raise ValueError("DYNAMIC_READ_ORDER_ID_MISSING")
    if on_order is not None:
        on_order(order_id)
    if not wait_for_order(client, order_id, serial):
        raise ValueError("DYNAMIC_READ_UNCONFIRMED")
    for attempt in range(VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(VERIFY_INTERVAL_SECONDS)
        result = client.read_dynamic_tou_result(order_id)
        if result.get("timeUseSettingItems"):
            return comparable_settings(result)
    raise ValueError("DYNAMIC_TOU_UNAVAILABLE")


def without_verified_sell(settings):
    # The omission test only established preservation when selling was off.
    settings = deepcopy(settings)
    for row in settings["timeUseSettingItems"]:
        if row.pop("enableSell", None) is not False:
            raise ValueError("TOU_SELL_STATE_UNVERIFIED")
    return settings


def check_inverter(client, installation, battery, now):
    serial = installation["device_sn"]
    inventory = client.query_read("deye_list_station_devices",
                                  {"station_id": installation["station_id"], "size": 100})
    if not any(row["deviceSn"] == serial and row["deviceType"] == "INVERTER"
               and row["connectStatus"] == 1 for row in inventory["deviceListItems"]):
        raise ValueError("INVERTER_NOT_CONNECTED")
    latest = client.query_read("deye_device_latest", {"device_sns": [serial]})
    values = []
    for device in latest.get("deviceDataList") or []:
        stamp = device.get("collectionTime")
        if device["deviceSn"] != serial or type(stamp) is not int or not 0 <= now.timestamp() - stamp <= 900:
            continue
        for item in device.get("dataList") or []:
            if item["key"] in {"SOC", "BMSSOC"} and item["unit"] == "%" and item["value"] is not None:
                value = float(item["value"])
                if 0 <= value <= 100:
                    values.append(value)
    if (not values or min(values) <= battery["battShutDownCapacity"] or max(values) - min(values) > 10):
        raise ValueError("SOC_UNAVAILABLE_OR_UNSAFE")
    if client.query_read("deye_read_battery_settings", {"device_sn": serial}) != battery:
        raise ValueError("BATTERY_SETTINGS_CHANGED")


def apply_approved(factory, client_factory, day, identity, authorize, clock):
    with closing(factory()) as store:
        payload = authorize(store, clock())
    if payload is None:
        return "NO_EXECUTABLE_APPROVAL"
    installation = payload["installation"]
    serial = installation["device_sn"]
    path = f"approvals/executions/{day}/{identity}.json"
    try:
        with InverterLock(factory, serial, clock, execution_path=path) as lock:
            with client_factory() as client:
                client.ensure_authenticated()
                observed = without_verified_sell(read_dynamic_tou(client, serial))
                if tou_differences(payload["tou_baseline"], observed):
                    raise ValueError("TOU_CHANGED_SINCE_APPROVAL")
                check_inverter(client, installation, payload["battery_baseline"], clock())
                def recheck_approval(store):
                    if authorize(store, clock()) != payload:
                        raise ValueError("APPROVAL_CHANGED")

                lock.checkpoint("sending", validate=recheck_approval)
                if clock() >= datetime.fromisoformat(payload["expires_at"]):
                    lock.checkpoint("expired", locked=False)
                    return "APPROVAL_EXPIRED"

                stage = "submission"
                try:
                    order = client.write_tou(payload["write_body"]).get("orderId")
                    if type(order) is not int or not 0 < order < 2**63:
                        raise ValueError("WRITE_ORDER_ID_MISSING")
                    lock.checkpoint("submitted", order_id=order)
                    stage = "write_status"
                    if not wait_for_order(client, order, serial):
                        raise ValueError("WRITE_ORDER_UNCONFIRMED")
                    stage = "verification"
                    readback = read_dynamic_tou(client, serial, on_order=lambda read_id: lock.checkpoint(
                        "verifying", verification_order_id=read_id))
                    expected = comparable_settings({"touAction": "on", **payload["write_body"]})
                    differences = tou_differences(expected, without_verified_sell(readback))
                    if differences:
                        lock.checkpoint("unconfirmed", stage=stage, differences=differences)
                        return "TOU_VERIFICATION_MISMATCH"
                except DeyeClientError as exc:
                    failed = stage == "write_status" and exc.code == "ORDER_FAILED"
                    lock.checkpoint("failed" if failed else "unconfirmed", locked=not failed,
                                    stage=stage, reason=exc.code, details=exc.details or {})
                    return "ORDER_FAILED" if failed else "EXECUTION_UNCONFIRMED"
                except ValueError as exc:
                    lock.checkpoint("unconfirmed", stage=stage, reason=str(exc))
                    return "EXECUTION_UNCONFIRMED"
                lock.checkpoint("verified", locked=False)
                return "VERIFIED"
    except InverterBusy as exc:
        return str(exc)
