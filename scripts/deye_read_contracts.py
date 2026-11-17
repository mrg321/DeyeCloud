"""Closed read contracts, verified against Deye's official reference 2026-11-17.

Source: https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html
Paths: .agents/skills/deye-cloud-scenarios/SKILL.md, API parameter memorandum.
No arbitrary body, path, query, configuration change or command is accepted.
"""

import math
import re
from datetime import date


class MeasurementMap:
    """Official Map<string,string>, restricted to requested/verified measure keys."""


MEASUREMENT_MAP = MeasurementMap()
DAILY_KEYS = frozenset({"ChargingCapacity", "DischargingCapacity", "Production",
                        "GridFeed-in", "Consumption", "ElectricityPurchasing"})
SAMPLE_KEYS = frozenset({"SOC", "BMSSOC", "TotalDCInputPower", "TotalConsumptionPower",
    "TotalGridPower", "BatteryPower", "DC Temperature", "AC Temperature",
    "DailyActiveProduction", "PVDailyPowerGenerationActive", "DailyConsumption", "DailyEnergyPurchased", "DailyGridFeedIn",
    "DailyChargingEnergy", "DailyDischargingEnergy"})


def fields(names, kind):
    return dict.fromkeys(names.split(), kind)


ALERT = {
    **fields("alertCode alertId alertName protocolName", str),
    **fields("alertEndTime alertStartTime impact level status", int),
}
DEVICE = {
    **fields("deviceSn deviceType productId", str),
    **fields("collectionTime connectStatus deviceId parentId stationId", int),
}
READS = {
    "deye_list_station_devices": ("/v1.0/station/device", {
        "deviceListItems": [DEVICE], "total": int,
    }),
    "deye_station_latest": ("/v1.0/station/latest", {
        **fields("batteryPower batterySOC chargePower consumptionPower dischargePower "
                 "generationPower gridPower irradiateIntensity purchasePower wirePower", (int, float)),
        "lastUpdateTime": str,
    }),
    "deye_device_latest": ("/v1.0/device/latest", {
        "deviceDataList": [{
            **fields("deviceSn deviceType", str),
            **fields("collectionTime deviceState", int),
            "dataList": [fields("key unit value", str)],
        }],
    }),
    "deye_device_measure_points": ("/v1.0/device/measurePoints", {
        **fields("deviceSn deviceType productId timeZone", str), "measurePoints": [str],
    }),
    "deye_read_battery_settings": ("/v1.0/config/battery", fields(
        "battCapacity battLowCapacity battShutDownCapacity maxChargeCurrent maxDischargeCurrent", int)),
    "deye_read_system_settings": ("/v1.0/config/system", {
        **fields("energyPattern systemWorkMode", str),
        **fields("maxSellPower maxSolarPower zeroExportPower", int),
    }),
    "deye_read_tou": ("/v1.0/config/tou", {
        "touAction": str, "timeUseSettingItems": [{
            **fields("enableGeneration enableGridCharge", bool),
            **fields("power soc voltage", int), "time": str,
        }],
    }),
    "deye_station_alerts": ("/v1.0/station/alertList", {
        "stationAlertItems": [{**ALERT, **fields("deviceId deviceSn deviceType", str)}],
        "total": int,
    }),
    "deye_device_alerts": ("/v1.0/device/alertList", {
        "alertList": [{**ALERT, **fields("description reason solution", str)}],
        "deviceId": int, **fields("deviceSn deviceType", str), "total": int,
    }),
}
STATION_HISTORY_ITEM = {
    **fields("generationValue consumptionValue gridValue purchaseValue chargeValue dischargeValue "
             "generationPower consumptionPower gridPower purchasePower batteryPower chargePower "
             "dischargePower batterySOC fullPowerHours", (int, float)),
    "timeStamp": str,
}
READS.update({
    "deye_station_metadata": ("/v1.0/station/detail", {"station": {
        "id": int, **fields("name regionTimezone connectionStatus", str),
    }}),
    "deye_station_daily_history": ("/v1.0/station/history", {"stationDataItems": [{
        **STATION_HISTORY_ITEM, **fields("year month day", int),
    }], "total": int}),
    "deye_station_power_history": ("/v1.0/station/history/power", {
        "stationDataItems": [STATION_HISTORY_ITEM], "total": int,
    }),
    "deye_device_daily_history": ("/v1.0/device/history", {
        "deviceSn": str, "deviceType": str, "deviceId": int, "granularity": int,
        "dataList": [{"time": str, "itemList": [MEASUREMENT_MAP]}],
    }),
    "deye_device_intraday_history": ("/v1.0/device/history", {
        "deviceSn": str, "deviceType": str, "deviceId": int, "granularity": int,
        "dataList": [{"time": str, "itemList": [MEASUREMENT_MAP]}],
    }),
})
HISTORY_NAMES = frozenset(name for name in READS if name.endswith("_history"))

INT_ID = {"type": "integer", "minimum": 1, "maximum": 2**63 - 1}
SN = {"type": "string", "minLength": 1, "maxLength": 128,
      "pattern": r"^[A-Za-z0-9_-]+$"}
PAGE = {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1}
SIZE = {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}
STAMP = {"type": "integer", "minimum": 1000000000, "maximum": 9999999999}
DATE = {"type": "string", "minLength": 10, "maxLength": 10, "pattern": r"\d{4}-\d{2}-\d{2}"}
POINT = {"type": "string", "minLength": 1, "maxLength": 128,
         "pattern": r"[A-Za-z0-9 _.-]+", "enum": sorted(SAMPLE_KEYS)}
DESCRIPTIONS = {
    "deye_list_station_devices": "Lista una página de dispositivos de una instalación; lectura EU.",
    "deye_station_latest": "Lee datos actuales de la instalación. null significa no disponible; sin conversión de unidades.",
    "deye_device_latest": "Lee telemetría en caché de 1–10 dispositivos, conservando key/value/unit oficiales.",
    "deye_device_measure_points": "Consulta las claves de medida admitidas por un inversor; lectura EU.",
    "deye_read_battery_settings": "Lee parámetros de batería existentes, sin modificar ni enviar órdenes.",
    "deye_read_system_settings": "Lee el modo y parámetros de funcionamiento existentes del inversor.",
    "deye_read_tou": "Lee horarios TOU existentes, sin modificar ni enviar órdenes.",
    "deye_station_alerts": "Lee una página de alarmas de instalación en un intervalo Unix en segundos (máximo 180 días).",
    "deye_device_alerts": "Lee una página de alarmas del dispositivo en un intervalo Unix en segundos (máximo 180 días).",
}
DESCRIPTIONS.update({
    "deye_station_metadata": "Lee nombre y zona horaria de instalación; excluye dirección, contacto y propietario.",
    "deye_station_daily_history": "Lee energía diaria oficial por fechas locales YYYY-MM-DD, inclusivas; máximo 7 días.",
    "deye_station_power_history": "Lee curva de potencia y SOC por Unix segundos; máximo 25 horas; no es energía horaria.",
    "deye_device_daily_history": "Lee resúmenes diarios del dispositivo; fechas locales inclusivas, máximo 7 días.",
    "deye_device_intraday_history": "Lee un día local de medidas verificadas del inversor (SOC/potencia/temperatura/contadores).",
})


def input_schema(name):
    props, required = {}, []
    if name in {"deye_list_station_devices", "deye_station_latest", "deye_station_alerts",
                "deye_station_metadata", "deye_station_daily_history", "deye_station_power_history"}:
        props["station_id"] = INT_ID
        required.append("station_id")
    elif name == "deye_device_latest":
        props["device_sns"] = {"type": "array", "items": SN, "minItems": 1,
                               "maxItems": 10, "uniqueItems": True}
        required.append("device_sns")
    else:
        props["device_sn"] = SN
        required.append("device_sn")
    if name in {"deye_list_station_devices", "deye_station_alerts", "deye_device_alerts"}:
        props.update(page=PAGE, size=SIZE)
    if name.endswith("_alerts"):
        props.update(start_timestamp=STAMP, end_timestamp=STAMP)
        required.extend(["start_timestamp", "end_timestamp"])
    if name in {"deye_station_daily_history", "deye_device_daily_history"}:
        props.update(start_date=DATE, end_date=DATE)
        required.extend(["start_date", "end_date"])
    if name == "deye_station_power_history":
        props.update(start_timestamp=STAMP, end_timestamp=STAMP)
        required.extend(["start_timestamp", "end_timestamp"])
    if name == "deye_device_intraday_history":
        props.update(date=DATE, measure_points={"type": "array", "items": POINT,
                     "minItems": 1, "maxItems": 15, "uniqueItems": True})
        required.extend(["date", "measure_points"])
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def validate(value, schema):
    """Small closed schema validator; never returns input in an error message."""
    kind = schema["type"]
    if kind == "object":
        return (type(value) is dict and not (value.keys() - schema["properties"].keys())
                and all(key in value for key in schema.get("required", []))
                and all(validate(val, schema["properties"][key]) for key, val in value.items()))
    if kind == "integer":
        return type(value) is int and schema["minimum"] <= value <= schema["maximum"]
    if kind == "string":
        return (type(value) is str and schema["minLength"] <= len(value) <= schema["maxLength"]
                and re.fullmatch(schema["pattern"], value) is not None
                and ("enum" not in schema or value in schema["enum"]))
    if kind == "array":
        return (type(value) is list and schema["minItems"] <= len(value) <= schema["maxItems"]
                and all(validate(item, schema["items"]) for item in value)
                and len(set(value)) == len(value))
    return False


def request_body(name, args):
    if not validate(args, input_schema(name)):
        raise ValueError("INVALID_ARGUMENTS")
    if name in {"deye_station_daily_history", "deye_device_daily_history"}:
        first, last = date.fromisoformat(args["start_date"]), date.fromisoformat(args["end_date"])
        if not 0 <= (last - first).days <= 6:
            raise ValueError("INVALID_ARGUMENTS")
    if name == "deye_device_intraday_history":
        date.fromisoformat(args["date"])
    if name == "deye_station_power_history" and not (
        0 < args["end_timestamp"] - args["start_timestamp"] <= 25 * 3600
    ):
        raise ValueError("INVALID_ARGUMENTS")
    if name.endswith("_alerts") and not (
        0 <= args["end_timestamp"] - args["start_timestamp"] <= 180 * 86400
    ):
        raise ValueError("INVALID_ARGUMENTS")
    body = {}
    if "station_id" in args:
        if name == "deye_list_station_devices":
            body["stationIds"] = [args["station_id"]]
        else:
            body["stationId"] = args["station_id"]
    if "device_sn" in args:
        body["deviceSn"] = args["device_sn"]
    if "device_sns" in args:
        body["deviceList"] = args["device_sns"]
    if "page" in input_schema(name)["properties"]:
        body.update(page=args.get("page", 1), size=args.get("size", 50))
    if name.endswith("_alerts"):
        body.update(startTimestamp=args["start_timestamp"], endTimestamp=args["end_timestamp"])
    if name in {"deye_station_daily_history", "deye_device_daily_history"}:
        body.update(startAt=args["start_date"], endAt=args["end_date"], granularity=2)
    if name == "deye_device_intraday_history":
        body.update(startAt=args["date"], endAt=args["date"], granularity=1, measurePoints=args["measure_points"])
    if name == "deye_station_power_history":
        body.update(startTimestamp=args["start_timestamp"], endTimestamp=args["end_timestamp"])
    return body


def project(value, shape, *, measure_keys=frozenset()):
    """Discard all undocumented fields recursively; preserve missing data as null."""
    if value is None:
        return None
    if shape is MEASUREMENT_MAP:
        if type(value) is not dict or len(value) > 64:
            raise ValueError("INVALID_RESPONSE")
        keys = {"key", "value", "unit"} | set(measure_keys)
        # Preserve only named measures; ignore arbitrary metadata/secrets.
        return {key: project(item, str) for key, item in value.items() if key in keys}
    if type(shape) is dict:
        if type(value) is not dict:
            raise ValueError("INVALID_RESPONSE")
        return {key: project(value.get(key), child, measure_keys=measure_keys) for key, child in shape.items()}
    if type(shape) is list:
        if type(value) is not list or len(value) > 2000 or any(item is None for item in value):
            raise ValueError("INVALID_RESPONSE")
        return [project(item, shape[0], measure_keys=measure_keys) for item in value]
    kinds = shape if type(shape) is tuple else (shape,)
    if type(value) not in kinds or (type(value) is str and len(value) > 4096):
        raise ValueError("INVALID_RESPONSE")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("INVALID_RESPONSE")
    return value


def schema_issues(value, shape, prefix=""):
    """Known field names and type names only; never response values or unknown keys."""
    issues = []
    if type(shape) is list:
        if type(value) is not list:
            return [{"field": prefix or "response", "expected": "list", "actual": type(value).__name__}]
        for item in value[:10]:
            if item is not None:
                issues.extend(schema_issues(item, shape[0], prefix + "[]."))
        return issues[:10]
    if type(value) is not dict or type(shape) is not dict:
        return [{"field": "response", "expected": "object", "actual": type(value).__name__}]
    for key, expected in shape.items():
        item = value.get(key)
        if item is None:
            continue
        if type(expected) is dict and type(item) is dict:
            issues.extend(schema_issues(item, expected, prefix + key + "."))
        elif type(expected) is list:
            issues.extend(schema_issues(item, expected, prefix + key))
        elif isinstance(expected, type) or type(expected) is tuple:
            kinds = expected if type(expected) is tuple else (expected,)
            if type(item) not in kinds:
                issues.append({"field": prefix + key,
                    "expected": "/".join(kind.__name__ for kind in kinds),
                    "actual": type(item).__name__})
    return issues[:10]
