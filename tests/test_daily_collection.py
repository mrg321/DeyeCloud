"""Standalone offline tests: no MCP imports, Windows dependency or real network."""

import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

import requests

if __name__ == "__main__" and not __package__:
    # Running this file directly puts tests/ on sys.path instead of the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import collect_deye_daily as collector
from scripts import deye_client as client_module
from scripts.deye_history_quality import day_bounds
from scripts.deye_private_store import GitHubPrivateStore, PrivateStoreError, canonical, blob_sha, valid_data_path

FAKE = "FAKE_SECRET_FOR_OFFLINE_TESTS"
CONFIG = {"station_id": 123, "device_sn": "SIM123", "installation_start": "2026-01-01", "timezone": "Europe/Madrid"}
ENV = {"DEYE_APP_ID": "fake-app", "DEYE_APP_SECRET": FAKE,
       "DEYE_EMAIL": "fake@example.invalid", "DEYE_PASSWORD": "fake-password"}
NOW = datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc)


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    return result


class MemoryStore:
    def __init__(self):
        self.files = {"config/installation.json": deepcopy(CONFIG)}
        self.commits = 0
        self.fail = False

    def read(self, path):
        return deepcopy(self.files.get(path))

    def publish(self, changes):
        if self.fail:
            raise PrivateStoreError(FAKE)
        result = {**self.files, **deepcopy(changes)}
        if result == self.files:
            return False
        self.files = result
        self.commits += 1
        return True


class FakeDeye:
    def __init__(self, originals=None):
        self.originals = originals
        self.calls = []
        self.production = "1.20"
        self.missing = False
        self.fail_at = None

    def ensure_authenticated(self):
        pass

    def query_read(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if name == self.fail_at:
            raise client_module.DeyeClientError(FAKE, code="RATE_LIMITED")
        if name == "deye_list_station_devices":
            result = {"has_more": False, "deviceListItems": [{"deviceSn": "SIM123", "deviceType": "INVERTER"}]}
        elif name == "deye_station_metadata":
            result = {"station": {"regionTimezone": "Europe/Amsterdam"}}
        elif name == "deye_device_measure_points":
            result = {"measurePoints": [point for group in collector.POINT_GROUPS for point in group]}
        elif name in {"deye_station_daily_history", "deye_device_daily_history"}:
            first = datetime.fromisoformat(args["start_date"]).date()
            last = datetime.fromisoformat(args["end_date"]).date()
            from datetime import timedelta
            days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
            if name == "deye_station_daily_history":
                result = {"quality": {"complete_by_total": True}, "stationDataItems": [
                    {"year": day.year, "month": day.month, "day": day.day,
                     **{key: 1.2 for key in collector.ENERGY.values()}} for day in days]}
            else:
                result = {"dataList": [{"time": str(day), "itemList": [
                    {"key": key, "unit": "kWh", "value": None if self.missing else self.production}
                    for key in collector.ENERGY]} for day in days]}
        elif name == "deye_device_intraday_history":
            start, _ = day_bounds(args["date"])
            result = {"dataList": [] if self.missing else [{"time": str(start + 3600), "itemList": [
                {"key": key, "value": "30" if key in {"SOC", "BMSSOC"} else "100",
                 "unit": "%" if key in {"SOC", "BMSSOC"} else "℃" if "Temperature" in key else "W"}
                for key in args["measure_points"]]}]}
        elif name in {"deye_read_battery_settings", "deye_read_tou"}:
            raise client_module.DeyeClientError(FAKE, code="UNSUPPORTED_CONFIG_POINT")
        else:
            result = {"systemWorkMode": "SIMULATED"}
        if self.originals is not None:
            self.originals.append({"path": name, "payload": collector.clean_original({**result, "accessToken": FAKE, "msg": FAKE}, [FAKE])})
        return result


class Offline(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("requests.sessions.Session.send", side_effect=AssertionError("REAL_IO_BLOCKED")))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("REAL_IO_BLOCKED")))
        self.stack.enter_context(patch("scripts.setup_credentials.win32cred.CredRead", side_effect=AssertionError("REAL_IO_BLOCKED")))
        self.stack.enter_context(patch("scripts.setup_credentials.win32cred.CredWrite", side_effect=AssertionError("REAL_IO_BLOCKED")))


class CollectorTests(Offline):
    def test_combined_collects_madrid_today_and_three_closed_days_in_one_commit(self):
        now = datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)
        store, client = MemoryStore(), FakeDeye()
        self.assertTrue(collector.collect(client, store, now=now, mode="combined"))
        today = "2026-10-26"
        closed_dates = ["2026-10-23", "2026-10-24", "2026-10-25"]
        self.assertEqual(store.files["state/collection.json"]["queried_dates"], closed_dates)
        self.assertTrue(store.files[f"evening/{today}.json"]["partial"])
        self.assertEqual({path for path in store.files if path.startswith("daily/")},
                         {f"daily/{day}.json" for day in closed_dates})
        self.assertEqual(store.commits, 1)
        settings_calls = [name for name, _ in client.calls if name == "deye_read_tou"]
        self.assertEqual(len(settings_calls), 1)
        with ZipFile(io.BytesIO(store.files[f"reports/combined/{today}.xlsx"])) as workbook:
            self.assertIn(b"Daily energy", workbook.read("xl/workbook.xml"))
            self.assertIn(today.encode(), workbook.read("xl/worksheets/sheet1.xml"))
        store.files["recommendations/2026-10-27.json"] = {
            "date": "2026-10-27", "forecast": {"source": "Open-Meteo"},
            "recommendation": {"decision": "hold", "confidence": "low"}}
        self.assertTrue(collector.collect(client, store, now=now, mode="combined"))
        with ZipFile(io.BytesIO(store.files[f"reports/combined/{today}.xlsx"])) as workbook:
            self.assertIn(b"Recommendation", workbook.read("xl/workbook.xml"))
            self.assertIn(b"2026-10-27", workbook.read("xl/worksheets/sheet7.xml"))

        store, client = MemoryStore(), FakeDeye()
        store.files["config/installation.json"]["installation_start"] = today
        self.assertTrue(collector.collect(client, store, now=now, mode="combined"))
        self.assertNotIn("state/collection.json", store.files)
        self.assertFalse(any(name == "deye_device_daily_history" for name, _ in client.calls))

    def test_evening_snapshot_uses_madrid_today_and_keeps_closed_history_separate(self):
        now = datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)
        today = "2026-10-26"
        start, _ = day_bounds(today)

        class EveningDeye(FakeDeye):
            def query_read(self, name, args):
                result = super().query_read(name, args)
                if name == "deye_device_latest":
                    return {"deviceDataList": [{"deviceSn": "SIM123", "collectionTime": start + 900,
                        "dataList": [{"key": "SOC", "value": "62", "unit": "%"}]}]}
                if name == "deye_device_intraday_history":
                    row = result["dataList"][0]
                    result["dataList"] = [{**row, "time": str(start + 600)}, row]
                return result

        store, client = MemoryStore(), EveningDeye()
        self.assertTrue(collector.collect(client, store, now=now, mode="evening"))
        path = f"evening/{today}.json"
        self.assertTrue(valid_data_path(path))
        workbook_path = f"reports/evening/{today}.xlsx"
        self.assertEqual(set(store.files), {"config/installation.json", path, workbook_path})
        with ZipFile(io.BytesIO(store.files[workbook_path])) as workbook:
            self.assertIsNone(workbook.testzip())
            for name in workbook.namelist():
                ElementTree.fromstring(workbook.read(name))
            self.assertIn(b"Telemetry", workbook.read("xl/workbook.xml"))
            self.assertIn(b"<v>62.0</v>", workbook.read("xl/worksheets/sheet1.xml"))
        snapshot = store.files[path]
        self.assertTrue(snapshot["partial"])
        self.assertEqual(snapshot["captured_at"], now.isoformat())
        self.assertEqual(snapshot["latest_sample_at"], "2026-10-25T23:10:00+00:00")
        self.assertEqual(len(snapshot["samples"]), 8)
        self.assertEqual(snapshot["latest_soc"]["SOC"]["value"], 62)
        self.assertEqual(snapshot["latest_soc"]["BMSSOC"]["value"], 30)
        self.assertEqual(snapshot["parameters"]["deye_read_tou"]["status"], "UNSUPPORTED_CONFIG_POINT")
        self.assertFalse(any(name in {"deye_station_daily_history", "deye_device_daily_history"}
                             for name, _ in client.calls))
        with self.assertRaises(collector.CollectionError):
            collector.collect(client, store, now=now, mode="evening", end_date=today)

    def test_four_closed_dates_dst_and_installation_boundary(self):
        self.assertEqual(collector.collection_days(CONFIG, NOW), ["2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"])
        self.assertEqual(day_bounds("2026-10-25")[1] - day_bounds("2026-10-25")[0], 90000)
        config = {**CONFIG, "installation_start": "2026-10-25"}
        self.assertEqual(collector.collection_days(config, NOW), ["2026-10-25"])
        self.assertEqual(collector.collection_days({**CONFIG, "installation_start": "2026-10-27"}, NOW), [])
        with self.assertRaises(collector.CollectionError):
            collector.collection_days(CONFIG, NOW, "2026-10-26")
        with self.assertRaises(collector.CollectionError):
            collector.collection_days(CONFIG, NOW, "20261025")
        self.assertEqual(collector.collection_days(CONFIG, NOW, "2026-10-24")[-1], "2026-10-24")

    def test_idempotent_transaction_originals_private_parameters_and_no_today(self):
        originals, store = [], MemoryStore()
        client = FakeDeye(originals)
        self.assertTrue(collector.collect(client, store, now=NOW, originals=originals))
        snapshot = deepcopy(store.files)
        self.assertFalse(collector.collect(client, store, now=NOW, originals=originals))
        self.assertEqual(store.files, snapshot)
        self.assertEqual(store.commits, 1)
        self.assertNotIn(FAKE, json.dumps({path: value for path, value in store.files.items()
                                           if path.endswith(".json")}))
        self.assertTrue(any(path.startswith("originals/") for path in store.files))
        self.assertEqual(store.files["parameters/latest.json"]["deye_read_tou"]["status"], "UNSUPPORTED_CONFIG_POINT")
        self.assertEqual(len([path for path in store.files if path.startswith("daily/")]), 4)
        self.assertFalse(any("2026-10-26" in path for path in store.files))
        self.assertEqual(len(client.calls), 32)  # sixteen named reads per run, no polling.

    def test_late_correction_updates_same_day_and_preserves_missing_values(self):
        store, client = MemoryStore(), FakeDeye()
        collector.collect(client, store, now=NOW)
        client.production = "2.30"
        collector.collect(client, store, now=NOW)
        self.assertEqual(store.files["daily/2026-10-25.json"]["energy"]["device"]["Production"], 2.3)
        count = len(store.files["intraday/2026-10-25.json"]["samples"])
        client.missing = True
        collector.collect(client, store, now=NOW)
        record = store.files["daily/2026-10-25.json"]
        self.assertEqual(record["energy"]["device"]["Production"], 2.3)
        self.assertIn("Production", record["quality"]["retained_device_fields"])
        self.assertEqual(len(store.files["intraday/2026-10-25.json"]["samples"]), count)

    def test_api_failure_and_private_publish_failure_leave_previous_state_unchanged(self):
        store, client = MemoryStore(), FakeDeye()
        collector.collect(client, store, now=NOW)
        before = deepcopy(store.files)
        client.fail_at = "deye_device_intraday_history"
        with self.assertRaises(client_module.DeyeClientError):
            collector.collect(client, store, now=NOW)
        self.assertEqual(store.files, before)
        client.fail_at = None
        store.fail = True
        with self.assertRaises(PrivateStoreError):
            collector.collect(client, store, now=NOW)
        self.assertEqual(store.files, before)

    def test_validation_rejects_wrong_units_duplicates_dates_and_timezone(self):
        for unit, value in [("Wh", "1"), ("kWh", "NaN"), ("kWh", "-1")]:
            with self.assertRaises(collector.CollectionError):
                collector.device_daily({"dataList": [{"time": "2026-10-25", "itemList": [
                    {"key": "Production", "value": value, "unit": unit}]}]}, ["2026-10-25"])
        with self.assertRaises(collector.CollectionError):
            collector.device_daily({"dataList": [{"time": "2026-10-26"}]}, ["2026-10-25"])
        with self.assertRaises(collector.CollectionError):
            collector.validate_timezone("UTC", ["2026-10-25"])
        self.assertIsNone(collector.validate_timezone("Europe/Amsterdam", ["2026-10-25"]))
        start, _ = day_bounds("2026-10-25")
        for stamp, key, value, unit in [(start - 1, "SOC", "30", "%"), (start, "SOC", "101", "%"),
                (start, "BatteryPower", "10", "kWh")]:
            with self.assertRaises(collector.CollectionError):
                collector.intraday_samples({"dataList": [{"time": str(stamp), "itemList": [
                    {"key": key, "value": value, "unit": unit}]}]}, [key], "2026-10-25", "Europe/Madrid")

    def test_no_output_or_network_without_explicit_enable_and_failure_logs_are_fixed(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(collector, "GitHubPrivateStore") as store:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(collector.main([]), 1)
            store.assert_not_called()
            self.assertNotIn(FAKE, out.getvalue())
        with patch.dict("os.environ", {"DEYE_AUTOMATION_ENABLED": "true"}, clear=True), patch.object(
                collector, "GitHubPrivateStore", side_effect=RuntimeError(FAKE)):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(collector.main([]), 1)
            self.assertNotIn(FAKE, out.getvalue())
            self.assertNotIn("Traceback", out.getvalue())


class AuthenticationTests(Offline):
    def test_environment_auth_hashes_password_observes_reads_only_and_reuses_token(self):
        captures, observed = [], []
        def post(url, **kwargs):
            captures.append((url, deepcopy(kwargs["json"])))
            if url.endswith("/account/token"):
                return response({"success": True, "code": "1000000", "accessToken": FAKE, "refreshToken": FAKE})
            return response({"success": True, "code": "1000000", "stationDataItems": [], "total": 0})
        with patch.dict("os.environ", ENV, clear=True), patch("requests.sessions.Session.post", side_effect=post), patch(
                "scripts.deye_client.read_and_validate_credentials") as windows:
            with client_module.DeyeClient(auth_mode="environment", read_observer=lambda p, d: observed.append((p, deepcopy(d)))) as client:
                client.ensure_authenticated()
                client.ensure_authenticated()
                client.query_read("deye_station_daily_history", {"station_id": 123, "start_date": "2026-10-25", "end_date": "2026-10-25"})
                self.assertEqual(client._request_count, 2)
            windows.assert_not_called()
        self.assertEqual(captures[0][1]["password"], hashlib.sha256(ENV["DEYE_PASSWORD"].encode()).hexdigest())
        self.assertEqual(len(observed), 1)
        self.assertNotIn("accessToken", observed[0][1])

    def test_partial_environment_never_falls_back_and_request_budget_blocks_io(self):
        with patch.dict("os.environ", {"DEYE_APP_ID": "fake"}, clear=True), patch(
                "scripts.deye_client.read_and_validate_credentials") as windows:
            with client_module.DeyeClient(auth_mode="environment") as client:
                with self.assertRaises(Exception):
                    client.ensure_authenticated()
            windows.assert_not_called()
        with patch.dict("os.environ", ENV, clear=True):
            with client_module.DeyeClient(auth_mode="environment", max_requests=0) as client:
                with self.assertRaises(client_module.DeyeClientError) as error:
                    client.ensure_authenticated()
                self.assertEqual(error.exception.code, "REQUEST_BUDGET")

    def test_original_payload_excludes_secrets_recursively(self):
        original = {"accessToken": FAKE, "refreshToken": FAKE, "msg": FAKE, "requestId": FAKE,
            "data": [{"value": "prefix " + FAKE, "password": FAKE, "unit": "W", "stationId": 123}]}
        cleaned = collector.clean_original(original, [FAKE])
        self.assertNotIn(FAKE, json.dumps(cleaned))
        self.assertNotIn("accessToken", cleaned)
        self.assertEqual(cleaned["data"][0]["stationId"], 123)


class PrivateStoreTests(Offline):
    def init_responses(self, entries=None, *, private=True):
        return [response({"private": private, "visibility": "private" if private else "public"}),
            response({"object": {"sha": "a" * 40}}), response({"tree": {"sha": "b" * 40}}),
            response({"tree": entries or [], "truncated": False})]

    def test_ref_update_is_atomic_nonforced_and_public_destination_is_rejected(self):
        private = {"private": True, "visibility": "private"}
        workbook = collector.collection_workbook({}, "closed", "2026-10-25")
        responses = self.init_responses() + [response(private), response({"sha": "c" * 40}, 201),
            response({"sha": "f" * 40}, 201),
            response({"sha": "d" * 40}, 201), response({"sha": "e" * 40}, 201), response(private), response({"ref": "refs/heads/main"})]
        with patch("requests.sessions.Session.request", side_effect=responses) as http:
            store = GitHubPrivateStore("example/DeyeCloud-data", FAKE, public_repository="example/DeyeCloud")
            self.addCleanup(store.close)
            self.assertTrue(store.publish({"daily/2026-10-25.json": {"value": 1},
                                           "reports/closed/2026-10-25.xlsx": workbook}))
            blob_calls = [call for call in http.call_args_list if call.args[0] == "POST"
                          and call.args[1].endswith("/git/blobs")]
            self.assertEqual(base64.b64decode(blob_calls[1].kwargs["json"]["content"]), workbook)
            self.assertEqual(http.call_args.args[0], "PATCH")
            self.assertEqual(http.call_args.kwargs["json"], {"sha": "e" * 40, "force": False})
            commit_call = next(call for call in http.call_args_list if call.args[0] == "POST" and call.args[1].endswith("/git/commits"))
            self.assertEqual(commit_call.kwargs["json"]["parents"], ["a" * 40])
        with patch("requests.sessions.Session.request", side_effect=self.init_responses(private=False)) as http:
            with self.assertRaises(PrivateStoreError):
                GitHubPrivateStore("example/DeyeCloud-data", FAKE, public_repository="example/DeyeCloud")
            self.assertEqual(http.call_count, 1)

    def test_identical_blobs_are_noop_and_untrusted_paths_rejected(self):
        value = {"value": 1}
        entries = [{"path": "daily/2026-10-25.json", "sha": blob_sha(canonical(value)), "type": "blob", "mode": "100644"}]
        with patch("requests.sessions.Session.request", side_effect=self.init_responses(entries)) as http:
            store = GitHubPrivateStore("example/DeyeCloud-data", FAKE, public_repository="example/DeyeCloud")
            self.addCleanup(store.close)
            self.assertFalse(store.publish({"daily/2026-10-25.json": value}))
            self.assertEqual(http.call_count, 4)
            for path in ("../public.json", "config/installation.json", "README.md"):
                with self.assertRaises(PrivateStoreError):
                    store.publish({path: value})
            self.assertEqual(http.call_count, 4)

    def test_conflict_has_no_force_or_blind_retry_and_sensitive_errors_are_sanitized(self):
        private = {"private": True, "visibility": "private"}
        responses = self.init_responses() + [response(private), response({"sha": "c" * 40}, 201),
            response({"sha": "d" * 40}, 201), response({"sha": "e" * 40}, 201), response(private), response({"message": FAKE}, 422)]
        with patch("requests.sessions.Session.request", side_effect=responses) as http:
            store = GitHubPrivateStore("example/DeyeCloud-data", FAKE, public_repository="example/DeyeCloud")
            self.addCleanup(store.close)
            with self.assertRaises(PrivateStoreError) as error:
                store.publish({"daily/2026-10-25.json": {"value": 1}})
            self.assertNotIn(FAKE, str(error.exception))
            self.assertEqual(http.call_count, 10)
            self.assertIs(http.call_args.kwargs["json"]["force"], False)

    def test_read_config_is_private_and_request_limit_is_enforced(self):
        content = canonical(CONFIG)
        entries = [{"path": "config/installation.json", "sha": blob_sha(content), "type": "blob", "mode": "100644"}]
        responses = self.init_responses(entries) + [response({"encoding": "base64", "content": base64.b64encode(content).decode()})]
        with patch("requests.sessions.Session.request", side_effect=responses) as http:
            store = GitHubPrivateStore("example/DeyeCloud-data", FAKE, public_repository="example/DeyeCloud")
            self.addCleanup(store.close)
            self.assertEqual(store.read("config/installation.json"), CONFIG)
            self.assertEqual(store.read("config/installation.json"), CONFIG)
            self.assertEqual(http.call_count, 5)
            store.calls = 128
            with self.assertRaises(PrivateStoreError):
                store.publish({"state/collection.json": {}})
            self.assertEqual(http.call_count, 5)

    def test_visibility_change_prevents_writing_any_data(self):
        with patch("requests.sessions.Session.request", side_effect=self.init_responses() + [
                response({"private": False, "visibility": "public"})]) as http:
            store = GitHubPrivateStore("example/DeyeCloud-data", FAKE, public_repository="example/DeyeCloud")
            self.addCleanup(store.close)
            with self.assertRaises(PrivateStoreError):
                store.publish({"daily/2026-10-25.json": {"value": 1}})
            self.assertEqual(http.call_count, 5)
            self.assertTrue(all(call.args[0] == "GET" for call in http.call_args_list))


class WorkflowTests(unittest.TestCase):
    def test_workflow_has_no_public_writes_artifacts_or_untrusted_trigger(self):
        workflow = json.loads((Path(__file__).resolve().parents[1] / ".github/workflows/deye-daily.yml").read_text())
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertFalse(workflow["on"]["workflow_dispatch"]["inputs"]["recommendation_dry_run"]["default"])
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        job = workflow["jobs"]["collect"]
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertEqual(job["timeout-minutes"], 15)
        collector = next(step for step in job["steps"] if "scripts/collect_deye_daily.py" in step.get("run", ""))
        recommendation = job["steps"][-1]
        self.assertEqual(collector["env"]["DEYE_COLLECTION_MODE"], "combined")
        self.assertIn("!inputs.recommendation_dry_run", collector["if"])
        self.assertIn("scripts/recommend_deye.py", recommendation["run"])
        self.assertIn("DEYE_RECOMMENDATION_ENABLED", recommendation["if"])
        self.assertEqual(set(recommendation["env"]),
                         {"DEYE_RECOMMENDATION_ENABLED", "DEYE_RECOMMENDATION_DRY_RUN",
                          "DEYE_DATA_REPOSITORY", "DEYE_DATA_TOKEN"})
        self.assertIn("DEYE_AUTOMATION_ENABLED", job["if"])
        self.assertIn("default_branch", job["if"])
        for step in job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"@[a-f0-9]{40}$")
                self.assertNotIn("upload-artifact", step["uses"])
        self.assertFalse(job["steps"][0]["with"]["persist-credentials"])
        app = next(step for step in job["steps"] if step.get("id") == "app-token")
        self.assertEqual(app["with"]["repositories"], "DeyeCloud-data")
        self.assertIn("inputs.recommendation_dry_run && 'read' || 'write'", app["with"]["permission-contents"])


if __name__ == "__main__":
    unittest.main()
