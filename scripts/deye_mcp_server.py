"""Local, read-only MCP server. stdout is reserved for MCP stdio messages."""

import http.client
import json
import logging
import sys
import threading

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

if __package__:
    from .deye_client import DeyeClient, DeyeClientError
    from .deye_read_contracts import READS, DESCRIPTIONS, input_schema, request_body
else:
    from deye_client import DeyeClient, DeyeClientError
    from deye_read_contracts import READS, DESCRIPTIONS, input_schema, request_body


MAX_RESULT_BYTES = 512 * 1024
TOOLS = [
    types.Tool(
        name="deye_connection_status",
        description="Comprueba autenticación EU y acceso de lectura a instalaciones.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=types.ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
        ),
    ),
    types.Tool(
        name="deye_list_stations",
        description="Lista una página de instalaciones EU: solo identificadores y nombres.",
        inputSchema={
            "type": "object",
            "properties": {
                "page": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1},
                "size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
        ),
    ),
]
TOOLS.extend(types.Tool(
    name=name, description=DESCRIPTIONS[name], inputSchema=input_schema(name),
    annotations=types.ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    ),
) for name in READS)


def tool_result(payload: dict, *, error: bool = False) -> types.CallToolResult:
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=payload,
        isError=error,
    )
    # Count both protocol representations, including JSON escaping overhead.
    if len(result.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
        payload = {"ok": False, "error": "RESPONSE_TOO_LARGE"}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structuredContent=payload,
            isError=True,
        )
    return result


class DeyeService:
    """One local client, lazy authentication, and no simultaneous Deye requests."""

    def __init__(self, client: DeyeClient | None = None) -> None:
        self._client = client if client is not None else DeyeClient()
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def call(self, name: str, arguments: dict | None) -> types.CallToolResult:
        # Validate ourselves: SDK validation errors can echo arbitrary input.
        if name not in {tool.name for tool in TOOLS}:
            return tool_result({"ok": False, "error": "UNKNOWN_TOOL"}, error=True)
        if arguments is None:
            arguments = {}
        if type(arguments) is not dict:
            return tool_result({"ok": False, "error": "INVALID_ARGUMENTS"}, error=True)
        if name == "deye_connection_status":
            valid = not arguments
            page, size = 1, 1
        elif name == "deye_list_stations":
            page, size = arguments.get("page", 1), arguments.get("size", 50)
            valid = (
                not (arguments.keys() - {"page", "size"})
                and type(page) is int and 1 <= page <= 10000
                and type(size) is int and 1 <= size <= 100
            )
        else:
            try:
                request_body(name, arguments)
                valid = True
            except ValueError:
                valid = False
        if not valid:
            return tool_result({"ok": False, "error": "INVALID_ARGUMENTS"}, error=True)
        if not self._lock.acquire(blocking=False):
            return tool_result({"ok": False, "error": "BUSY"}, error=True)
        try:
            self._client.ensure_authenticated()
            if name in READS:
                return tool_result({"ok": True, "data": self._client.query_read(name, arguments)})
            result = self._client.list_stations(page=page, size=size)
            if name == "deye_connection_status":
                return tool_result({"ok": True, "connected": True, "data_center": "eu"})
            stations = []
            for item in result["stations"]:
                if type(item["id"]) is not int or not 1 <= item["id"] <= 2**63 - 1:
                    return tool_result({"ok": False, "error": "INVALID_STATION_ID"}, error=True)
                stations.append({"id": item["id"], "name": item["name"]})
            return tool_result({
                "ok": True,
                "stations": stations,
                "total": result["total"],
                "page": page,
                "size": size,
                "has_more": page * size < result["total"],
            })
        except Exception as error:
            # Do not propagate raw client/Windows/SDK exceptions, even known ones.
            self._client.close()
            print("Deye EU: no se pudo completar la consulta de lectura.", file=sys.stderr)
            safe_codes = {"DEYE_UNAVAILABLE", "AUTH_REJECTED", "ACCESS_DENIED", "RATE_LIMITED",
                          "DEVICE_OFFLINE", "UNSUPPORTED_CONFIG_POINT", "UNSUPPORTED_DEVICE",
                          "DEVICE_NOT_FOUND_FOR_OPERATION", "INVALID_RESPONSE", "INVALID_ARGUMENTS",
                          "API_ARGUMENT_REJECTED", "API_ARGUMENT_TYPE_REJECTED", "API_INTERVAL_REJECTED"}
            code = error.code if isinstance(error, DeyeClientError) and error.code in safe_codes else "DEYE_UNAVAILABLE"
            payload = {"ok": False, "error": code}
            if name in READS and isinstance(error, DeyeClientError) and error.issues:
                payload["schema_issues"] = error.issues
            return tool_result(payload, error=True)
        finally:
            self._lock.release()


def create_server(service: DeyeService) -> Server:
    server = Server("deye_local", version="0.4.1")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict | None) -> types.CallToolResult:
        try:
            return await anyio.to_thread.run_sync(service.call, name, arguments)
        except Exception:
            print("Deye EU: fallo interno del servidor local.", file=sys.stderr)
            return tool_result({"ok": False, "error": "INTERNAL_ERROR"}, error=True)

    return server


def configure_diagnostics() -> None:
    # Dependency debug logs can include URLs, request headers or tracebacks.
    # Emit only fixed application diagnostics on stderr, never raw exceptions.
    logging.disable(logging.CRITICAL)
    http.client.HTTPConnection.debuglevel = 0


async def run() -> None:
    service = DeyeService()
    server = create_server(service)
    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        service.close()


def main() -> int:
    configure_diagnostics()
    try:
        anyio.run(run)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Deye EU: el servidor local se ha detenido por un error.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
