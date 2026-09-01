"""Bounded inspection of Apache Parquet task data.

The implementation deliberately reads record batches rather than materialising a
whole file.  That makes a preview useful even when a task hands the agent a
multi-gigabyte analytical export.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow.dataset as ds
import pyarrow.parquet as pq

MAX_PARQUET_FILE_BYTES = 512 * 1024 * 1024
MAX_PARQUET_ROWS = 1_000
MAX_PARQUET_OFFSET = 1_000_000
MAX_PARQUET_COLUMNS = 200
MAX_PARQUET_TEXT_CHARACTERS = 120_000


class ParquetError(Exception):
    """A Parquet file or its requested preview could not be read."""


def inspect_parquet(
    path: Path,
    *,
    columns: Sequence[str] | None = None,
    filters: Sequence[Mapping[str, Any]] | None = None,
    max_rows: int = 200,
    offset: int = 0,
) -> str:
    """Return schema, metadata and a TSV preview, optionally predicate-filtered."""
    if not path.is_file():
        raise ParquetError(f"{path.name!r} is not a Parquet file.")
    if path.stat().st_size > MAX_PARQUET_FILE_BYTES:
        raise ParquetError(f"The file exceeds the {MAX_PARQUET_FILE_BYTES}-byte Parquet limit.")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_PARQUET_ROWS:
        raise ParquetError(f"max_rows must be a whole number from 1 to {MAX_PARQUET_ROWS}.")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_PARQUET_OFFSET:
        raise ParquetError(f"offset must be a whole number from 0 to {MAX_PARQUET_OFFSET}.")
    try:
        parquet = pq.ParquetFile(path)
        dataset = ds.dataset(path, format="parquet")
        requested = _columns(columns, dataset.schema.names)
        predicate = _predicate(filters, dataset.schema.names)
        scanner = dataset.scanner(columns=requested, filter=predicate, batch_size=min(1024, max_rows + offset))
        rows: list[dict[str, Any]] = []
        skipped = 0
        for batch in scanner.to_batches():
            for row in batch.to_pylist():
                if skipped < offset:
                    skipped += 1
                    continue
                rows.append(row)
                if len(rows) >= max_rows:
                    break
            if len(rows) >= max_rows:
                break
    except (OSError, ValueError, TypeError, ArithmeticError) as error:
        raise ParquetError(f"Could not read Parquet data: {error}") from error
    metadata = parquet.metadata
    schema = ", ".join(f"{field.name}: {field.type}" for field in dataset.schema)
    lines = [
        f"Schema: {schema}",
        f"Metadata: {metadata.num_rows} rows, {metadata.num_row_groups} row groups, {len(dataset.schema)} columns.",
        f"Preview: {len(rows)} rows" + (f" after offset {offset}." if offset else "."),
    ]
    names = requested or dataset.schema.names
    lines.append("\t".join(names))
    lines.extend("\t".join(_cell(row.get(name)) for name in names) for row in rows)
    result = "\n".join(lines)
    if len(result) > MAX_PARQUET_TEXT_CHARACTERS:
        return result[:MAX_PARQUET_TEXT_CHARACTERS] + f"\n... [truncated at {MAX_PARQUET_TEXT_CHARACTERS} characters]"
    return result


def _columns(value: Sequence[str] | None, available: Sequence[str]) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value or len(value) > MAX_PARQUET_COLUMNS:
        raise ParquetError(f"columns must be a non-empty list of at most {MAX_PARQUET_COLUMNS} column names.")
    names: list[str] = []
    for name in value:
        if not isinstance(name, str) or not name or name not in available:
            raise ParquetError(f"Unknown Parquet column {name!r}.")
        if name in names:
            raise ParquetError(f"Column {name!r} was requested more than once.")
        names.append(name)
    return names


def _predicate(value: Sequence[Mapping[str, Any]] | None, available: Sequence[str]) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) > 20:
        raise ParquetError("filters must be a list of at most 20 filter objects.")
    expression = None
    operations = {"eq", "ne", "lt", "le", "gt", "ge", "in", "is_null"}
    for item in value:
        if not isinstance(item, Mapping):
            raise ParquetError("Every filter must be an object.")
        column, operation = item.get("column"), item.get("op")
        if not isinstance(column, str) or column not in available or operation not in operations:
            raise ParquetError("Each filter needs an existing column and op: eq, ne, lt, le, gt, ge, in, or is_null.")
        field = ds.field(column)
        if operation == "is_null":
            part = field.is_null()
        else:
            operand = item.get("value")
            if operation == "in":
                if not isinstance(operand, list) or not operand or len(operand) > 100:
                    raise ParquetError("An 'in' filter needs a non-empty value list of at most 100 items.")
                part = field.isin(operand)
            elif operation == "eq": part = field == operand
            elif operation == "ne": part = field != operand
            elif operation == "lt": part = field < operand
            elif operation == "le": part = field <= operand
            elif operation == "gt": part = field > operand
            else: part = field >= operand
        expression = part if expression is None else expression & part
    return expression


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    text = str(value)
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
