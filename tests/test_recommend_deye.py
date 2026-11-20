import sys
import unittest
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from xml.etree import ElementTree
from zipfile import ZipFile

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from deye_history_quality import MADRID, UTC
from deye_workbook import SHEET_NS, collection_workbook
from recommend_deye import (ENERGY_FIELDS, TOU_TIMES, WEATHER_FIELDS, day_type, forecast,
                            history, hour_starts, load_profile, recommendation, run, settings)


NOW = datetime(2026, 11, 19, 19, 15, tzinfo=MADRID)
TARGET = date(2026, 11, 20)


def snapshot(soc=30, targets=(40, 40)):
    return {"captured_at": NOW.isoformat(), "latest_soc": {
        "SOC": {"value": soc, "unit": "%", "timestamp": NOW.isoformat()}},
        "parameters": {"deye_read_tou": {"status": "available", "data": {
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
                self.assertEqual(len(result["hours"]), count)
                self.assertEqual(len({row["hour"] for row in result["hours"]}), count)
                self.assertEqual(result["daylight_hours"], 12)
                self.assertEqual(result["sunshine_hours"], count / 2)
                self.assertAlmostEqual(result["irradiation_kwh_m2"], count * 0.2)
                self.assertLess(get.call_args.kwargs["params"]["start_date"], str(day))
                data["hourly"]["sunshine_duration"][-1] = None
                with self.assertRaises(ValueError):
                    forecast(day, settings(), get=get)

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
        store = Store({"config/recommendation.json": settings(), f"evening/{NOW.date()}.json": snapshot()})
        fetch = Mock(side_effect=requests.Timeout("sensitive"))
        self.assertEqual(run(store, NOW, dry_run=True, fetch=fetch), "DRY_RUN_COMPLETED")
        store.publish.assert_not_called()
        self.assertEqual(run(store, NOW, fetch=fetch), "RECORDED_HOLD")
        report = store.entries[f"recommendations/{TARGET}.json"]
        self.assertEqual(report["forecast"]["confidence"], "unavailable")
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
        store.entries[workbook_path] = original
        self.assertEqual(run(store, NOW, fetch=fetch), "WORKBOOK_UPDATED")
        fetch.assert_called_once()
        self.assertEqual(set(store.publish.call_args.args[0]), {workbook_path})


if __name__ == "__main__":
    unittest.main()
