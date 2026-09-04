"""Streaming previews for CSV and other delimiter-separated task data."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

MAX_DELIMITED_BYTES = 1024 * 1024 * 1024
MAX_DELIMITED_ROWS = 1_000
MAX_DELIMITED_OFFSET = 1_000_000
MAX_DELIMITED_TEXT_CHARACTERS = 120_000
_SAMPLE_BYTES = 64 * 1024


class DelimitedError(Exception):
    """A delimited text source could not be inspected."""


def inspect_delimited(path: Path, *, max_rows: int = 200, offset: int = 0) -> str:
    """Return detected format, columns, inferred types, and a bounded row preview.

    The file is iterated rather than loaded, so a preview remains useful for
    datasets much larger than memory.  ``offset`` counts data records, not the
    header record.
    """
    if not path.is_file():
        raise DelimitedError(f"{path.name!r} is not a delimited data file.")
    if path.stat().st_size > MAX_DELIMITED_BYTES:
        raise DelimitedError(
            f"The file exceeds the {MAX_DELIMITED_BYTES}-byte delimited-data limit."
        )
    if (
        isinstance(max_rows, bool)
        or not isinstance(max_rows, int)
        or not 1 <= max_rows <= MAX_DELIMITED_ROWS
    ):
        raise DelimitedError(f"max_rows must be a whole number from 1 to {MAX_DELIMITED_ROWS}.")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= MAX_DELIMITED_OFFSET
    ):
        raise DelimitedError(f"offset must be a whole number from 0 to {MAX_DELIMITED_OFFSET}.")
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as source:
            sample = source.read(_SAMPLE_BYTES)
            source.seek(0)
            dialect = _dialect(sample, path.suffix.lower())
            reader = csv.reader(source, dialect)
            header = next(reader, None)
            if header is None:
                raise DelimitedError("The delimited data file is empty.")
            columns = _columns(header)
            for _ in range(offset):
                if next(reader, None) is None:
                    break
            rows = []
            for row in reader:
                rows.append(_record(columns, row))
                if len(rows) >= max_rows:
                    break
    except (OSError, csv.Error, UnicodeError) as error:
        raise DelimitedError(f"Could not read delimited data: {error}") from error
    types = {name: _infer_type([str(row.get(name, "")) for row in rows]) for name in columns}
    result: dict[str, Any] = {
        "format": "delimiter-separated text",
        "delimiter": dialect.delimiter,
        "quotechar": dialect.quotechar,
        "columns": columns,
        "inferred_types_from_preview": types,
        "offset": offset,
        "returned_rows": len(rows),
        "rows": rows,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if len(text) > MAX_DELIMITED_TEXT_CHARACTERS:
        return (
            text[:MAX_DELIMITED_TEXT_CHARACTERS]
            + f"\n... [truncated at {MAX_DELIMITED_TEXT_CHARACTERS} characters]"
        )
    return text


def _dialect(sample: str, suffix: str) -> type[csv.Dialect]:
    if suffix in {".tsv", ".tab"}:
        return csv.excel_tab
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;|\t")
    except csv.Error:
        return csv.excel


def _columns(header: list[str]) -> list[str]:
    """Give blank/duplicate headers stable usable names without losing order."""
    used: dict[str, int] = {}
    result = []
    for index, value in enumerate(header, 1):
        base = value.strip() or f"column_{index}"
        used[base] = used.get(base, 0) + 1
        result.append(base if used[base] == 1 else f"{base}_{used[base]}")
    return result


def _record(columns: list[str], values: list[str]) -> dict[str, str | list[str]]:
    record: dict[str, str | list[str]] = {
        column: values[index] if index < len(values) else "" for index, column in enumerate(columns)
    }
    if len(values) > len(columns):
        record["__extra_fields__"] = values[len(columns) :]
    return record


def _infer_type(values: list[str]) -> str:
    meaningful = [value.strip() for value in values if value.strip()]
    if not meaningful:
        return "null"
    if all(_boolean(value) for value in meaningful):
        return "boolean"
    if all(_integer(value) for value in meaningful):
        return "integer"
    if all(_number(value) for value in meaningful):
        return "number"
    return "string"


def _boolean(value: str) -> bool:
    return value.lower() in {"true", "false", "yes", "no"}


def _integer(value: str) -> bool:
    try:
        int(value)
        return not any(character in value.lower() for character in (".", "e"))
    except ValueError:
        return False


def _number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False
