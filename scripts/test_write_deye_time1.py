"""Prepara el plan TOU del 25 de noviembre de 2026."""

import argparse
from copy import deepcopy
import json
import re
import sys
import time

if __package__:
    from .deye_client import DeyeClient, DeyeClientError
    from .deye_control import (
        DeyeControlClient as Time1WriteClient, DYNAMIC_READ_PATH, DYNAMIC_RESULT_PATH,
        TOU_READ, TOU_UPDATE_PATH, VERIFICATION_UNCONFIRMED, VERIFY_ATTEMPTS,
        VERIFY_INTERVAL_SECONDS, comparable_settings, log_attempt, tou_differences, wait_for_order,
    )
    from .setup_credentials import CredentialSetupError
else:
    from deye_client import DeyeClient, DeyeClientError
    from deye_control import (
        DeyeControlClient as Time1WriteClient, DYNAMIC_READ_PATH, DYNAMIC_RESULT_PATH,
        TOU_READ, TOU_UPDATE_PATH, VERIFICATION_UNCONFIRMED, VERIFY_ATTEMPTS,
        VERIFY_INTERVAL_SECONDS, comparable_settings, log_attempt, tou_differences, wait_for_order,
    )
    from setup_credentials import CredentialSetupError


ENABLE_SELL_MODE = "omit"  # "omit" o "false"
TARGET_DATE = "2026-11-25"
# Time 1 a Time 6. Cada inicio cierra el tramo anterior.
TOU_PLAN = (
    ("0000", 60, False),
    ("0200", 80, True),
    ("0800", 30, False),
    ("1000", 70, False),
    ("1600", 30, False),
    ("2100", 30, False),
)


def prepare_update(device_sn, settings):
    if settings.get("touAction") != "on":
        raise ValueError("El modo TOU debe estar activo para aplicar este plan.")
    periods = settings.get("timeUseSettingItems")
    if not isinstance(periods, list) or len(periods) != 6:
        raise ValueError("Deye debe devolver los seis tramos TOU.")
    for period in periods:
        if (not isinstance(period, dict)
                or not isinstance(period.get("time"), str)
                or type(period.get("power")) is not int
                or type(period.get("enableGeneration")) is not bool
                or type(period.get("enableGridCharge")) is not bool):
            raise ValueError("Deye devolvió un tramo TOU incompleto o inválido.")

    updated = deepcopy(periods)
    for period, (start, soc, grid_charge) in zip(updated, TOU_PLAN):
        period.update(time=start, soc=soc, enableGridCharge=grid_charge)
    return {
        "deviceSn": device_sn,
        "timeUseSettingItems": updated,
    }


def write_payload(update, sell_mode):
    if sell_mode not in ("false", "omit"):
        raise ValueError('ENABLE_SELL_MODE debe ser "false" u "omit".')

    payload = deepcopy(update)
    periods = payload["timeUseSettingItems"]
    for index, period in enumerate(periods, start=1):
        match = re.fullmatch(r"([01][0-9]|2[0-3]):?([0-5][0-9])", period["time"])
        if match is None:
            raise ValueError(f"Hora inválida en Time {index}. Se esperaba HHmm o HH:mm.")
        hour, minute = match.groups()
        period["time"] = f"{hour}:{minute}"
        for field in ("soc", "voltage", "enableSell"):
            if period.get(field) is None:
                period.pop(field, None)
        if sell_mode == "omit":
            period.pop("enableSell", None)
    if sell_mode == "false":
        periods[0]["enableSell"] = False
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-sn", required=True, help="Número de serie del inversor")
    parser.add_argument("--apply", action="store_true", help="Pedir confirmación para enviar la orden")
    args = parser.parse_args(argv)
    read_args = {"device_sn": args.device_sn}
    client_type = Time1WriteClient if args.apply else DeyeClient
    stage = "lectura inicial"
    try:
        with client_type() as client:
            client.ensure_authenticated()
            current = client.query_read(TOU_READ, read_args)
            stage = "preparación"
            update = prepare_update(args.device_sn, current)
            proposed = {**current, "timeUseSettingItems": update["timeUseSettingItems"]}
            payload = write_payload(update, ENABLE_SELL_MODE)
            print(json.dumps({
                "mode": "preview",
                "targetDate": TARGET_DATE,
                "current": current,
                "proposed": proposed,
                "writePayload": payload,
            }, indent=2, ensure_ascii=False), flush=True)
            if not args.apply:
                return 0
            if comparable_settings(current) == comparable_settings(proposed) and ENABLE_SELL_MODE == "omit":
                print("El plan TOU ya está configurado.")
                return 0
            stage = "confirmación"
            if input("Escribe APLICAR para enviar la petición mostrada: ") != "APLICAR":
                print("Orden cancelada.")
                return 1

            stage = "lectura previa a la escritura"
            client.ensure_authenticated()
            if client.query_read(TOU_READ, read_args) != current:
                raise ValueError("La configuración cambió desde la vista previa. Orden cancelada.")
            stage = f"escritura TOU (POST {TOU_UPDATE_PATH}, enableSell={ENABLE_SELL_MODE})"
            response = client.write_tou(payload)
            order_id = response.get("orderId")
            if type(order_id) is int:
                print(f"Deye confirmó el envío de la orden {order_id}.")
            else:
                print("Deye confirmó el envío de la orden.")

            stage = "consulta de estado de la orden"
            if not wait_for_order(client, order_id, args.device_sn):
                return VERIFICATION_UNCONFIRMED
            print(f"Deye confirmó la ejecución de la orden {order_id} (estado 666).")

            stage = "solicitud de lectura dinámica"
            print(f"Solicitando lectura del inversor (POST {DYNAMIC_READ_PATH}).", flush=True)
            read_order_id = client.start_dynamic_read(args.device_sn)
            stage = "consulta de estado de la lectura dinámica"
            if not wait_for_order(client, read_order_id, args.device_sn):
                return VERIFICATION_UNCONFIRMED

            stage = "resultado TOU de la lectura dinámica"
            expected = comparable_settings(proposed)
            started = time.monotonic()
            for attempt in range(VERIFY_ATTEMPTS):
                if attempt:
                    time.sleep(VERIFY_INTERVAL_SECONDS)
                log_attempt("Verificación TOU", attempt, VERIFY_ATTEMPTS, started,
                            f"POST {DYNAMIC_RESULT_PATH} (orden de lectura {read_order_id})")
                observed = comparable_settings(client.read_dynamic_tou_result(read_order_id))
                if observed["timeUseSettingItems"]:
                    break
            else:
                print("La orden de lectura dinámica todavía no devuelve los tramos TOU.", file=sys.stderr)
                return VERIFICATION_UNCONFIRMED

            for wanted, actual, sent in zip(expected["timeUseSettingItems"],
                                            observed["timeUseSettingItems"],
                                            payload["timeUseSettingItems"]):
                if "enableSell" in sent and actual.get("enableSell") is not None:
                    wanted["enableSell"] = sent["enableSell"]
                elif "enableSell" not in wanted:
                    actual.pop("enableSell", None)
            differences = tou_differences(expected, observed)
            if differences:
                print(json.dumps({"diferencias": differences}, ensure_ascii=False), file=sys.stderr)
                return VERIFICATION_UNCONFIRMED
            if any("enableSell" in sent and actual.get("enableSell") is None
                   for sent, actual in zip(payload["timeUseSettingItems"], observed["timeUseSettingItems"])):
                print("La lectura dinámica no devolvió enableSell para todos los tramos enviados con ese campo.")
            print("Configuración TOU visible verificada mediante lectura dinámica del inversor.")
            return 0
    except DeyeClientError as exc:
        print(f"Error en {stage}: {exc} [{exc.code}]", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False), file=sys.stderr)
        return 1
    except (CredentialSetupError, ValueError, EOFError) as exc:
        print(f"Error en {stage}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
