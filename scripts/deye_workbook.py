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
        parameters = record if path == "parameters/latest.json" else record.get("parameters", {})
        for name, result in sorted(parameters.items()):
            settings.extend((name, key, value) for key, value in fields(result))

    sheets = [("Summary", summary), ("Telemetry", telemetry)]
    if mode == "closed":
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
