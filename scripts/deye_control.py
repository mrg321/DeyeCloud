"""Transporte compartido para ?rdenes TOU y lectura din?mica."""

import json
import sys
import time

if __package__:
    from .deye_client import DeyeClient, DeyeClientError
    from .deye_read_contracts import READS, project
else:
    from deye_client import DeyeClient, DeyeClientError
    from deye_read_contracts import READS, project


TOU_UPDATE_PATH = "/v1.0/order/sys/tou/update"
TOU_READ = "deye_read_tou"
DYNAMIC_READ_PATH = "/v1.0/strategy/dynamicControl/read"
DYNAMIC_RESULT_PATH = "/v1.0/strategy/dynamicControl/readResult"
_TOU_SHAPE = READS[TOU_READ][1]
DYNAMIC_TOU_SHAPE = {
    **_TOU_SHAPE,
    "timeUseSettingItems": [{**_TOU_SHAPE["timeUseSettingItems"][0], "enableSell": bool}],
}
ORDER_POLL_ATTEMPTS = 25
ORDER_POLL_INTERVAL_SECONDS = 5
ORDER_PENDING_STATES = {0, 100, 300}
ORDER_FAILED_STATES = {400: "terminada", 500: "fallida"}
ORDER_SUCCEEDED = 666
VERIFY_ATTEMPTS = 13
VERIFY_INTERVAL_SECONDS = 10
VERIFICATION_UNCONFIRMED = 2


class DeyeControlClient(DeyeClient):
    def _allowed_post(self, path, authenticated):
        return (path in {TOU_UPDATE_PATH, DYNAMIC_READ_PATH, DYNAMIC_RESULT_PATH} and authenticated
                or super()._allowed_post(path, authenticated))

    def write_tou(self, body):
        return self._post(TOU_UPDATE_PATH, body, authenticated=True, diagnostics=True)

    def start_dynamic_read(self, device_sn):
        result = self._post(DYNAMIC_READ_PATH, {"deviceSn": device_sn},
                            authenticated=True, diagnostics=True)
        return result.get("orderId")

    def read_dynamic_tou_result(self, order_id):
        result = self._post(DYNAMIC_RESULT_PATH, {"orderId": order_id},
                            authenticated=True, diagnostics=True)
        try:
            return project(result, DYNAMIC_TOU_SHAPE)
        except (ValueError, TypeError):
            raise DeyeClientError("Deye devolvió un resultado de lectura dinámica inválido.",
                                  code="INVALID_RESPONSE") from None

    def read_order(self, order_id, device_sn):
        if type(order_id) is not int or not 0 < order_id < 2**63:
            raise DeyeClientError("La respuesta de envío no contiene un orderId válido.")
        result = self._request(
            self._session.get, f"/v1.0/order/{order_id}", authenticated=True,
            diagnostics=True, diagnostic_serial=device_sn, params={"language": "en"},
        )
        details = self._response_details(result, device_sn)
        if result.get("orderId") not in (None, order_id):
            raise DeyeClientError("Deye devolvió el estado de otra orden.", details=details)
        status = result.get("status")
        if type(status) is not int:
            raise DeyeClientError("Deye no devolvió un estado de orden válido.", details=details)
        return status, details

    def _application_succeeded(self, path, result):
        if (path in {TOU_UPDATE_PATH, DYNAMIC_READ_PATH} and str(result.get("code")) == "1106000"
                and result.get("msg") == "order sent successfully"):
            return True
        return super()._application_succeeded(path, result)


def wait_for_order(client, order_id, device_sn):
    started = time.monotonic()
    for attempt in range(ORDER_POLL_ATTEMPTS):
        if attempt:
            time.sleep(ORDER_POLL_INTERVAL_SECONDS)
        log_attempt("Estado", attempt, ORDER_POLL_ATTEMPTS, started, f"GET /v1.0/order/{order_id}")
        status, details = client.read_order(order_id, device_sn)
        print(json.dumps({"status": status, **details}, ensure_ascii=False), flush=True)
        if status == ORDER_SUCCEEDED:
            return True
        if status in ORDER_FAILED_STATES:
            raise DeyeClientError(
                f"Orden {order_id} {ORDER_FAILED_STATES[status]} por Deye.",
                code="ORDER_FAILED", details={"status": status, **details},
            )
        if status not in ORDER_PENDING_STATES:
            raise DeyeClientError(
                f"Estado desconocido para la orden {order_id}.",
                code="UNKNOWN_ORDER_STATUS", details={"status": status, **details},
            )
    print(f"Deye aún no confirma la terminación de la orden {order_id} (estado {status}).",
          file=sys.stderr)
    if details:
        print(json.dumps(details, ensure_ascii=False), file=sys.stderr)
    return False


def comparable_settings(settings):
    return {
        **settings,
        "timeUseSettingItems": [
            {**period, "time": period["time"].replace(":", "")
             if isinstance(period.get("time"), str) else period.get("time")}
            for period in settings.get("timeUseSettingItems") or []
        ],
    }


def log_attempt(stage, attempt, total, started, operation):
    elapsed = time.monotonic() - started
    print(f"[{stage} {attempt + 1}/{total}, +{elapsed:.1f}s] {operation}", flush=True)


def tou_differences(expected, observed):
    differences = {}
    if expected.get("touAction") != observed.get("touAction"):
        differences["touAction"] = {
            "esperado": expected.get("touAction"), "leído": observed.get("touAction"),
        }
    expected_periods = expected["timeUseSettingItems"]
    observed_periods = observed["timeUseSettingItems"]
    if len(expected_periods) != len(observed_periods):
        differences["número de tramos"] = {
            "esperado": len(expected_periods), "leído": len(observed_periods),
        }
    for index, (wanted, actual) in enumerate(zip(expected_periods, observed_periods), start=1):
        for field in sorted(wanted.keys() | actual.keys()):
            if wanted.get(field) != actual.get(field):
                differences[f"Time {index}.{field}"] = {
                    "esperado": wanted.get(field), "leído": actual.get(field),
                }
    return differences
