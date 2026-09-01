"""Bounded, dependency-free inspection of common spreadsheet inputs.

This deliberately reads values rather than executing workbook macros, formulas, or
external links.  It supports the ZIP/XML spreadsheet formats a task is most
likely to provide without making an office suite part of the agent image.
"""

from __future__ import annotations

import csv
import io
import json
import posixpath
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path
from xml.etree import ElementTree as ET

_MAX_INPUT_BYTES = 8_000_000
_MAX_UNZIPPED_BYTES = 32_000_000
_MAX_ZIP_MEMBERS = 2_000
_MAX_CELL_TEXT = 1_000
_CELL_REFERENCE = re.compile(r"([A-Z]+)([0-9]+)$")


class SpreadsheetError(ValueError):
    """The requested workbook is unsupported, malformed, or too large."""


def inspect_spreadsheet(
    path: Path, *, sheet: str | None = None, max_rows: int = 100, max_columns: int = 30
) -> str:
    """Return a compact JSON report containing a bounded rectangular preview."""
    if not 1 <= max_rows <= 500:
        raise SpreadsheetError("max_rows must be between 1 and 500.")
    if not 1 <= max_columns <= 100:
        raise SpreadsheetError("max_columns must be between 1 and 100.")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise SpreadsheetError(f"could not inspect {path.name!r}: {error}") from error
    if size > _MAX_INPUT_BYTES:
        raise SpreadsheetError(f"{path.name!r} is over the 8 MB inspection limit.")
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv", ".txt"}:
        name = path.stem or "data"
        if sheet and sheet != name:
            raise SpreadsheetError(f"text tables have one sheet named {name!r}.")
        rows = _csv_rows(path, "\t" if suffix == ".tsv" else None)
        return _report("text", [name], name, rows, max_rows, max_columns)
    if suffix == ".xlsx":
        sheets = _xlsx_sheets(path, max_rows + 1, max_columns + 1)
    elif suffix == ".ods":
        sheets = _ods_sheets(path, max_rows + 1, max_columns + 1)
    elif suffix == ".xls":
        raise SpreadsheetError("legacy .xls is not supported; save it as .xlsx or .csv.")
    else:
        raise SpreadsheetError("supported spreadsheet formats are .xlsx, .ods, .csv, and .tsv.")
    names = list(sheets)
    if not names:
        raise SpreadsheetError("the workbook contains no readable sheets.")
    selected = sheet or names[0]
    if selected not in sheets:
        raise SpreadsheetError(f"sheet {selected!r} was not found; available sheets: {', '.join(names)}.")
    return _report(suffix[1:], names, selected, sheets[selected], max_rows, max_columns)


def _report(format_name: str, names: list[str], selected: str, rows: Iterable[list[str]], max_rows: int, max_columns: int) -> str:
    preview: list[list[str]] = []
    extra_rows = False
    extra_columns = False
    for row in rows:
        if len(preview) >= max_rows:
            extra_rows = True
            break
        extra_columns = extra_columns or len(row) > max_columns
        preview.append([_limit(value) for value in row[:max_columns]])
    widest = max((len(row) for row in preview), default=0)
    for row in preview:
        row.extend([""] * (widest - len(row)))
    return json.dumps({
        "format": format_name,
        "sheets": names,
        "selected_sheet": selected,
        "preview": preview,
        "preview_rows": len(preview),
        "preview_columns": widest,
        "truncated": extra_rows or extra_columns,
        "note": "Values are displayed as stored; formulas are never executed.",
    }, ensure_ascii=False, indent=2)


def _csv_rows(path: Path, delimiter: str | None) -> Iterable[list[str]]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as error:
        raise SpreadsheetError(f"could not read text table: {error}") from error
    if delimiter is None:
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
    else:
        dialect = csv.excel_tab
    return csv.reader(io.StringIO(text), dialect)


def _safe_zip(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile) as error:
        raise SpreadsheetError(f"invalid workbook archive: {error}") from error
    if len(infos) > _MAX_ZIP_MEMBERS or sum(info.file_size for info in infos) > _MAX_UNZIPPED_BYTES:
        archive.close()
        raise SpreadsheetError("workbook archive exceeds the safe inspection limit.")
    return archive


def _xml(archive: zipfile.ZipFile, member: str) -> ET.Element:
    try:
        with archive.open(member) as source:
            return ET.parse(source).getroot()
    except (KeyError, ET.ParseError, OSError) as error:
        raise SpreadsheetError(f"malformed workbook XML {member!r}: {error}") from error


def _xlsx_sheets(path: Path, row_limit: int, column_limit: int) -> dict[str, list[list[str]]]:
    with _safe_zip(path) as archive:
        workbook = _xml(archive, "xl/workbook.xml")
        rels = _xml(archive, "xl/_rels/workbook.xml.rels")
        relationship = {item.attrib.get("Id"): item.attrib.get("Target", "") for item in rels}
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(node.itertext()) for node in _xml(archive, "xl/sharedStrings.xml")]
        result: dict[str, list[list[str]]] = {}
        for node in workbook.iter():
            if node.tag.rsplit("}", 1)[-1] != "sheet":
                continue
            name = node.attrib.get("name", "Unnamed")
            rel_id = next((value for key, value in node.attrib.items() if key.endswith("}id")), "")
            target = relationship.get(rel_id, "")
            # Relationship targets are relative to xl/workbook.xml, unless rooted.
            # Do not let a malformed target address an arbitrary ZIP member.
            member = (target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target)))
            if not member.startswith("xl/") or member == "xl/":
                raise SpreadsheetError(f"workbook relationship has unsafe sheet target {target!r}.")
            result[name] = _xlsx_rows(_xml(archive, member), shared, row_limit, column_limit)
        return result


def _xlsx_rows(
    root: ET.Element, shared: list[str], row_limit: int, column_limit: int
) -> list[list[str]]:
    cells: dict[tuple[int, int], str] = {}
    max_row = max_col = 0
    for cell in root.iter():
        if cell.tag.rsplit("}", 1)[-1] != "c":
            continue
        match = _CELL_REFERENCE.match(cell.attrib.get("r", ""))
        if not match:
            continue
        col, row = _column_number(match.group(1)), int(match.group(2))
        kind = cell.attrib.get("t")
        formula = next(("".join(x.itertext()) for x in cell if x.tag.rsplit("}", 1)[-1] == "f"), "")
        value = next(("".join(x.itertext()) for x in cell if x.tag.rsplit("}", 1)[-1] == "v"), "")
        if kind == "s" and value.isdigit() and int(value) < len(shared):
            value = shared[int(value)]
        elif kind == "inlineStr":
            value = "".join("".join(x.itertext()) for x in cell if x.tag.rsplit("}", 1)[-1] == "is")
        if formula:
            value = f"={formula}" + (f" → {value}" if value else "")
        # A sparse cell can legally name row 1,048,576 or column XFD.  Keep a
        # one-row/column lookahead for the report's truncation flag, rather
        # than allocating the rectangular range implied by its reference.
        max_row, max_col = max(max_row, min(row, row_limit)), max(max_col, min(col, column_limit))
        if row <= row_limit and col <= column_limit:
            cells[(row, col)] = value
    return [
        [cells.get((row, col), "") for col in range(1, max_col + 1)]
        for row in range(1, max_row + 1)
    ]


def _column_number(letters: str) -> int:
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - ord("A") + 1
    return value


def _ods_sheets(path: Path, row_limit: int, column_limit: int) -> dict[str, list[list[str]]]:
    with _safe_zip(path) as archive:
        root = _xml(archive, "content.xml")
    result: dict[str, list[list[str]]] = {}
    for table in root.iter():
        if table.tag.rsplit("}", 1)[-1] != "table":
            continue
        name = next((value for key, value in table.attrib.items() if key.endswith("}name")), "Unnamed")
        rows: list[list[str]] = []
        for row in table:
            if row.tag.rsplit("}", 1)[-1] != "table-row":
                continue
            values: list[str] = []
            for cell in row:
                local = cell.tag.rsplit("}", 1)[-1]
                if local not in {"table-cell", "covered-table-cell"}:
                    continue
                repeated = _repeat_count(cell, "number-columns-repeated")
                value = next((v for k, v in cell.attrib.items() if k.endswith("}value")), "".join(cell.itertext()))
                values.extend([value] * min(repeated, max(0, column_limit - len(values))))
            repeated_rows = _repeat_count(row, "number-rows-repeated")
            rows.extend([values] * min(repeated_rows, max(0, row_limit - len(rows))))
        result[name] = rows
    return result


def _repeat_count(element: ET.Element, attribute: str) -> int:
    value = next((v for k, v in element.attrib.items() if k.endswith("}" + attribute)), "1")
    try:
        return max(1, int(value))
    except ValueError as error:
        raise SpreadsheetError(f"invalid ODS {attribute!r}: {value!r}.") from error


def _limit(value: str) -> str:
    return value if len(value) <= _MAX_CELL_TEXT else value[:_MAX_CELL_TEXT] + "… [cell truncated]"
