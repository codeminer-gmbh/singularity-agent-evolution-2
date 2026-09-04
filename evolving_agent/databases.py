"""Bounded, read-only inspection of SQLite databases supplied to a task."""

from __future__ import annotations

import sqlite3
from pathlib import Path

MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_QUERY_ROWS = 1_000
MAX_RESULT_CHARACTERS = 120_000
_MAX_CELL_CHARACTERS = 4_000


class DatabaseError(Exception):
    """A database cannot be safely queried."""


def query_sqlite(path: Path, query: str, *, max_rows: int = 200) -> str:
    """Run one bounded read-only SQLite query and render its rows as TSV."""
    if not path.is_file():
        raise DatabaseError(f"{path.name!r} is not a database file.")
    if path.stat().st_size > MAX_DATABASE_BYTES:
        raise DatabaseError(f"{path.name!r} exceeds the {MAX_DATABASE_BYTES}-byte database limit.")
    if not isinstance(query, str) or not query.strip():
        raise DatabaseError("query must be a non-empty SQL string.")
    if (
        not isinstance(max_rows, int)
        or isinstance(max_rows, bool)
        or not 1 <= max_rows <= MAX_QUERY_ROWS
    ):
        raise DatabaseError(f"max_rows must be an integer from 1 through {MAX_QUERY_ROWS}.")
    # This inexpensive gate gives useful feedback; mode=ro and the authorizer
    # below provide the actual protection against mutation and extension loading.
    statement = query.lstrip().upper()
    if not statement.startswith(("SELECT", "WITH", "EXPLAIN")):
        raise DatabaseError("Only SELECT, WITH, and EXPLAIN queries are allowed.")
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        connection.set_authorizer(_read_only_authorizer)
        cursor = connection.execute(query)
        if cursor.description is None:
            raise DatabaseError("The query did not return rows.")
        headers = [column[0] for column in cursor.description]
        lines = ["\t".join(_cell(value) for value in headers)]
        for rows, row in enumerate(cursor):
            if rows >= max_rows:
                lines.append(f"... [truncated at {max_rows} rows]")
                break
            lines.append("\t".join(_cell(value) for value in row))
        text = "\n".join(lines)
        if len(text) > MAX_RESULT_CHARACTERS:
            return (
                text[:MAX_RESULT_CHARACTERS]
                + f"\n... [truncated at {MAX_RESULT_CHARACTERS} characters]"
            )
        return text
    except DatabaseError:
        raise
    except sqlite3.Error as error:
        raise DatabaseError(f"Could not query SQLite database {path.name!r}: {error}") from error
    finally:
        if "connection" in locals():
            connection.close()


def _read_only_authorizer(
    action: int, arg1: str | None, arg2: str | None, database: str | None, trigger: str | None
) -> int:
    """Allow the VM operations needed to evaluate ordinary read-only queries."""
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def _cell(value: object) -> str:
    """Make a scalar safe and legible in a TSV response."""
    if value is None:
        text = ""
    elif isinstance(value, bytes):
        text = f"<BLOB {len(value)} bytes>"
    else:
        text = str(value)
    text = text.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
    return text[:_MAX_CELL_CHARACTERS] + ("…" if len(text) > _MAX_CELL_CHARACTERS else "")
