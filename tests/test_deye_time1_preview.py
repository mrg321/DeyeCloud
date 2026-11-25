from copy import deepcopy
import unittest
from unittest.mock import patch
from io import StringIO
import json
import requests

from scripts.deye_client import DeyeClientError
from scripts.test_write_deye_time1 import (
    DYNAMIC_READ_PATH, DYNAMIC_RESULT_PATH, TOU_UPDATE_PATH,
    Time1WriteClient, main, prepare_update, write_payload,
    VERIFICATION_UNCONFIRMED,
)


class Time1PreviewTests(unittest.TestCase):
    def test_order_sent_code_is_accepted_only_for_tou_write(self):
        body = {"code": "1106000", "requestId": "req-42", "msg": "order sent successfully"}
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode("utf-8")
        response._content_consumed = True
        with Time1WriteClient() as client:
            client._token = "test-token"
            with patch.object(client._session, "post", return_value=response):
                self.assertEqual(client.write_tou({"deviceSn": "INVERTER"}), body)
            self.assertFalse(client._application_succeeded("/v1.0/config/tou", body))
            self.assertFalse(client._application_succeeded(TOU_UPDATE_PATH, {**body, "msg": "rejected"}))

    def test_write_rejection_exposes_deye_diagnostics_with_redaction(self):
        body = {"success": False, "code": "2101008", "requestId": "req-42",
                "msg": "Invalid enableSell for INVERTER\nToken: test-token"}
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode("utf-8")
        response._content_consumed = True
        with Time1WriteClient() as client:
            client._token = "test-token"
            with patch.object(client._session, "post", return_value=response):
                with self.assertRaises(DeyeClientError) as caught:
                    client.write_tou({"deviceSn": "INVERTER"})
        self.assertEqual(caught.exception.code, "API_ARGUMENT_TYPE_REJECTED")
        self.assertEqual(caught.exception.details, {
            "httpStatus": 200, "code": "2101008", "requestId": "req-42",
            "msg": "Invalid enableSell for [oculto] Token: [oculto]",
        })

    def settings(self, enabled):
        return {"touAction": "on", "timeUseSettingItems": [
            {"time": f"{hour:02d}00", "power": 8000, "soc": 60, "voltage": 49,
             "enableGeneration": False, "enableGridCharge": enabled, "enableSell": True}
            for hour in range(0, 24, 4)
        ]}

    def test_recommended_plan_preserves_other_fields(self):
        original = self.settings(True)
        update = prepare_update("INVERTER", original)
        expected = [
            ("0000", 60, False), ("0200", 80, True), ("0800", 30, False),
            ("1000", 70, False), ("1600", 30, False), ("2100", 30, False),
        ]
        for before, after, (start, soc, grid) in zip(
                original["timeUseSettingItems"], update["timeUseSettingItems"], expected):
            self.assertEqual(after, {**before, "time": start, "soc": soc, "enableGridCharge": grid})
        self.assertEqual(original, self.settings(True))

    def test_incomplete_read_is_rejected(self):
        settings = self.settings(False)
        del settings["timeUseSettingItems"][0]["enableGridCharge"]
        with self.assertRaises(ValueError):
            prepare_update("INVERTER", settings)

    def test_sell_modes_build_distinct_payloads_without_mutating_the_proposal(self):
        update = prepare_update("INVERTER", self.settings(False))
        original = deepcopy(update)
        explicit = write_payload(update, "false")["timeUseSettingItems"]
        self.assertIs(explicit[0]["enableSell"], False)
        self.assertEqual(explicit[1:], [
            {**period, "time": f"{period['time'][:2]}:{period['time'][2:]}"}
            for period in update["timeUseSettingItems"][1:]
        ])
        omitted = write_payload(update, "omit")["timeUseSettingItems"]
        self.assertTrue(all("enableSell" not in period for period in omitted))
        self.assertEqual(update, original)

    def test_write_payload_formats_all_hours_without_changing_read_values(self):
        settings = self.settings(False)
        update = prepare_update("INVERTER", settings)
        update["timeUseSettingItems"][1]["time"] = "02:00"
        payload = write_payload(update, "omit")
        self.assertEqual([period["time"] for period in payload["timeUseSettingItems"]],
                         ["00:00", "02:00", "08:00", "10:00", "16:00", "21:00"])
        self.assertEqual(update["timeUseSettingItems"][0]["time"], "0000")
        update["timeUseSettingItems"][1]["time"] = "2460"
        with self.assertRaisesRegex(ValueError, "Time 2"):
            write_payload(update, "omit")

    @patch("scripts.test_write_deye_time1.Time1WriteClient")
    def test_apply_confirms_then_writes_once_and_reads_back(self, client_factory):
        client = client_factory.return_value.__enter__.return_value
        current = self.settings(False)
        update = prepare_update("INVERTER", current)
        proposed = {**current, "timeUseSettingItems": update["timeUseSettingItems"]}
        client.query_read.side_effect = [current, current]
        client.write_tou.return_value = {"orderId": 123}
        client.start_dynamic_read.return_value = 456
        client.read_order.side_effect = [(100, {}), (666, {}), (100, {}), (666, {})]
        client.read_dynamic_tou_result.side_effect = [{"timeUseSettingItems": None}, proposed]
        with (patch("builtins.input", return_value="APLICAR"),
              patch("scripts.test_write_deye_time1.ENABLE_SELL_MODE", "omit"),
              patch("scripts.test_write_deye_time1.time.sleep"),
              patch("sys.stdout", new_callable=StringIO) as stdout):
            self.assertEqual(main(["--device-sn", "INVERTER", "--apply"]), 0)
        output = stdout.getvalue()
        self.assertIn("GET /v1.0/order/123", output)
        self.assertIn('"status": 100', output)
        self.assertIn('"status": 666', output)
        self.assertIn(DYNAMIC_READ_PATH, output)
        self.assertIn("GET /v1.0/order/456", output)
        self.assertIn(DYNAMIC_RESULT_PATH, output)
        self.assertLess(output.index('"status": 666'), output.index("[Verificación TOU"))
        client.write_tou.assert_called_once_with(write_payload(update, "omit"))
        self.assertEqual(client.query_read.call_count, 2)
        self.assertEqual([call[0] for call in client.method_calls], [
            "ensure_authenticated", "query_read", "ensure_authenticated", "query_read",
            "write_tou", "read_order", "read_order", "start_dynamic_read",
            "read_order", "read_order", "read_dynamic_tou_result", "read_dynamic_tou_result",
        ])

    @patch("scripts.test_write_deye_time1.Time1WriteClient")
    def test_stale_readback_is_unconfirmed_and_does_not_resend(self, client_factory):
        client = client_factory.return_value.__enter__.return_value
        client.query_read.return_value = self.settings(True)
        client.write_tou.return_value = {"orderId": 123}
        client.start_dynamic_read.return_value = 456
        client.read_order.return_value = (666, {})
        client.read_dynamic_tou_result.return_value = self.settings(True)
        with (patch("builtins.input", return_value="APLICAR"),
              patch("scripts.test_write_deye_time1.time.sleep"),
              patch("sys.stdout", new_callable=StringIO),
              patch("sys.stderr", new_callable=StringIO) as stderr):
            self.assertEqual(main(["--device-sn", "INVERTER", "--apply"]), VERIFICATION_UNCONFIRMED)
        self.assertIn('"Time 1.enableGridCharge": {"esperado": false, "leído": true}', stderr.getvalue())
        client.write_tou.assert_called_once()

    @patch("scripts.test_write_deye_time1.Time1WriteClient")
    def test_failed_order_reports_error_without_reading_back(self, client_factory):
        client = client_factory.return_value.__enter__.return_value
        client.query_read.return_value = self.settings(True)
        client.write_tou.return_value = {"orderId": 123}
        client.read_order.return_value = (500, {"error": "530", "msg": "Command Failure"})
        with (patch("builtins.input", return_value="APLICAR"),
              patch("sys.stdout", new_callable=StringIO),
              patch("sys.stderr", new_callable=StringIO) as stderr):
            self.assertEqual(main(["--device-sn", "INVERTER", "--apply"]), 1)
        self.assertIn('"error": "530"', stderr.getvalue())
        self.assertIn("Command Failure", stderr.getvalue())
        self.assertEqual(client.query_read.call_count, 2)
        client.write_tou.assert_called_once()
        client.start_dynamic_read.assert_not_called()

    def test_dynamic_read_posts_device_then_result_posts_order_id(self):
        payloads = [
            {"code": "1106000", "msg": "order sent successfully", "orderId": 456},
            {"success": True, "code": "1000000", **self.settings(True), "requestId": "request-1"},
        ]
        responses = []
        for payload in payloads:
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode("utf-8")
            response._content_consumed = True
            responses.append(response)
        with Time1WriteClient() as client:
            client._token = "test-token"
            with patch.object(client._session, "post", side_effect=responses) as post:
                order_id = client.start_dynamic_read("INVERTER")
                self.assertEqual(order_id, 456)
                self.assertEqual(client.read_dynamic_tou_result(order_id), self.settings(True))
        first, second = post.call_args_list
        self.assertTrue(first.args[0].endswith(DYNAMIC_READ_PATH))
        self.assertEqual(first.kwargs["json"], {"deviceSn": "INVERTER"})
        self.assertTrue(second.args[0].endswith(DYNAMIC_RESULT_PATH))
        self.assertEqual(second.kwargs["json"], {"orderId": 456})

    def test_order_status_request_captures_http_error_message(self):
        response = requests.Response()
        response.status_code = 403
        response._content = b'{"msg":"Permission denied","error":"530","requestId":"req-42"}'
        response._content_consumed = True
        with Time1WriteClient() as client:
            client._token = "test-token"
            with patch.object(client._session, "get", return_value=response) as get:
                with self.assertRaises(DeyeClientError) as caught:
                    client.read_order(123, "INVERTER")
        self.assertTrue(get.call_args.args[0].endswith("/v1.0/order/123"))
        self.assertEqual(caught.exception.code, "ACCESS_DENIED")
        self.assertEqual(caught.exception.details, {
            "httpStatus": 403, "requestId": "req-42", "msg": "Permission denied", "error": "530",
        })

    @patch("scripts.test_write_deye_time1.DeyeClient")
    def test_cli_only_reads(self, client_factory):
        client = client_factory.return_value.__enter__.return_value
        client.query_read.return_value = self.settings(False)
        with patch("sys.stdout", new_callable=StringIO) as stdout:
            self.assertEqual(main(["--device-sn", "INVERTER"]), 0)
        self.assertEqual([call[0] for call in client.method_calls],
                         ["ensure_authenticated", "query_read"])
        client.query_read.assert_called_once_with("deye_read_tou", {"device_sn": "INVERTER"})
        preview = json.loads(stdout.getvalue())
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(preview["targetDate"], "2026-11-25")
        periods = preview["writePayload"]["timeUseSettingItems"]
        self.assertEqual((periods[1]["time"], periods[1]["soc"], periods[1]["enableGridCharge"]),
                         ("02:00", 80, True))
        self.assertEqual((periods[2]["time"], periods[2]["enableGridCharge"]), ("08:00", False))
