"""Create bounded, editable XLSX workbooks from declarative sheet data."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


class SpreadsheetCreationError(ValueError):
    """The requested workbook cannot be represented safely."""


_MAX_SHEETS = 20
_MAX_ROWS_PER_SHEET = 500
_MAX_COLUMNS = 50
_MAX_CELLS = 10_000
_MAX_TEXT = 12_000
_CELL_REF = re.compile(r"^[A-Za-z]{1,3}[1-9][0-9]{0,5}$")
_INVALID_SHEET_CHARS = re.compile(r"[\\/*?:\[\]]")


def create_spreadsheet(destination: Path, sheets: object, *, title: object = None) -> str:
    """Write an XLSX workbook containing tables and optional individual cells.

    Sheet ``columns`` and ``rows`` make a styled Excel table.  ``cells`` can add
    notes or formulas at explicit A1 references.  Formula strings are stored,
    never evaluated by this process.
    """
    if destination.suffix.lower() != ".xlsx":
        raise SpreadsheetCreationError("'path' must end in .xlsx.")
    if not isinstance(sheets, list) or not sheets:
        raise SpreadsheetCreationError("'sheets' must be a non-empty list of sheet objects.")
    if len(sheets) > _MAX_SHEETS:
        raise SpreadsheetCreationError(f"'sheets' may contain at most {_MAX_SHEETS} sheets.")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise SpreadsheetCreationError("'title' must be a non-blank string when supplied.")

    workbook = Workbook()
    workbook.remove(workbook.active)
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    if title is not None:
        workbook.properties.title = title.strip()
    used_cells = 0
    names: set[str] = set()
    for number, spec in enumerate(sheets):
        used_cells += _add_sheet(workbook, spec, number, names)
        if used_cells > _MAX_CELLS:
            raise SpreadsheetCreationError(f"workbook may contain at most {_MAX_CELLS} populated cells.")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(destination)
    except (OSError, ValueError) as error:
        raise SpreadsheetCreationError(f"Could not save {destination.name!r}: {error}") from error
    return f"Created editable XLSX workbook with {len(sheets)} sheets and {used_cells} populated cells at {destination.name}."


def _add_sheet(workbook: Workbook, spec: object, index: int, names: set[str]) -> int:
    if not isinstance(spec, Mapping):
        raise SpreadsheetCreationError(f"sheets[{index}] must be an object.")
    name = _sheet_name(spec.get("name"), index, names)
    names.add(name.casefold())
    sheet = workbook.create_sheet(name)
    columns_object = spec.get("columns")
    rows_object = spec.get("rows", [])
    cells_object = spec.get("cells", [])
    if columns_object is None and rows_object:
        raise SpreadsheetCreationError(f"sheets[{index}].rows requires non-empty 'columns'.")
    columns = ([] if columns_object is None else _strings(
        columns_object, f"sheets[{index}].columns", maximum=_MAX_COLUMNS, nonempty=True
    ))
    if not isinstance(rows_object, list) or len(rows_object) > _MAX_ROWS_PER_SHEET:
        raise SpreadsheetCreationError(f"sheets[{index}].rows must be a list of at most {_MAX_ROWS_PER_SHEET} rows.")
    count = 0
    if columns:
        for column, value in enumerate(columns, 1):
            cell = sheet.cell(1, column, value)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
        count += len(columns)
        for row_number, row in enumerate(rows_object, 2):
            if not isinstance(row, list) or len(row) != len(columns):
                raise SpreadsheetCreationError(f"sheets[{index}].rows[{row_number - 2}] must have {len(columns)} values.")
            for column, value in enumerate(row, 1):
                sheet.cell(row_number, column, _value(value, f"sheets[{index}].rows[{row_number - 2}][{column - 1}]"))
            count += len(row)
        # Excel tables must include at least a header and use unique names.
        reference = f"A1:{get_column_letter(len(columns))}{len(rows_object) + 1}"
        table = Table(displayName=f"Table{index + 1}", ref=reference)
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        sheet.add_table(table)
        sheet.freeze_panes = "A2"
        for col in range(1, len(columns) + 1):
            widest = max(len(str(sheet.cell(row, col).value or "")) for row in range(1, len(rows_object) + 2))
            sheet.column_dimensions[get_column_letter(col)].width = min(max(widest + 2, 10), 40)
    if not isinstance(cells_object, list) or len(cells_object) > _MAX_CELLS:
        raise SpreadsheetCreationError(f"sheets[{index}].cells must be a list of at most {_MAX_CELLS} cell objects.")
    for cell_index, spec_cell in enumerate(cells_object):
        if not isinstance(spec_cell, Mapping):
            raise SpreadsheetCreationError(f"sheets[{index}].cells[{cell_index}] must be an object.")
        reference = spec_cell.get("reference")
        if not isinstance(reference, str) or not _CELL_REF.fullmatch(reference):
            raise SpreadsheetCreationError(f"sheets[{index}].cells[{cell_index}].reference must be an A1 cell reference.")
        if "value" not in spec_cell:
            raise SpreadsheetCreationError(f"sheets[{index}].cells[{cell_index}].value is required.")
        cell = sheet[reference.upper()]
        cell.value = _value(spec_cell["value"], f"sheets[{index}].cells[{cell_index}].value")
        format_value = spec_cell.get("number_format")
        if format_value is not None:
            if not isinstance(format_value, str) or not format_value or len(format_value) > 100:
                raise SpreadsheetCreationError(f"sheets[{index}].cells[{cell_index}].number_format must be a non-blank string of at most 100 characters.")
            cell.number_format = format_value
        count += 1
    return count


def _sheet_name(value: object, index: int, names: set[str]) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 31 or _INVALID_SHEET_CHARS.search(value):
        raise SpreadsheetCreationError(f"sheets[{index}].name must be a non-blank Excel sheet name of at most 31 characters.")
    name = value.strip()
    if name.casefold() in names:
        raise SpreadsheetCreationError(f"sheets[{index}].name duplicates an earlier sheet name.")
    return name


def _strings(value: object, field: str, *, maximum: int, nonempty: bool) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or len(value) > maximum:
        qualifier = "a non-empty list" if nonempty else "a list"
        raise SpreadsheetCreationError(f"{field} must be {qualifier} of at most {maximum} strings.")
    result: list[str] = []
    for number, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or len(item) > _MAX_TEXT:
            raise SpreadsheetCreationError(f"{field}[{number}] must be a non-blank string of at most {_MAX_TEXT} characters.")
        result.append(item.strip())
    return result


def _value(value: object, field: str) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > _MAX_TEXT:
            raise SpreadsheetCreationError(f"{field} may contain at most {_MAX_TEXT} characters.")
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise SpreadsheetCreationError(f"{field} cannot be NaN or infinite.")
        return value
    raise SpreadsheetCreationError(f"{field} must be a string, number, boolean, or null.")
