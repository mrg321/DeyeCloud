"""Offline phase 4B report tables from sanitized evidence; performs no HTTP."""

import json
from pathlib import Path

if __package__:
    from .deye_history_quality import daily_date_quality, numeric
else:
    from deye_history_quality import daily_date_quality, numeric


def summarize(prior, followup):
    window = prior["requested_window"]
    station_rows = next(o["result"]["data"]["stationDataItems"] for o in prior["operations"]
                        if o["tool"] == "deye_station_daily_history" and o["result"].get("ok"))
    device_rows = next(o["result"]["data"]["dataList"] for o in prior["operations"]
                       if o["tool"] == "deye_device_daily_history" and o["result"].get("ok"))
    station_dates = []
    for row in station_rows:
        try:
            station_dates.append(f'{row["year"]:04d}-{row["month"]:02d}-{row["day"]:02d}')
        except (TypeError, KeyError, ValueError):
            station_dates.append(None)
    daily = {"station": daily_date_quality(station_dates, window["first_date"], window["last_date"]),
             "device": daily_date_quality([row.get("time") for row in device_rows], window["first_date"], window["last_date"])}
    points, units, invalid, dates = {}, {}, {}, {}
    for operation in followup["operations"]:
        if operation["tool"] != "deye_device_intraday_history" or not operation["result"].get("ok"):
            continue
        day = operation["arguments"]["date"]
        dates.setdefault(day, set())
        for row in operation["result"]["data"].get("dataList") or []:
            dates[day].add(row["time"])
            for item in row.get("itemList") or []:
                key = item.get("key")
                if key not in operation["arguments"]["measure_points"]:
                    continue
                number = numeric(item.get("value"))
                if number is None:
                    invalid[key] = invalid.get(key, 0) + 1
                    continue
                points.setdefault(key, []).append(number)
                units.setdefault(key, set()).add(item.get("unit"))
    return {"daily_coverage": daily, "intraday_counts": {day: len(items) for day, items in dates.items()},
        "intraday_ranges": {key: {"min": min(values), "max": max(values), "samples": len(values),
            "units": sorted(units[key], key=str)} for key, values in points.items()},
        "invalid_numeric_values": invalid,
        "total_tool_calls": followup["total_phase4b_tool_calls"],
        "hour_buckets": len({row["hour_madrid"] for row in followup["device_quality"]["hours"]})}


def hourly_markdown(followup):
    rows = {}
    for item in followup["device_quality"]["hours"]:
        rows.setdefault(item["hour_madrid"], {})[item["key"]] = item
    lines = ["# Fase 4B: muestras por hora en Europe/Madrid", "",
        "Medias aritméticas de muestras en W; no son energía en kWh ni medias ponderadas.",
        "Red y batería conservan el signo recibido. SOC y temperaturas son rangos observados.",
        "Horas de los extremos parciales; no se rellenan huecos ni se interpolan límites.", "",
        "| Hora local (offset incluido) | Muestras por serie | FV media W | Vivienda media W | Red media W | Batería media W | SOC % mín–máx | DC ℃ mín–máx |",
        "|---|---:|---:|---:|---:|---:|---|---|"]
    def mean(values, key):
        item = values.get(key)
        return f'{item["sample_mean"]:.1f}' if item and item["unit"] == "W" else "ND"
    def interval(values, key, unit):
        item = values.get(key)
        return f'{item["min"]:g}–{item["max"]:g}' if item and item["unit"] == unit else "ND"
    for hour, values in sorted(rows.items()):
        counts = sorted({item["samples"] for item in values.values()})
        lines.append("| " + " | ".join([hour, "/".join(map(str, counts)),
            mean(values, "TotalDCInputPower"), mean(values, "TotalConsumptionPower"),
            mean(values, "TotalGridPower"), mean(values, "BatteryPower"),
            interval(values, "SOC", "%"), interval(values, "DC Temperature", "℃")]) + " |")
    return "\n".join(lines) + "\n"


def main():
    root = Path(__file__).resolve().parents[1]
    prior = json.loads((root / "docs/deye-phase4b-results.json").read_text(encoding="utf-8"))
    followup = json.loads((root / "docs/deye-phase4b-followup.json").read_text(encoding="utf-8"))
    summary = summarize(prior, followup)
    (root / "docs/deye-phase4b-quality.json").write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
    (root / "docs/deye-phase4b-hourly.md").write_text(hourly_markdown(followup), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
