"""Bounded, streaming inspection of JSON arrays and JSON Lines datasets.

JSON is a frequent interchange format, but using ``json.load`` on a task export
can consume the agent's entire memory.  This module uses ijson for ordinary
JSON arrays and reads JSON Lines one record at a time.  A JSON Pointer can name
an array nested in a JSON document without materialising its enclosing object.
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import ijson

MAX_JSON_BYTES = 1024 * 1024 * 1024
MAX_JSON_ROWS = 1_000
MAX_JSON_OFFSET = 1_000_000
MAX_JSON_TEXT_CHARACTERS = 120_000
MAX_JSON_RECORD_BYTES = 4 * 1024 * 1024
# A gzip member can be far larger than its compressed file.  Keep inspection
# bounded by the same limit after decompression as well.
MAX_JSON_UNCOMPRESSED_BYTES = MAX_JSON_BYTES


class JsonDataError(Exception):
    """A JSON dataset could not be inspected."""


def inspect_json(path: Path, *, pointer: str = "", max_rows: int = 200, offset: int = 0) -> str:
    """Return a typed preview of a JSON array or JSON Lines dataset.

    ``pointer`` is an RFC 6901 JSON Pointer to an array inside a conventional
    JSON document (for example ``/results/items``).  It is not needed for a
    root array.  JSON Lines/NDJSON is always treated as one value per line.
    """
    if not path.is_file():
        raise JsonDataError(f"{path.name!r} is not a JSON data file.")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise JsonDataError(f"The file exceeds the {MAX_JSON_BYTES}-byte JSON-data limit.")
    _bound("max_rows", max_rows, 1, MAX_JSON_ROWS)
    _bound("offset", offset, 0, MAX_JSON_OFFSET)
    if not isinstance(pointer, str):
        raise JsonDataError("pointer must be a string JSON Pointer.")
    if _is_json_lines(path):
        if pointer:
            raise JsonDataError("pointer is only supported for a JSON document, not JSON Lines.")
        rows = list(_json_lines(path, offset, max_rows))
        source_format = "JSON Lines"
        selection = "one JSON value per non-empty line"
    else:
        prefix = _array_prefix(path, pointer)
        try:
            with _open_binary(path) as source:
                values = ijson.items(source, prefix)
                rows = _take(values, offset, max_rows)
        except (OSError, ValueError, TypeError, ijson.JSONError) as error:
            raise JsonDataError(f"Could not parse JSON data: {error}") from error
        source_format = "JSON"
        selection = pointer or "root array"
    result = {
        "format": source_format,
        "selection": selection,
        "file_bytes": path.stat().st_size,
        "offset": offset,
        "returned_rows": len(rows),
        "inferred_types_from_preview": _types(rows),
        "rows": rows,
    }
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as error:
        raise JsonDataError(f"Could not render JSON preview: {error}") from error
    if len(text) > MAX_JSON_TEXT_CHARACTERS:
        return text[:MAX_JSON_TEXT_CHARACTERS] + f"\n... [truncated at {MAX_JSON_TEXT_CHARACTERS} characters]"
    return text


def _bound(name: str, value: object, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise JsonDataError(f"{name} must be a whole number from {minimum} to {maximum}.")


def _is_json_lines(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith((".jsonl", ".ndjson", ".jsonl.gz", ".ndjson.gz"))


def _is_gzip(path: Path) -> bool:
    return path.name.lower().endswith(".gz")


class _CappedBinaryReader(io.RawIOBase):
    """File adapter that stops a compressed stream exceeding its output cap."""

    def __init__(self, source: io.BufferedIOBase, limit: int) -> None:
        self._source = source
        self._remaining = limit

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        try:
            self._source.close()
        finally:
            super().close()

    def readinto(self, buffer: bytearray) -> int:
        if self._remaining <= 0:
            # Reading once more is how parsers discover EOF; make an exact-limit
            # file fail rather than silently accepting a possible gzip bomb.
            raise JsonDataError(f"Decompressed JSON exceeds the {MAX_JSON_UNCOMPRESSED_BYTES}-byte limit.")
        data = self._source.read(min(len(buffer), self._remaining))
        size = len(data)
        buffer[:size] = data
        self._remaining -= size
        return size


def _open_binary(path: Path):
    """Open ordinary or gzip JSON while enforcing a decompressed-size limit."""
    if _is_gzip(path):
        raw = gzip.open(path, "rb")
        return io.BufferedReader(_CappedBinaryReader(raw, MAX_JSON_UNCOMPRESSED_BYTES))
    return path.open("rb")


def _json_lines(path: Path, offset: int, maximum: int) -> Iterator[Any]:
    skipped = 0
    emitted = 0
    try:
        with _open_text(path) as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                if len(line.encode("utf-8")) > MAX_JSON_RECORD_BYTES:
                    raise JsonDataError(f"JSON Lines record {line_number} exceeds the {MAX_JSON_RECORD_BYTES}-byte record limit.")
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise JsonDataError(f"Invalid JSON on line {line_number}: {error.msg}.") from error
                if skipped < offset:
                    skipped += 1
                    continue
                yield value
                emitted += 1
                if emitted >= maximum:
                    return
    except (OSError, UnicodeError) as error:
        raise JsonDataError(f"Could not read JSON Lines data: {error}") from error


def _open_text(path: Path):
    binary = _open_binary(path)
    return io.TextIOWrapper(binary, encoding="utf-8-sig", errors="strict")


def _array_prefix(path: Path, pointer: str) -> str:
    if pointer and not pointer.startswith("/"):
        raise JsonDataError("pointer must be empty or begin with '/'.")
    try:
        with _open_binary(path) as source:
            first = next(ijson.parse(source), None)
    except (OSError, ValueError, ijson.JSONError) as error:
        raise JsonDataError(f"Could not parse JSON data: {error}") from error
    if first is None:
        raise JsonDataError("The JSON file is empty.")
    if not pointer:
        if first[1] != "start_array":
            raise JsonDataError("The JSON root is not an array; provide pointer to an array such as '/items'.")
        return "item"
    # ijson uses dot-separated prefixes; a JSON Pointer segment is an object
    # member name.  Numeric array indexing is intentionally not supported,
    # since this tool's streaming contract is to preview an array, not seek it.
    segments = [_unescape(segment) for segment in pointer[1:].split("/")]
    if any(not segment for segment in segments):
        raise JsonDataError("pointer contains an empty segment and cannot name an array.")
    if any("." in segment for segment in segments):
        raise JsonDataError("pointer segments cannot contain '.' for streaming selection.")
    return ".".join((*segments, "item"))


def _unescape(segment: str) -> str:
    if "~" in segment:
        segment = segment.replace("~1", "/").replace("~0", "~")
        if "~" in segment:
            raise JsonDataError("pointer has an invalid '~' escape.")
    return segment


def _take(values: Iterator[Any], offset: int, maximum: int) -> list[Any]:
    rows: list[Any] = []
    try:
        for index, value in enumerate(values):
            if index < offset:
                continue
            # ijson materialises each selected item.  Refuse pathological
            # single records before a preview can make the tool output huge.
            if len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")) > MAX_JSON_RECORD_BYTES:
                raise JsonDataError(f"A selected JSON record exceeds the {MAX_JSON_RECORD_BYTES}-byte record limit.")
            rows.append(value)
            if len(rows) >= maximum:
                break
    except JsonDataError:
        raise
    except (ValueError, TypeError, ijson.JSONError) as error:
        raise JsonDataError(f"Could not parse JSON data: {error}") from error
    return rows


def _types(rows: list[Any]) -> dict[str, str]:
    """Describe row values, and object fields when the preview has objects."""
    result = {"row": _type_name(rows)}
    objects = [row for row in rows if isinstance(row, dict)]
    if objects:
        keys = sorted({str(key) for row in objects for key in row})
        result["fields"] = {key: _type_name([row.get(key) for row in objects if key in row]) for key in keys}
    return result


def _type_name(values: list[Any]) -> str:
    kinds = {type(value).__name__ if value is not None else "null" for value in values}
    if not kinds:
        return "unknown"
    return next(iter(kinds)) if len(kinds) == 1 else "mixed(" + ", ".join(sorted(kinds)) + ")"
