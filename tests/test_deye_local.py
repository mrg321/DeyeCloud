"""Offline tests. All credential reads and HTTP requests are intercepted."""

import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from scripts import deye_client as client_module
from scripts import deye_mcp_server as server_module
from scripts import setup_credentials


CANARY = "FAKE_SECRET_MUST_NOT_ESCAPE"
CREDS = {
    "AppId": "fake-app-id",
    "AppSecret": CANARY,
    "Email": "fake@example.invalid",
    "Password": "fake-password-ñ",
}


def response(payload=None, *, status=200, raw=None):
    result = requests.Response()
    result.status_code = status
    result._content = raw if raw is not None else json.dumps(payload).encode("utf-8")
    result._content_consumed = True
    return result


def token_response():
    return response({
        "success": True, "code": "1000000", "accessToken": CANARY,
        "refreshToken": CANARY, "expiresIn": "5183999",
    })


def stations_response(items=None, *, total=None):
    if items is None:
        items = [{"id": 123, "name": "Solar simulado", "ownerName": CANARY}]
    return response({
        "success": True, "code": "1000000", "stationList": items,
        "total": len(items) if total is None else total,
    })


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Fail closed: no test can accidentally use Windows credentials or HTTP.
        for target in (
            "requests.sessions.Session.send",
            "socket.create_connection",
            "scripts.setup_credentials.win32cred.CredRead",
            "scripts.setup_credentials.win32cred.CredWrite",
        ):
            self.stack.enter_context(patch(target, side_effect=AssertionError("REAL_IO_BLOCKED")))
        self.credentials = self.stack.enter_context(patch(
            "scripts.deye_client.read_and_validate_credentials",
            side_effect=lambda: CREDS.copy(),
        ))
        self.post = self.stack.enter_context(patch("requests.sessions.Session.post"))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.stack.enter_context(redirect_stdout(self.out))
        self.stack.enter_context(redirect_stderr(self.err))

    def make_client(self):
        client = client_module.DeyeClient()
        self.addCleanup(client.close)
        return client

    def make_service(self):
        service = server_module.DeyeService(self.make_client())
        self.addCleanup(service.close)
        return service

    def assert_no_secret(self, result):
        self.assertNotIn(CANARY, result.model_dump_json())
        self.assertNotIn(CANARY, self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")


class ClientTests(OfflineTestCase):
    def test_authentication_contract_and_memory_reuse(self):
        captured = []

        def fake_post(url, **kwargs):
            captured.append((url, {**kwargs, "params": dict(kwargs["params"]),
                                    "json": dict(kwargs["json"])}))
            return token_response()

        self.post.side_effect = fake_post
        client = self.make_client()
        client.ensure_authenticated()
        client.ensure_authenticated()
        self.assertEqual(self.post.call_count, 1)
        url, kwargs = captured[0]
        self.assertEqual(url, client_module.BASE_URL + client_module.TOKEN_PATH)
        self.assertEqual(kwargs["params"], {"appId": CREDS["AppId"]})
        self.assertEqual(kwargs["json"], {
            "appSecret": CREDS["AppSecret"], "email": CREDS["Email"],
            "password": hashlib.sha256(CREDS["Password"].encode("utf-8")).hexdigest(),
        })
        self.assertTrue(kwargs["verify"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["timeout"], (5, 10))
        self.assertNotIn("Authorization", kwargs["headers"])
        client._authenticated_until = 0
        client.ensure_authenticated()
        self.assertEqual(self.post.call_count, 2)
        client.close()
        self.assertIsNone(client._token)

    def test_names_command_reuses_paginated_query(self):
        client = self.make_client()
        with patch.object(client, "list_stations", side_effect=[
            {"stations": [{"id": 1, "name": "A"}], "total": 2},
            {"stations": [{"id": 2, "name": "B"}], "total": 2},
        ]) as pages:
            self.assertEqual(client.list_station_names(), ["A", "B"])
            self.assertEqual(pages.call_count, 2)

    def test_endpoint_allowlist_blocks_other_paths_and_modes(self):
        client = self.make_client()
        for path, auth in (("/v1.0/order/sys/tou/update", True),
                           (client_module.TOKEN_PATH, True),
                           (client_module.STATIONS_PATH, False)):
            with self.subTest(path=path), self.assertRaises(client_module.DeyeClientError):
                client._post(path, {}, authenticated=auth)
        self.post.assert_not_called()

    def test_response_byte_limit(self):
        client = self.make_client()
        self.post.return_value = response(raw=b"x" * (client_module.MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(client_module.DeyeClientError):
            client.authenticate()

    def test_request_deadline(self):
        client = self.make_client()
        self.post.return_value = token_response()
        with patch("scripts.deye_client.time.monotonic", side_effect=[0, 31]):
            with self.assertRaises(client_module.DeyeClientError):
                client.authenticate()

    def test_missing_credentials_no_http(self):
        self.credentials.side_effect = setup_credentials.CredentialSetupError(CANARY)
        result = self.make_service().call("deye_connection_status", {})
        self.assertTrue(result.isError)
        self.post.assert_not_called()
        self.assert_no_secret(result)


class ServiceTests(OfflineTestCase):
    def test_lazy_start(self):
        self.make_service()
        self.credentials.assert_not_called()
        self.post.assert_not_called()

    def test_connection_and_station_projection(self):
        self.post.side_effect = [token_response(), stations_response(), stations_response()]
        service = self.make_service()
        status = service.call("deye_connection_status", {})
        self.assertEqual(status.structuredContent,
                         {"ok": True, "connected": True, "data_center": "eu"})
        result = service.call("deye_list_stations", {"page": 1, "size": 10})
        self.assertEqual(result.structuredContent["stations"], [{"id": 123, "name": "Solar simulado"}])
        self.assertFalse(result.structuredContent["has_more"])
        self.assertEqual(self.credentials.call_count, 1)
        self.assert_no_secret(status)
        self.assert_no_secret(result)
        calls = self.post.call_args_list
        self.assertEqual([call.args[0] for call in calls], [
            client_module.BASE_URL + client_module.TOKEN_PATH,
            client_module.BASE_URL + client_module.STATIONS_PATH,
            client_module.BASE_URL + client_module.STATIONS_PATH,
        ])

    def test_arguments_rejected_before_io(self):
        service = self.make_service()
        for args in ({"page": True}, {"page": "1"}, {"page": 0},
                     {"page": 10001}, {"size": 101}, {"size": 0},
                     {"password": CANARY}, {"size": 1.0}, []):
            with self.subTest(args=args):
                result = service.call("deye_list_stations", args)
                self.assertTrue(result.isError)
                self.assert_no_secret(result)
        self.assertTrue(service.call("deye_connection_status", {"x": CANARY}).isError)
        self.assertTrue(service.call("execute_http_" + CANARY, {}).isError)
        self.credentials.assert_not_called()
        self.post.assert_not_called()

    def test_http_tls_timeout_application_and_invalid_json_are_sanitized(self):
        cases = [
            response(status=401, raw=CANARY.encode()),
            response(status=429, raw=CANARY.encode()),
            response(status=500, raw=CANARY.encode()),
            response(status=302, raw=CANARY.encode()),
            requests.exceptions.SSLError(CANARY),
            requests.exceptions.Timeout(CANARY),
            requests.exceptions.ConnectionError(CANARY),
            RuntimeError(CANARY),
            response({"success": False, "code": CANARY, "msg": CANARY}),
            response(raw=CANARY.encode()),
            response({"success": True, "code": "1000000"}),
        ]
        for case in cases:
            with self.subTest(case=type(case).__name__):
                self.post.side_effect = [case]
                result = self.make_service().call("deye_connection_status", {})
                self.assertTrue(result.isError)
                self.assert_no_secret(result)

    def test_page_and_has_more(self):
        self.post.side_effect = [token_response(), stations_response(total=101)]
        result = self.make_service().call("deye_list_stations", {"page": 2, "size": 50})
        self.assertTrue(result.structuredContent["has_more"])
        self.assertEqual(result.structuredContent["page"], 2)
        self.assertEqual(self.post.call_args.kwargs["json"], {"page": 2, "size": 50})

    def test_empty_account(self):
        self.post.side_effect = [token_response(), stations_response([])]
        result = self.make_service().call("deye_list_stations", {})
        self.assertEqual(result.structuredContent["stations"], [])
        self.assertEqual(result.structuredContent["total"], 0)

    def test_invalid_or_excessive_station_data(self):
        cases = [
            [{"id": None, "name": "A"}],
            [{"id": 2**63, "name": "A"}],
            [{"id": 1, "name": "x" * 257}],
            [{"id": 1, "name": "A"}, {"id": 1, "name": "A"}],
            [{"id": 1, "name": "A"}] * 101,
        ]
        for items in cases:
            self.post.side_effect = [token_response(), stations_response(items)]
            result = self.make_service().call("deye_list_stations", {"size": 100})
            self.assertTrue(result.isError)
            self.assert_no_secret(result)

    def test_busy_rejected_without_io(self):
        service = self.make_service()
        with service._lock:
            result = service.call("deye_connection_status", {})
        self.assertTrue(result.isError)
        self.post.assert_not_called()

    def test_result_size_cap(self):
        result = server_module.tool_result({"text": "x" * (server_module.MAX_RESULT_BYTES // 2)})
        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["error"], "RESPONSE_TOO_LARGE")


class ProtocolTests(OfflineTestCase):
    def test_real_stdio_transport_with_io_blocked_in_child(self):
        root = Path(__file__).resolve().parents[1]
        # Child uses the real entry point with cloud/credential I/O disabled.
        bootstrap = "\n".join([
            "import runpy, sys, socket, requests, win32cred",
            "def blocked(*args, **kwargs): raise AssertionError('REAL_IO_BLOCKED')",
            "requests.sessions.Session.send = blocked",
            "socket.create_connection = blocked",
            "win32cred.CredRead = blocked",
            "win32cred.CredWrite = blocked",
            f"sys.path.insert(0, {str(root / 'scripts')!r})",
            f"runpy.run_path({str(root / 'scripts/deye_mcp_server.py')!r}, run_name='__main__')",
        ])

        async def exercise(errlog):
            params = StdioServerParameters(
                command=sys.executable, args=["-B", "-u", "-c", bootstrap], cwd=str(root)
            )
            with anyio.fail_after(15):
                async with stdio_client(params, errlog=errlog) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        catalog = await session.list_tools()
                        self.assertEqual(len(catalog.tools), len(server_module.TOOLS))
                        result = await session.call_tool("deye_list_stations", {"size": CANARY})
                        self.assertTrue(result.isError)
                        self.assert_no_secret(result)
                        # Deliberately blocked credentials must become a fixed error.
                        result = await session.call_tool("deye_connection_status", {})
                        self.assertTrue(result.isError)
                        self.assert_no_secret(result)

        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
            asyncio.run(exercise(errlog))
            errlog.seek(0)
            diagnostics = errlog.read()
            self.assertNotIn(CANARY, diagnostics)
            self.assertNotIn("Traceback", diagnostics)
            self.assertNotIn("REAL_IO_BLOCKED", diagnostics)

    def test_sdk_protocol_catalog_call_and_validation(self):
        async def exercise():
            self.post.side_effect = [token_response(), stations_response()]
            server = server_module.create_server(self.make_service())
            async with create_connected_server_and_client_session(server) as session:
                catalog = await session.list_tools()
                self.assertEqual({tool.name for tool in catalog.tools},
                                 {tool.name for tool in server_module.TOOLS})
                self.credentials.assert_not_called()
                for tool in catalog.tools:
                    self.assertTrue(tool.annotations.readOnlyHint)
                    self.assertFalse(tool.inputSchema["additionalProperties"])
                invalid = await session.call_tool("deye_list_stations", {"size": CANARY})
                self.assertTrue(invalid.isError)
                self.assert_no_secret(invalid)
                result = await session.call_tool("deye_list_stations", {"size": 1})
                self.assertFalse(result.isError)
                self.assert_no_secret(result)
        asyncio.run(exercise())

    def test_public_config_template_paths_and_isolation(self):
        import tomllib
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "config/deye_local.example.toml").read_text(encoding="utf-8"))
        self.assertEqual(set(config["mcp_servers"]), {"deye_local"})
        server = config["mcp_servers"]["deye_local"]
        template_root = Path(server["cwd"])
        self.assertEqual(server["cwd"], r"C:\PATH\TO\DeyeCloud")
        self.assertEqual(Path(server["command"]), template_root / ".venv/Scripts/python.exe")
        self.assertEqual(server["args"][0], "-u")
        relative_script = Path(server["args"][-1]).relative_to(template_root)
        self.assertEqual(relative_script, Path("scripts/deye_mcp_server.py"))
        self.assertTrue((root / relative_script).is_file())
        self.assertEqual(set(server["enabled_tools"]), {tool.name for tool in server_module.TOOLS})


if __name__ == "__main__":
    unittest.main()
