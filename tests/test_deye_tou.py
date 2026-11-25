import sys
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest import TestCase
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from approve_deye import decide, digest, notify, proposal_path, tou_preflight
from deye_tou import Hour, Period, comparison, model_limits, read_periods, simulate
from deye_workbook import add_recommendation_sheet, collection_workbook
from recommend_deye import MADRID, hour_starts, settings, tou_hours, tou_recommendation
from test_approve_deye import DAY, NOW, REVIEWERS, Store


def configuration():
    return settings({"battery_nominal_kwh": 16, "battery_usable_kwh": 16, "grid_kw": 5.75,
                     "battery_kw": 4, "verified_device_minimum_soc": 30, "tou_search_passes": 2})


def snapshot(now, soc=100):
    return {"captured_at": now.isoformat(), "latest_soc": {"SOC": {
        "timestamp": now.isoformat(), "unit": "%", "value": soc}}, "parameters": {
        "deye_read_tou": {"status": "available", "data": {"touAction": "on",
            "timeUseSettingItems": [dict(Period(hour, 30, hour < 8, 8000).record(),
                                         enableGeneration=False)
                                    for hour in (0, 5, 8, 13, 17, 21)]}},
        "deye_read_battery_settings": {"status": "available", "data": {
            "battShutDownCapacity": 10, "battLowCapacity": 15,
            "maxChargeCurrent": 100, "maxDischargeCurrent": 100}}}}


def weather(day, sunny=False):
    return {"confidence": "medium", "hours": [
        {"hour": local.isoformat(), "pv_kwh": 3 if sunny and 9 <= local.hour < 17 else 0}
        for stamp in hour_starts(day) for local in [datetime.fromtimestamp(stamp, MADRID)]]}


class TouTests(TestCase):
    def setUp(self):
        self.config = configuration()
        self.day = date(2026, 11, 23)
        self.now = datetime(2026, 11, 22, 23, 50, tzinfo=MADRID)
        self.snapshot = snapshot(self.now)
        self.periods = read_periods(self.snapshot)
        self.limits = model_limits(self.snapshot, self.config)

    def compare(self, profile, *, sunny=False, weekend=False, initial=100):
        day = self.day + timedelta(days=5 if weekend else 0)
        rows = tou_hours(day, profile, weather(day, sunny), self.config)
        return comparison(rows, self.periods, initial, self.limits, self.config,
                          night_hours=set(range(8)))["strategies"]

    def test_evening_reservation_and_both_peak_periods(self):
        result = self.compare([0.5] * 8 + [1.8] * 10 + [2] * 4 + [0.4] * 2)
        current, full = result["current"], result["full"]
        self.assertAlmostEqual(current["soc_at_18"], 30)
        self.assertGreater(full["soc_at_18"], current["soc_at_18"])
        self.assertLess(full["peak_import_kwh"], current["peak_import_kwh"])
        self.assertLess(full["objective_eur"], current["objective_eur"])
        flat_full = sum(row["import_kwh"] for row in full["hours"] if row["band"] == "flat")
        flat_current = sum(row["import_kwh"] for row in current["hours"] if row["band"] == "flat")
        self.assertGreater(flat_full, flat_current)
        self.assertEqual({row["hour"][11:13] for row in full["hours"] if row["band"] == "peak"},
                         {"10", "11", "12", "13", "18", "19", "20", "21"})
        self.assertNotEqual(current["periods"], full["periods"])
        self.assertEqual(len(full["periods"]), 6)

    def test_quarter_hour_peak_can_import_despite_hourly_solar_surplus(self):
        stamp = "2026-11-23T10:00:00+01:00"
        flat = Hour(stamp, 10, 2, 3, 0.193, "peak")
        peaked = Hour(stamp, 10, 2, 3, 0.193, "peak", load_shares=(1, 0, 0, 0))
        hourly = simulate([flat], self.periods, 50, self.limits, self.config, detail=True)
        detailed = simulate([peaked], self.periods, 50, self.limits, self.config, detail=True)

        self.assertAlmostEqual(hourly["import_kwh"], 0)
        self.assertAlmostEqual(detailed["import_kwh"], 0.25)
        self.assertEqual(len(detailed["hours"]), 1)
        row = detailed["hours"][0]
        self.assertEqual(row["hour"], stamp)
        self.assertAlmostEqual(row["load_kwh"], 2)
        self.assertAlmostEqual(row["pv_kwh"], 3)
        self.assertAlmostEqual(row["discharge_kwh"], 1)
        self.assertAlmostEqual(row["soc_start"], 50)
        self.assertAlmostEqual(row["soc_end"], detailed["final_soc"])

    def test_night_soc_bypasses_savings_but_not_charge_power(self):
        self.config.update(minimum_saving_eur=10, minimum_night_soc_next_non_valley=35,
                           minimum_night_soc_next_valley=30)
        for day, grid_kw, expected, minimum in (
                (self.day, 5.75, "requires_change", 35),
                (self.day, 0.1, "unreachable", 35),
                (date(2026, 12, 8), 5.75, "met", 30)):
            with self.subTest(day=day, grid_kw=grid_kw):
                profiles = [(day - timedelta(days=delta), [1] * 24, {}) for delta in (7, 14, 21)]
                result = tou_recommendation(day, self.now, weather(day), profiles, snapshot(self.now, 30),
                                            dict(self.config, grid_kw=grid_kw), fetch=weather)
                night = result["night_soc"]
                self.assertEqual(night["status"], expected)
                self.assertEqual(night["minimum_soc"], minimum)
                self.assertEqual(night["hour"], 8)
                self.assertEqual(result["decision"], "review" if expected == "requires_change" else "hold")
                if expected == "requires_change":
                    self.assertGreaterEqual(night["proposed_worst_soc"] + 1e-9, minimum)
                    self.assertLess(result["strategies"]["full"]["avoided_net_cost_eur"],
                                    self.config["minimum_saving_eur"])

    def test_weekend_uniform_and_sunny(self):
        for sunny in (True, False):
            with self.subTest(sunny=sunny):
                result = self.compare([0.3] * 24, sunny=sunny, weekend=True, initial=30)
                self.assertEqual(result["full"]["grid_charge_kwh"], 0)
                self.assertEqual(result["full"]["peak_import_kwh"], 0)
                self.assertEqual(result["night"]["periods"][0]["soc"], 30)

    def test_night_charge_can_reach_100(self):
        result = self.compare([0.2] * 8 + [3] * 16, initial=30)
        periods = result["night"]["periods"]
        self.assertEqual([period["time"] for period in periods],
                         ["0000", "0800", "1000", "1400", "1800", "2200"])
        self.assertEqual(periods[0]["soc"], 100)
        self.assertEqual(periods[1]["soc"], 30)

    def test_five_tariff_bands_keep_every_boundary_and_split_only_one(self):
        self.config["weekday_periods"] = [
            [0, 8, "valley"], [8, 10, "flat"], [10, 14, "peak"],
            [14, 18, "flat"], [18, 24, "peak"],
        ]
        result = self.compare([0.2] * 8 + [3] * 16, initial=30)
        required = {"0000", "0800", "1000", "1400", "1800"}
        for name in ("night", "full"):
            starts = {period["time"] for period in result[name]["periods"]}
            self.assertTrue(required <= starts)
            self.assertEqual(len(starts - required), 1)

    def test_tariff_alignment_can_require_change_without_savings(self):
        self.config.update(minimum_saving_eur=100, minimum_avoided_grid_charge_kwh=100)
        profiles = [(self.day - timedelta(days=i), [0.5] * 24, {}) for i in (7, 14, 21)]
        report = tou_recommendation(self.day, self.now, weather(self.day), profiles,
                                    self.snapshot, self.config, fetch=weather)
        self.assertEqual(report["decision"], "review")
        self.assertEqual(report["decision_reason"], "TOU_TARIFF_ALIGNMENT_REQUIRED")
        self.assertFalse(report["tariff_alignment"]["current_aligned"])
        self.assertTrue(report["tariff_alignment"]["proposed_aligned"])
        proposals = report["strategies"]["full"]["periods"]
        self.assertEqual([p["time"] for p in proposals],
                         ["0000", "0800", "1000", "1400", "1800", "2200"])

    def test_already_aligned_schedule_still_requires_economic_benefit(self):
        self.config.update(minimum_saving_eur=100, minimum_avoided_grid_charge_kwh=100)
        self.periods = tuple(Period(hour, 30, hour < 8, 8000)
                             for hour in (0, 8, 10, 14, 18, 22))
        result = self.compare([0.5] * 24)
        self.assertEqual(result["full"]["periods"], [p.record() for p in self.periods])

    def test_weekend_can_optimize_without_weekday_boundaries(self):
        self.periods = tuple(Period(hour, 100, hour < 8, 8000)
                             for hour in (0, 5, 8, 13, 17, 21))
        result = self.compare([0.3] * 24, sunny=True, weekend=True, initial=30)
        self.assertGreater(result["full"]["avoided_grid_charge_kwh"], 0)
        self.assertNotEqual([p["time"] for p in result["full"]["periods"]],
                            ["0000", "0800", "1000", "1400", "1800", "2200"])

    def test_reduces_unnecessary_grid_charge(self):
        self.periods = (Period(0, 100, True, 8000), Period(5, 100, True, 8000), *self.periods[2:])
        result = self.compare([0.3] * 24, sunny=True, initial=30)
        self.assertGreater(result["full"]["avoided_grid_charge_kwh"], 2)

    def test_energy_balance_and_power_limits(self):
        hours = [Hour("a", 9, 0.5, 4, .12, "flat"), Hour("b", 18, 5, 0, .19, "peak")]
        result = simulate(hours, self.periods, 45, (30, 1, 2), self.config, detail=True)
        self.assertEqual(result["solar_charge_kwh"], 1)
        self.assertEqual(result["discharge_kwh"], 2)
        for row in result["hours"]:
            delta = (row["soc_end"] - row["soc_start"]) * 16 / 100
            curtailed = row["pv_kwh"] - row["direct_pv_kwh"] - row["solar_charge_kwh"] - row["export_kwh"]
            self.assertAlmostEqual(row["pv_kwh"] + row["import_kwh"],
                                   row["load_kwh"] + row["export_kwh"] + delta + row["loss_kwh"] + curtailed)

    def test_daylight_saving(self):
        for day, count in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25)):
            rows = tou_hours(day, [1] * 24, weather(day), self.config)
            self.assertEqual(len(rows), count)
            self.assertEqual(len({row.stamp for row in rows}), count)
            self.assertEqual(sum(row.hour == 2 for row in rows), count - 23)

    def test_forecast_history_soc_and_battery_fail_safely(self):
        profiles = [(self.day - timedelta(days=i), [1] * 24, {}) for i in (7, 14, 21)]
        def run(data, forecast, selected):
            return tou_recommendation(self.day, self.now, forecast, selected, data, self.config,
                                      fetch=weather)
        self.assertEqual(run(self.snapshot, weather(self.day), [])["decision"], "abstain")
        self.assertEqual(run(self.snapshot, {}, profiles)["decision"], "abstain")
        self.assertEqual(run(snapshot(self.now - timedelta(hours=3)), weather(self.day), profiles)["decision"], "abstain")
        self.config["tou_horizon_days"] = 2
        result = run(snapshot(self.now, 80), weather(self.day), profiles)
        self.assertEqual(result["horizon"]["hours"], 48)
        self.assertLess(result["estimated_midnight_soc"], result["current_soc"])
        fallback = tou_recommendation(self.day, self.now, weather(self.day), profiles, self.snapshot,
                                      self.config, fetch=lambda *_: {"confidence": "low"})
        self.assertEqual(fallback["horizon"]["hours"], 24)
        self.assertIn("SECOND_DAY_FORECAST_UNAVAILABLE", fallback["issues"])
        battery = self.snapshot["parameters"]["deye_read_battery_settings"]["data"]
        battery["maxDischargeCurrent"] = 20
        self.assertAlmostEqual(model_limits(self.snapshot, self.config)[2], .88)
        battery["maxChargeCurrent"] = 0
        self.assertEqual(run(self.snapshot, weather(self.day), profiles)["decision"], "abstain")

    def test_preflight_configuration_changes_and_no_permission(self):
        payload = {"snapshot_sha256": digest(self.snapshot["parameters"]),
                   "current_periods": [period.record() for period in self.periods]}
        self.assertEqual(tou_preflight(payload, self.snapshot), "EXECUTION_DISABLED")
        rows = self.snapshot["parameters"]["deye_read_tou"]["data"]["timeUseSettingItems"]
        rows[2]["soc"] = 35
        self.assertEqual(tou_preflight(payload, self.snapshot), "CONFIGURATION_CHANGED")
        rows.pop()
        self.assertEqual(tou_preflight(payload, self.snapshot), "CONFIGURATION_UNAVAILABLE")

    def test_mobile_approval_binds_all_six_periods_and_cannot_apply(self):
        day = date.fromisoformat(DAY)
        data = snapshot(NOW, 75)
        load = [0.5] * 8 + [1.8] * 10 + [2] * 4 + [0.4] * 2
        profiles = [(day - timedelta(days=i), load, {}) for i in range(7, 10)]
        # An unnecessarily high night target creates a review on a sunny Saturday.
        data["parameters"]["deye_read_tou"]["data"]["timeUseSettingItems"][0]["soc"] = 100
        data["parameters"]["deye_read_tou"]["data"]["timeUseSettingItems"][1]["soc"] = 100
        result = tou_recommendation(day, NOW, weather(day, True), profiles, data, self.config, fetch=weather)
        self.assertEqual(result["decision"], "review")
        report = Store().entries[f"recommendations/{DAY}.json"]
        report["tou_simulation"] = result
        workbook = add_recommendation_sheet(collection_workbook({}, "combined", DAY), report)
        with ZipFile(BytesIO(workbook)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn(b"Compra en punta", archive.read("xl/worksheets/sheet7.xml"))
        for command, mutate, expected in (("approve", False, "SIMULATION_APPROVED"),
                                          ("approve", True, "CONFIGURATION_CHANGED"),
                                          ("reject", False, "REJECTED")):
            with self.subTest(command=command, mutate=mutate):
                store = Store()
                store.entries[f"recommendations/{DAY}.json"]["tou_simulation"] = result
                store.entries[f"evening/{day - timedelta(days=1)}.json"] = data
                self.assertEqual(notify(store, DAY, "23:00", NOW, REVIEWERS), "NOTIFIED")
                identity, event = store.event(command)
                record = store.entries[proposal_path(DAY, identity)]
                self.assertEqual(len(record["payload"]["proposed_periods"]), 6)
                self.assertIs(record["payload"]["execution_enabled"], False)
                periods = data["parameters"]["deye_read_tou"]["data"]["timeUseSettingItems"]
                if mutate:
                    periods[2]["soc"] += 1
                self.assertEqual(decide(store, event, REVIEWERS, NOW), expected)
                self.assertEqual(decide(store, event, REVIEWERS, NOW), "ALREADY_DECIDED")
                if mutate:
                    periods[2]["soc"] -= 1
