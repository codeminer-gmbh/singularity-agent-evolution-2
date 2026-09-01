"""Bounded creation of ordinary, non-interactive PDF deliverables.

The writer deliberately accepts structured text rather than PDF syntax, HTML, or
external assets.  ReportLab generates a conventional PDF content stream with no
scripts, links, forms, or embedded files.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_MAX_BLOCKS = 500
_MAX_TEXT_CHARACTERS = 200_000
_MAX_TABLE_ROWS = 500
_MAX_TABLE_COLUMNS = 50
_MAX_TABLE_CELLS = 5_000
_MAX_RESULT_BYTES = 10 * 1024 * 1024


class PdfWriterError(ValueError):
    """The requested PDF is outside this writer's small, safe contract."""


def create_pdf(path: Path, *, title: str | None, blocks: object) -> dict[str, int]:
    """Write a bounded text-and-table PDF atomically and return a short report."""
    if path.suffix.lower() != ".pdf":
        raise PdfWriterError("The deliverable path must end in .pdf.")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)) or not blocks:
        raise PdfWriterError("'blocks' must be a non-empty array of document blocks.")
    if len(blocks) > _MAX_BLOCKS:
        raise PdfWriterError(f"'blocks' may contain at most {_MAX_BLOCKS} items.")
    checked_title = _optional_text(title, "title")
    styles = _styles()
    story: list[Any] = []
    total_text = 0
    table_cells = headings = paragraphs = tables = 0
    if checked_title is not None:
        story.extend((Paragraph(_markup(checked_title), styles["Title"]), Spacer(1, 12)))
        total_text += len(checked_title)
        headings += 1

    for index, block in enumerate(blocks, 1):
        if not isinstance(block, Mapping):
            raise PdfWriterError(f"blocks[{index}] must be an object.")
        kind = block.get("type")
        if kind in {"heading", "paragraph", "bullet", "numbered"}:
            text = _required_text(block.get("text"), f"blocks[{index}].text")
            total_text += len(text)
            _text_limit(total_text)
            if kind == "heading":
                level = block.get("level", 1)
                if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 3:
                    raise PdfWriterError(f"blocks[{index}].level must be an integer from 1 to 3.")
                story.extend((Paragraph(_markup(text), styles[f"Heading{level}"]), Spacer(1, 5)))
                headings += 1
            else:
                prefix = "• " if kind == "bullet" else "# " if kind == "numbered" else ""
                story.extend((Paragraph(_markup(prefix + text), styles["BodyText"]), Spacer(1, 4)))
                paragraphs += 1
        elif kind == "page_break":
            if set(block) != {"type"}:
                raise PdfWriterError(f"blocks[{index}] page_break cannot have other fields.")
            story.append(PageBreak())
        elif kind == "table":
            rows, characters, cells = _table_rows(block.get("rows"), index)
            total_text += characters
            table_cells += cells
            _text_limit(total_text)
            if table_cells > _MAX_TABLE_CELLS:
                raise PdfWriterError(f"Tables may contain at most {_MAX_TABLE_CELLS} cells in total.")
            rendered = [[Paragraph(_markup(cell), styles["TableCell"]) for cell in row] for row in rows]
            table = Table(rendered, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF7")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#9AA7B8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.extend((table, Spacer(1, 8)))
            tables += 1
        else:
            raise PdfWriterError(f"blocks[{index}].type must be heading, paragraph, bullet, numbered, page_break, or table.")

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".pdf-write-", suffix=".pdf", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        document = SimpleDocTemplate(str(temporary), pagesize=LETTER, title=checked_title or "", leftMargin=0.72 * inch, rightMargin=0.72 * inch, topMargin=0.72 * inch, bottomMargin=0.72 * inch)
        document.build(story)
        size = temporary.stat().st_size
        if size > _MAX_RESULT_BYTES:
            raise PdfWriterError("The generated PDF exceeds the 10 MB deliverable limit.")
        os.replace(temporary, path)
    except PdfWriterError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise PdfWriterError(f"Could not write {path.name!r}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {"size_bytes": size, "headings": headings, "paragraphs": paragraphs, "tables": tables}


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "Title": ParagraphStyle("SafeTitle", parent=base["Title"], alignment=TA_CENTER, spaceAfter=8),
        "Heading1": ParagraphStyle("SafeHeading1", parent=base["Heading1"], spaceBefore=8, spaceAfter=3),
        "Heading2": ParagraphStyle("SafeHeading2", parent=base["Heading2"], spaceBefore=7, spaceAfter=3),
        "Heading3": ParagraphStyle("SafeHeading3", parent=base["Heading3"], spaceBefore=6, spaceAfter=2),
        "BodyText": ParagraphStyle("SafeBody", parent=base["BodyText"], leading=14),
        "TableCell": ParagraphStyle("SafeTable", parent=base["BodyText"], fontSize=8, leading=10),
    }


def _markup(text: str) -> str:
    return escape(text).replace("\n", "<br/>")


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _required_text(value, field)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PdfWriterError(f"'{field}' must be a non-blank string.")
    if len(value) > _MAX_TEXT_CHARACTERS:
        raise PdfWriterError(f"'{field}' exceeds the {_MAX_TEXT_CHARACTERS}-character limit.")
    return value


def _text_limit(value: int) -> None:
    if value > _MAX_TEXT_CHARACTERS:
        raise PdfWriterError(f"Document text exceeds the {_MAX_TEXT_CHARACTERS}-character limit.")


def _table_rows(value: object, block_index: int) -> tuple[list[list[str]], int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise PdfWriterError(f"blocks[{block_index}].rows must be a non-empty array of rows.")
    if len(value) > _MAX_TABLE_ROWS:
        raise PdfWriterError(f"blocks[{block_index}] has more than {_MAX_TABLE_ROWS} rows.")
    rows: list[list[str]] = []
    columns: int | None = None
    characters = 0
    for row_index, row in enumerate(value, 1):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or not row:
            raise PdfWriterError(f"blocks[{block_index}].rows[{row_index}] must be a non-empty array.")
        if len(row) > _MAX_TABLE_COLUMNS:
            raise PdfWriterError(f"Table rows may have at most {_MAX_TABLE_COLUMNS} columns.")
        if columns is None:
            columns = len(row)
        elif len(row) != columns:
            raise PdfWriterError(f"blocks[{block_index}] table rows must all have the same number of columns.")
        cells = [_required_text(cell, f"blocks[{block_index}].rows[{row_index}]") for cell in row]
        characters += sum(map(len, cells))
        rows.append(cells)
    return rows, characters, len(rows) * (columns or 0)
