"""Local integration check for the debugger; no command-line arguments needed.

Select .venv/Scripts/python.exe, open this file and debug the Python file.
When run, this reads existing Windows credentials, authenticates against EU
and lists installation names using deye_client's sanitized output.
It never creates credentials or changes inverter settings.
"""

if __package__:
    from .deye_client import main
else:
    from deye_client import main


if __name__ == "__main__":
    # Set breakpoints in DeyeClient.authenticate or list_station_names.
    raise SystemExit(main(["--test"]))
