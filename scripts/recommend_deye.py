"""Forecast and simulate TOU targets from the collector's private read snapshots."""

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path
from math import ceil
from statistics import median
from time import monotonic

import requests

from deye_history_quality import MADRID, day_bounds, numeric, sample_time
from deye_private_store import ApprovalWorkflowMissing, GitHubPrivateStore, canonical
from deye_workbook import add_recommendation_sheet
from deye_tou import Hour, comparison, hourly_steps, model_limits, read_periods, simulate as simulate_tou


UTC = timezone.utc
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_MODEL = "icon_eu"
WEATHER_FALLBACK_MODEL = "ecmwf_ifs"
WEATHER_FIELDS = {"global_tilted_irradiance": "W/m²", "sunshine_duration": "s", "cloud_cover": "%"}
WEATHER_ERRORS = (requests.RequestException, ValueError, KeyError, TypeError, IndexError)
TOU_TIMES = ("0000", "0500", "0800", "1300", "1700", "2100")
ENERGY_FIELDS = ("Consumption", "Production", "ElectricityPurchasing", "GridFeed-in",
                 "ChargingCapacity", "DischargingCapacity")
PREVIOUS_MINIMUM_NIGHT_SOC = 40
NIGHT_SOC_KEYS = ("minimum_night_soc_next_non_valley", "minimum_night_soc_next_valley")


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
        "minimum_avoided_grid_charge_kwh": (0.001, 2400),
        "maximum_grid_reduction_cost_increase_eur": (0, 100),
        "backup_reserve_soc": (0, 100), "backup_autonomy_kwh": (0, 500),
        "tou_minimum_battery_voltage": (1, 100),
        "tou_export_limit_kw": (0, 100),
    }
    if not all(bounded(config[key], *bounds) for key, bounds in limits.items()):
        raise ValueError("INVALID_RECOMMENDATION_SETTING")
    targets, prices = config["targets"], config["prices"]
    if (config["battery_usable_kwh"] > config["battery_nominal_kwh"] or type(targets) is not list or not targets
            or not all(type(x) is int and 0 <= x <= 100 for x in targets)
            or not bounded(config["minimum_night_soc"], 0, 100)
            or any(config[key] is not None and not bounded(config[key], 0, 100) for key in NIGHT_SOC_KEYS)
            or (config["verified_device_minimum_soc"] is not None
                and not bounded(config["verified_device_minimum_soc"], 0, 100))
            or targets != sorted(set(targets)) or config["fallback"] not in ("hold", "abstain")
            or set(prices) != {"valley", "flat", "peak"}
            or not all(bounded(value, 0, 10) for value in prices.values())
            or type(config["history_days"]) is not int or not 1 <= config["history_days"] <= 366
            or type(config["min_profiles"]) is not int or not 1 <= config["min_profiles"] <= 12
            or type(config["tou_simulation_enabled"]) is not bool
            or type(config["tou_credit_export"]) is not bool
            or (config["tou_terminal_eur_per_kwh"] is not None
                and not bounded(config["tou_terminal_eur_per_kwh"], 0, 10))
            or type(config["tou_search_passes"]) is not int or not 1 <= config["tou_search_passes"] <= 5
            or type(config["tou_horizon_days"]) is not int or config["tou_horizon_days"] not in (1, 2)):
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
    models = {}
    for model in (WEATHER_MODEL, WEATHER_FALLBACK_MODEL):
        try:
            models[model] = forecast_with_model(day, config, model, get)
        except WEATHER_ERRORS:
            models[model] = None
    operational = models[WEATHER_MODEL] or models[WEATHER_FALLBACK_MODEL]
    if operational is None:
        raise ValueError("INCOMPLETE_OR_FAILED_FORECAST")
    return {**operational, "models": models}


def forecast_with_model(day, config, model, get):
    hours = hour_starts(day)
    # Radiation and sunshine are preceding-hour aggregates. Request their UTC end times.
    end_times = [stamp + 3600 for stamp in hours]
    utc = [datetime.fromtimestamp(stamp, UTC) for stamp in end_times]
    response = get(WEATHER_URL, timeout=(5, 15), allow_redirects=False, params={
        "latitude": config["latitude"], "longitude": config["longitude"],
        "tilt": config["tilt"], "azimuth": config["azimuth"],
        "models": model,
        "timezone": "GMT", "timeformat": "unixtime",
        "start_date": str(min(datetime.fromtimestamp(hours.start, UTC).date(), day)),
        "end_date": str(max(utc[-1].date(), day)),
        "hourly": ",".join(WEATHER_FIELDS), "daily": "daylight_duration",
    })
    if response.status_code != 200:
        raise ValueError("WEATHER_HTTP_ERROR")
    data = response.json()
    hourly = data["hourly"]
    units = data["hourly_units"]
    if not isinstance(units, dict) or any(units.get(key) != unit for key, unit in WEATHER_FIELDS.items()):
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
    return {"source": "Open-Meteo", "model": model, "daylight_hours": daylight / 3600,
            "sunshine_hours": sum(row["sunshine_seconds"] for row in records) / 3600,
            "irradiation_kwh_m2": sum(row["irradiation_kwh_m2"] for row in records),
            "cloud_cover_mean_pct": sum(row["cloud_cover_pct"] for row in records) / len(records),
            "pv_kwh": sum(row["pv_kwh"] for row in records), "hours": records,
            "pv_model": {key: config[key] for key in ("pv_kwp", "pv_losses", "tilt", "azimuth")},
            "confidence": "medium", "issues": []}


def day_type(day, config):
    holidays = config["holidays"].get(str(day.year))
    if holidays is None:
        return None
    if str(day)[5:] in holidays:
        return "holiday"
    return "weekend" if day.weekday() >= 5 else "weekday"


class LoadProfile(list):
    """Hourly totals with optional quarter-hour energy for each hour."""

    def __init__(self, hourly, quarters):
        super().__init__(hourly)
        self.quarters = quarters

    def shares(self, hour):
        return tuple(value / self[hour] for value in self.quarters[hour]) if self[hour] else ()


def load_profile(samples, day, consumption, *, power_key="TotalConsumptionPower"):
    """Integrate valid short power spans, then normalize to a measured daily total."""
    hours = hour_starts(day)
    if len(hours) != 24:  # Use ordinary historical days to learn each wall-clock hour.
        return None
    start, end = day_bounds(day)
    points = {}
    for row in samples:
        if row.get("key") != power_key:
            continue
        stamp, _ = sample_time(row.get("timestamp"), None)
        value, unit = numeric(row.get("value")), row.get("unit")
        if (stamp is None or not start <= stamp.timestamp() < end or stamp.timestamp() in points
                or unit not in ("W", "kW") or value is None or value < 0):
            return None
        points[stamp.timestamp()] = value / 1000 if unit == "W" else value
    energy, duration = [0.0] * 96, [0.0] * 96
    ordered = sorted(points.items())
    for (left, a), (right, b) in zip(ordered, ordered[1:]):
        if right - left > 600:
            continue
        slope = (b - a) / (right - left)
        origin = left
        while left < right:
            quarter = int((left - start) // 900)
            stop = min(right, start + (quarter + 1) * 900)
            mean_power = a + slope * ((left - origin) + (stop - origin)) / 2
            energy[quarter] += mean_power * (stop - left) / 3600
            duration[quarter] += stop - left
            left = stop
    hourly_duration = [sum(duration[i:i + 4]) for i in range(0, 96, 4)]
    if min(hourly_duration) < 2700:
        return None
    estimated = [sum(energy[i:i + 4]) * 3600 / covered
                 for i, covered in zip(range(0, 96, 4), hourly_duration)]
    total = sum(estimated)
    if total <= 0 or not 0.65 <= total / consumption <= 1.35:
        return None
    hourly = [value * consumption / total for value in estimated]
    quarters = []
    for i, value in zip(range(0, 96, 4), hourly):
        covered = duration[i:i + 4]
        parts = [e / seconds for e, seconds in zip(energy[i:i + 4], covered)] if min(covered) >= 600 else []
        subtotal = sum(parts)
        quarters.append(tuple(value * part / subtotal for part in parts) if subtotal else ())
    return LoadProfile(hourly, quarters)


LOAD_WEIGHT_HALF_LIFE_DAYS = 7


def recent_consumption_median(selected):
    newest = max(day for day, _, _ in selected)
    weighted = sorted((sum(profile), 2 ** (-(newest - day).days / LOAD_WEIGHT_HALF_LIFE_DAYS))
                      for day, profile, _ in selected)
    halfway = sum(weight for _, weight in weighted) / 2
    cumulative = 0
    for index, (total, weight) in enumerate(weighted):
        cumulative += weight
        if cumulative > halfway:
            return total
        if cumulative == halfway:
            return (total + weighted[index + 1][0]) / 2
    return weighted[-1][0]


def consumption_profiles(selected):
    """Scale the central shape by recent daily demand; retain complete stress days."""
    profiles = [row[1] for row in selected]
    hourly = [median(profile[h] for profile in profiles) for h in range(24)]
    daily_total = recent_consumption_median(selected)
    shape_total = sum(hourly)
    if shape_total == 0 and daily_total > 0:
        hourly = min(profiles, key=lambda profile: abs(sum(profile) - daily_total))
        shape_total = sum(hourly)
    scale = daily_total / shape_total if shape_total else 1
    scaled = [value * scale for value in hourly]
    high = max(profiles, key=sum)
    stress = [high] if sum(high) >= daily_total and any(
        abs(a - b) > 1e-9 for a, b in zip(high, scaled)) else []
    detailed = [profile for profile in profiles if any(getattr(profile, "quarters", ()))]
    quarters = [()] * 24
    if detailed:
        typical = min(detailed, key=lambda profile: sum(abs(a - b) for a, b in zip(profile, hourly)))
        peak = max(detailed, key=lambda profile: (
            max((value for parts in profile.quarters[5:12] for value in parts), default=0),
            sum(profile[5:12]),
        ))
        if peak is not high or not stress:
            stress.append(peak)
        quarters = [tuple(value * target / original for value in parts) if original else ()
                    for parts, target, original in zip(typical.quarters, scaled, typical)]
    return LoadProfile(scaled, quarters), stress


def load_for_hours(profile, hours):
    load = [profile[hour] for hour in hours]
    if len(hours) == 24:
        return load
    total = sum(profile)
    observed = sum(load)
    if not observed:
        return [total / len(hours)] * len(hours)
    return [value * total / observed for value in load]


def history(store, target, config, *, latest_complete=None):
    season = (target.month % 12) // 3
    kind = day_type(target, config)
    days = [target - timedelta(days=offset) for offset in range(2, config["history_days"] + 2)]
    days = [day for day in days if day_type(day, config) is not None
            and (latest_complete is None or day <= latest_complete)
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


def measured_soc(snapshot, now, config):
    soc = soc_time = None
    captured, _ = sample_time(snapshot.get("captured_at"), None)
    if captured is None or not timedelta(0) <= now - captured <= timedelta(minutes=config["max_soc_age_minutes"]):
        return soc, soc_time
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
    return soc, soc_time


def evening_state(snapshot, now, config):
    soc, soc_time = measured_soc(snapshot, now, config)
    captured, _ = sample_time(snapshot.get("captured_at"), None)
    if captured is None or not timedelta(0) <= now - captured <= timedelta(minutes=config["max_soc_age_minutes"]):
        return soc, None, soc_time
    targets = None
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


def tariff_band(hour, kind, config):
    return "valley" if kind != "weekday" else next(
        name for start, end, name in config["weekday_periods"] if start <= hour < end)


def import_price(hour, kind, config):
    return config["prices"][tariff_band(hour, kind, config)]


def night_soc_limits(snapshot, config, configured_minimum):
    """Use live protections as additional bounds, with explicit TOU/BMS confirmation for lower targets."""
    battery = snapshot.get("parameters", {}).get("deye_read_battery_settings") or {}
    data = battery.get("data") or {}
    shutdown, low = data.get("battShutDownCapacity"), data.get("battLowCapacity")
    available = battery.get("status") == "available"
    valid = (available and type(shutdown) is int and type(low) is int and 0 <= shutdown <= low < 100)
    verified = config["verified_device_minimum_soc"]
    minimum = max(config["reserve_soc"], configured_minimum, verified or 0)
    if valid:
        minimum = max(minimum, low + 1)
    if not valid or verified is None:
        minimum = max(minimum, PREVIOUS_MINIMUM_NIGHT_SOC)
    conflict = available and (not valid or low >= config["reserve_soc"])
    return minimum, valid and not conflict and verified is not None, conflict


def simulate(hours, load, pv, initial, targets, kind, config, *, profile=None, night_end=8):
    capacity = config["battery_usable_kwh"]
    battery = initial * capacity / 100
    minimum_battery_after_first_slot = capacity
    charge_eff, discharge_eff = config["charge_efficiency"], config["discharge_efficiency"]
    power = config["battery_kw"]
    cost = imports = exports = grid_charge = discharge = daytime_discharge = direct_pv = pv_charge = 0.0
    night_end_soc = initial
    steps = ((hour, demand * share, production * duration, duration, last)
             for hour, demand, production in zip(hours, load, pv)
             for share, duration, last in hourly_steps(profile.shares(hour) if profile is not None else ()))
    for hour, demand, production, duration, last in steps:
        step_power = power * duration
        direct_pv += min(demand, production)
        deficit = max(0, demand - production)
        surplus = max(0, production - demand)
        solar_input = min(surplus, step_power, (capacity - battery) / charge_eff)
        battery += solar_input * charge_eff
        pv_charge += solar_input
        floor = config["reserve_soc"]
        grid_input = 0.0
        if hour < 8:
            floor = targets[0 if hour < 5 else 1]
            grid_input = min(max(0, (floor * capacity / 100 - battery) / charge_eff),
                             step_power - solar_input, max(0, config["grid_kw"] * duration - deficit))
            battery += grid_input * charge_eff
        delivered = min(deficit, step_power, max(0, battery - floor * capacity / 100) * discharge_eff)
        battery -= delivered / discharge_eff
        # Keep numerical drift from producing negative charge headroom.
        battery = min(capacity, battery)
        if last and hour < night_end:
            night_end_soc = battery / capacity * 100
        # The first slot ends at 05:00. Check its achieved charge and every subsequent hour.
        if hour > 4 or (hour == 4 and last):
            minimum_battery_after_first_slot = min(minimum_battery_after_first_slot, battery)
        bought, sold = deficit - delivered + grid_input, surplus - solar_input
        imports += bought
        exports += sold
        grid_charge += grid_input
        discharge += delivered
        if hour >= 8:
            daytime_discharge += delivered
        cost += bought * import_price(hour, kind, config) - sold * config["export_price"]
    return {"cost_eur": cost, "grid_import_kwh": imports, "grid_export_kwh": exports,
            "grid_to_battery_kwh": grid_charge, "pv_to_battery_kwh": pv_charge,
            "battery_start_kwh": initial * capacity / 100, "battery_end_kwh": battery,
            "minimum_soc_from_05": minimum_battery_after_first_slot / capacity * 100,
            "night_end_soc": night_end_soc,
            "battery_charge_loss_kwh": (grid_charge + pv_charge) * (1 - charge_eff),
            "battery_discharge_loss_kwh": discharge * (1 / discharge_eff - 1),
            "direct_pv_self_consumption_kwh": direct_pv, "battery_output_kwh": discharge,
            "battery_output_08_24_kwh": daytime_discharge, "end_soc": battery / capacity * 100}


def recommendation(target, now, weather, selected, snapshot, config):
    soc, baseline, soc_time = evening_state(snapshot, now, config)
    night_end, next_band, configured_minimum = night_soc_policy(target, config)
    night_minimum, lower_targets_verified, invalid_protection = night_soc_limits(
        snapshot, config, configured_minimum)
    backup_required = bool(config["backup_reserve_soc"] or config["backup_autonomy_kwh"])
    backup_floor = max(config["backup_reserve_soc"], config["reserve_soc"] +
                       100 * config["backup_autonomy_kwh"] /
                       (config["battery_usable_kwh"] * config["discharge_efficiency"]))
    kind = day_type(target, config)
    # The legacy 40% rule limits new targets when protections are unverified.
    # It does not by itself require changing an existing plan.
    required_soc = max(configured_minimum, backup_floor, config["verified_device_minimum_soc"] or 0)
    if lower_targets_verified:
        required_soc = max(required_soc, night_minimum)
    stamps = hour_starts(target)
    hours = [datetime.fromtimestamp(stamp, MADRID).hour for stamp in stamps]
    profile, stress_profiles = consumption_profiles(selected) if selected else (None, ())
    load = load_for_hours(profile, hours) if profile else None
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
    if invalid_protection:
        reasons.append("INCONSISTENT_BATTERY_PROTECTION")
    report = {"day_type": kind, "current_soc": soc, "soc_measured_at": soc_time,
              "current_targets": baseline, "recommended_soc": None, "optimal_soc": None,
              "optimal_targets": None, "optimal_cost_eur": None, "gross_saving_eur": None,
              "optimal_minimum_scenario_saving_eur": None, "minimum_saving_eur": config["minimum_saving_eur"],
              "decision_reason": reasons[0] if reasons else None,
              "reserve_soc": config["reserve_soc"] if baseline is not None else None,
              "minimum_allowed_soc": max(night_minimum, backup_floor) if baseline is not None and not invalid_protection else None,
              "lower_targets_verified": baseline is not None and lower_targets_verified,
              "optimization_policy": "COST_THEN_AVOID_GRID_CHARGING",
              "avoided_grid_charge_kwh": None, "avoided_conversion_loss_kwh": None,
              "decision": "hold" if baseline and config["fallback"] == "hold" else "abstain",
              "confidence": "low", "issues": reasons, "historic_days": [str(row[0]) for row in selected],
              "historical_medians_kwh": historical, "battery_nominal_kwh": config["battery_nominal_kwh"],
              "battery_usable_kwh": config["battery_usable_kwh"],
              "battery_available_above_reserve_kwh": config["battery_usable_kwh"] * (1 - config["reserve_soc"] / 100),
              "expected_load_kwh": sum(load) if load else None,
              "expected_load_08_24_kwh": sum(value for hour, value in zip(hours, load) if hour >= 8) if load else None,
              "forecast_pv_kwh": weather.get("pv_kwh"), "expected_cost_eur": None,
              "estimated_saving_eur": None, "expected_battery_output_08_24_kwh": None}
    if any(reason != "INSUFFICIENT_HOUSEHOLD_PROFILES" for reason in reasons) or not selected:
        if report["decision"] == "hold" and baseline[0] == baseline[1]:
            report["recommended_soc"] = baseline[0]
        report["explanation"] = "Datos insuficientes o inconsistentes. Se descarta proponer un cambio de SOC."
        return report
    pv = [row["pv_kwh"] for row in weather["hours"]]
    # Estimate discharge until midnight from historical demand, power and reserve.
    local = datetime.fromisoformat(soc_time).astimezone(MADRID)
    available = max(0, soc - config["reserve_soc"]) / 100 * config["battery_usable_kwh"]
    remaining_fraction = (3600 - local.minute * 60 - local.second) / 3600
    remaining = min(profile[local.hour], config["battery_kw"]) * remaining_fraction
    remaining += sum(min(value, config["battery_kw"]) for value in profile[local.hour + 1:])
    projected_discharge = min(remaining, available * config["discharge_efficiency"])
    initial = soc - projected_discharge / config["discharge_efficiency"] / config["battery_usable_kwh"] * 100
    scenarios = [(profile, 1, 1)] + [(profile, 1 + load_sign * config["load_uncertainty"],
                                     1 + pv_sign * config["pv_uncertainty"])
                            for load_sign in (-1, 1) for pv_sign in (-1, 1)]
    scenarios.extend((stress, 1, 1 - config["pv_uncertainty"]) for stress in stress_profiles)
    outcomes = {}
    candidates = [(x, x) for x in sorted({*config["targets"], ceil(max(night_minimum, required_soc))})
                  if x >= config["reserve_soc"]]
    for setting in dict.fromkeys([tuple(baseline)] + candidates):
        outcomes[setting] = [simulate(hours, [value * lf for value in load_for_hours(shape, hours)], [x * pf for x in pv],
                                      initial, setting, kind, config, profile=shape, night_end=night_end)
                             for shape, lf, pf in scenarios]
    reference = outcomes[tuple(baseline)]
    current_meets_night = min(row["night_end_soc"] for row in reference) + 1e-9 >= required_soc
    comparable_season = (target.month % 12) // 3
    same_type_season = sum(day_type(row[0], config) == kind and (row[0].month % 12) // 3 == comparable_season
                           for row in selected)
    confidence = ("medium" if same_type_season >= config["min_profiles"]
                  and weather.get("confidence") != "low" else "low")
    prices = {import_price(hour, kind, config) for hour in hours}
    flat_price = next(iter(prices)) if len(prices) == 1 else None
    minimum_grid_reduction = config["minimum_avoided_grid_charge_kwh"]
    max_extra_cost = config["maximum_grid_reduction_cost_increase_eur"] if confidence != "low" else 0
    threshold = config["minimum_saving_eur"]
    options = []
    for setting, outcomes_by_scenario in outcomes.items():
        savings = [base["cost_eur"] - option["cost_eur"]
                   for base, option in zip(reference, outcomes_by_scenario)]
        avoided = [base["grid_to_battery_kwh"] - option["grid_to_battery_kwh"]
                   for base, option in zip(reference, outcomes_by_scenario)]
        central = outcomes_by_scenario[0]
        losses = ("battery_charge_loss_kwh", "battery_discharge_loss_kwh")
        avoided_losses = sum(reference[0][key] - central[key] for key in losses)
        minimum_soc = min(row["minimum_soc_from_05"] for row in outcomes_by_scenario)
        backup_ok = not backup_required or min(min(setting), minimum_soc) + 1e-9 >= backup_floor
        meets_night = min(row["night_end_soc"] for row in outcomes_by_scenario) + 1e-9 >= required_soc
        increases = any(candidate > current for candidate, current in zip(setting, baseline))
        reduces = setting != tuple(baseline) and not increases
        policy_reason = None
        if setting != tuple(baseline) and not reasons:
            if not backup_ok:
                policy_reason = "BACKUP_RESERVE_NOT_MET"
            elif min(setting) < night_minimum:
                policy_reason = ("LOW_SOC_LIMITS_UNVERIFIED" if not lower_targets_verified
                                 and min(setting) < PREVIOUS_MINIMUM_NIGHT_SOC else "MINIMUM_SOC_NOT_MET")
            elif not meets_night:
                policy_reason = "NIGHT_SOC_UNREACHABLE"
            elif not current_meets_night:
                policy_reason = "NIGHT_SOC_MINIMUM_REQUIRES_CHANGE"
            elif increases and flat_price is not None:
                policy_reason = "FLAT_TARIFF_NO_GRID_ARBITRAGE"
            elif reduces and min(avoided) + 1e-9 < minimum_grid_reduction:
                policy_reason = "INSUFFICIENT_GRID_CHARGING_REDUCTION"
            elif min(savings) > threshold:
                policy_reason = "ROBUST_SAVING_MEETS_THRESHOLD"
            elif reduces and min(savings) + 1e-9 >= -max_extra_cost:
                policy_reason = "AVOID_UNNECESSARY_GRID_CHARGING"
        options.append({"targets": list(setting), **central,
                        "saving_eur": savings[0], "cumulative_saving_eur": savings[0],
                        "avoided_grid_charge_kwh": avoided[0], "minimum_avoided_grid_charge_kwh": min(avoided),
                        "avoided_conversion_loss_kwh": avoided_losses, "backup_reserve_met": backup_ok,
                        "policy_reason": policy_reason,
                        "minimum_scenario_saving_eur": min(savings)})
    optimal = min(options, key=lambda row: row["cost_eur"])
    economic = [row for row in options if row["policy_reason"] == "ROBUST_SAVING_MEETS_THRESHOLD"]
    reductions = [row for row in options if row["policy_reason"] == "AVOID_UNNECESSARY_GRID_CHARGING"]
    mandatory = [row for row in options if row["policy_reason"] == "NIGHT_SOC_MINIMUM_REQUIRES_CHANGE"]
    viable = mandatory or economic or reductions
    best = min(viable, key=lambda row: (row["cost_eur"], -row["avoided_grid_charge_kwh"])) if viable else options[0]
    targets = best["targets"]
    gross = optimal["saving_eur"]
    worst = optimal["minimum_scenario_saving_eur"]
    if reasons:
        decision_reason = reasons[0]
        justification = "Hay perfiles para estimar costes, pero no se alcanza el mínimo para proponer un cambio."
    elif mandatory:
        decision_reason = "NIGHT_SOC_MINIMUM_REQUIRES_CHANGE"
        justification = "El plan vigente no alcanza el mínimo nocturno. Se propone la alternativa viable de menor coste."
    elif not current_meets_night:
        decision_reason = "NIGHT_SOC_UNREACHABLE"
        justification = "Ninguna alternativa evaluada alcanza el mínimo nocturno en todos los escenarios."
    elif viable:
        decision_reason = best["policy_reason"]
        if economic:
            direction = ("El aumento aprovecha la diferencia entre tarifas."
                         if any(target > current for target, current in zip(targets, baseline))
                         else "La reducción se justifica por su ahorro económico.")
            justification = (f"{direction} La opción propuesta ahorra al menos "
                             f"{best['minimum_scenario_saving_eur']:.6f} EUR en los escenarios simulados.")
        else:
            justification = (f"La reducción evita al menos {best['minimum_avoided_grid_charge_kwh']:.3f} kWh "
                             f"de carga desde la red. Su ahorro en el peor escenario es "
                             f"{best['minimum_scenario_saving_eur']:.6f} EUR.")
    elif optimal["policy_reason"] in {"BACKUP_RESERVE_NOT_MET", "FLAT_TARIFF_NO_GRID_ARBITRAGE",
                                     "INSUFFICIENT_GRID_CHARGING_REDUCTION", "LOW_SOC_LIMITS_UNVERIFIED",
                                     "MINIMUM_SOC_NOT_MET"}:
        decision_reason = optimal["policy_reason"]
        justification = {
            "BACKUP_RESERVE_NOT_MET": "El óptimo no garantiza la reserva de respaldo configurada.",
            "FLAT_TARIFF_NO_GRID_ARBITRAGE": "La tarifa uniforme no justifica subir un objetivo nocturno.",
            "INSUFFICIENT_GRID_CHARGING_REDUCTION": "El óptimo no reduce suficientemente la carga desde la red.",
            "LOW_SOC_LIMITS_UNVERIFIED": "El óptimo queda bloqueado porque falta verificar el mínimo TOU/BMS.",
            "MINIMUM_SOC_NOT_MET": "El óptimo queda por debajo del SOC mínimo permitido.",
        }[decision_reason]
    elif gross <= 0:
        decision_reason = "NO_ESTIMATED_SAVING"
        justification = "Ninguna alternativa reduce el coste estimado frente al TOU actual."
    elif gross <= threshold:
        decision_reason = "BELOW_MINIMUM_SAVING"
        justification = f"El ahorro bruto de {gross:.6f} EUR no supera el mínimo de {threshold:.6f} EUR."
    else:
        decision_reason = "INSUFFICIENT_ROBUST_SAVING"
        justification = f"El ahorro del óptimo baja a {worst:.6f} EUR en el peor escenario. Ninguna opción supera el criterio de ahorro robusto."
    decision = report["decision"] if reasons else "recommend" if viable else "hold"
    report.update({"decision": decision, "decision_reason": decision_reason,
                   "confidence": confidence,
                   "same_type_and_season_profiles": same_type_season,
                   "recommended_soc": targets[0] if decision != "abstain" and targets[0] == targets[1] else None,
                   "optimal_soc": optimal["targets"][0] if optimal["targets"][0] == optimal["targets"][1] else None,
                   "optimal_targets": optimal["targets"], "optimal_cost_eur": optimal["cost_eur"],
                   "gross_saving_eur": gross, "optimal_minimum_scenario_saving_eur": worst,
                   "avoided_grid_charge_kwh": best["avoided_grid_charge_kwh"],
                   "avoided_conversion_loss_kwh": best["avoided_conversion_loss_kwh"],
                   "minimum_avoided_grid_charge_kwh": minimum_grid_reduction,
                   "night_soc": {"hour": night_end, "next_band": next_band, "minimum_soc": required_soc,
                                 "current_soc": reference[0]["night_end_soc"],
                                 "proposed_soc": best["night_end_soc"],
                                 "status": "unverified" if reasons else
                                 "met" if current_meets_night else "requires_change" if mandatory else "unreachable"},
                   "maximum_grid_reduction_cost_increase_eur": max_extra_cost,
                   "backup_minimum_soc": backup_floor if backup_required else None,
                   "estimated_midnight_soc": initial, "projected_precharge_battery_output_kwh": projected_discharge,
                   "decision_horizon": {
                       "start": datetime.fromtimestamp(stamps.start, MADRID).isoformat(),
                       "end_exclusive": datetime.fromtimestamp(stamps.stop, MADRID).isoformat(),
                       "hours": len(stamps)},
                   "flat_import_price_eur_per_kwh": flat_price, "options": options,
                   "expected_cost_eur": best["cost_eur"], "estimated_saving_eur": best["saving_eur"],
                   "expected_battery_output_08_24_kwh": best["battery_output_08_24_kwh"],
                   "explanation": (f"Coste óptimo {optimal['cost_eur']:.6f} EUR frente a "
                                   f"{reference[0]['cost_eur']:.6f} EUR con el TOU actual. "
                                   + justification + (" La tarifa plana no ofrece arbitraje horario. "
                                   "La simulación contabiliza las pérdidas de la batería."
                                   if flat_price is not None else ""))})
    if (viable and optimal is not best and optimal["policy_reason"] in
            {"BACKUP_RESERVE_NOT_MET", "LOW_SOC_LIMITS_UNVERIFIED", "MINIMUM_SOC_NOT_MET"}):
        report["explanation"] += " El óptimo económico queda bloqueado por el límite de seguridad o su verificación."
    return report


def tou_hours(day, profile, weather, config):
    kind = day_type(day, config)
    expected = [datetime.fromtimestamp(stamp, MADRID) for stamp in hour_starts(day)]
    records = weather.get("hours") or []
    if kind is None or len(records) != len(expected):
        raise ValueError("INCOMPLETE_TOU_FORECAST")
    result = []
    loads = load_for_hours(profile, [stamp.hour for stamp in expected])
    for stamp, row, load in zip(expected, records, loads):
        pv = numeric(row.get("pv_kwh"))
        if row.get("hour") != stamp.isoformat() or pv is None or pv < 0:
            raise ValueError("INCOMPLETE_TOU_FORECAST")
        band = tariff_band(stamp.hour, kind, config)
        shares = profile.shares(stamp.hour) if isinstance(profile, LoadProfile) else ()
        result.append(Hour(stamp.isoformat(), stamp.hour, load, pv, config["prices"][band],
                           band, load_shares=shares))
    return result


def night_window(config):
    end = next((start for start, _, band in config["weekday_periods"] if band != "valley"), 24)
    return set(range(end))


def night_soc_policy(day, config):
    end = len(night_window(config))
    next_day = day + timedelta(days=1) if end == 24 else day
    kind = day_type(next_day, config)
    band = tariff_band(end % 24, kind, config) if kind is not None else None
    key = NIGHT_SOC_KEYS[1 if band == "valley" else 0]
    minimum = config[key] if band is not None else None
    return end, band, config["minimum_night_soc"] if minimum is None else minimum


def replay_tou_day(day, daily, intraday, reference_snapshot, config):
    """Replay metered load and PV with hindsight under a stated reference TOU."""
    totals = daily["energy"]["device"]
    samples = intraday["samples"]
    load = load_profile(samples, day, totals["Consumption"])
    pv = load_profile(samples, day, totals["Production"], power_key="TotalDCInputPower")
    if load is None or pv is None:
        raise ValueError("INSUFFICIENT_HOURLY_COVERAGE")
    soc_samples = []
    for row in samples:
        if row["key"] != "SOC" or row["unit"] != "%":
            continue
        stamp, _ = sample_time(row["timestamp"], None)
        soc = numeric(row["value"])
        if stamp is not None and soc is not None and 0 <= soc <= 100:
            soc_samples.append((stamp.timestamp(), soc))
    start = hour_starts(day).start
    opening = [row for row in soc_samples if 0 <= row[0] - start <= 600]
    if not opening:
        raise ValueError("MISSING_OPENING_SOC")
    opening_stamp, soc = min(opening)
    weather = {"hours": [{"hour": datetime.fromtimestamp(stamp, MADRID).isoformat(),
                          "pv_kwh": pv[datetime.fromtimestamp(stamp, MADRID).hour]}
                         for stamp in hour_starts(day)]}
    hours = tou_hours(day, load, weather, config)
    result = comparison(hours, read_periods(reference_snapshot), soc,
                        model_limits(reference_snapshot, config), config,
                        night_hours=night_window(config))
    ordered = sorted(soc_samples)
    first_at_reserve = next((right for (_, previous), (right, value) in zip(ordered, ordered[1:])
                             if previous > config["reserve_soc"] >= value), None)
    result["observed"] = {"totals_kwh": totals, "opening_soc": soc,
                          "opening_at": datetime.fromtimestamp(opening_stamp, MADRID).isoformat(),
                          "first_at_reserve": datetime.fromtimestamp(first_at_reserve, MADRID).isoformat()
                          if first_at_reserve is not None else None}
    return result


def tou_recommendation(target, now, weather, selected, snapshot, config, *, fetch, history_for=None):
    report = {"decision": "abstain", "confidence": "low", "mode": "six_period_simulation",
              "execution_enabled": False}

    try:
        soc, measured_at = measured_soc(snapshot, now, config)
        if measured_at is None:
            raise ValueError("INVALID_OR_STALE_SOC")
        periods = read_periods(snapshot)
        limits = model_limits(snapshot, config)
        if day_type(target, config) == "weekday" and len(config["weekday_periods"]) > len(periods):
            raise ValueError("TOU_TARIFF_PERIODS_EXCEED_SLOTS")
        if len(selected) < config["min_profiles"]:
            raise ValueError("INSUFFICIENT_HOUSEHOLD_PROFILES")
        profile, stress_profiles = consumption_profiles(selected)
        hours = tou_hours(target, profile, weather, config)
        stress_hours = [tou_hours(target, stress, weather, config) for stress in stress_profiles]
    except (ValueError, TypeError, KeyError) as error:
        return {**report, "decision_reason": str(error)}

    # Project measured SOC under the current TOU until local midnight. Neither
    # measurements nor forecast hours are indexed by ambiguous wall time.
    today = target - timedelta(days=1)
    measured = datetime.fromisoformat(measured_at).timestamp()
    remaining = []
    for stamp in hour_starts(today):
        duration = min(1., (stamp + 3600 - measured) / 3600)
        if duration > 0:
            local = datetime.fromtimestamp(stamp, MADRID)
            remaining.append(Hour(local.isoformat(), local.hour, profile[local.hour] * duration,
                                  0., 0., "projection", duration))
    initial = simulate_tou(remaining, periods, soc, limits, config)["final_soc"]
    issues = []
    if config["tou_horizon_days"] == 2:
        following = target + timedelta(days=1)
        try:
            following_kind = day_type(following, config)
            next_profiles = (history_for(following) if history_for else
                             [row for row in selected if day_type(row[0], config) == following_kind])
            if following_kind is None or len(next_profiles) < config["min_profiles"]:
                raise ValueError("INSUFFICIENT_SECOND_DAY_PROFILES")
            second = fetch(following, config)
            if second.get("confidence") != "medium":
                raise ValueError("LOW_CONFIDENCE")
            next_profile, next_stress = consumption_profiles(next_profiles)
            next_hours = tou_hours(following, next_profile, second, config)
            next_stress_hours = [tou_hours(following, stress, second, config) for stress in next_stress]
            stress_hours = [(first if first is not None else hours) + (second if second is not None else next_hours)
                            for first, second in zip_longest(stress_hours, next_stress_hours)]
            hours += next_hours
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
            issues.append("SECOND_DAY_FORECAST_UNAVAILABLE")

    night_end, next_band, configured_minimum = night_soc_policy(target, config)
    required_soc = max(configured_minimum, limits[0], config["verified_device_minimum_soc"] or 0)
    checkpoint = sum(row.stamp[:10] == str(target) and row.hour < night_end for row in hours)
    outcome = comparison(hours, periods, initial, limits, config, night_hours=night_window(config),
                         stress_hours_profiles=stress_hours, checkpoint=checkpoint, required_soc=required_soc)
    night = outcome["night_soc"]
    night.update(hour=night_end, next_band=next_band)
    reference, proposed = (outcome["strategies"][key] for key in ("current", "full"))
    changes = []
    for index, (before, after) in enumerate(zip(reference["periods"], proposed["periods"]), 1):
        fields = [key for key in ("time", "soc", "enableGridCharge") if before[key] != after[key]]
        if fields:
            reasons = []
            if "time" in fields:
                reasons.append("Reubicar el inicio del periodo")
            if "soc" in fields:
                reasons.append("Aumentar el SOC objetivo para conservar energía" if after["soc"] > before["soc"]
                               else "Bajar el SOC objetivo para liberar energía o reducir carga")
            if "enableGridCharge" in fields:
                reasons.append("Permitir carga de red hasta el objetivo" if after["enableGridCharge"]
                               else "Desactivar la carga de red programada")
            changes.append({"period": index, "fields": fields,
                            "reason": ". ".join(reasons) + "."})
    evening = [row for row in hours if row.stamp[:10] == str(target) and 18 <= row.hour < 22]
    deficit = sum(max(0., row.load - row.pv) for row in evening)
    evening_required = limits[0] + 100 * deficit / (
        config["battery_usable_kwh"] * config["discharge_efficiency"])
    kind = day_type(target, config)
    season = (target.month % 12) // 3
    comparable = sum(day_type(row[0], config) == kind and (row[0].month % 12) // 3 == season
                     for row in selected)
    alignment = outcome["tariff_alignment"]
    decision_reason = {
        "requires_change": "NIGHT_SOC_MINIMUM_REQUIRES_CHANGE",
        "unreachable": "NIGHT_SOC_UNREACHABLE",
    }.get(night["status"], "SIMULATED_TOU_CHANGE" if changes else "NO_ROBUST_TOU_CHANGE")
    if not alignment["current_aligned"]:
        decision_reason = ("TOU_TARIFF_ALIGNMENT_REQUIRED" if alignment["proposed_aligned"]
                           else "TOU_TARIFF_ALIGNMENT_UNREACHABLE")
    return {**report, **outcome, "current_soc": soc, "soc_measured_at": measured_at,
            "estimated_midnight_soc": initial, "minimum_soc": limits[0], "issues": issues,
            "horizon": {"start": hours[0].stamp, "hours": len(hours)},
            "confidence": ("medium" if weather.get("confidence") == "medium" and not issues
                           and comparable >= config["min_profiles"] else "low"),
            "snapshot_sha256": hashlib.sha256(canonical(snapshot["parameters"])).hexdigest(),
            "changes": changes, "decision": "review" if changes else "hold",
            "decision_reason": decision_reason,
            "evening": {"load_kwh": sum(row.load for row in evening), "pv_kwh": sum(row.pv for row in evening),
                        "deficit_kwh": deficit, "required_start_soc": evening_required,
                        "extra_flat_import_cost_eur": sum(
                            (b["import_kwh"] - a["import_kwh"]) * a["price"]
                            for a, b in zip(reference["hours"], proposed["hours"])
                            if a["hour"][:10] == str(target) and a["band"] == "flat"
                            and int(a["hour"][11:13]) < 18),
                        "baseline_import_cost_eur": sum(row["import_kwh"] * row["price"] for row in reference["hours"]
                                                        if row["hour"][:10] == str(target) and 18 <= int(row["hour"][11:13]) < 22),
                        "full_import_cost_eur": sum(row["import_kwh"] * row["price"] for row in proposed["hours"]
                                                    if row["hour"][:10] == str(target) and 18 <= int(row["hour"][11:13]) < 22)}}


def run(store, now, *, dry_run=False, recalculate=False, fetch=forecast):
    today = now.astimezone(MADRID).date()
    target = today + timedelta(days=1)
    path = f"recommendations/{target}.json"
    workbook_path = f"reports/combined/{today}.xlsx"
    if not dry_run and not recalculate and path in store.entries:
        if workbook_path in store.entries:
            workbook = store.read_workbook(workbook_path)
            updated = add_recommendation_sheet(workbook, store.read(path))
            if updated != workbook:
                store.publish({workbook_path: updated})
                return "WORKBOOK_UPDATED"
        return "ALREADY_RECORDED"
    installation = store.read("config/recommendation.json")
    night_settings = store.read("config/night_soc.json")
    if night_settings is not None:
        if not isinstance(night_settings, dict) or night_settings.keys() - set(NIGHT_SOC_KEYS):
            raise ValueError("INVALID_NIGHT_SOC_SETTINGS")
        installation = {**(installation or {}), **night_settings}
    config = settings(installation, require_installation=True)
    snapshot = store.read(f"evening/{today}.json") or {}
    selected = history(store, target, config)
    try:
        weather = dict(fetch(target, config))
    except WEATHER_ERRORS:
        weather = {"source": "Open-Meteo", "model": None, "confidence": "unavailable",
                   "issues": ["INCOMPLETE_OR_FAILED_FORECAST"],
                   "models": dict.fromkeys((WEATHER_MODEL, WEATHER_FALLBACK_MODEL)),
                   "pv_model": {"pv_losses": config["pv_losses"]}}
    weather.update({"queried_at": now.astimezone(MADRID).isoformat(), "forecast_date": str(target)})
    result = {"schema_version": 1, "date": str(target), "timezone": "Europe/Madrid",
              "settings_sha256": hashlib.sha256(canonical(config)).hexdigest(),
              "history_commit": store.head, "forecast": weather,
              "recommendation": recommendation(target, now, weather, selected, snapshot, config)}
    if config["tou_simulation_enabled"]:
        result["tou_simulation"] = tou_recommendation(
            target, now, weather, selected, snapshot, config, fetch=fetch,
            history_for=lambda day: history(store, day, config, latest_complete=today - timedelta(days=1)),
        )
    if dry_run:
        return "DRY_RUN_COMPLETED"
    changes = {path: result}
    if workbook_path in store.entries:
        workbook = store.read_workbook(workbook_path)
        changes[workbook_path] = add_recommendation_sheet(workbook, result)
    store.publish(changes)
    return "RECORDED_" + result["recommendation"]["decision"].upper()


def dispatch_approval(repository, data_token, public_repository, day):
    # The daily workflow has already synchronized the private Actions configuration.
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
    recalculate = os.environ.get("DEYE_RECALCULATE_RECOMMENDATION") == "true"
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
            result = run(store, now, dry_run=dry_run, recalculate=recalculate)
            if not dry_run and os.environ.get("DEYE_APPROVAL_ENABLED") == "true":
                dispatch_approval(repository, data_token, public_repository,
                                  str(now.date() + timedelta(days=1)))
        finally:
            store.close()
    except ApprovalWorkflowMissing:
        print("Recommendation retained. Synchronize the private approval workflow from DeyeCloud and rerun.")
        return 1
    except Exception:
        print(f"Recommendation failed after {monotonic() - start:.1f} s. Private details suppressed.")
        return 1
    status = "RECORDED" if result.startswith("RECORDED_") else result
    print(f"Deye recommendation {status} in {monotonic() - start:.1f} s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
