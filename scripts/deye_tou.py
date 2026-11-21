"""Read-only simulation of six recurring Deye TOU periods."""

from bisect import bisect_right
from dataclasses import dataclass, replace
from math import ceil
import re

EPS = 1e-9


@dataclass(frozen=True, order=True)
class Period:
    hour: int
    soc: int
    grid: bool
    power: int

    def record(self):
        return {"time": f"{self.hour:02d}00", "soc": self.soc,
                "enableGridCharge": self.grid, "power": self.power}


def read_periods(snapshot):
    params = snapshot.get("parameters", {})
    tou = params.get("deye_read_tou") or {}
    data = tou.get("data") or {}
    rows = data.get("timeUseSettingItems")
    if (tou.get("status") != "available" or data.get("touAction") != "on"
            or not isinstance(rows, list) or len(rows) != 6):
        raise ValueError("TOU_UNAVAILABLE")
    periods = []
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("time"), str)
                or not re.fullmatch(r"(?:[01][0-9]|2[0-3])00", row["time"])
                or type(row.get("soc")) is not int or not 0 <= row["soc"] <= 100
                or type(row.get("power")) is not int or row["power"] <= 0
                or row.get("enableGeneration") is not False
                or type(row.get("enableGridCharge")) is not bool):
            raise ValueError("UNSUPPORTED_TOU_PERIOD")
        periods.append(Period(int(row["time"][:2]), row["soc"], row["enableGridCharge"], row["power"]))
    starts = [period.hour for period in periods]
    if starts != sorted(set(starts)):
        raise ValueError("UNORDERED_TOU_PERIODS")
    return tuple(periods)


def model_limits(snapshot, config):
    battery = (snapshot.get("parameters", {}).get("deye_read_battery_settings") or {})
    data = battery.get("data") or {}
    shutdown, low, charge, discharge = (data.get(key) for key in (
        "battShutDownCapacity", "battLowCapacity", "maxChargeCurrent", "maxDischargeCurrent"))
    if (battery.get("status") != "available"
            or any(type(value) is not int for value in (shutdown, low, charge, discharge))
            or not 0 <= shutdown <= low < 100 or min(charge, discharge) <= 0):
        raise ValueError("BATTERY_LIMITS_UNAVAILABLE")
    floor = max(config["reserve_soc"], config["backup_reserve_soc"], low + 1,
                config["reserve_soc"] + 100 * config["backup_autonomy_kwh"] /
                (config["battery_usable_kwh"] * config["discharge_efficiency"]))
    if floor > 100:
        raise ValueError("BACKUP_RESERVE_INFEASIBLE")
    voltage = config["tou_minimum_battery_voltage"]
    return floor, min(config["battery_kw"], charge * voltage / 1000), min(
        config["battery_kw"], discharge * voltage / 1000)


@dataclass(frozen=True)
class Hour:
    stamp: str
    hour: int
    load: float
    pv: float
    price: float
    band: str
    duration: float = 1


def simulate(hours, periods, initial, limits, cfg, *, detail=False):
    capacity = cfg["battery_usable_kwh"]
    eta_c, eta_d = cfg["charge_efficiency"], cfg["discharge_efficiency"]
    floor, charge_kw, discharge_kw = limits
    energy = capacity * initial / 100
    starts = [period.hour for period in periods]
    result = dict.fromkeys(("net_cost_eur", "import_cost_eur", "export_credit_eur", "load_kwh", "pv_kwh",
                            "direct_pv_kwh", "solar_charge_kwh", "grid_charge_kwh", "discharge_kwh",
                            "import_kwh", "export_kwh", "curtailed_kwh", "loss_kwh", "peak_import_kwh"), 0.)
    curve = []
    minimum = initial
    minimum_end = 100.
    for row in hours:
        period = periods[bisect_right(starts, row.hour) - 1]
        start_soc = 100 * energy / capacity
        target = capacity * max(floor, period.soc) / 100
        direct = min(row.load, row.pv)
        demand, surplus = row.load - direct, row.pv - direct
        headroom = charge_kw * row.duration
        solar = min(surplus, headroom, max(0., capacity - energy) / eta_c)
        energy += solar * eta_c
        discharge = min(demand, min(discharge_kw, period.power / 1000) * row.duration,
                        max(0., energy - target) * eta_d)
        energy -= discharge / eta_d
        grid = 0.
        if period.grid and discharge <= EPS:
            grid = min(max(0., target - energy) / eta_c, headroom - solar,
                       max(0., cfg["grid_kw"] * row.duration - demand))
        energy += grid * eta_c
        imported = demand - discharge + grid
        exported = min(surplus - solar, cfg["tou_export_limit_kw"] * row.duration)
        credit = exported * (cfg["export_price"] if cfg["tou_credit_export"] else 0.)
        cost = imported * row.price
        loss = (solar + grid) * (1 - eta_c) + discharge * (1 / eta_d - 1)
        end_soc = 100 * energy / capacity
        minimum = min(minimum, end_soc)
        minimum_end = min(minimum_end, end_soc)
        for key, value in zip(result, (cost - credit, cost, credit, row.load, row.pv, direct, solar, grid,
                                       discharge, imported, exported, surplus - solar - exported, loss,
                                       imported if row.band == "peak" else 0.)):
            result[key] += value
        if detail:
            curve.append({"hour": row.stamp, "load_kwh": row.load, "pv_kwh": row.pv,
                          "soc_start": start_soc, "soc_end": end_soc, "import_kwh": imported,
                          "export_kwh": exported, "direct_pv_kwh": direct, "solar_charge_kwh": solar,
                          "grid_charge_kwh": grid, "discharge_kwh": discharge, "loss_kwh": loss,
                          "band": row.band, "price": row.price, "cost_eur": cost - credit})
    # Value usable energy at configured replacement cost. Keep invoice estimate separate.
    terminal_price = cfg["tou_terminal_eur_per_kwh"]
    if terminal_price is None:
        terminal_price = cfg["prices"]["valley"]
    terminal = max(0., energy - floor * capacity / 100) * terminal_price
    return {**result, "final_soc": 100 * energy / capacity, "minimum_soc": minimum,
            "minimum_hour_end_soc": minimum_end,
            "terminal_value_eur": terminal, "objective_eur": result["net_cost_eur"] - terminal,
            **({"hours": curve} if detail else {})}


def optimize(hours, current, initial, limits, cfg, *, night_hours):
    """Coordinate search from current, tariff and night-only configurations."""
    scenarios = [hours] + [
        [replace(row, load=row.load * (1 + load * cfg["load_uncertainty"]),
                 pv=row.pv * (1 + sun * cfg["pv_uncertainty"])) for row in hours]
        for load in (-1, 1) for sun in (-1, 1)]
    minimum = ceil(max(limits[0], cfg["verified_device_minimum_soc"] or 0))
    night_minimum = ceil(max(minimum, cfg["minimum_night_soc"]))
    targets = sorted({minimum, night_minimum, 100, *cfg["targets"]})
    cache = {}

    def evaluate(plan):
        if plan not in cache:
            cache[plan] = [simulate(rows, plan, initial, limits, cfg) for rows in scenarios]
        return cache[plan]

    def score(plan):
        outcome = evaluate(plan)[0]
        return (round(outcome["objective_eur"], 8), outcome["grid_charge_kwh"],
                outcome["loss_kwh"], outcome["peak_import_kwh"], -outcome["solar_charge_kwh"],
                sum(a != b for a, b in zip(current, plan)), plan)

    reference = evaluate(current)
    peak_hours = {row.hour for row in hours if row.band == "peak"}

    def peak_charge_windows(plan):
        return {hour for hour in peak_hours if plan[bisect_right(
            [period.hour for period in plan], hour) - 1].grid}

    def eligible(plan):
        if plan == current:
            return True
        if any(period.soc < minimum for period in plan):
            return False
        if peak_charge_windows(plan) - peak_charge_windows(current):
            return False
        outcomes = evaluate(plan)
        # A measured SOC can already be below reserve. Only consider plans
        # that recover it within the first modeled hour without discharging.
        if min(row["minimum_hour_end_soc"] for row in outcomes) + EPS < limits[0]:
            return False
        savings = [a["objective_eur"] - b["objective_eur"] for a, b in zip(reference, outcomes)]
        avoided = [a["grid_charge_kwh"] - b["grid_charge_kwh"] for a, b in zip(reference, outcomes)]
        invoice_savings = [a["net_cost_eur"] - b["net_cost_eur"] for a, b in zip(reference, outcomes)]
        return (min(savings) >= cfg["minimum_saving_eur"] and min(savings) > EPS
                or min(avoided) + EPS >= cfg["minimum_avoided_grid_charge_kwh"]
                and min(savings) >= -EPS and min(invoice_savings) >= -EPS)

    night_plans = {current}
    starts = [p.hour for p in current]
    indices = {bisect_right(starts, hour) - 1 for hour in night_hours}
    active_hours = {hour for hour in range(24) if bisect_right(starts, hour) - 1 in indices}
    if active_hours == night_hours:
        for soc in targets:
            if soc >= night_minimum:
                night_plans.add(tuple(replace(p, soc=soc, grid=True) if i in indices else p
                                       for i, p in enumerate(current)))
    night = min(filter(eligible, night_plans), key=score)
    tariff = [start for start, _, _ in cfg["weekday_periods"]]
    seeds = {current, night}
    if len(tariff) == 6:
        seeds.add(tuple(replace(p, hour=hour) for p, hour in zip(current, tariff)))
    candidates = set(seeds) | night_plans

    def neighbors(plan, i):
        p = plan[i]
        previous = plan[i - 1].hour + 1 if i else 0
        following = plan[i + 1].hour if i < 5 else 24
        for hour in sorted({p.hour, p.hour - 1, p.hour + 1, *tariff}):
            if previous <= hour < following:
                for soc in targets:
                    if soc < minimum:
                        continue
                    for grid in (False, True):
                        changed = plan[:i] + (replace(p, hour=hour, soc=soc, grid=grid),) + plan[i + 1:]
                        if grid:
                            end = changed[(i + 1) % 6].hour
                            covered = {(hour + offset) % 24 for offset in range((end - hour) % 24)}
                            if covered & peak_hours:
                                continue
                        yield changed

    for seed in sorted(seeds):
        plan = seed
        for sweep in range(cfg["tou_search_passes"]):
            previous = plan
            indices = range(6) if sweep % 2 == 0 else range(5, -1, -1)
            for i in indices:
                options = set(neighbors(plan, i)) | {plan}
                candidates.update(options)
                plan = min(options, key=score)
            if plan == previous:
                break
    full = min(filter(eligible, candidates), key=score)
    return {"current": current, "night": night, "full": full}, len(cache)


def comparison(hours, current, initial, limits, config, *, night_hours):
    plans, evaluated = optimize(hours, current, initial, limits, config, night_hours=night_hours)
    results = {}
    for name, periods in plans.items():
        results[name] = {"periods": [period.record() for period in periods],
                         **simulate(hours, periods, initial, limits, config, detail=True)}
    baseline = results["current"]
    for result in results.values():
        for key in ("net_cost_eur", "objective_eur", "peak_import_kwh", "grid_charge_kwh", "loss_kwh"):
            result["avoided_" + key] = baseline[key] - result[key]
        result["saving_vs_night_eur"] = results["night"]["net_cost_eur"] - result["net_cost_eur"]
        result["adjusted_saving_vs_night_eur"] = results["night"]["objective_eur"] - result["objective_eur"]
        result["soc_at_18"] = next((row["soc_start"] for row in result["hours"]
                                    if row["hour"][11:16] == "18:00"), None)
        result["soc_at_22"] = next((row["soc_start"] for row in result["hours"]
                                    if row["hour"][11:16] == "22:00"), None)
    return {"strategies": results, "evaluated_plans": evaluated}
