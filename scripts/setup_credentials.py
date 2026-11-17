"""Store Deye Cloud EU credentials locally; no network calls are made.

Generic credential blobs contain UTF-16LE text without a trailing NUL.
Password stores the original password, not its SHA-256 hash.
"""

from getpass import getpass
import hmac
import sys

if sys.platform == "win32":
    import pywintypes
    import win32cred
else:
    # Importable on Linux for the environment-authenticated collector and tests.
    # There is no plaintext-file fallback for Windows Credential Manager.
    class _UnavailableCredentials:
        CRED_TYPE_GENERIC = 1
        CRED_PERSIST_LOCAL_MACHINE = 2

        @staticmethod
        def CredRead(*args, **kwargs):
            raise OSError("WINDOWS_CREDENTIALS_UNAVAILABLE")

        CredWrite = CredRead

    class _WindowsErrors:
        error = OSError

    win32cred = _UnavailableCredentials()
    pywintypes = _WindowsErrors()


FIELDS = ("AppId", "AppSecret", "Email", "Password")
PREFIX = "DeyeCloud/eu/"
# Windows CRED_MAX_CREDENTIAL_BLOB_SIZE, measured in bytes.
MAX_BLOB_BYTES = 2560


class CredentialSetupError(RuntimeError):
    """A credential error whose message contains no credential values."""


def encode_value(field: str, value: str) -> bytes:
    """Validate without trimming or otherwise changing the supplied value."""
    if not isinstance(value, str) or not value or not value.strip():
        raise CredentialSetupError(f"{field}: el valor no puede estar vacío.")
    if "\x00" in value:
        raise CredentialSetupError(f"{field}: no se permiten caracteres NUL.")
    try:
        blob = value.encode("utf-16-le", errors="strict")
    except UnicodeError:
        raise CredentialSetupError(
            f"{field}: el valor no es compatible con UTF-16LE."
        ) from None
    if len(blob) > MAX_BLOB_BYTES:
        raise CredentialSetupError(
            f"{field}: el valor supera el tamaño admitido por Windows."
        )
    return blob


def read_and_validate_credentials(
    expected: dict[str, str] | None = None,
) -> dict[str, str]:
    """Read all four entries and validate UTF-16LE without displaying values.

    If expected is supplied, also verify exact equality with the original
    inputs. The returned dictionary is sensitive: do not print or log it.
    """
    values = {}
    for field in FIELDS:
        try:
            credential = win32cred.CredRead(
                PREFIX + field, win32cred.CRED_TYPE_GENERIC, 0
            )
        except pywintypes.error:
            raise CredentialSetupError(
                f"{field}: no se pudo leer la credencial de Windows."
            ) from None

        blob = credential.get("CredentialBlob")
        if not isinstance(blob, bytes):
            raise CredentialSetupError(
                f"{field}: CredRead no devolvió el formato binario esperado."
            )
        try:
            value = blob.decode("utf-16-le", errors="strict")
        except UnicodeError:
            raise CredentialSetupError(
                f"{field}: la credencial no contiene UTF-16LE válido."
            ) from None

        encoded = encode_value(field, value)
        if not hmac.compare_digest(encoded, blob):
            raise CredentialSetupError(
                f"{field}: falló la comprobación de codificación."
            )
        if expected is not None:
            if field not in expected or not hmac.compare_digest(
                encoded, encode_value(field, expected[field])
            ):
                raise CredentialSetupError(
                    f"{field}: la lectura no coincide con el valor introducido."
                )
        values[field] = value
    return values


def main() -> int:
    print("Configuración local de Deye Cloud EU (sin acceso a la red).")
    print("Se sustituirán las entradas existentes con las mismas claves.")
    print("Los cuatro valores se introducirán sin mostrarse en pantalla.")
    values = {}
    try:
        # Gather and validate every input before writing the first entry.
        for field in FIELDS:
            values[field] = getpass(f"{field}: ")
            encode_value(field, values[field])

        for field in FIELDS:
            try:
                win32cred.CredWrite(
                    {
                        "Type": win32cred.CRED_TYPE_GENERIC,
                        "TargetName": PREFIX + field,
                        "UserName": "DeyeCloud/eu",
                        # pywin32 requires str here and converts it to UTF-16.
                        # CredRead returns bytes, decoded and checked below.
                        "CredentialBlob": values[field],
                        "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                    },
                    0,
                )
            except pywintypes.error:
                raise CredentialSetupError(
                    f"{field}: no se pudo guardar la credencial. "
                    "Pueden haberse guardado entradas anteriores."
                ) from None

        verified = read_and_validate_credentials(expected=values)
        verified.clear()
        print("Las cuatro credenciales se guardaron y su lectura fue validada.")
        return 0
    except CredentialSetupError as error:
        print(f"Error: {error}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nConfiguración interrumpida; no se muestran los valores.")
        return 1
    finally:
        # Reduce references; Python does not guarantee secure memory erasure.
        values.clear()


if __name__ == "__main__":
    # getpass can otherwise fall back to echoed input in unsuitable terminals.
    import warnings
    from getpass import GetPassWarning

    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            raise SystemExit(main())
        except GetPassWarning:
            print("Error: usa una terminal local que permita ocultar la entrada.")
            raise SystemExit(1) from None
