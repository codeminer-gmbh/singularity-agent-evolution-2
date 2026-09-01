"""Bounded creation of ordinary DOCX deliverables.

The writer accepts already-structured content rather than templates, macros, or raw
Office XML.  It writes atomically to a contained output path so a failed save
cannot replace a requested deliverable with a partial ZIP package.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_BREAK

_MAX_BLOCKS = 500
_MAX_TEXT_CHARACTERS = 200_000
_MAX_TABLE_ROWS = 500
_MAX_TABLE_COLUMNS = 50
_MAX_TABLE_CELLS = 5_000
_MAX_RESULT_BYTES = 10 * 1024 * 1024


class DocxWriterError(ValueError):
    """The requested document cannot be made within the writer's bounds."""


def create_docx(path: Path, *, title: str | None, blocks: object) -> dict[str, int]:
    """Create a DOCX from bounded heading, paragraph, list, page-break, and table blocks."""
    if path.suffix.lower() != ".docx":
        raise DocxWriterError("The deliverable path must end in .docx.")
    normalized_title = _optional_text(title, "title")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        raise DocxWriterError("'blocks' must be a non-empty array of document blocks.")
    if not blocks or len(blocks) > _MAX_BLOCKS:
        raise DocxWriterError(f"'blocks' must contain between 1 and {_MAX_BLOCKS} items.")

    document = Document()
    used_characters = 0
    table_cells = 0
    headings = paragraphs = tables = 0
    if normalized_title is not None:
        used_characters += len(normalized_title)
        document.core_properties.title = normalized_title
        document.add_heading(normalized_title, level=0)
        headings += 1

    for index, block in enumerate(blocks, start=1):
        if not isinstance(block, Mapping):
            raise DocxWriterError(f"blocks[{index}] must be an object.")
        kind = block.get("type")
        if kind in {"heading", "paragraph", "bullet", "numbered"}:
            text = _required_text(block.get("text"), f"blocks[{index}].text")
            used_characters += len(text)
            _check_text_limit(used_characters)
            if kind == "heading":
                level = block.get("level", 1)
                if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 9:
                    raise DocxWriterError(f"blocks[{index}].level must be an integer from 1 to 9.")
                document.add_heading(text, level=level)
                headings += 1
            else:
                style = {"bullet": "List Bullet", "numbered": "List Number"}.get(kind)
                document.add_paragraph(text, style=style)
                paragraphs += 1
        elif kind == "page_break":
            if set(block) - {"type"}:
                raise DocxWriterError(f"blocks[{index}] page_break cannot have other fields.")
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        elif kind == "table":
            rows, character_count, cell_count = _table_rows(block.get("rows"), index)
            used_characters += character_count
            table_cells += cell_count
            _check_text_limit(used_characters)
            if table_cells > _MAX_TABLE_CELLS:
                raise DocxWriterError(f"Tables may contain at most {_MAX_TABLE_CELLS} cells in total.")
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = "Table Grid"
            for row_index, row in enumerate(rows):
                for column_index, value in enumerate(row):
                    table.cell(row_index, column_index).text = value
            tables += 1
        else:
            raise DocxWriterError(
                f"blocks[{index}].type must be heading, paragraph, bullet, numbered, page_break, or table."
            )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".docx-write-", suffix=".docx", dir=path.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            document.save(temporary)
            size = temporary.stat().st_size
            if size > _MAX_RESULT_BYTES:
                raise DocxWriterError("The generated DOCX exceeds the 10 MB deliverable limit.")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except DocxWriterError:
        raise
    except OSError as exc:
        raise DocxWriterError(f"Could not write {path.name!r}: {exc}") from exc
    return {"size_bytes": size, "headings": headings, "paragraphs": paragraphs, "tables": tables}


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocxWriterError(f"'{field}' must be a non-blank string.")
    if len(value) > _MAX_TEXT_CHARACTERS:
        raise DocxWriterError(f"'{field}' exceeds the {_MAX_TEXT_CHARACTERS}-character limit.")
    return value


def _check_text_limit(total: int) -> None:
    if total > _MAX_TEXT_CHARACTERS:
        raise DocxWriterError(f"Document text exceeds the {_MAX_TEXT_CHARACTERS}-character limit.")


def _table_rows(value: object, block_index: int) -> tuple[list[list[str]], int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise DocxWriterError(f"blocks[{block_index}].rows must be a non-empty array of rows.")
    if len(value) > _MAX_TABLE_ROWS:
        raise DocxWriterError(f"blocks[{block_index}] has more than {_MAX_TABLE_ROWS} table rows.")
    rows: list[list[str]] = []
    columns: int | None = None
    characters = 0
    for row_index, row in enumerate(value, start=1):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or not row:
            raise DocxWriterError(f"blocks[{block_index}].rows[{row_index}] must be a non-empty array.")
        if len(row) > _MAX_TABLE_COLUMNS:
            raise DocxWriterError(f"Table rows may have at most {_MAX_TABLE_COLUMNS} columns.")
        if columns is None:
            columns = len(row)
        elif len(row) != columns:
            raise DocxWriterError(f"blocks[{block_index}] table rows must all have the same number of columns.")
        cells = [_required_text(cell, f"blocks[{block_index}].rows[{row_index}]") for cell in row]
        characters += sum(len(cell) for cell in cells)
        rows.append(cells)
    return rows, characters, len(rows) * (columns or 0)
