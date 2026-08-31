"""Bounded, read-only inspection of SQLite evidence attachments."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

MAX_SCHEMA_OBJECTS = 100
MAX_ROWS = 1_000
DEFAULT_ROWS = 200
MAX_CELL_CHARACTERS = 1_000
MAX_OUTPUT_CHARACTERS = 24_000
QUERY_SECONDS = 8.0


class DatabaseError(Exception):
    """A database could not be safely inspected."""


def inspect_database(path: Path, query: str | None = None, max_rows: int = DEFAULT_ROWS) -> str:
    """Describe a SQLite database, optionally returning bounded SELECT results.

    The database is opened using SQLite's read-only immutable mode.  Querying is
    additionally protected by an authorizer and a progress deadline, so a task
    attachment cannot mutate files or occupy an unbounded model turn.
    """
    if not 1 <= max_rows <= MAX_ROWS:
        raise DatabaseError(f"'max_rows' must be between 1 and {MAX_ROWS}.")
    if not path.is_file():
        raise DatabaseError("The database path is not a file.")
    try:
        with path.open("rb") as source:
            header = source.read(16)
    except OSError as error:
        raise DatabaseError(f"Could not read database: {error}") from error
    if header != b"SQLite format 3\x00":
        raise DatabaseError("This is not a SQLite 3 database.")

    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            schema = _schema(connection)
            if query is None:
                return _render({"database": path.name, "schema": schema})
            _check_query(query)
            rows, columns, truncated = _query(connection, query, max_rows)
            return _render({
                "database": path.name,
                "schema": schema,
                "query": query,
                "columns": columns,
                "rows": rows,
                "truncated": truncated,
            })
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise DatabaseError(f"Could not inspect SQLite database: {error}") from error


def _schema(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    objects = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name LIMIT ?", (MAX_SCHEMA_OBJECTS,)
    ).fetchall()
    result: list[dict[str, Any]] = []
    for kind, name, sql in objects:
        columns = connection.execute(f"PRAGMA table_info({_quote_identifier(name)})").fetchall()
        result.append({
            "type": kind,
            "name": name,
            "columns": [
                {"name": column[1], "type": column[2], "not_null": bool(column[3]), "primary_key": bool(column[5])}
                for column in columns
            ],
            "sql": _shorten(sql or "", MAX_CELL_CHARACTERS),
        })
    return result


def _query(connection: sqlite3.Connection, query: str, max_rows: int) -> tuple[list[list[Any]], list[str], bool]:
    deadline = time.monotonic() + QUERY_SECONDS
    connection.set_authorizer(_read_only_authorizer)
    connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cursor = connection.execute(query)
        if cursor.description is None:
            raise DatabaseError("The query did not produce rows; use a SELECT query.")
        fetched = cursor.fetchmany(max_rows + 1)
        columns = [item[0] for item in cursor.description]
    except sqlite3.OperationalError as error:
        if "interrupted" in str(error).lower():
            raise DatabaseError(f"Query exceeded the {QUERY_SECONDS:g}-second inspection limit.") from error
        raise
    finally:
        connection.set_authorizer(None)
        connection.set_progress_handler(None, 0)
    return [[_value(value) for value in row] for row in fetched[:max_rows]], columns, len(fetched) > max_rows


def _read_only_authorizer(action: int, arg1: str | None, arg2: str | None, database: str | None, source: str | None) -> int:
    del arg1, arg2, database, source
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def _check_query(query: str) -> None:
    compact = query.strip()
    if not compact:
        raise DatabaseError("'query' must not be empty when supplied.")
    # sqlite3.execute rejects multiple statements.  This early check gives the
    # model a useful correction instead of attempting obvious non-read queries.
    first = compact.split(None, 1)[0].upper()
    if first not in {"SELECT", "WITH", "EXPLAIN"}:
        raise DatabaseError("Only a single read-only SELECT, WITH, or EXPLAIN query is allowed.")


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"binary_bytes": len(value), "preview_hex": value[:32].hex()}
    if isinstance(value, str):
        return _shorten(value, MAX_CELL_CHARACTERS)
    return value


def _shorten(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + f"… [truncated at {limit} characters]"


def _render(value: dict[str, Any]) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return _shorten(text, MAX_OUTPUT_CHARACTERS)
