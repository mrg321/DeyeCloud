import sys
import unittest
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree
from zipfile import ZipFile

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from deye_history_quality import MADRID, UTC
from deye_private_store import valid_data_path
from deye_workbook import SHEET_NS, add_recommendation_sheet, collection_workbook
import recommend_deye
from recommend_deye import (ENERGY_FIELDS, TOU_TIMES, WEATHER_FIELDS, day_type, forecast,
                            history, hour_starts, load_profile, recommendation, run, settings)


NOW = datetime(2026, 11, 19, 19, 15, tzinfo=MADRID)
TARGET = date(2026, 11, 20)


def snapshot(soc=30, targets=(40, 40), *, protection=None):
    return {"captured_at": NOW.isoformat(), "latest_soc": {
        "SOC": {"value": soc, "unit": "%", "timestamp": NOW.isoformat()}},
        "parameters": {**({"deye_read_battery_settings": {"status": "available", "data": protection}}
                         if protection is not None else {}),
                       "deye_read_tou": {"status": "available", "data": {
            "touAction": "on", "timeUseSettingItems": [
                {"time": time, "enableGridCharge": i < 2, "enableGeneration": False, "soc": target}
                for i, (time, target) in enumerate(zip(TOU_TIMES, (*targets, 30, 30, 30, 30)))]}}}}


def samples(day, watts=1000):
    start = hour_starts(day).start
    return [{"key": "TotalConsumptionPower", "value": watts, "unit": "W",
             "timestamp": datetime.fromtimestamp(stamp, UTC).isoformat()}
            for stamp in range(start, start + 86400, 300)]


def profiles(demand):
    return [(TARGET - timedelta(days=i), [demand] * 24, dict.fromkeys(ENERGY_FIELDS, demand * 24))
            for i in (2, 3, 4)]


def solar(power):
    hours = [{"pv_kwh": power if 8 <= h < 16 else 0} for h in range(24)]
    return {"hours": hours, "pv_kwh": power * 8}


class Store:
    head = "test-commit"

    def __init__(self, entries=None):
        self.entries = entries or {}
        self.publish = Mock(side_effect=self.entries.update)

    def read(self, path):
        return self.entries.get(path)

    def read_workbook(self, path):
        return self.entries[path]


class ReadOnlyRecommendationTests(unittest.TestCase):
    def test_night_minimum_follows_the_tariff_at_the_configured_cutoff(self):
        overrides = {"minimum_night_soc": 32, "minimum_night_soc_next_non_valley": 35,
                     "minimum_night_soc_next_valley": 30,
                     "weekday_periods": [[0, 5, "valley"], [5, 7, "valley"], [7, 24, "flat"]]}
        config = settings(overrides)
        policy = recommend_deye.night_soc_policy
        self.assertEqual(policy(date(2026, 11, 23), config), (7, "flat", 35))
        self.assertEqual(policy(date(2026, 11, 21), config), (7, "valley", 30))
        self.assertEqual(policy(date(2026, 12, 8), config), (7, "valley", 30))
        config["minimum_night_soc_next_valley"] = None
        self.assertEqual(policy(date(2026, 12, 8), config), (7, "valley", 32))
        with self.assertRaises(ValueError):
            settings({**overrides, "minimum_night_soc_next_valley": 101})

    def test_simple_recommender_checks_achieved_night_soc_before_savings(self):
        config = settings({"minimum_night_soc_next_non_valley": 35,
                           "verified_device_minimum_soc": 30, "minimum_saving_eur": 10})
        live = snapshot(30, (30, 30), protection={"battShutDownCapacity": 10, "battLowCapacity": 20})
        outcome = recommendation(TARGET, NOW, solar(0), profiles(1), live, config)
        self.assertEqual(outcome["decision_reason"], "NIGHT_SOC_MINIMUM_REQUIRES_CHANGE")
        self.assertGreaterEqual(outcome["recommended_soc"], 35)
        self.assertGreaterEqual(outcome["night_soc"]["proposed_soc"] + 1e-9, 35)
        self.assertLess(outcome["estimated_saving_eur"], config["minimum_saving_eur"])

        outcome = recommendation(TARGET, NOW, solar(0), profiles(1), live, dict(config, grid_kw=0.1))
        self.assertEqual(outcome["decision_reason"], "NIGHT_SOC_UNREACHABLE")
        self.assertEqual(outcome["decision"], "hold")

    def test_november_21_flat_tariff_low_confidence_and_threshold(self):
        target = date(2026, 11, 21)
        # Reconstruct an hourly profile from the reported daily totals. All PV is consumed directly.
        selected = profiles(20.8262637 / 24)
        weather = {"pv_kwh": 2.99668032, "hours": [
            {"pv_kwh": 2.99668032 / 6 if 10 <= hour < 16 else 0} for hour in range(24)]}
        config = settings({"battery_nominal_kwh": 16, "battery_usable_kwh": 14.4, "grid_kw": 5.75,
                           "verified_device_minimum_soc": 30})
        baseline = snapshot(48, targets=(100, 100),
                            protection={"battShutDownCapacity": 10, "battLowCapacity": 15})
        report = recommendation(target, NOW, weather, selected, baseline, config)
        self.assertEqual(report["confidence"], "low")
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["current_targets"], [100, 100])
        self.assertEqual(report["optimal_soc"], 30)
        self.assertEqual(report["recommended_soc"], 30)
        self.assertEqual(report["minimum_allowed_soc"], 30)
        self.assertEqual(report["reserve_soc"], 30)
        self.assertEqual(report["decision"], "recommend")
        self.assertEqual(report["decision_reason"], "ROBUST_SAVING_MEETS_THRESHOLD")
        self.assertEqual(report["optimization_policy"], "COST_THEN_AVOID_GRID_CHARGING")
        self.assertAlmostEqual(report["avoided_grid_charge_kwh"], 10.7234042553)
        self.assertAlmostEqual(report["avoided_conversion_loss_kwh"], 1.2482042553)
        self.assertEqual(report["flat_import_price_eur_per_kwh"], 0.085)
        self.assertAlmostEqual(report["gross_saving_eur"], 0.10609736, places=7)
        self.assertAlmostEqual(report["estimated_saving_eur"], 0.10609736, places=7)
        self.assertAlmostEqual(report["expected_cost_eur"], 1.51551459, places=7)
        self.assertAlmostEqual(report["optimal_cost_eur"], 1.51551459, places=7)
        self.assertAlmostEqual(report["estimated_midnight_soc"], 30)
        self.assertAlmostEqual(report["projected_precharge_battery_output_kwh"], 2.43648)
        for option in report["options"][:3]:
            self.assertAlmostEqual(option["battery_start_kwh"], 4.32)
            self.assertAlmostEqual(option["battery_end_kwh"], 4.32)
            self.assertAlmostEqual(option["grid_to_battery_kwh"] - option["battery_output_kwh"],
                                   option["battery_charge_loss_kwh"] + option["battery_discharge_loss_kwh"])
        self.assertAlmostEqual(report["options"][0]["grid_to_battery_kwh"], 10.7234042553)
        self.assertAlmostEqual(report["options"][0]["battery_output_kwh"], 9.4752)
        self.assertAlmostEqual(report["options"][1]["grid_to_battery_kwh"], 0)
        self.assertAlmostEqual(report["options"][1]["grid_import_kwh"], 17.82958338)
        self.assertAlmostEqual(report["options"][1]["battery_output_kwh"], 0)
        self.assertAlmostEqual(report["options"][2]["grid_to_battery_kwh"], 1.5319148936)
        self.assertAlmostEqual(report["options"][2]["battery_output_kwh"], 1.3536)
        self.assertEqual(report["decision_horizon"], {
            "start": "2026-11-21T00:00:00+01:00", "end_exclusive": "2026-11-22T00:00:00+01:00", "hours": 24})
        self.assertEqual(report["options"][0]["cumulative_saving_eur"], 0)
        self.assertAlmostEqual(report["options"][2]["cumulative_saving_eur"], 0.090941, places=6)
        book = add_recommendation_sheet(collection_workbook({}, "combined", str(NOW.date())),
                                        {"date": str(target), "forecast": weather, "recommendation": report})
        with ZipFile(BytesIO(book)) as archive:
            sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet7.xml"))
            rows = {row[0].findtext(".//{*}t"): row[1].findtext("{*}v")
                    for row in sheet.iterfind(".//{*}row")}
        self.assertAlmostEqual(float(rows["40 / 40 % — Grid-to-battery energy (kWh)"]), 1.5319148936)
        self.assertAlmostEqual(float(rows["40 / 40 % — Cumulative saving vs current TOU (EUR)"]), 0.090941, places=6)
        lower_capacity = recommendation(target, NOW, weather, selected, baseline,
                                        {**config, "battery_usable_kwh": 10.8})
        self.assertEqual(lower_capacity["decision_reason"], "AVOID_UNNECESSARY_GRID_CHARGING")
        self.assertEqual(lower_capacity["recommended_soc"], 30)
        self.assertLess(lower_capacity["gross_saving_eur"], 0.10)
        self.assertGreater(lower_capacity["avoided_grid_charge_kwh"], 2)
        insufficient = recommendation(target, NOW, weather, selected[:1], baseline, config)
        self.assertEqual(insufficient["decision"], "hold")
        self.assertEqual(insufficient["decision_reason"], "INSUFFICIENT_HOUSEHOLD_PROFILES")
        self.assertAlmostEqual(insufficient["gross_saving_eur"], report["gross_saving_eur"])

    def test_backup_floor_and_autonomy_block_lower_targets(self):
        target = date(2026, 11, 21)
        for backup in ({"backup_reserve_soc": 45}, {"backup_autonomy_kwh": 2}):
            with self.subTest(backup=backup):
                config = settings(backup)
                report = recommendation(target, NOW, solar(0), profiles(1), snapshot(48, (100, 100)), config)
                self.assertEqual(report["optimal_soc"], 30)
                self.assertEqual(report["decision"], "hold")
                self.assertEqual(report["decision_reason"], "BACKUP_RESERVE_NOT_MET")
                self.assertFalse(report["options"][1]["backup_reserve_met"])
                self.assertGreater(report["backup_minimum_soc"], 40)

    def test_increase_requires_economic_threshold_and_variable_tariff(self):
        config = settings({"charge_efficiency": 1, "discharge_efficiency": 1,
                           "prices": {"valley": 0.085, "flat": 0.090, "peak": 0.095}})
        weekday = recommendation(TARGET, NOW, solar(0), profiles(1.5), snapshot(), config)
        saturday = recommendation(date(2026, 11, 21), NOW, solar(0), profiles(1.5), snapshot(), config)
        self.assertGreater(weekday["optimal_soc"], 40)
        self.assertGreater(weekday["gross_saving_eur"], 0)
        self.assertLess(weekday["gross_saving_eur"], 0.10)
        self.assertEqual(weekday["decision_reason"], "BELOW_MINIMUM_SAVING")
        self.assertEqual(weekday["recommended_soc"], 40)
        self.assertEqual(saturday["flat_import_price_eur_per_kwh"], 0.085)
        self.assertEqual(saturday["recommended_soc"], 40)

    def test_solar_headroom_and_low_forecast_confidence(self):
        config = settings()
        forecast = {**solar(4), "confidence": "low"}
        target = date(2026, 11, 21)
        comparable = [(target - timedelta(weeks=i), profile, totals)
                      for i, (_, profile, totals) in enumerate(profiles(0.5), 1)]
        baseline_snapshot = snapshot(45, (100, 100))
        report = recommendation(target, NOW, forecast, comparable, baseline_snapshot, config)
        baseline, lower = report["options"][:2]
        self.assertEqual(report["confidence"], "low")
        self.assertEqual(report["same_type_and_season_profiles"], 3)
        self.assertEqual(report["recommended_soc"], 40)
        self.assertGreater(lower["pv_to_battery_kwh"], baseline["pv_to_battery_kwh"])
        self.assertGreater(report["avoided_grid_charge_kwh"], 2)
        strict = recommendation(target, NOW, forecast, comparable, baseline_snapshot,
                                {**config, "minimum_avoided_grid_charge_kwh": 99, "targets": [40, 50, 60, 70, 80]})
        self.assertEqual(strict["decision"], "hold")
        self.assertEqual(strict["decision_reason"], "INSUFFICIENT_GRID_CHARGING_REDUCTION")

    def test_lower_target_requires_verified_limits_and_valid_readback(self):
        target = date(2026, 11, 21)
        valid = {"battShutDownCapacity": 10, "battLowCapacity": 15}
        for limits, protection, reason in [
            ({}, valid, "LOW_SOC_LIMITS_UNVERIFIED"),
            ({"verified_device_minimum_soc": 30}, None, "LOW_SOC_LIMITS_UNVERIFIED"),
            ({"verified_device_minimum_soc": 35}, valid, "MINIMUM_SOC_NOT_MET"),
        ]:
            with self.subTest(limits=limits, protection=protection):
                report = recommendation(target, NOW, solar(0), profiles(1),
                                        snapshot(48, (100, 100), protection=protection), settings(limits))
                self.assertGreater(report["minimum_allowed_soc"], 30)
                self.assertGreater(report["recommended_soc"], 30)
                self.assertEqual(report["options"][1]["policy_reason"], reason)
        for protection in ({"battShutDownCapacity": 20, "battLowCapacity": 15},
                           {**valid, "battLowCapacity": 35}):
            wrong = snapshot(48, (100, 100), protection=protection)
            report = recommendation(target, NOW, solar(0), profiles(1), wrong, settings())
            self.assertEqual(report["decision_reason"], "INCONSISTENT_BATTERY_PROTECTION")
            self.assertEqual(report["decision"], "hold")
            self.assertIsNone(report["minimum_allowed_soc"])

    def test_soc_above_30_does_not_force_discharge_without_load(self):
        config = settings()
        result = recommend_deye.simulate(range(24), [0] * 24, [0] * 24, 48,
                                         (30, 30), "weekend", config)
        self.assertEqual(result["grid_to_battery_kwh"], 0)
        self.assertEqual(result["battery_output_kwh"], 0)
        self.assertAlmostEqual(result["end_soc"], 48)

    def test_enabled_cli_runs_in_normal_mode_at_midday(self):
        midday = NOW.replace(hour=12)
        with patch.dict("os.environ", {"DEYE_RECOMMENDATION_ENABLED": "true"}, clear=True), \
                patch.object(recommend_deye, "datetime") as clock, \
                patch.object(recommend_deye, "GitHubPrivateStore") as store, \
                patch.object(recommend_deye, "run", return_value="RECORDED_HOLD") as calculate, \
                patch("builtins.print"):
            clock.now.return_value = midday
            self.assertEqual(recommend_deye.main([]), 0)
            calculate.assert_called_once_with(store.return_value, midday, dry_run=False, recalculate=False)
            store.return_value.close.assert_called_once()

    def test_battery_capacity_names_and_legacy_config(self):
        legacy = settings()
        legacy["nominal_kwh"] = legacy.pop("battery_nominal_kwh")
        legacy["usable_kwh"] = legacy.pop("battery_usable_kwh")
        config = settings(legacy, require_installation=True)
        report = recommendation(TARGET, NOW, {}, [], snapshot(), config)
        self.assertEqual(report["battery_nominal_kwh"], 12)
        self.assertEqual(report["battery_usable_kwh"], 10.8)
        self.assertAlmostEqual(report["battery_available_above_reserve_kwh"], 7.56)
        with self.assertRaisesRegex(ValueError, "CONFLICTING_BATTERY_CAPACITY_SETTINGS"):
            settings({**legacy, "battery_usable_kwh": 9})

    def test_forecast_dst_uses_interval_end_for_radiation(self):
        for day, count in [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]:
            with self.subTest(day=day):
                hours = hour_starts(day)
                midnight = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
                gti = [0] + [200] * count
                data = {"hourly_units": WEATHER_FIELDS, "hourly": {
                    "time": list(range(hours.start, hours.stop + 1, 3600)),
                    "global_tilted_irradiance": gti,
                    "sunshine_duration": [1800] * len(gti), "cloud_cover": [40] * len(gti)},
                    "daily_units": {"daylight_duration": "s"},
                    "daily": {"time": [midnight], "daylight_duration": [43200]}}
                get = Mock(return_value=Mock(status_code=200, **{"json.return_value": data}))
                result = forecast(day, settings(), get=get)
                self.assertEqual([call.kwargs["params"]["models"] for call in get.call_args_list],
                                 ["icon_eu", "ecmwf_ifs"])
                self.assertEqual(result["model"], "icon_eu")
                self.assertEqual(result["models"]["ecmwf_ifs"]["model"], "ecmwf_ifs")
                self.assertEqual(result["models"]["icon_eu"]["pv_kwh"], result["pv_kwh"])
                self.assertEqual(len(result["hours"]), count)
                self.assertEqual(len({row["hour"] for row in result["hours"]}), count)
                self.assertEqual(result["daylight_hours"], 12)
                self.assertEqual(result["sunshine_hours"], count / 2)
                self.assertAlmostEqual(result["irradiation_kwh_m2"], count * 0.2)
                self.assertLess(get.call_args.kwargs["params"]["start_date"], str(day))
                fallback_data = {**data, "hourly": {**data["hourly"],
                                                   "sunshine_duration": [900] * len(gti)}}
                data["hourly"]["sunshine_duration"][-1] = None
                get.side_effect = [Mock(status_code=200, **{"json.return_value": fallback_data}),
                                   Mock(status_code=200, **{"json.return_value": data})]
                primary_only = forecast(day, settings(), get=get)
                self.assertEqual(primary_only["model"], "icon_eu")
                self.assertIsNone(primary_only["models"]["ecmwf_ifs"])
                get.side_effect = None
                with self.assertRaises(ValueError):
                    forecast(day, settings(), get=get)
                get.reset_mock()
                get.side_effect = [Mock(status_code=200, **{"json.return_value": data}),
                                   Mock(status_code=200, **{"json.return_value": fallback_data})]
                fallback = forecast(day, settings(), get=get)
                self.assertEqual([call.kwargs["params"]["models"] for call in get.call_args_list],
                                 ["icon_eu", "ecmwf_ifs"])
                self.assertEqual(fallback["model"], "ecmwf_ifs")
                self.assertIsNone(fallback["models"]["icon_eu"])
                self.assertEqual(fallback["sunshine_hours"], count / 4)

    def test_household_power_and_daily_consumption_not_grid_import(self):
        day = TARGET - timedelta(days=2)
        points = samples(day)
        self.assertAlmostEqual(sum(load_profile(points, day, 24)), 24)
        self.assertIsNone(load_profile(points[24:], day, 24))
        self.assertIsNone(load_profile(points, day, 3))
        entries = {f"daily/{day}.json": {"date": str(day), "energy_unit": "kWh",
                   "energy": {"device": {"Consumption": 24, "ElectricityPurchasing": 3}}},
                   f"intraday/{day}.json": {"date": str(day), "samples": points}}
        day_used, profile, totals = history(Store(entries), TARGET, settings())[0]
        self.assertEqual(day_used, day)
        self.assertAlmostEqual(sum(profile), 24)
        self.assertEqual(totals["ElectricityPurchasing"], 3)
        del entries[f"daily/{day}.json"]["energy"]["device"]["Consumption"]
        self.assertEqual(history(Store(entries), TARGET, settings()), [])

    def test_historical_shape_retains_a_short_morning_peak(self):
        day = TARGET - timedelta(days=2)
        points = samples(day)
        for point in points[8 * 12 + 4:8 * 12 + 6]:
            point["value"] = 6000
        peaked = load_profile(points, day, 24)
        typical = load_profile(samples(day), day, 24)
        profile, stress = recommend_deye.consumption_profiles(
            [(day, typical, {}), (day, typical, {}), (day, peaked, {})])

        self.assertAlmostEqual(profile[8], typical[8])
        self.assertEqual(len(stress), 1)
        self.assertIs(stress[0], peaked)
        self.assertAlmostEqual(sum(stress[0].quarters[8]), stress[0][8])
        self.assertGreater(stress[0].shares(8)[1], 0.5)

    def test_recent_daily_median_scales_each_quarter_and_keeps_high_day(self):
        day = TARGET - timedelta(days=2)

        def flat(total):
            hourly = total / 24
            return recommend_deye.LoadProfile([hourly] * 24, [
                tuple(hourly * share for share in (0.1, 0.2, 0.3, 0.4))
            ] * 24)

        high = flat(45)
        central, stress = recommend_deye.consumption_profiles([
            (day, flat(30), {}), (day - timedelta(days=7), flat(23.6), {}),
            (day - timedelta(days=14), flat(20), {}), (day - timedelta(days=21), high, {}),
        ])
        self.assertAlmostEqual(sum(central), 30)
        self.assertAlmostEqual(sum(map(sum, central.quarters)), 30)
        for actual, share in zip(central.quarters[8], (0.1, 0.2, 0.3, 0.4)):
            self.assertAlmostEqual(actual, 30 / 24 * share)
        self.assertIs(stress[0], high)
        self.assertAlmostEqual(sum(stress[0]), 45)
        self.assertAlmostEqual(sum(recommend_deye.load_for_hours(central, [0, 1, *range(3, 24)])), 30)

    def test_cloud_high_load_vs_sun_low_load(self):
        config = settings()
        cloudy = recommendation(TARGET, NOW, solar(0), profiles(1.5), snapshot(), config)
        sunny = recommendation(TARGET, NOW, solar(4), profiles(0.25), snapshot(), config)
        self.assertEqual(cloudy["decision"], "recommend")
        self.assertGreater(cloudy["recommended_soc"], sunny["recommended_soc"])
        self.assertGreater(cloudy["estimated_saving_eur"], 0)
        self.assertEqual(sunny["decision"], "hold")
        self.assertEqual(sunny["forecast_pv_kwh"], 32)
        self.assertEqual(sunny["expected_load_kwh"], 6)
        self.assertEqual(day_type(date(2026, 12, 8), config), "holiday")
        self.assertEqual(day_type(date(2026, 4, 3), config), "weekday")

    def test_missing_invalid_and_stale_telemetry_fail_conservatively(self):
        config = settings()
        result = recommendation(TARGET, NOW, solar(0), [], snapshot(110), config)
        self.assertEqual(result["decision"], "hold")
        self.assertIn("INVALID_OR_STALE_SOC", result["issues"])
        self.assertIn("INSUFFICIENT_HOUSEHOLD_PROFILES", result["issues"])
        self.assertIsNone(result["estimated_saving_eur"])
        result = recommendation(TARGET, NOW + timedelta(hours=2), solar(0),
                                profiles(1), snapshot(), config)
        self.assertEqual(result["decision"], "abstain")
        self.assertIn("TOU_UNAVAILABLE_OR_UNEXPECTED", result["issues"])
        result = recommendation(TARGET, NOW, {}, profiles(1), snapshot(targets=(40, 50)), config)
        self.assertEqual(result["decision"], "hold")
        self.assertIsNone(result["recommended_soc"])
        self.assertEqual(result["current_targets"], [40, 50])

    def test_weather_failure_is_private_and_dispatch_is_idempotent(self):
        self.assertTrue(valid_data_path("config/night_soc.json"))
        store = Store({"config/recommendation.json": settings(),
                       "config/night_soc.json": {"minimum_night_soc_next_non_valley": 35,
                                                 "minimum_night_soc_next_valley": 30},
                       f"evening/{NOW.date()}.json": snapshot()})
        fetch = Mock(side_effect=requests.Timeout("sensitive"))
        self.assertEqual(run(store, NOW, dry_run=True, fetch=fetch), "DRY_RUN_COMPLETED")
        store.publish.assert_not_called()
        self.assertEqual(run(store, NOW, fetch=fetch), "RECORDED_HOLD")
        report = store.entries[f"recommendations/{TARGET}.json"]
        self.assertEqual(report["forecast"]["confidence"], "unavailable")
        self.assertEqual(fetch.call_args.args[1]["minimum_night_soc_next_non_valley"], 35)
        self.assertEqual(fetch.call_args.args[1]["minimum_night_soc_next_valley"], 30)
        self.assertNotIn("sensitive", str(report))
        self.assertEqual(run(store, NOW, fetch=fetch), "ALREADY_RECORDED")
        self.assertEqual(fetch.call_count, 2)
        store.publish.assert_called_once()

    def test_combined_workbook_and_recommendation_are_published_together(self):
        workbook_path = f"reports/combined/{NOW.date()}.xlsx"
        report_path = f"recommendations/{TARGET}.json"
        original = collection_workbook({}, "combined", str(NOW.date()))
        store = Store({"config/recommendation.json": settings(),
                       f"evening/{NOW.date()}.json": snapshot(), workbook_path: original})
        fetch = Mock(return_value=solar(4))
        self.assertEqual(run(store, NOW, fetch=fetch), "RECORDED_HOLD")
        self.assertEqual(set(store.publish.call_args.args[0]), {report_path, workbook_path})
        with ZipFile(BytesIO(store.entries[workbook_path])) as workbook, ZipFile(BytesIO(original)) as base:
            self.assertIsNone(workbook.testzip())
            self.assertEqual(workbook.read("xl/worksheets/sheet1.xml"), base.read("xl/worksheets/sheet1.xml"))
            sheets = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
            self.assertEqual(sheets.findall(f".//{{{SHEET_NS}}}sheet")[-1].get("name"), "Recommendation")
            rows = ElementTree.fromstring(workbook.read("xl/worksheets/sheet7.xml"))
            self.assertTrue(any(cell.get("r") == "B18" and cell.findtext(f"{{{SHEET_NS}}}v") == "32"
                                for cell in rows.iter(f"{{{SHEET_NS}}}c")))
            self.assertIn(str(TARGET).encode(), workbook.read("xl/worksheets/sheet7.xml"))
        self.assertEqual(run(store, NOW, fetch=fetch), "ALREADY_RECORDED")
        newer = NOW + timedelta(minutes=1)
        self.assertEqual(run(store, newer, recalculate=True, dry_run=True, fetch=fetch), "DRY_RUN_COMPLETED")
        self.assertEqual(store.entries[report_path]["forecast"]["queried_at"], NOW.isoformat())
        self.assertEqual(run(store, newer, recalculate=True, fetch=fetch), "RECORDED_HOLD")
        self.assertEqual(store.entries[report_path]["forecast"]["queried_at"], newer.isoformat())
        self.assertEqual(set(store.publish.call_args.args[0]), {report_path, workbook_path})
        store.entries[workbook_path] = original
        self.assertEqual(run(store, NOW, fetch=fetch), "WORKBOOK_UPDATED")
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(set(store.publish.call_args.args[0]), {workbook_path})


if __name__ == "__main__":
    unittest.main()
