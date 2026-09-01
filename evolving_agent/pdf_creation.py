"""Create portable PDF reports from bounded declarative content.

The creator uses ReportLab's high-level Platypus layout engine: text is escaped
rather than interpreted as markup, local images are supplied by the contained
workspace resolver, and no active PDF features (JavaScript, attachments, or
external links) are emitted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from html import escape
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_MAX_BLOCKS = 100
_MAX_TEXT_LENGTH = 12_000
_MAX_BULLETS = 50
_MAX_TABLE_ROWS = 100
_MAX_TABLE_COLUMNS = 20
_MAX_IMAGES = 20
_PAGE_WIDTH, _PAGE_HEIGHT = LETTER
_MARGIN = 0.65 * inch


class PdfCreationError(ValueError):
    """The requested PDF could not be created."""


def create_pdf(
    destination: Path,
    blocks: object,
    *,
    image_path: Callable[[str], Path],
    title: object = None,
    author: object = None,
) -> str:
    """Build a PDF report at *destination* from a non-empty list of blocks."""
    if destination.suffix.lower() != ".pdf":
        raise PdfCreationError("'path' must end in .pdf.")
    if not isinstance(blocks, list) or not blocks:
        raise PdfCreationError("'blocks' must be a non-empty list of PDF block objects.")
    if len(blocks) > _MAX_BLOCKS:
        raise PdfCreationError(f"'blocks' may contain at most {_MAX_BLOCKS} blocks.")
    checked_title = _optional_text(title, "title")
    checked_author = _optional_text(author, "author")
    styles = _styles()
    story: list[Any] = []
    if checked_title:
        story.extend((Paragraph(_markup(checked_title), styles["Title"]), Spacer(1, 0.2 * inch)))

    images = 0
    for index, block in enumerate(blocks):
        flowables, added_images = _block_flowables(block, index, styles, image_path)
        images += added_images
        if images > _MAX_IMAGES:
            raise PdfCreationError(f"'blocks' may contain at most {_MAX_IMAGES} image blocks.")
        story.extend(flowables)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        document = SimpleDocTemplate(
            str(destination), pagesize=LETTER, title=checked_title or "", author=checked_author or "",
            leftMargin=_MARGIN, rightMargin=_MARGIN, topMargin=0.7 * inch, bottomMargin=0.65 * inch,
        )
        document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    except PdfCreationError:
        raise
    except Exception as failed:
        raise PdfCreationError(f"Could not save {destination.name!r}: {failed}") from failed
    return f"Created PDF report with {len(blocks)} blocks at {destination.name}."


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "Title": ParagraphStyle("PdfTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=22, leading=27, textColor=colors.HexColor("#17365D"), alignment=TA_CENTER, spaceAfter=8),
        "Body": ParagraphStyle("PdfBody", parent=base["BodyText"], fontName="Helvetica", fontSize=10.5, leading=14, spaceAfter=9),
        "Bullet": ParagraphStyle("PdfBullet", parent=base["BodyText"], fontName="Helvetica", fontSize=10.5, leading=14, leftIndent=18, firstLineIndent=-10, spaceAfter=3),
        **{f"Heading{level}": ParagraphStyle(f"PdfHeading{level}", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=max(12, 19 - 2 * level), leading=max(15, 23 - 2 * level), textColor=colors.HexColor("#17365D"), spaceBefore=10, spaceAfter=6) for level in range(1, 10)},
        "TableHeader": ParagraphStyle("PdfTableHeader", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=8.5, leading=10, textColor=colors.white),
        "TableCell": ParagraphStyle("PdfTableCell", parent=base["BodyText"], fontName="Helvetica", fontSize=8.5, leading=10),
        "Caption": ParagraphStyle("PdfCaption", parent=base["BodyText"], fontName="Helvetica-Oblique", fontSize=9, leading=11, alignment=TA_CENTER, textColor=colors.HexColor("#555555"), spaceBefore=4, spaceAfter=8),
    }


def _block_flowables(block: object, index: int, styles: Mapping[str, ParagraphStyle], image_path: Callable[[str], Path]) -> tuple[list[Any], int]:
    if not isinstance(block, Mapping):
        raise PdfCreationError(f"blocks[{index}] must be an object.")
    kind = block.get("type")
    if not isinstance(kind, str):
        raise PdfCreationError(f"blocks[{index}].type is required and must be a string.")
    if kind == "heading":
        level = block.get("level", 1)
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 9:
            raise PdfCreationError(f"blocks[{index}].level must be a whole number from 1 to 9.")
        return [Paragraph(_markup(_text(block, "text", index)), styles[f"Heading{level}"])], 0
    if kind == "paragraph":
        return [Paragraph(_markup(_text(block, "text", index)), styles["Body"])], 0
    if kind == "bullets":
        values = _text_list(block.get("items"), index, "items", _MAX_BULLETS)
        return [Paragraph("• " + _markup(value), styles["Bullet"]) for value in values], 0
    if kind == "table":
        return [_table(block, index, styles), Spacer(1, 0.08 * inch)], 0
    if kind == "page_break":
        if set(block) != {"type"}:
            raise PdfCreationError(f"blocks[{index}] page_break cannot have other fields.")
        return [PageBreak()], 0
    if kind == "image":
        return _image(block, index, styles, image_path), 1
    raise PdfCreationError("blocks[%d].type must be heading, paragraph, bullets, table, page_break, or image." % index)


def _table(block: Mapping[str, object], index: int, styles: Mapping[str, ParagraphStyle]) -> Table:
    columns = _text_list(block.get("columns"), index, "columns", _MAX_TABLE_COLUMNS)
    rows = block.get("rows")
    if not isinstance(rows, list) or not rows:
        raise PdfCreationError(f"blocks[{index}].rows must be a non-empty list.")
    if len(rows) > _MAX_TABLE_ROWS:
        raise PdfCreationError(f"blocks[{index}].rows may contain at most {_MAX_TABLE_ROWS} rows.")
    data: list[list[Paragraph]] = [[Paragraph(_markup(value), styles["TableHeader"]) for value in columns]]
    for row_number, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise PdfCreationError(f"blocks[{index}].rows[{row_number}] must contain {len(columns)} strings.")
        data.append([Paragraph(_markup(_checked_text(value, index, f"rows[{row_number}][{column}]")), styles["TableCell"]) for column, value in enumerate(row)])
    width = (_PAGE_WIDTH - 2 * _MARGIN) / len(columns)
    table = Table(data, colWidths=[width] * len(columns), repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17365D")), ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#AAB7C4")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F8")])]))
    return table


def _image(block: Mapping[str, object], index: int, styles: Mapping[str, ParagraphStyle], image_path: Callable[[str], Path]) -> list[Any]:
    name = _text(block, "path", index)
    try:
        path = image_path(name)
        if not path.is_file():
            raise PdfCreationError(f"blocks[{index}].path does not name a file: {name!r}.")
        source = ImageReader(str(path))
        source_width, source_height = source.getSize()
        if source_width <= 0 or source_height <= 0:
            raise PdfCreationError(f"blocks[{index}].path is not a usable image: {name!r}.")
        maximum_width, maximum_height = _PAGE_WIDTH - 2 * _MARGIN, 5.8 * inch
        scale = min(maximum_width / source_width, maximum_height / source_height, 1)
        flowables: list[Any] = [Image(str(path), width=source_width * scale, height=source_height * scale, hAlign="CENTER")]
        caption = block.get("caption")
        if caption is not None:
            flowables.append(Paragraph(_markup(_checked_text(caption, index, "caption")), styles["Caption"]))
        return [KeepTogether(flowables)]
    except PdfCreationError:
        raise
    except Exception as failed:
        raise PdfCreationError(f"Could not add image for blocks[{index}]: {failed}") from failed


def _text(block: Mapping[str, object], field: str, index: int) -> str:
    return _checked_text(block.get(field), index, field)


def _checked_text(value: object, index: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PdfCreationError(f"blocks[{index}].{field} must be a non-blank string.")
    if len(value) > _MAX_TEXT_LENGTH:
        raise PdfCreationError(f"blocks[{index}].{field} may contain at most {_MAX_TEXT_LENGTH} characters.")
    return value.strip()


def _text_list(value: object, index: int, field: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise PdfCreationError(f"blocks[{index}].{field} must be a non-empty list of at most {maximum} strings.")
    return [_checked_text(item, index, f"{field}[{number}]") for number, item in enumerate(value)]


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT_LENGTH:
        raise PdfCreationError(f"'{field}' must be a non-blank string of at most {_MAX_TEXT_LENGTH} characters when supplied.")
    return value.strip()


def _markup(text: str) -> str:
    return escape(text).replace("\n", "<br/>")


def _footer(canvas: Any, document: Any) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#666666"))
    canvas.drawRightString(_PAGE_WIDTH - _MARGIN, 0.36 * inch, f"Page {document.page}")
    canvas.restoreState()
