"""Convert a private collection transaction to an Excel workbook."""

from io import BytesIO
import re
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml"
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def fields(value, path=""):
    """Yield leaf values using JSON Pointer paths, including array indexes."""
    if isinstance(value, dict) and value:
        for key in sorted(value):
            component = key.replace("~", "~0").replace("/", "~1")
            yield from fields(value[key], f"{path}/{component}")
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from fields(item, f"{path}/{index}")
    else:
        yield path, value


def cell(value, reference):
    element = f'<c r="{reference}"'
    if value is None:
        return element + "/>"
    if isinstance(value, bool):
        return f'{element} t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)) and not (isinstance(value, int) and abs(value) >= 10**15):
        return f'{element}><v>{value}</v></c>'
    # Inline strings preserve literal text, including any formula-like values.
    value = escape(INVALID_XML.sub("\ufffd", str(value)))
    return f'{element} t="inlineStr"><is><t xml:space="preserve">{value}</t></is></c>'


def worksheet(rows):
    widths = [len(header) for header in rows[0]]
    xml_rows = []
    for number, row in enumerate(rows, 1):
        cells = []
        for column, value in enumerate(row):
            widths[column] = max(widths[column], len(str(value)))
            cells.append(cell(value, f"{chr(65 + column)}{number}"))
        xml_rows.append(f'<row r="{number}">{"".join(cells)}</row>')
    columns = "".join(f'<col min="{index}" max="{index}" width="{min(width + 3, 85)}" customWidth="1"/>'
                      for index, width in enumerate(widths, 1))
    last_column = chr(64 + len(widths))
    return (f'<worksheet xmlns="{SHEET_NS}"><sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            f'</sheetView></sheetViews><cols>{columns}</cols><sheetData>{"".join(xml_rows)}</sheetData>'
            f'<autoFilter ref="A1:{last_column}{len(rows)}"/></worksheet>')


def add_recommendation_sheet(workbook, report):
    """Add or refresh the recommendation in a collector-generated XLSX."""
    forecast, result = report["forecast"], report["recommendation"]
    targets = result.get("current_targets") or [None, None]
    rows = [
        ("Field", "Value"),
        ("Target date (Madrid)", report["date"]),
        ("Decision", result["decision"]),
        ("Confidence", result["confidence"]),
        ("Current battery SOC (%)", result.get("current_soc")),
        ("Current target 00:00–05:00 (%)", targets[0]),
        ("Current target 05:00–08:00 (%)", targets[1]),
        ("Recommended battery SOC (%)", result.get("recommended_soc")),
        ("Nominal battery capacity (kWh)", result.get("battery_nominal_kwh", result.get("nominal_capacity_kwh"))),
        ("Estimated usable battery capacity (kWh)", result.get("battery_usable_kwh", result.get("usable_capacity_kwh"))),
        ("Forecast source", forecast.get("source")),
        ("Forecast queried at", forecast.get("queried_at")),
        ("Forecast confidence", forecast.get("confidence")),
        ("Astronomical daylight (h)", forecast.get("daylight_hours")),
        ("Effective sunshine (h)", forecast.get("sunshine_hours")),
        ("Mean cloud cover (%)", forecast.get("cloud_cover_mean_pct")),
        ("Solar irradiation (kWh/m²)", forecast.get("irradiation_kwh_m2")),
        ("Expected PV production (kWh)", forecast.get("pv_kwh")),
        ("Expected household consumption (kWh)", result.get("expected_load_kwh")),
        ("Expected battery output 08:00–24:00 (kWh)", result.get("expected_battery_output_08_24_kwh")),
        ("Expected cost (EUR)", result.get("expected_cost_eur")),
        ("Estimated savings (EUR)", result.get("estimated_saving_eur")),
        ("Explanation", result.get("explanation")),
        ("Forecast issues", ", ".join(forecast.get("issues", []))),
        ("Recommendation issues", ", ".join(result.get("issues", []))),
        ("Lowest-cost battery SOC (%)", result.get("optimal_soc")),
        ("Lowest estimated cost (EUR)", result.get("optimal_cost_eur")),
        ("Gross saving at lowest cost (EUR)", result.get("gross_saving_eur")),
        ("Minimum saving required (EUR)", result.get("minimum_saving_eur")),
        ("Decision reason", result.get("decision_reason")),
        ("Optimization policy", result.get("optimization_policy")),
        ("Avoided grid-to-battery energy (kWh)", result.get("avoided_grid_charge_kwh")),
        ("Avoided battery conversion losses (kWh)", result.get("avoided_conversion_loss_kwh")),
    ]
    horizon = result.get("decision_horizon", {})
    if horizon:
        rows.extend([("Decision horizon start (Madrid)", horizon["start"]),
                     ("Decision horizon end, exclusive (Madrid)", horizon["end_exclusive"]),
                     ("Decision horizon duration (h)", horizon["hours"])])
    for option in result.get("options", []):
        targets = " / ".join(str(target) for target in option["targets"]) + " %"
        if option["targets"] == result.get("current_targets"):
            targets += " (current TOU)"
        rows.extend([(f"{targets} — Grid-to-battery energy (kWh)", option.get("grid_to_battery_kwh")),
                     (f"{targets} — Cumulative saving vs current TOU (EUR)",
                      option.get("cumulative_saving_eur", option.get("saving_eur")))])
    sheet = worksheet(rows).encode("utf-8")
    output = BytesIO()
    with ZipFile(BytesIO(workbook)) as source:
        book = source.read("xl/workbook.xml").decode("utf-8")
        existing = re.search(r'<sheet name="Recommendation" sheetId="(\d+)"', book)
        index = int(existing[1]) if existing else book.count("<sheet ") + 1
        part = f"worksheets/sheet{index}.xml"
        updates = {f"xl/{part}": sheet}
        if existing:
            if source.read(f"xl/{part}") == sheet:
                return workbook
        else:
            updates["xl/workbook.xml"] = book.replace(
                "</sheets>", f'<sheet name="Recommendation" sheetId="{index}" r:id="rId{index}"/></sheets>')
            paths = ("xl/_rels/workbook.xml.rels", "[Content_Types].xml")
            relationships, types = (source.read(path).decode("utf-8") for path in paths)
            updates[paths[0]] = relationships.replace(
                "</Relationships>",
                f'<Relationship Id="rId{index}" Type="{OFFICE_REL}/worksheet" Target="{part}"/></Relationships>')
            updates[paths[1]] = types.replace(
                "</Types>", f'<Override PartName="/xl/{part}" ContentType="{CONTENT_TYPE}.worksheet+xml"/></Types>')
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for name in source.namelist():
                if name not in updates:
                    archive.writestr(ZipInfo(name), source.read(name), compress_type=ZIP_DEFLATED)
            for name, content in updates.items():
                archive.writestr(ZipInfo(name), content, compress_type=ZIP_DEFLATED)
    return output.getvalue()


def collection_workbook(changes, mode, day):
    """Build deterministic XLSX bytes from the same validated JSON being published."""
    summary = [("Field", "Value"), ("Mode", mode), ("Madrid date", day)]
    telemetry = [("Date", "Timestamp UTC", "Measurement", "Value", "Unit")]
    energy = [("Date", "Source", "Measurement", "Value", "Unit")]
    settings = [("Read", "Field path", "Value")]
    sources = [("JSON file", "Field path", "Value")]
    responses = [("Response file", "Endpoint", "Field path", "Value")]
    for path, record in sorted(changes.items()):
        if path.startswith("originals/"):
            responses.extend((path, record["path"], key, value)
                             for key, value in fields(record["payload"]))
            continue
        sources.extend((path, key, value) for key, value in fields(record))
        if path.startswith(("intraday/", "evening/")):
            telemetry.extend((record["date"], sample["timestamp"], sample["key"], sample["value"], sample["unit"])
                             for sample in record["samples"])
        if path.startswith("daily/"):
            for source, measures in sorted(record["energy"].items()):
                energy.extend((record["date"], source, key, value, record["energy_unit"])
                              for key, value in sorted(measures.items()))
        if path.startswith("evening/"):
            summary.extend((key, record[key]) for key in ("partial", "captured_at", "latest_sample_at"))
            for key, sample in sorted(record["latest_soc"].items()):
                summary.extend([(f"{key} (%)", sample["value"]), (f"{key} timestamp UTC", sample["timestamp"])])
        parameters = record if path == "parameters/latest.json" else {}
        if mode == "evening" and path.startswith("evening/"):
            parameters = record["parameters"]
        for name, result in sorted(parameters.items()):
            settings.extend((name, key, value) for key, value in fields(result))

    sheets = [("Summary", summary), ("Telemetry", telemetry)]
    if mode in {"closed", "combined"}:
        sheets.append(("Daily energy", energy))
    sheets.extend([("Settings", settings), ("API responses", responses), ("Result JSON", sources)])
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        def write(name, data):
            entry = ZipInfo(name)
            archive.writestr(entry, data, compress_type=ZIP_DEFLATED)

        sheet_tags, relationships, overrides = [], [], []
        for index, (name, rows) in enumerate(sheets, 1):
            part = f"worksheets/sheet{index}.xml"
            sheet_tags.append(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>')
            relationships.append(f'<Relationship Id="rId{index}" Type="{OFFICE_REL}/worksheet" Target="{part}"/>')
            overrides.append(f'<Override PartName="/xl/{part}" ContentType="{CONTENT_TYPE}.worksheet+xml"/>')
            write(f"xl/{part}", worksheet(rows))
        write("[Content_Types].xml",
                         f'<Types xmlns="{CONTENT_NS}">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         f'<Override PartName="/xl/workbook.xml" ContentType="{CONTENT_TYPE}.sheet.main+xml"/>'
                         f'{"".join(overrides)}</Types>')
        write("_rels/.rels", f'<Relationships xmlns="{PACKAGE_REL}">'
                         f'<Relationship Id="rId1" Type="{OFFICE_REL}/officeDocument" Target="xl/workbook.xml"/>'
                         '</Relationships>')
        write("xl/_rels/workbook.xml.rels",
                         f'<Relationships xmlns="{PACKAGE_REL}">{"".join(relationships)}</Relationships>')
        write("xl/workbook.xml", f'<workbook xmlns="{SHEET_NS}" xmlns:r="{OFFICE_REL}">'
              f'<bookViews><workbookView/></bookViews><sheets>{"".join(sheet_tags)}</sheets></workbook>')
    return output.getvalue()
