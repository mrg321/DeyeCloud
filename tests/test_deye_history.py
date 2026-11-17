"""Phase 4B offline contracts, civil time, DST, units and missing data."""

import asyncio
from datetime import datetime, timezone
import json

from mcp.shared.memory import create_connected_server_and_client_session
from scripts import deye_read_contracts as contracts
from scripts import deye_mcp_server as server
from scripts import deye_history_quality as quality
from scripts import audit_deye_history as audit
from scripts import audit_deye_history_followup as followup
from scripts import report_deye_history_quality as reporting
from test_deye_local import OfflineTestCase, token_response, CANARY
from test_deye_audit import reading


ARGS = {
    "deye_station_metadata": {"station_id": 123},
    "deye_station_daily_history": {"station_id": 123, "start_date": "2026-11-11", "end_date": "2026-11-17"},
    "deye_station_power_history": {"station_id": 123, "start_timestamp": 1794866400, "end_timestamp": 1794956399},
    "deye_device_daily_history": {"device_sn": "SIM123", "start_date": "2026-11-11", "end_date": "2026-11-17"},
    "deye_device_intraday_history": {"device_sn": "SIM123", "date": "2026-11-17", "measure_points": ["SOC", "DC Temperature"]},
}
PAYLOADS = {
    "deye_station_metadata": {"station": {"id": 123, "name": "Simulated", "regionTimezone": "Europe/Amsterdam", "ownerName": CANARY}},
    "deye_station_daily_history": {"stationDataItems": [{"year": 2026, "month": 11, "day": 17, "generationValue": 0, "purchaseValue": None}], "total": 1},
    "deye_station_power_history": {"stationDataItems": [{"timeStamp": "2026-11-17T12:00:00+01:00", "generationPower": 100, "batterySOC": 30}], "total": 1},
    "deye_device_daily_history": {"deviceSn": "SIM123", "granularity": 2, "dataList": [{"time": "2026-11-17", "itemList": [{"key": "Production", "value": "1.2", "unit": "kWh", "token": CANARY}]}]},
    "deye_device_intraday_history": {"deviceSn": "SIM123", "granularity": 1, "dataList": [{"time": "2026-11-17 12:00:00", "itemList": [{"SOC": "0", "password": CANARY}, {"key": "DC Temperature", "value": "-35.50", "unit": "℃"}]}]},
}


class HistoryTests(OfflineTestCase):
    def test_new_reads_through_real_mcp_and_fixed_request_bodies(self):
        expected = {
            "deye_station_metadata": {"stationId": 123},
            "deye_station_daily_history": {"stationId": 123, "startAt": "2026-11-11", "endAt": "2026-11-17", "granularity": 2},
            "deye_station_power_history": {"stationId": 123, "startTimestamp": 1794866400, "endTimestamp": 1794956399},
            "deye_device_daily_history": {"deviceSn": "SIM123", "startAt": "2026-11-11", "endAt": "2026-11-17", "granularity": 2},
            "deye_device_intraday_history": {"deviceSn": "SIM123", "startAt": "2026-11-17", "endAt": "2026-11-17", "granularity": 1, "measurePoints": ["SOC", "DC Temperature"]},
        }

        async def exercise():
            service = self.make_service()
            async with create_connected_server_and_client_session(server.create_server(service)) as session:
                for name, args in ARGS.items():
                    self.post.side_effect = ([] if service._client._token else [token_response()]) + [reading(PAYLOADS[name])]
                    result = await session.call_tool(name, args)
                    self.assertFalse(result.isError, result.structuredContent)
                    self.assert_no_secret(result)
                    self.assertEqual(self.post.call_args.kwargs["json"], expected[name])
                    self.assertEqual(self.post.call_args.args[0], "https://eu1-developer.deyecloud.com" + contracts.READS[name][0])
        asyncio.run(exercise())

    def test_bad_dates_ranges_and_points_rejected_before_io(self):
        cases = [(name, {**args, "path": CANARY}) for name, args in ARGS.items()]
        for first, last in [("2026-02-30", "2026-03-01"), ("2026-11-17", "2026-11-11"),
                            ("2026-11-10", "2026-11-17"), ("2026-1-01", "2026-01-01")]:
            cases.append(("deye_station_daily_history", {"station_id": 123, "start_date": first, "end_date": last}))
        cases.extend([
            ("deye_device_intraday_history", {**ARGS["deye_device_intraday_history"], "date": "2026-02-29"}),
            ("deye_device_intraday_history", {**ARGS["deye_device_intraday_history"], "measure_points": [CANARY]}),
            ("deye_device_intraday_history", {**ARGS["deye_device_intraday_history"], "measure_points": ["SOC", "SOC"]}),
            ("deye_device_daily_history", {**ARGS["deye_device_daily_history"], "measure_points": ["SOC"]}),
            ("deye_station_power_history", {**ARGS["deye_station_power_history"], "end_timestamp": 1794956401}),
            ("deye_station_power_history", {**ARGS["deye_station_power_history"], "end_timestamp": 1794866400}),
        ])
        service = self.make_service()
        for name, args in cases:
            result = service.call(name, args)
            self.assertTrue(result.isError)
            self.assertEqual(result.structuredContent["error"], "INVALID_ARGUMENTS")
            self.assert_no_secret(result)
        self.credentials.assert_not_called()
        self.post.assert_not_called()

    def test_missing_rows_and_partial_total_not_filled(self):
        for payload, rows, complete in [({"total": 0}, [], True),
                ({"total": 2, "stationDataItems": []}, [], False),
                ({}, None, None)]:
            self.post.side_effect = [token_response(), reading(payload)]
            result = self.make_service().call("deye_station_daily_history", ARGS["deye_station_daily_history"])
            self.assertFalse(result.isError)
            data = result.structuredContent["data"]
            self.assertEqual(data["stationDataItems"], rows)
            self.assertEqual(data["quality"]["complete_by_total"], complete)

    def test_schema_diagnostics_show_types_only(self):
        self.post.side_effect = [token_response(), reading({"lastUpdateTime": 1794916771, "generationPower": CANARY})]
        result = self.make_service().call("deye_station_latest", {"station_id": 123})
        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["schema_issues"], [
            {"field": "generationPower", "expected": "int/float", "actual": "str"},
            {"field": "lastUpdateTime", "expected": "str", "actual": "int"}])
        self.assert_no_secret(result)

    def test_history_map_types_and_granularity_remain_strict(self):
        for payload in [
            {"granularity": 2},
            {"granularity": 1, "dataList": [{"time": "12:00", "itemList": [{"SOC": 30}]}]},
            {"granularity": 1, "dataList": {}},
            {"granularity": 1, "dataList": [None]},
        ]:
            self.post.side_effect = [token_response(), reading({"deviceSn": "SIM123", **payload})]
            result = self.make_service().call("deye_device_intraday_history", ARGS["deye_device_intraday_history"])
            self.assertTrue(result.isError)
            self.assert_no_secret(result)

    def test_civil_days_are_23_24_and_25_hours(self):
        for day, hours in [("2026-03-29", 23), ("2026-10-25", 25), ("2026-11-17", 24)]:
            start, end = quality.day_bounds(day)
            self.assertEqual(end - start, hours * 3600)
        self.assertEqual(quality.day_bounds("2026-03-29")[1], quality.day_bounds("2026-03-30")[0])

    def test_ambiguous_and_nonexistent_times_are_not_guessed(self):
        self.assertEqual(quality.sample_time("2026-03-29 02:30:00", "Europe/Madrid")[1], "NONEXISTENT_LOCAL_TIME")
        self.assertEqual(quality.sample_time("2026-10-25 02:30:00", "Europe/Madrid")[1], "AMBIGUOUS_LOCAL_TIME")
        early, _ = quality.sample_time("2026-10-25T02:30:00+02:00", None)
        late, _ = quality.sample_time("2026-10-25T02:30:00+01:00", None)
        self.assertEqual((late - early).total_seconds(), 3600)
        self.assertEqual(quality.sample_time("12:00", "Europe/Madrid", "2026-11-17")[0].hour, 11)

    def test_power_averages_are_not_kwh_and_missing_is_not_zero(self):
        start, end = quality.day_bounds("2026-11-17")
        def point(offset, value, unit="W", key="TotalDCInputPower"):
            return {"timestamp": datetime.fromtimestamp(start + offset, timezone.utc).isoformat(),
                    "key": key, "value": value, "unit": unit}
        samples = [point(0, 1000), point(300, 0), point(600, None), point(300, 0),
                   point(3600, 10, "kW"), point(86400, 1), point(900, 101, "%", "SOC")]
        result = quality.analyze_samples(samples, start, end)
        hour = result["hours"][0]
        self.assertEqual(hour["sample_mean"], 500)
        self.assertIsNone(hour["complete_hour_energy_kwh"])
        self.assertFalse(result["power_integrated_to_energy"])
        self.assertEqual(result["missing_by_key"]["TotalDCInputPower"], 1)
        for issue in ["DUPLICATE_TIMESTAMP_AND_KEY", "SOC_OUT_OF_RANGE", "OUTSIDE_REQUESTED_INTERVAL", "INCONSISTENT_UNIT:TotalDCInputPower"]:
            self.assertIn(issue, result["issues"])
        self.assertGreater(result["coverage"]["TotalDCInputPower"]["max_gap_seconds"], 600)

    def test_counter_reset_and_repeated_autumn_hours(self):
        start, end = quality.day_bounds("2026-10-25")
        samples = [{"timestamp": stamp, "key": "DailyConsumption", "value": value, "unit": "kWh"}
            for stamp, value in [("2026-10-25T02:00:00+02:00", 3),
                                 ("2026-10-25T02:05:00+02:00", 2),
                                 ("2026-10-25T02:00:00+01:00", 4)]]
        result = quality.analyze_samples(samples, start, end)
        self.assertEqual(len(result["hours"]), 2)
        self.assertIsNone(result["hours"][0]["counter_change_observed_kwh"])
        self.assertIn("COUNTER_DECREASE_WITHIN_HOUR", result["issues"])

    def test_daily_coverage_does_not_confuse_total_with_seven_days(self):
        result = quality.daily_date_quality(["2026-11-16", "2026-11-16", None,
            "2026-02-30", "2026-11-18"], "2026-11-11", "2026-11-17")
        self.assertEqual(result["returned_dates"], ["2026-11-16"])
        self.assertEqual(len(result["missing_dates"]), 6)
        self.assertEqual(result["duplicate_dates"], ["2026-11-16"])
        self.assertEqual(result["issues"]["DATE_OUTSIDE_REQUESTED_INTERVAL"], 1)

    def test_nonfinite_values_and_samples_from_wrong_day(self):
        for value in (None, True, "NaN", "Infinity", "1e9999", "invalid"):
            self.assertIsNone(quality.numeric(value))
        self.assertEqual(quality.numeric("0"), 0)
        data = {"dataList": [{"time": "2026-11-17T12:00:00+01:00",
            "itemList": [{"key": "SOC", "value": "30", "unit": "%"}]}]}
        samples, issues = quality.extract_device_samples(data, {"SOC"}, "Europe/Madrid", "2026-11-16")
        self.assertIsNone(samples[0]["timestamp"])
        self.assertEqual(issues, {"OUTSIDE_REQUESTED_DAY": 1})

    def test_followup_budget_is_cumulative_and_serials_are_masked(self):
        prior = {"operations": [
            {"tool": "deye_list_stations", "result": {"ok": True, "stations": [{"id": 123}]}},
            {"tool": "deye_device_measure_points", "result": {"ok": True, "data": {"measurePoints": ["SOC"]}}},
            {"tool": "deye_device_daily_history", "result": {"ok": True, "data": {"dataList": [{"time": "2026-11-17"}]}}},
        ] + [{"tool": "placeholder"}] * 39, "station_timezone": "Europe/Madrid",
            "requested_window": {"first_date": "2026-11-11", "last_date": "2026-11-17"}}
        class Session:
            def __init__(self):
                self.calls = 0

            async def call_tool(self, name, args):
                self.calls += 1
                if name == "deye_list_station_devices":
                    return server.tool_result({"ok": True, "data": {"has_more": False,
                        "deviceListItems": [{"deviceSn": "SIM123", "deviceType": "INVERTER"}]}})
                return server.tool_result({"ok": False, "error": "INVALID_RESPONSE"}, error=True)
        session = Session()
        result = asyncio.run(followup.collect_followup(session, prior, pace=0))
        self.assertEqual(session.calls, 2)
        self.assertEqual(result["total_phase4b_tool_calls"], 44)
        self.assertNotIn("SIM123", json.dumps(result))

    def test_hourly_report_preserves_unit_mean_and_partial_sample_count(self):
        data = {"device_quality": {"hours": [{"hour_madrid": "2026-11-17T13:00:00+01:00",
            "key": "TotalDCInputPower", "unit": "W", "sample_mean": 311.0,
            "samples": 6, "min": 100, "max": 500}]}}
        text = reporting.hourly_markdown(data)
        self.assertIn("no son energía", text)
        self.assertIn("| 6 | 311.0 |", text)
        self.assertIn("+01:00", text)

    def test_bounded_audit_no_secrets_and_stop_after_429(self):
        class Session:
            def __init__(self, fail=False):
                self.calls = []
                self.fail = fail

            async def call_tool(self, name, args):
                self.calls.append(name)
                if self.fail and name == "deye_station_metadata":
                    return server.tool_result({"ok": False, "error": "RATE_LIMITED"}, error=True)
                payload = {
                    "deye_connection_status": {"ok": True},
                    "deye_list_stations": {"ok": True, "total": 1, "stations": [{"id": 123}]},
                    "deye_list_station_devices": {"ok": True, "data": {"has_more": False,
                        "deviceListItems": [{"deviceSn": "SIM123", "deviceType": "INVERTER"}]}},
                    "deye_device_latest": {"ok": True, "data": {"deviceDataList": []}},
                    "deye_device_measure_points": {"ok": True, "data": {"measurePoints": [key for group in audit.GROUPS for key in group]}},
                }.get(name, {"ok": True, "data": PAYLOADS.get(name, {"stationDataItems": [], "dataList": []})})
                return server.tool_result(payload)
        session = Session()
        result = asyncio.run(audit.collect(session, now=datetime(2026, 11, 17, 12, tzinfo=timezone.utc), pace=0))
        self.assertLessEqual(len(session.calls), audit.MAX_TOOL_CALLS)
        self.assertEqual(result["requested_window"]["first_date"], "2026-11-11")
        self.assertTrue(result["requested_window"]["last_day_partial"])
        self.assertTrue(result["source_offsets_match_madrid_for_window"])
        self.assertNotIn("SIM123", json.dumps(result))
        session = Session(fail=True)
        result = asyncio.run(audit.collect(session, now=datetime(2026, 11, 17, 12, tzinfo=timezone.utc), pace=0))
        self.assertEqual(session.calls[-1], "deye_station_metadata")
