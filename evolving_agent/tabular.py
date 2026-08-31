"""Bounded, read-only inspection of CSV, TSV, JSON, JSONL and NDJSON evidence."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import duckdb

MAX_ROWS = 1_000
DEFAULT_ROWS = 200
MAX_COLUMNS = 500
MAX_CELL_CHARACTERS = 1_000
MAX_OUTPUT_CHARACTERS = 24_000
QUERY_SECONDS = 8.0

_FORBIDDEN = re.compile(
    r"\b(?:attach|copy|create|delete|drop|export|import|insert|install|load|"
    r"merge|pragma|replace|update|vacuum|call|read_[a-z0-9_]*|parquet_[a-z0-9_]*|"
    r"csv_scan|glob|httpfs|query_table|sqlite_scan)\b", re.IGNORECASE,
)
_FORMATS = {".csv": "csv", ".tsv": "tsv", ".json": "json", ".jsonl": "json", ".ndjson": "json"}


class TabularError(Exception):
    """A tabular attachment could not be safely inspected."""


def inspect_tabular(path: Path, query: str | None = None, max_rows: int = DEFAULT_ROWS) -> str:
    """Describe a supported table, optionally executing bounded evidence SQL.

    DuckDB relation methods receive the attachment path as a value, not SQL.
    The resulting sole ``data`` view is the only source queries may address.
    """
    if not 1 <= max_rows <= MAX_ROWS:
        raise TabularError(f"'max_rows' must be between 1 and {MAX_ROWS}.")
    if not path.is_file():
        raise TabularError("The tabular path is not a file.")
    kind = _FORMATS.get(path.suffix.lower())
    if kind is None:
        raise TabularError("Supported tabular formats are CSV, TSV, JSON, JSONL, and NDJSON.")
    connection: duckdb.DuckDBPyConnection | None = None
    try:
        connection = duckdb.connect(database=":memory:")
        if kind == "tsv":
            relation = connection.read_csv(str(path), delimiter="\t", header=True, auto_detect=True)
        elif kind == "csv":
            relation = connection.from_csv_auto(str(path), header=True)
        else:
            relation = connection.read_json(str(path), format="auto")
        relation.create_view("data", replace=True)
        schema = [
            {"name": row[0], "type": row[1], "nullable": str(row[2]).upper() != "NO"}
            for row in connection.execute("DESCRIBE data").fetchall()[:MAX_COLUMNS]
        ]
        result: dict[str, Any] = {"file": path.name, "format": kind, "schema": schema}
        if query is None:
            return _render(result)
        _check_query(query)
        rows, columns, truncated = _query(connection, query, max_rows)
        result.update({"query": query, "columns": columns, "rows": rows, "truncated": truncated})
        return _render(result)
    except duckdb.Error as error:
        raise TabularError(f"Could not inspect tabular file: {error}") from error
    finally:
        if connection is not None:
            connection.close()


def _check_query(query: str) -> None:
    compact = query.strip()
    if not compact:
        raise TabularError("'query' must not be empty when supplied.")
    first = compact.split(None, 1)[0].upper()
    if first not in {"SELECT", "WITH", "EXPLAIN"} or ";" in compact:
        raise TabularError("Only one read-only SELECT, WITH, or EXPLAIN query against data is allowed.")
    if _FORBIDDEN.search(compact):
        raise TabularError("The query may only inspect the supplied data view; external sources and state-changing SQL are not allowed.")


def _query(connection: duckdb.DuckDBPyConnection, query: str, max_rows: int) -> tuple[list[list[Any]], list[str], bool]:
    started = time.monotonic()
    cursor = connection.execute(query)
    if time.monotonic() - started > QUERY_SECONDS:
        raise TabularError(f"Query exceeded the {QUERY_SECONDS:g}-second inspection limit.")
    if cursor.description is None:
        raise TabularError("The query did not produce rows; use a SELECT query.")
    fetched = cursor.fetchmany(max_rows + 1)
    if time.monotonic() - started > QUERY_SECONDS:
        raise TabularError(f"Query exceeded the {QUERY_SECONDS:g}-second inspection limit.")
    return [[_value(v) for v in row] for row in fetched[:max_rows]], [item[0] for item in cursor.description], len(fetched) > max_rows


def _value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"binary_bytes": len(value), "preview_hex": value[:32].hex()}
    if isinstance(value, str):
        return _shorten(value, MAX_CELL_CHARACTERS)
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    return value


def _shorten(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + f"… [truncated at {limit} characters]"


def _render(value: dict[str, Any]) -> str:
    return _shorten(json.dumps(value, ensure_ascii=False, indent=2, default=str), MAX_OUTPUT_CHARACTERS)
