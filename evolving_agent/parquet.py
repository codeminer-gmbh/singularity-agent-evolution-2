"""Bounded, read-only inspection of Parquet evidence attachments.

DuckDB reads Parquet directly without materializing it in the agent workspace.
Queries deliberately address the attachment through the single ``data`` view;
this keeps useful SQL aggregation and filtering while refusing SQL constructs
that could name unrelated local or remote files.
"""

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

# These tokens either write state or introduce another file/network source.
# The attachment is exposed as ``data``, so none are needed for evidence SQL.
_FORBIDDEN = re.compile(
    r"\b(?:attach|copy|create|delete|drop|export|import|insert|install|load|"
    r"merge|pragma|replace|update|vacuum|call|read_[a-z0-9_]*|parquet_[a-z0-9_]*|"
    r"csv_scan|glob|httpfs|query_table|sqlite_scan)\b",
    re.IGNORECASE,
)


class ParquetError(Exception):
    """A Parquet attachment could not be safely inspected."""


def inspect_parquet(path: Path, query: str | None = None, max_rows: int = DEFAULT_ROWS) -> str:
    """Describe one Parquet file, optionally running bounded SQL against ``data``.

    The file path is supplied only to DuckDB's relation API, never interpolated
    into model-provided SQL. Query text may select, join CTEs, filter, and
    aggregate the ``data`` view, but cannot open another source or mutate state.
    """
    if not 1 <= max_rows <= MAX_ROWS:
        raise ParquetError(f"'max_rows' must be between 1 and {MAX_ROWS}.")
    if not path.is_file():
        raise ParquetError("The Parquet path is not a file.")
    # The magic bytes cheaply distinguish an accidental CSV/JSON attachment and
    # produces a useful error before DuckDB's more implementation-specific one.
    try:
        with path.open("rb") as source:
            if source.read(4) != b"PAR1":
                raise ParquetError("This is not a Parquet file (missing PAR1 header).")
    except OSError as error:
        raise ParquetError(f"Could not read Parquet file: {error}") from error

    connection: duckdb.DuckDBPyConnection | None = None
    try:
        connection = duckdb.connect(database=":memory:")
        # from_parquet accepts a path value rather than SQL text, avoiding quote
        # handling and injection through unusual but valid filenames.
        connection.from_parquet(str(path)).create_view("data", replace=True)
        schema_rows = connection.execute("DESCRIBE data").fetchall()
        schema = [
            {"name": row[0], "type": row[1], "nullable": str(row[2]).upper() != "NO"}
            for row in schema_rows[:MAX_COLUMNS]
        ]
        if query is None:
            return _render({"parquet": path.name, "schema": schema})
        _check_query(query)
        rows, columns, truncated = _query(connection, query, max_rows)
        return _render({
            "parquet": path.name,
            "schema": schema,
            "query": query,
            "columns": columns,
            "rows": rows,
            "truncated": truncated,
        })
    except duckdb.Error as error:
        raise ParquetError(f"Could not inspect Parquet file: {error}") from error
    finally:
        if connection is not None:
            connection.close()


def _check_query(query: str) -> None:
    compact = query.strip()
    if not compact:
        raise ParquetError("'query' must not be empty when supplied.")
    first = compact.split(None, 1)[0].upper()
    if first not in {"SELECT", "WITH", "EXPLAIN"} or ";" in compact:
        raise ParquetError("Only one read-only SELECT, WITH, or EXPLAIN query against data is allowed.")
    if _FORBIDDEN.search(compact):
        raise ParquetError("The query may only inspect the supplied data view; external sources and state-changing SQL are not allowed.")


def _query(connection: duckdb.DuckDBPyConnection, query: str, max_rows: int) -> tuple[list[list[Any]], list[str], bool]:
    started = time.monotonic()
    cursor = connection.execute(query)
    if time.monotonic() - started > QUERY_SECONDS:
        raise ParquetError(f"Query exceeded the {QUERY_SECONDS:g}-second inspection limit.")
    if cursor.description is None:
        raise ParquetError("The query did not produce rows; use a SELECT query.")
    fetched = cursor.fetchmany(max_rows + 1)
    if time.monotonic() - started > QUERY_SECONDS:
        raise ParquetError(f"Query exceeded the {QUERY_SECONDS:g}-second inspection limit.")
    return [[_value(value) for value in row] for row in fetched[:max_rows]], [item[0] for item in cursor.description], len(fetched) > max_rows


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
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return _shorten(text, MAX_OUTPUT_CHARACTERS)
