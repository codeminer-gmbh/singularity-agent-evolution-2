"""Create static, print-ready PDF files from bounded declarative blocks.

This intentionally exposes only ReportLab's high-level flowables.  Callers cannot
supply PDF syntax, links, annotations, forms, or JavaScript; generated files are
ordinary page content with optional document title metadata.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape


class PdfCreationError(Exception):
    """The requested PDF could not be created."""


_MAX_BLOCKS = 100
_MAX_TEXT_LENGTH = 12_000
_MAX_BULLETS = 50
_MAX_TABLE_ROWS = 100
_MAX_TABLE_COLUMNS = 20
_MAX_IMAGES = 20


def create_pdf(
    destination: Path,
    blocks: object,
    *,
    image_path: Callable[[str], Path],
    title: object = None,
) -> str:
    """Build a static PDF at *destination* from a non-empty list of blocks."""
    if destination.suffix.lower() != ".pdf":
        raise PdfCreationError("'path' must end in .pdf.")
    if not isinstance(blocks, list) or not blocks:
        raise PdfCreationError("'blocks' must be a non-empty list of PDF block objects.")
    if len(blocks) > _MAX_BLOCKS:
        raise PdfCreationError(f"'blocks' may contain at most {_MAX_BLOCKS} blocks.")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise PdfCreationError("'title' must be a non-blank string when supplied.")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("AgentTitle", parent=styles["Title"], alignment=TA_CENTER, spaceAfter=18)
    body_style = ParagraphStyle("AgentBody", parent=styles["BodyText"], leading=14, spaceAfter=8)
    story: list[Any] = []
    if title is not None:
        story.append(Paragraph(_markup(title.strip()), title_style))

    images = 0
    try:
        for index, block in enumerate(blocks):
            flowables, used_images = _block_flowables(block, index, image_path, styles, body_style)
            story.extend(flowables)
            images += used_images
            if images > _MAX_IMAGES:
                raise PdfCreationError(f"'blocks' may contain at most {_MAX_IMAGES} image blocks.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        document = SimpleDocTemplate(
            str(destination), pagesize=letter, title=title.strip() if isinstance(title, str) else "",
            leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        )
        document.build(story)
    except PdfCreationError:
        raise
    except Exception as failed:
        raise PdfCreationError(f"Could not create {destination.name!r}: {failed}") from failed
    return f"Created static PDF with {len(blocks)} blocks at {destination.name}."


def _block_flowables(block: object, index: int, image_path: Callable[[str], Path], styles: Any, body_style: ParagraphStyle) -> tuple[list[Any], int]:
    if not isinstance(block, Mapping):
        raise PdfCreationError(f"blocks[{index}] must be an object.")
    kind = block.get("type")
    if not isinstance(kind, str):
        raise PdfCreationError(f"blocks[{index}].type is required and must be a string.")
    if kind == "heading":
        level = block.get("level", 1)
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise PdfCreationError(f"blocks[{index}].level must be a whole number from 1 to 6.")
        return [Paragraph(_markup(_text(block, "text", index)), styles[f"Heading{level}"])], 0
    if kind == "paragraph":
        return [Paragraph(_markup(_text(block, "text", index)), body_style)], 0
    if kind == "bullets":
        values = _text_list(block.get("items"), index, "items", _MAX_BULLETS, nonempty=True)
        bullet_style = ParagraphStyle("AgentBullet", parent=body_style, leftIndent=18, firstLineIndent=-10)
        return [Paragraph(_markup(value), bullet_style, bulletText="•") for value in values], 0
    if kind == "table":
        return [_table(block, index, body_style), Spacer(1, 8)], 0
    if kind == "page_break":
        if set(block) != {"type"}:
            raise PdfCreationError(f"blocks[{index}] page_break cannot have other fields.")
        return [PageBreak()], 0
    if kind == "image":
        return _image(block, index, image_path, body_style), 1
    raise PdfCreationError("blocks[%d].type must be heading, paragraph, bullets, table, page_break, or image." % index)


def _table(block: Mapping[str, object], index: int, style: ParagraphStyle) -> Table:
    columns = _text_list(block.get("columns"), index, "columns", _MAX_TABLE_COLUMNS, nonempty=True)
    rows = block.get("rows")
    if not isinstance(rows, list) or not rows:
        raise PdfCreationError(f"blocks[{index}].rows must be a non-empty list.")
    if len(rows) > _MAX_TABLE_ROWS:
        raise PdfCreationError(f"blocks[{index}].rows may contain at most {_MAX_TABLE_ROWS} rows.")
    data: list[list[Any]] = [[Paragraph(_markup(value), style) for value in columns]]
    for row_number, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise PdfCreationError(f"blocks[{index}].rows[{row_number}] must contain {len(columns)} strings.")
        data.append([Paragraph(_markup(_checked_text(value, index, f"rows[{row_number}][{column}]")), style) for column, value in enumerate(row)])
    table = Table(data, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#A6A6A6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _image(block: Mapping[str, object], index: int, image_path: Callable[[str], Path], style: ParagraphStyle) -> list[Any]:
    name = _text(block, "path", index)
    try:
        source = image_path(name)
        if not source.is_file():
            raise PdfCreationError(f"blocks[{index}].path does not name a file: {name!r}.")
        picture = Image(str(source), width=6.25 * inch, height=4.75 * inch, kind="proportional")
    except PdfCreationError:
        raise
    except Exception as failed:
        raise PdfCreationError(f"Could not add image for blocks[{index}]: {failed}") from failed
    result: list[Any] = [picture]
    caption = block.get("caption")
    if caption is not None:
        result.append(Paragraph(_markup(_checked_text(caption, index, "caption")), styles_caption(style)))
    return result


def styles_caption(style: ParagraphStyle) -> ParagraphStyle:
    return ParagraphStyle("AgentCaption", parent=style, fontSize=9, textColor=colors.HexColor("#555555"), alignment=TA_CENTER)


def _markup(text: str) -> str:
    return escape(text).replace("\n", "<br/>")


def _text(block: Mapping[str, object], field: str, index: int) -> str:
    return _checked_text(block.get(field), index, field)


def _checked_text(value: object, index: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PdfCreationError(f"blocks[{index}].{field} must be a non-blank string.")
    if len(value) > _MAX_TEXT_LENGTH:
        raise PdfCreationError(f"blocks[{index}].{field} may contain at most {_MAX_TEXT_LENGTH} characters.")
    return value.strip()


def _text_list(value: object, index: int, field: str, maximum: int, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or len(value) > maximum:
        requirement = "a non-empty list" if nonempty else "a list"
        raise PdfCreationError(f"blocks[{index}].{field} must be {requirement} of at most {maximum} strings.")
    return [_checked_text(item, index, f"{field}[{number}]") for number, item in enumerate(value)]
