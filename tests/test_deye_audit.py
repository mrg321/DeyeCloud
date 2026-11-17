"""Read contracts with simulated official responses and all real I/O blocked."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
from unittest.mock import patch, AsyncMock

from mcp.shared.memory import create_connected_server_and_client_session

from scripts import deye_read_contracts as contracts
from scripts import deye_mcp_server as server
from scripts import audit_deye_readonly as auditor
from test_deye_local import OfflineTestCase, response, token_response, stations_response, CANARY


ARGS = {
    "deye_list_station_devices": {"station_id": 123},
    "deye_station_latest": {"station_id": 123},
    "deye_device_latest": {"device_sns": ["SIM123"]},
    "deye_device_measure_points": {"device_sn": "SIM123"},
    "deye_read_battery_settings": {"device_sn": "SIM123"},
    "deye_read_system_settings": {"device_sn": "SIM123"},
    "deye_read_tou": {"device_sn": "SIM123"},
    "deye_station_alerts": {"station_id": 123, "start_timestamp": 1794870000,
                            "end_timestamp": 1794956400},
    "deye_device_alerts": {"device_sn": "SIM123", "start_timestamp": 1794870000,
                           "end_timestamp": 1794956400},
}
PAYLOADS = {
    "deye_list_station_devices": {"deviceListItems": [{
        "deviceSn": "SIM123", "deviceType": "INVERTER", "connectStatus": 1,
        "productId": "0_5411_1", "accessToken": CANARY}], "total": 1},
    "deye_station_latest": {"batterySOC": 0, "generationPower": None,
                            "lastUpdateTime": "2026-11-17T10:00:00Z"},
    "deye_device_latest": {"deviceDataList": [{"deviceSn": "SIM123", "deviceState": 1,
        "collectionTime": 1794909600, "dataList": [{"key": "BatterySOC", "value": "0",
        "unit": "%", "password": CANARY}]}]},
    "deye_device_measure_points": {"deviceSn": "SIM123", "measurePoints": ["BatterySOC"]},
    "deye_read_battery_settings": {"battCapacity": 200, "battLowCapacity": 20,
        "battShutDownCapacity": 10, "maxChargeCurrent": 50, "maxDischargeCurrent": 60},
    "deye_read_system_settings": {"energyPattern": "SIMULATED", "systemWorkMode": "SIMULATED",
                                  "maxSellPower": 5000},
    "deye_read_tou": {"touAction": "SIMULATED", "timeUseSettingItems": [{
        "enableGeneration": False, "enableGridCharge": True, "power": 1000,
        "soc": 40, "time": "0105", "voltage": None, "token": CANARY}]},
    "deye_station_alerts": {"stationAlertItems": [{"deviceSn": "SIM123", "alertCode": "SIM",
        "level": 1, "status": 1}], "total": 1},
    "deye_device_alerts": {"deviceSn": "SIM123", "alertList": [{"alertCode": "SIM",
        "level": 2, "status": 0, "reason": "Simulated"}], "total": 1},
}


def reading(payload):
    return response({"success": True, "code": "1000000", "msg": CANARY,
                     "accessToken": CANARY, "requestId": CANARY, **payload})


class AuditReadTests(OfflineTestCase):
    def test_every_read_over_mcp_and_exact_http_contract(self):
        async def exercise():
            service = self.make_service()
            async with create_connected_server_and_client_session(server.create_server(service)) as session:
                for name, args in ARGS.items():
                    with self.subTest(name=name):
                        self.post.side_effect = ([token_response()] if service._client._token is None else []) + [reading(PAYLOADS[name])]
                        result = await session.call_tool(name, args)
                        self.assertFalse(result.isError, result.structuredContent)
                        self.assert_no_secret(result)
                        self.assertEqual(self.post.call_args.args[0],
                                         "https://eu1-developer.deyecloud.com" + contracts.READS[name][0])
                        kwargs = self.post.call_args.kwargs
                        self.assertEqual(kwargs["json"], contracts.request_body(name, args))
                        self.assertIsNone(kwargs["params"])
                        self.assertTrue(kwargs["verify"])
                        self.assertFalse(kwargs["allow_redirects"])
                        if name == "deye_station_latest":
                            self.assertEqual(result.structuredContent["data"]["batterySOC"], 0)
                            self.assertIsNone(result.structuredContent["data"]["generationPower"])
        asyncio.run(exercise())

    def test_invalid_arguments_never_authenticate(self):
        service = self.make_service()
        cases = [(name, {**args, "path": CANARY}) for name, args in ARGS.items()]
        cases += [(name, {}) for name in ARGS]
        cases += [
            ("deye_list_station_devices", {"station_id": True}),
            ("deye_list_station_devices", {"station_id": 123, "size": 101}),
            ("deye_device_latest", {"device_sns": []}),
            ("deye_device_latest", {"device_sns": ["A"] * 11}),
            ("deye_device_latest", {"device_sns": ["A", "A"]}),
            ("deye_device_latest", {"device_sns": [[]]}),
            ("deye_device_measure_points", {"device_sn": "https://invalid"}),
            ("deye_device_alerts", {**ARGS["deye_device_alerts"], "end_timestamp": 1000000000}),
            ("deye_station_alerts", {**ARGS["deye_station_alerts"], "end_timestamp": 1999999999}),
        ]
        for name, args in cases:
            result = service.call(name, args)
            self.assertTrue(result.isError)
            self.assertEqual(result.structuredContent["error"], "INVALID_ARGUMENTS")
            self.assert_no_secret(result)
        self.credentials.assert_not_called()
        self.post.assert_not_called()

    def test_closed_catalog_and_paths(self):
        self.assertEqual(len(server.TOOLS), 16)
        for tool in server.TOOLS:
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)
        client = self.make_client()
        for path in ("/v1.0/config/battery/update", "/v1.0/order/custom/control",
                     "/v1.0/strategy/dynamicControl/read", "/v1.0/device/register",
                     "/v1.0/config/battery?x=1", "https://example.invalid"):
            with self.assertRaises(Exception):
                client._post(path, {}, authenticated=True)
        with self.assertRaises(Exception):
            client._post("/v1.0/config/battery", {}, params={"x": CANARY}, authenticated=True)
        self.post.assert_not_called()

    def test_documented_errors_only_and_unknown_errors_sanitized(self):
        cases = {"2101027": "ACCESS_DENIED", "2104006": "DEVICE_OFFLINE",
                 "2106001": "UNSUPPORTED_CONFIG_POINT", "2104005": "UNSUPPORTED_DEVICE",
                 "2101040": "DEVICE_NOT_FOUND_FOR_OPERATION", CANARY: "DEYE_UNAVAILABLE"}
        for code, expected in cases.items():
            self.post.side_effect = [token_response(), response({"success": False, "code": code, "msg": CANARY})]
            service = self.make_service()
            result = service.call("deye_read_battery_settings", {"device_sn": "SIM123"})
            self.assertEqual(result.structuredContent["error"], expected)
            self.assertIsNone(service._client._token)
            self.assert_no_secret(result)

    def test_missing_empty_and_malformed_data(self):
        cases = [
            ("deye_device_latest", {"deviceDataList": []}, False),
            ("deye_device_latest", {}, False),
            ("deye_station_alerts", {"total": 0}, False),
            ("deye_station_alerts", {"total": 1}, True),
            ("deye_station_alerts", {"total": -1, "stationAlertItems": []}, True),
            ("deye_device_latest", {"deviceDataList": [{"deviceSn": "OTHER"}]}, True),
            ("deye_device_latest", {"deviceDataList": [{"deviceSn": "SIM123", "dataList": {}}]}, True),
            ("deye_read_battery_settings", {"maxChargeCurrent": "not-an-int"}, True),
            ("deye_station_latest", {"batterySOC": True}, True),
            ("deye_station_latest", {"batterySOC": float("nan")}, True),
            ("deye_device_measure_points", {"measurePoints": ["x" * 4097]}, True),
        ]
        for name, payload, is_error in cases:
            self.post.side_effect = [token_response(), reading(payload)]
            result = self.make_service().call(name, ARGS[name])
            self.assertEqual(result.isError, is_error, name)
            self.assert_no_secret(result)

    def test_pagination_and_rate_limit_no_retry(self):
        self.post.side_effect = [token_response(), reading({"total": 101,
            "deviceListItems": [{"deviceSn": "SIM123"}]})]
        result = self.make_service().call("deye_list_station_devices", {"station_id": 123, "page": 2})
        self.assertTrue(result.structuredContent["data"]["has_more"])
        self.post.reset_mock()
        self.post.side_effect = [token_response(), response(status=429, raw=CANARY.encode())]
        result = self.make_service().call("deye_station_latest", {"station_id": 123})
        self.assertEqual(result.structuredContent["error"], "RATE_LIMITED")
        self.assertEqual(self.post.call_count, 2)
        self.assert_no_secret(result)

    def test_audit_runner_masks_all_serials_before_persisting(self):
        service = self.make_service()

        def fake_http(url, **kwargs):
            if url.endswith("/account/token"):
                return token_response()
            if url.endswith("/station/list"):
                return stations_response()
            name = next(name for name, (path, _) in contracts.READS.items() if url.endswith(path))
            if name == "deye_device_alerts":
                return reading({**PAYLOADS[name], "total": 2})
            return reading(PAYLOADS[name])

        self.post.side_effect = fake_http

        @asynccontextmanager
        async def fake_stdio(*args, **kwargs):
            yield None, None

        class LocalSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def initialize(self):
                pass

            async def call_tool(self, name, arguments):
                return service.call(name, arguments)

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "audit.json"
            with (patch.object(auditor, "stdio_client", fake_stdio),
                  patch.object(auditor, "ClientSession", lambda *args: LocalSession()),
                  patch.object(auditor.asyncio, "sleep", new_callable=AsyncMock)):
                result = asyncio.run(auditor.audit(output))
            saved = output.read_text(encoding="utf-8")
            self.assertNotIn("SIM123", saved)
            self.assertNotIn(CANARY, saved)
            self.assertIn("***M123", saved)
            self.assertTrue(result["read_only"])
            # The deliberately partial alert fixture must not be reported as complete.
            self.assertIn("deye_device_alerts:PAGINATION_INCOMPLETE", result["limitations"])

    def test_nested_redaction_and_short_serials(self):
        self.assertEqual(auditor.redact({"a": ["ABC123 appears", "AB"]}, {"ABC123", "AB"}),
                         {"a": ["***C123 appears", "***"]})
