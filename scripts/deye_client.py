"""Read-only Deye Cloud EU client. Run the local check with --test.

Authentication contract:
  .agents/skills/deye-openapi-quickstart/references/token-and-control-examples.md
Station endpoint and pagination contract:
  .agents/skills/deye-cloud-scenarios/SKILL.md
  https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html#station_list

Credentials come from Windows Credential Manager by default, or explicit
environment mode for Actions. Tokens are never persisted or logged. A trusted
in-process read observer may archive successful read JSON in a private store.
Do not enable HTTP debug logging or
local-variable capture when integrating this module into another application.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

import requests

if __package__:
    from .deye_read_contracts import READS, project, request_body, DAILY_KEYS, schema_issues
else:
    from deye_read_contracts import READS, project, request_body, DAILY_KEYS, schema_issues

if __package__:
    from .setup_credentials import (
        CredentialSetupError,
        read_and_validate_credentials,
    )
else:
    from setup_credentials import (
        CredentialSetupError,
        read_and_validate_credentials,
    )


BASE_URL = "https://eu1-developer.deyecloud.com"
TOKEN_PATH = "/v1.0/account/token"
STATIONS_PATH = "/v1.0/station/list"
PAGE_SIZE = 100
# Bound requests if an unexpected server response prevents pagination ending.
MAX_PAGES = 1000
MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_DEADLINE_SECONDS = 30


class DeyeClientError(RuntimeError):
    """Only fixed, sanitized messages may be supplied to this exception."""

    def __init__(self, message, *, code="DEYE_UNAVAILABLE", issues=None, details=None):
        super().__init__(message)
        self.code = code
        self.issues = issues
        self.details = details


def read_environment_credentials():
    """Explicit, all-or-nothing Actions source; never falls back to Windows."""
    names = {"AppId": "DEYE_APP_ID", "AppSecret": "DEYE_APP_SECRET",
             "Email": "DEYE_EMAIL", "Password": "DEYE_PASSWORD"}
    credentials = {field: os.environ.get(name, "") for field, name in names.items()}
    if any(not value or not value.strip() or "\x00" in value or len(value) > 4096
           for value in credentials.values()):
        credentials.clear()
        raise CredentialSetupError("Credenciales de entorno incompletas o inválidas.")
    return credentials


class DeyeClient:
    def __init__(self, *, auth_mode=None, read_observer=None,
                 min_interval=0.0, max_requests=None) -> None:
        self._auth_mode = auth_mode or os.environ.get("DEYE_AUTH_MODE", "windows")
        if self._auth_mode not in {"windows", "environment"}:
            raise DeyeClientError("Modo de autenticación inválido.")
        self._read_observer = read_observer
        self._min_interval = min_interval
        self._max_requests = max_requests
        self._request_count = 0
        self._last_request = None
        self._token: str | None = None
        self._authenticated_until = 0.0
        self._session = requests.Session()
        # Do not implicitly load .netrc credentials or environment proxies.
        self._session.trust_env = False
        self._session.headers.update(
            {"Content-Type": "application/json", "Accept": "application/json"}
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        self._token = None
        self._authenticated_until = 0.0
        self._session.close()

    def _allowed_post(self, path, authenticated):
        return ((path, authenticated) in {(TOKEN_PATH, False), (STATIONS_PATH, True)}
                or (authenticated and any(path == read_path for read_path, _ in READS.values())))

    def _application_succeeded(self, path, result):
        return (result.get("success") is True
                and (result.get("code") is None or str(result["code"]) == "1000000"))

    def _post(
        self,
        path: str,
        body: dict,
        *,
        params: dict | None = None,
        authenticated: bool = False,
        diagnostics: bool = False,
    ) -> dict:
        if not self._allowed_post(path, authenticated):
            raise DeyeClientError("Operación no permitida por este cliente.")
        if authenticated and params is not None:
            raise DeyeClientError("Parámetros de consulta no permitidos.")
        return self._request(self._session.post, path, authenticated=authenticated,
                             diagnostics=diagnostics, params=params, json=body)

    def _request(self, send, path, *, authenticated, diagnostics=False, diagnostic_serial=None, **kwargs):
        if authenticated and self._token is None:
            raise DeyeClientError("Es necesario autenticar antes de consultar.")
        if self._max_requests is not None and self._request_count >= self._max_requests:
            raise DeyeClientError("Presupuesto de peticiones agotado.", code="REQUEST_BUDGET")
        if self._last_request is not None and self._min_interval:
            time.sleep(max(0, self._min_interval - (time.monotonic() - self._last_request)))
        self._request_count += 1
        self._last_request = time.monotonic()
        headers = {}
        if authenticated:
            headers["Authorization"] = f"bearer {self._token}"
        deadline = self._last_request + REQUEST_DEADLINE_SECONDS
        try:
            with send(
                BASE_URL + path,
                headers=headers,
                timeout=(5, 10),
                verify=True,
                allow_redirects=False,
                stream=True,
                **kwargs,
            ) as response:
                # Reject redirects as well as HTTP errors, without exposing
                # URLs (which contain AppId), response bodies or exceptions.
                http_status = response.status_code
                http_error = None
                if not 200 <= http_status < 300:
                    http_error = DeyeClientError(
                        "Deye no pudo completar la consulta.",
                        code={401: "AUTH_REJECTED", 403: "ACCESS_DENIED",
                              429: "RATE_LIMITED"}.get(response.status_code, "DEYE_UNAVAILABLE"),
                        details={"httpStatus": http_status} if diagnostics else None,
                    )
                    if not diagnostic_serial or http_status < 400:
                        raise http_error
                content = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if time.monotonic() > deadline:
                        raise DeyeClientError("Se agotó el tiempo de espera.")
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise DeyeClientError("La respuesta supera el tamaño permitido.")
                    content.extend(chunk)
                try:
                    result = json.loads(content)
                except ValueError:
                    if http_error is not None:
                        raise http_error from None
                    raise DeyeClientError(
                        "El servidor no devolvió JSON válido."
                    ) from None
                if http_error is not None:
                    if isinstance(result, dict):
                        http_error.details.update(self._response_details(result, diagnostic_serial))
                    raise http_error
        except requests.exceptions.SSLError:
            raise DeyeClientError(
                "Fallo TLS: no se pudo establecer una conexión verificada."
            ) from None
        except requests.exceptions.Timeout:
            raise DeyeClientError("Se agotó el tiempo de espera.") from None
        except requests.exceptions.RequestException:
            raise DeyeClientError("Fallo de comunicación HTTP.") from None
        finally:
            headers.clear()

        if not isinstance(result, dict):
            raise DeyeClientError("Formato de respuesta inesperado.")
        if not self._application_succeeded(path, result):
            # Keep read errors fixed. Only the explicit write test requests details.
            safe_code = {
                "2101027": "ACCESS_DENIED", "2104006": "DEVICE_OFFLINE",
                "2106001": "UNSUPPORTED_CONFIG_POINT", "2104005": "UNSUPPORTED_DEVICE",
                "2101040": "DEVICE_NOT_FOUND_FOR_OPERATION",
                "2101006": "API_ARGUMENT_REJECTED", "2101008": "API_ARGUMENT_TYPE_REJECTED",
                "2101012": "API_INTERVAL_REJECTED",
            }.get(str(result.get("code")), "DEYE_UNAVAILABLE")
            details = None
            if diagnostics:
                serial = diagnostic_serial or kwargs.get("json", {}).get("deviceSn")
                details = {"httpStatus": http_status, **self._response_details(result, serial)}
            raise DeyeClientError("Deye devolvió un error de aplicación.", code=safe_code, details=details)
        if authenticated and self._read_observer is not None:
            self._read_observer(path, result)
        return result

    def _response_details(self, result, serial=None):
        details = {}
        for field, pattern in (("code", r"[0-9]{1,16}"),
                               ("requestId", r"[A-Za-z0-9_-]{1,128}")):
            raw_value = result.get(field)
            value = str(raw_value) if type(raw_value) in (int, str) else ""
            if re.fullmatch(pattern, value) and value not in (self._token, serial):
                details[field] = value
        for field in ("msg", "error"):
            message = result.get(field)
            if type(message) in (str, int):
                message = str(message)
                for value in (self._token, serial):
                    if value:
                        message = message.replace(value, "[oculto]")
                details[field] = "".join(char if char.isprintable() else " "
                                         for char in message[:500])
        return details

    def query_read(self, name: str, arguments: dict) -> dict:
        """Only named, documented read operations; no user-supplied HTTP body."""
        if name not in READS:
            raise DeyeClientError("Operación no permitida.")
        try:
            body = request_body(name, arguments)
        except ValueError:
            raise DeyeClientError("Argumentos inválidos.", code="INVALID_ARGUMENTS") from None
        path, shape = READS[name]
        result = self._post(path, body, authenticated=True)
        try:
            data = project(result, shape, measure_keys=DAILY_KEYS | set(body.get("measurePoints", [])))
            if name in {"deye_station_daily_history", "deye_station_power_history"}:
                rows, total = data["stationDataItems"], data["total"]
                if rows is None and total == 0:
                    rows = data["stationDataItems"] = []
                if total is not None and (total < 0 or (rows is not None and len(rows) > total)):
                    raise ValueError("INVALID_RESPONSE")
                data["quality"] = {"returned_records": len(rows) if rows is not None else None,
                    "reported_total": total,
                    "complete_by_total": len(rows) == total if rows is not None and total is not None else None}
            if name in {"deye_device_daily_history", "deye_device_intraday_history"}:
                if data["granularity"] not in (None, body["granularity"]):
                    raise ValueError("INVALID_RESPONSE")
            list_key = {"deye_list_station_devices": "deviceListItems",
                        "deye_station_alerts": "stationAlertItems",
                        "deye_device_alerts": "alertList"}.get(name)
            if list_key:
                total, items = data["total"], data[list_key]
                if type(total) is not int or total < 0:
                    raise ValueError("INVALID_RESPONSE")
                # The official schemas make empty collections optional.
                if items is None and total == 0:
                    items = data[list_key] = []
                if (type(items) is not list or len(items) > body["size"]
                        or len(items) > total
                        or (not items and (body["page"] - 1) * body["size"] < total)):
                    raise ValueError("INVALID_RESPONSE")
                data.update(page=body["page"], size=body["size"],
                            has_more=body["page"] * body["size"] < total)
            if name == "deye_list_station_devices":
                sns = [item["deviceSn"] for item in data["deviceListItems"]]
                if any(not sn for sn in sns) or len(set(sns)) != len(sns):
                    raise ValueError("INVALID_RESPONSE")
            if name == "deye_device_latest" and data["deviceDataList"] is not None:
                sns = [item["deviceSn"] for item in data["deviceDataList"]]
                if (any(sn not in body["deviceList"] for sn in sns)
                        or len(set(sns)) != len(sns)):
                    raise ValueError("INVALID_RESPONSE")
            if "deviceSn" in data and data["deviceSn"] not in (None, body.get("deviceSn")):
                raise ValueError("INVALID_RESPONSE")
            return data
        except (ValueError, TypeError):
            issues = schema_issues(result, shape)
            raise DeyeClientError("Formato de respuesta inesperado.", code="INVALID_RESPONSE", issues=issues) from None
        finally:
            result.clear()

    def authenticate(self) -> None:
        """Read existing entries and obtain a personal token in memory only."""
        self._token = None
        self._authenticated_until = 0.0
        credentials = (read_environment_credentials() if self._auth_mode == "environment"
                       else read_and_validate_credentials())
        body = {}
        params = {}
        result = {}
        try:
            params["appId"] = credentials["AppId"]
            body.update(
                {
                    "appSecret": credentials["AppSecret"],
                    "email": credentials["Email"],
                    "password": hashlib.sha256(
                        credentials["Password"].encode("utf-8")
                    ).hexdigest(),
                }
            )
            credentials.clear()
            result = self._post(TOKEN_PATH, body, params=params)
            token = result.get("accessToken")
            if (
                not isinstance(token, str)
                or not token
                or any(character.isspace() or ord(character) < 32 for character in token)
                or not token.isascii()
            ):
                raise DeyeClientError("La respuesta no contiene un token válido.")
            self._token = token
            # Conservative local reuse window; never persist either token.
            expires = result.get("expiresIn", 1800)
            try:
                lifetime = min(1800, max(0, int(expires) - 60))
            except (ValueError, TypeError, OverflowError):
                lifetime = 0
            self._authenticated_until = time.monotonic() + lifetime
        finally:
            credentials.clear()
            body.clear()
            params.clear()
            # In particular, discard any refreshToken included by the server.
            result.clear()

    def ensure_authenticated(self) -> None:
        """Reuse a token for at most 30 minutes, then read existing credentials."""
        if self._token is None or time.monotonic() >= self._authenticated_until:
            self.authenticate()

    def list_stations(self, page: int = 1, size: int = PAGE_SIZE) -> dict:
        """One documented station/list page, reduced to IDs and names only."""
        if type(page) is not int or not 1 <= page <= 10000:
            raise DeyeClientError("Página inválida: debe estar entre 1 y 10000.")
        if type(size) is not int or not 1 <= size <= PAGE_SIZE:
            raise DeyeClientError("Tamaño inválido: debe estar entre 1 y 100.")
        result = self._post(
            STATIONS_PATH, {"page": page, "size": size}, authenticated=True
        )
        try:
            total = result.get("total")
            stations = result.get("stationList")
            if type(total) is not int or total < 0:
                raise DeyeClientError("Total de instalaciones inválido.")
            if stations is None and total == 0:
                stations = []
            if not isinstance(stations, list) or len(stations) > size:
                raise DeyeClientError("Formato del listado de instalaciones inválido.")
            if len(stations) > total or ((page - 1) * size < total and not stations):
                raise DeyeClientError("El listado de instalaciones está incompleto.")
            items = []
            seen = set()
            for station in stations:
                if not isinstance(station, dict):
                    raise DeyeClientError("Formato de instalación inválido.")
                station_id = station.get("id")
                if station_id is not None:
                    if type(station_id) is not int or station_id <= 0 or station_id in seen:
                        raise DeyeClientError("Identificador de instalación inválido.")
                    seen.add(station_id)
                name = station.get("name")
                if name is not None and (not isinstance(name, str) or len(name) > 256):
                    raise DeyeClientError("Nombre de instalación inválido o demasiado largo.")
                items.append({"id": station_id, "name": name or "(sin nombre)"})
            return {"stations": items, "total": total, "page": page, "size": size}
        finally:
            result.clear()

    def list_station_names(self) -> list[str]:
        """Fetch every page; retain only names and internal pagination IDs."""
        names = []
        seen_ids = set()
        expected_total = None
        for page in range(1, MAX_PAGES + 1):
            result = self.list_stations(page=page, size=PAGE_SIZE)
            total = result["total"]
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise DeyeClientError(
                    "El listado cambió durante la paginación; repite la prueba."
                )
            stations = result["stations"]
            for station in stations:
                # IDs are optional in the published schema. When present,
                # check that pagination did not repeat an installation.
                station_id = station["id"]
                if station_id is not None:
                    if station_id in seen_ids:
                        raise DeyeClientError("Paginación de instalaciones inconsistente.")
                    seen_ids.add(station_id)
                names.append(station["name"])
            result.clear()
            if len(names) == total:
                return names
            if not stations or len(names) > total:
                raise DeyeClientError("El listado de instalaciones está incompleto.")
        raise DeyeClientError("Se alcanzó el límite de páginas de la consulta.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prueba de lectura de Deye Cloud EU.")
    parser.add_argument(
        "--test", action="store_true", help="Autenticar y listar nombres de instalaciones."
    )
    args = parser.parse_args(argv)
    if not args.test:
        parser.print_help()
        return 0

    authenticated = False
    try:
        with DeyeClient() as client:
            client.authenticate()
            authenticated = True
            print("Autenticación: correcta.")
            names = client.list_station_names()
            print(f"Instalaciones: {len(names)}")
            for name in names:
                # Escape terminal control characters and preserve one line per name.
                print(f"- {json.dumps(name, ensure_ascii=True)}")
        return 0
    except (CredentialSetupError, DeyeClientError) as error:
        if not authenticated:
            print("Autenticación: no completada.")
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Prueba interrumpida.", file=sys.stderr)
        return 130
    except Exception:
        # No raw traceback: third-party exceptions can contain request secrets.
        if not authenticated:
            print("Autenticación: no completada.")
        print("Error interno; detalles omitidos para proteger credenciales.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
