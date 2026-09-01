"""Create editable Word documents from bounded declarative content.

The document API intentionally uses ordinary Word objects—headings, paragraphs,
lists, tables, page breaks, and images—rather than exposing package internals.
Images are resolved by the workspace tool before reaching this module, preserving
its contained-path policy.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Inches, Pt


class DocumentError(Exception):
    """The requested DOCX could not be created."""


_MAX_BLOCKS = 100
_MAX_TEXT_LENGTH = 12_000
_MAX_BULLETS = 50
_MAX_TABLE_ROWS = 100
_MAX_TABLE_COLUMNS = 20
_MAX_IMAGES = 20


def create_document(
    destination: Path,
    blocks: object,
    *,
    image_path: Callable[[str], Path],
    title: object = None,
) -> str:
    """Build a DOCX at *destination* from a non-empty list of block objects."""
    if destination.suffix.lower() != ".docx":
        raise DocumentError("'path' must end in .docx.")
    if not isinstance(blocks, list) or not blocks:
        raise DocumentError("'blocks' must be a non-empty list of document block objects.")
    if len(blocks) > _MAX_BLOCKS:
        raise DocumentError(f"'blocks' may contain at most {_MAX_BLOCKS} blocks.")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise DocumentError("'title' must be a non-blank string when supplied.")

    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = "Aptos"
    normal.font.size = Pt(11)
    if title is not None:
        document.core_properties.title = title.strip()
        document.add_heading(title.strip(), level=0)

    images = 0
    for index, block in enumerate(blocks):
        images += _add_block(document, block, index, image_path)
        if images > _MAX_IMAGES:
            raise DocumentError(f"'blocks' may contain at most {_MAX_IMAGES} image blocks.")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        document.save(destination)
    except (OSError, ValueError) as failed:
        raise DocumentError(f"Could not save {destination.name!r}: {failed}") from failed
    return f"Created editable Word document with {len(blocks)} blocks at {destination.name}."


def _add_block(document: Any, block: object, index: int, image_path: Callable[[str], Path]) -> int:
    if not isinstance(block, Mapping):
        raise DocumentError(f"blocks[{index}] must be an object.")
    kind = block.get("type")
    if not isinstance(kind, str):
        raise DocumentError(f"blocks[{index}].type is required and must be a string.")
    if kind == "heading":
        text = _text(block, "text", index)
        level = block.get("level", 1)
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 9:
            raise DocumentError(f"blocks[{index}].level must be a whole number from 1 to 9.")
        document.add_heading(text, level=level)
    elif kind == "paragraph":
        document.add_paragraph(_text(block, "text", index))
    elif kind == "bullets":
        values = _text_list(block.get("items"), index, "items", _MAX_BULLETS, nonempty=True)
        for value in values:
            document.add_paragraph(value, style="List Bullet")
    elif kind == "table":
        _add_table(document, block, index)
    elif kind == "page_break":
        if set(block) != {"type"}:
            raise DocumentError(f"blocks[{index}] page_break cannot have other fields.")
        document.add_page_break()
    elif kind == "image":
        name = _text(block, "path", index)
        try:
            path = image_path(name)
            if not path.is_file():
                raise DocumentError(f"blocks[{index}].path does not name a file: {name!r}.")
            document.add_picture(str(path), width=Inches(6.25))
            caption = block.get("caption")
            if caption is not None:
                document.add_paragraph(_checked_text(caption, index, "caption"), style="Caption")
        except DocumentError:
            raise
        except Exception as failed:
            raise DocumentError(f"Could not add image for blocks[{index}]: {failed}") from failed
        return 1
    else:
        raise DocumentError(
            f"blocks[{index}].type must be heading, paragraph, bullets, table, page_break, or image."
        )
    return 0


def _add_table(document: Any, block: Mapping[str, object], index: int) -> None:
    columns = _text_list(block.get("columns"), index, "columns", _MAX_TABLE_COLUMNS, nonempty=True)
    rows = block.get("rows")
    if not isinstance(rows, list) or not rows:
        raise DocumentError(f"blocks[{index}].rows must be a non-empty list.")
    if len(rows) > _MAX_TABLE_ROWS:
        raise DocumentError(f"blocks[{index}].rows may contain at most {_MAX_TABLE_ROWS} rows.")
    table = document.add_table(rows=1, cols=len(columns))
    table.style = "Table Grid"
    for column, text in enumerate(columns):
        table.rows[0].cells[column].text = text
    for row_number, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise DocumentError(f"blocks[{index}].rows[{row_number}] must contain {len(columns)} strings.")
        cells = table.add_row().cells
        for column, value in enumerate(row):
            cells[column].text = _checked_text(value, index, f"rows[{row_number}][{column}]")


def _text(block: Mapping[str, object], field: str, index: int) -> str:
    return _checked_text(block.get(field), index, field)


def _checked_text(value: object, index: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocumentError(f"blocks[{index}].{field} must be a non-blank string.")
    if len(value) > _MAX_TEXT_LENGTH:
        raise DocumentError(f"blocks[{index}].{field} may contain at most {_MAX_TEXT_LENGTH} characters.")
    return value.strip()


def _text_list(value: object, index: int, field: str, maximum: int, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or len(value) > maximum:
        requirement = "a non-empty list" if nonempty else "a list"
        raise DocumentError(f"blocks[{index}].{field} must be {requirement} of at most {maximum} strings.")
    return [_checked_text(item, index, f"{field}[{number}]") for number, item in enumerate(value)]
