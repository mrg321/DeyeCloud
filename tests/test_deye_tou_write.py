from copy import deepcopy
from datetime import timedelta
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from test_approve_deye import DAY, NOW, REVIEWERS, Store, approval
from deye_client import DeyeClientError
from deye_write_lock import InverterBusy, InverterLock


class TouWriteTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.close = Mock()
        self.store.publish.side_effect = lambda changes: self.store.entries.update(deepcopy(changes)) or True
        self.serial = "TEST-INVERTER"
        self.store.entries["config/installation.json"] = {
            "installation_start": "2026-01-01", "timezone": "Europe/Madrid",
            "device_sn": self.serial, "station_id": 12,
        }
        current = [
            {"time": start, "soc": 45 + i, "power": 8000, "enableGridCharge": i < 2}
            for i, start in enumerate(("0000", "0200", "0900", "1000", "1600", "2100"))
        ]
        proposed = [{**row, "soc": 65, "enableGridCharge": i % 2 == 0}
                    for i, row in enumerate(current)]
        proposed[-1]["time"] = "2200"
        self.baseline = {"touAction": "on", "timeUseSettingItems": [
            {**row, "enableGeneration": False, "voltage": 49} for row in current
        ]}
        self.battery = {"battShutDownCapacity": 10, "battLowCapacity": 20}
        parameters = {
            "deye_read_tou": {"status": "available", "data": self.baseline},
            "deye_read_battery_settings": {"status": "available", "data": self.battery},
        }
        self.store.entries["evening/2026-11-20.json"] = {"parameters": parameters}
        metrics = dict.fromkeys((
            "net_cost_eur", "grid_charge_kwh", "peak_import_kwh", "soc_at_18", "soc_at_22",
            "final_soc", "load_kwh", "pv_kwh", "avoided_net_cost_eur", "avoided_objective_eur",
            "saving_vs_night_eur", "avoided_peak_import_kwh",
        ), 0)
        metrics.update(hours=[dict.fromkeys(("soc_end", "load_kwh", "import_kwh", "pv_kwh", "price"), 0)])
        report = self.store.entries[f"recommendations/{DAY}.json"]
        report["tou_simulation"] = {
            "decision": "review", "decision_reason": "SIMULATED_TOU_CHANGE", "confidence": "medium",
            "minimum_soc": 30, "snapshot_sha256": approval.digest(parameters),
            "strategies": {"current": {**metrics, "periods": current},
                           "night": {**metrics, "periods": current},
                           "full": {**metrics, "periods": proposed}},
            "current_soc": 55, "estimated_midnight_soc": 55, "horizon": {"hours": 1},
            "evening": {"load_kwh": 0, "pv_kwh": 0, "deficit_kwh": 0, "required_start_soc": 30},
            "changes": [],
        }
        self.body = {"deviceSn": self.serial, "timeUseSettingItems": [
            {**row, "time": f"{row['time'][:2]}:{row['time'][2:]}",
             "enableGeneration": False, "voltage": 49} for row in proposed
        ]}
        self.before, self.after = [
            {"touAction": "on", "timeUseSettingItems": [{**row, "enableSell": False} for row in rows]}
            for rows in (self.baseline["timeUseSettingItems"], self.body["timeUseSettingItems"])
        ]
        self.client = MagicMock()
        self.client.__enter__.return_value = self.client
        self.client.start_dynamic_read.side_effect = [101, 102]
        self.client.read_dynamic_tou_result.side_effect = [self.before, self.after]
        self.client.read_order.return_value = (666, {})
        self.client.write_tou.return_value = {"orderId": 200}
        devices = {
            "deye_list_station_devices": {"deviceListItems": [
                {"deviceSn": self.serial, "deviceType": "INVERTER", "connectStatus": 1},
            ]},
            "deye_device_latest": {"deviceDataList": [
                {"deviceSn": self.serial, "collectionTime": int(NOW.timestamp()),
                 "dataList": [{"key": "SOC", "value": "55", "unit": "%"}]},
            ]},
            "deye_read_battery_settings": self.battery,
        }
        self.client.query_read.side_effect = lambda name, arguments: devices[name]
        self.client_factory = Mock(return_value=self.client)

    def vote(self, *, enabled=True, command="approve"):
        approval.notify(self.store, DAY, "23:00", NOW, REVIEWERS, execution_enabled=enabled)
        identity, event = self.store.event(command)
        decision = approval.decide(self.store, event, REVIEWERS, NOW)
        return identity, event, decision

    def execute(self, event, now=NOW):
        with patch("builtins.print"):
            return approval.execute_event(lambda: self.store, event, REVIEWERS, lambda: now,
                                          client_factory=self.client_factory)

    def audit(self, identity):
        return self.store.read(f"approvals/executions/{DAY}/{identity}.json")

    def test_approval_writes_all_six_periods_and_holds_lock_until_verified(self):
        identity, event, decision = self.vote()
        self.assertEqual(decision, "APPROVED")
        self.assertEqual(self.store.read(approval.proposal_path(DAY, identity))["payload"]["write_body"],
                         self.body)

        def read_result(order):
            self.assertTrue(self.audit(identity)["locked"])
            with self.assertRaisesRegex(InverterBusy, "INVERTER_LOCKED"):
                with InverterLock(lambda: self.store, self.serial, lambda: NOW):
                    self.fail("another writer acquired the inverter")
            return {101: self.before, 102: self.after}[order]

        self.client.read_dynamic_tou_result.side_effect = read_result
        self.assertEqual(self.execute(event), "VERIFIED")
        self.client.write_tou.assert_called_once_with(self.body)
        self.assertEqual([call.args[0] for call in self.client.read_order.call_args_list],
                         [101, 200, 102])
        self.assertFalse(self.audit(identity)["locked"])
        self.assertEqual(self.audit(identity)["verification_order_id"], 102)
        self.assertEqual(self.execute(event), "ALREADY_ATTEMPTED")
        self.client.write_tou.assert_called_once()

    def test_simulation_approval_never_contacts_inverter(self):
        _, event, decision = self.vote(enabled=False)
        self.assertEqual(decision, "SIMULATION_APPROVED")
        self.assertEqual(self.execute(event), "NO_EXECUTABLE_APPROVAL")
        self.client_factory.assert_not_called()

    def test_issue_body_remains_stable_after_canonical_storage(self):
        approval.notify(self.store, DAY, "23:00", NOW, REVIEWERS, execution_enabled=True)
        identity, event = self.store.event()
        path = approval.proposal_path(DAY, identity)
        self.store.entries[path] = json.loads(approval.canonical(self.store.entries[path]))
        self.assertEqual(approval.decide(self.store, event, REVIEWERS, NOW), "APPROVED")

    def test_pending_issue_with_previous_json_order_can_be_approved(self):
        report_path = f"recommendations/{DAY}.json"
        self.store.entries[report_path] = json.loads(approval.canonical(self.store.entries[report_path]))
        approval.notify(self.store, DAY, "23:00", NOW, REVIEWERS, execution_enabled=True)
        identity, event = self.store.event()
        path = approval.proposal_path(DAY, identity)
        record = self.store.entries[path]
        report = self.store.entries[report_path]
        issue = self.store.issues[record["issue"]]
        legacy = approval.issue_text(record["payload"], identity, report, sort_write_keys=False)
        self.assertNotEqual(issue["body"], legacy)
        self.store.entries[path] = json.loads(approval.canonical(record))
        issue["body"] = legacy.replace('"soc": 65', '"soc": 64', 1)
        self.assertEqual(approval.decide(self.store, event, REVIEWERS, NOW), "ISSUE_CHANGED_OR_CLOSED")
        issue["body"] = legacy
        self.assertEqual(approval.decide(self.store, event, REVIEWERS, NOW), "APPROVED")

    def test_rejected_approval_never_contacts_inverter(self):
        _, event, decision = self.vote(command="reject")
        self.assertEqual(decision, "REJECTED")
        self.assertEqual(self.execute(event), "NO_EXECUTABLE_APPROVAL")
        self.client_factory.assert_not_called()

    def test_expired_or_edited_approval_never_contacts_inverter(self):
        _, event, _ = self.vote()
        self.assertEqual(self.execute(event, NOW + timedelta(hours=5)), "NO_EXECUTABLE_APPROVAL")
        self.store.comment["updated_at"] = (NOW + timedelta(seconds=1)).isoformat()
        self.assertEqual(self.execute(event), "NO_EXECUTABLE_APPROVAL")
        self.client_factory.assert_not_called()

    def test_different_inverters_can_be_reserved_independently(self):
        with InverterLock(lambda: self.store, self.serial, lambda: NOW):
            with InverterLock(lambda: self.store, "ANOTHER-INVERTER", lambda: NOW):
                pass

    def test_changed_configuration_aborts_before_submission_and_unlocks(self):
        identity, event, _ = self.vote()
        self.before["timeUseSettingItems"][5]["soc"] += 1
        with self.assertRaisesRegex(ValueError, "TOU_CHANGED_SINCE_APPROVAL"):
            self.execute(event)
        self.assertFalse(self.audit(identity)["locked"])
        self.client.write_tou.assert_not_called()

    def test_rejected_lock_commit_never_touches_inverter(self):
        _, event, _ = self.vote()
        self.store.publish.side_effect = None
        self.store.publish.return_value = False
        with self.assertRaisesRegex(RuntimeError, "INVERTER_LOCK_COMMIT_FAILED"):
            self.execute(event)
        self.client_factory.assert_not_called()

    def test_terminal_write_failure_releases_without_verifying(self):
        identity, event, _ = self.vote()
        self.client.read_order.side_effect = [(666, {}), (500, {"error": "530"})]
        self.assertEqual(self.execute(event), "ORDER_FAILED")
        self.assertFalse(self.audit(identity)["locked"])
        self.client.start_dynamic_read.assert_called_once()

    def test_write_timeout_retains_lock_without_retry(self):
        identity, event, _ = self.vote()
        self.client.write_tou.side_effect = DeyeClientError("Timeout")
        self.assertEqual(self.execute(event), "EXECUTION_UNCONFIRMED")
        self.assertTrue(self.audit(identity)["locked"])
        self.assertEqual(self.execute(event), "ALREADY_ATTEMPTED")
        self.client.write_tou.assert_called_once()

    def test_verification_mismatch_retains_lock_and_records_difference(self):
        identity, event, _ = self.vote()
        self.after["timeUseSettingItems"][5]["soc"] = 40
        self.assertEqual(self.execute(event), "TOU_VERIFICATION_MISMATCH")
        record = self.audit(identity)
        self.assertTrue(record["locked"])
        self.assertEqual(record["differences"]["Time 6.soc"], {"esperado": 65, "leído": 40})
