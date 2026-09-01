"""Create simple, portable PowerPoint deliverables from structured content.

The tool deliberately exposes a small declarative surface rather than arbitrary
PPTX XML: titles, bullet lists, tables, and local images cover the common task
of turning an analysis into a slide deck while keeping all input paths inside
one of the agent's workspace trees.
"""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.util import Inches, Pt


class PresentationError(Exception):
    """The requested presentation could not be created."""


_MAX_SLIDES = 30
_MAX_BULLETS = 20
_MAX_TABLE_ROWS = 30
_MAX_TABLE_COLUMNS = 12


def create_presentation(
    destination: Path,
    slides: object,
    *,
    image_path: Callable[[str], Path],
) -> str:
    """Build a PPTX at *destination* from a bounded list of slide mappings.

    ``image_path`` is supplied by the workspace tool, rather than resolving a
    model-provided filename here, so images retain the same containment rules
    as every other input file.
    """
    if destination.suffix.lower() != ".pptx":
        raise PresentationError("'path' must end in .pptx.")
    if not isinstance(slides, list) or not slides:
        raise PresentationError("'slides' must be a non-empty list of slide objects.")
    if len(slides) > _MAX_SLIDES:
        raise PresentationError(f"'slides' may contain at most {_MAX_SLIDES} slides.")

    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    for number, specification in enumerate(slides, start=1):
        _add_slide(presentation, specification, number, image_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        presentation.save(destination)
    except (OSError, ValueError) as failed:
        raise PresentationError(f"Could not save {destination.name!r}: {failed}") from failed
    return f"Created {len(slides)}-slide PowerPoint presentation at {destination.name}."


def _add_slide(
    presentation: Presentation,
    specification: object,
    number: int,
    image_path: Callable[[str], Path],
) -> None:
    if not isinstance(specification, Mapping):
        raise PresentationError(f"slides[{number - 1}] must be an object.")
    title = _optional_text(specification, "title", number)
    bullets = _text_list(specification.get("bullets", []), "bullets", number, _MAX_BULLETS)
    image = _optional_text(specification, "image", number)
    table = specification.get("table")
    if not title and not bullets and not image and table is None:
        raise PresentationError(f"slides[{number - 1}] needs a title, bullets, image, or table.")

    # A title-and-content layout gives useful native editing behavior. Tables
    # and images use a blank layout to avoid overlapping its content placeholder.
    slide = presentation.slides.add_slide(presentation.slide_layouts[6 if (image or table is not None) else 1])
    _add_title(slide, title or f"Slide {number}")
    if bullets:
        _add_bullets(slide, bullets, has_visual=bool(image or table is not None))
    if table is not None:
        _add_table(slide, table, number)
    if image:
        _add_image(slide, image, number, image_path)


def _add_title(slide: Any, title: str) -> None:
    box = slide.shapes.add_textbox(Inches(0.55), Inches(0.3), Inches(12.2), Inches(0.65))
    paragraph = box.text_frame.paragraphs[0]
    paragraph.text = title
    paragraph.font.size = Pt(28)
    paragraph.font.bold = True


def _add_bullets(slide: Any, bullets: Sequence[str], *, has_visual: bool) -> None:
    width = 5.7 if has_visual else 11.7
    box = slide.shapes.add_textbox(Inches(0.75), Inches(1.25), Inches(width), Inches(5.7))
    frame = box.text_frame
    frame.word_wrap = True
    for index, bullet in enumerate(bullets):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.text = bullet
        paragraph.level = 0
        paragraph.font.size = Pt(20 if has_visual else 22)
        paragraph.space_after = Pt(10)


def _add_table(slide: Any, value: object, number: int) -> None:
    if not isinstance(value, Mapping):
        raise PresentationError(f"slides[{number - 1}].table must be an object.")
    columns = _text_list(value.get("columns"), "table.columns", number, _MAX_TABLE_COLUMNS, nonempty=True)
    rows = value.get("rows")
    if not isinstance(rows, list) or not rows:
        raise PresentationError(f"slides[{number - 1}].table.rows must be a non-empty list.")
    if len(rows) > _MAX_TABLE_ROWS:
        raise PresentationError(f"slides[{number - 1}].table.rows may contain at most {_MAX_TABLE_ROWS} rows.")
    checked_rows: list[list[str]] = []
    for row_number, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns) or not all(isinstance(cell, str) for cell in row):
            raise PresentationError(
                f"slides[{number - 1}].table.rows[{row_number}] must be a list of {len(columns)} strings."
            )
        checked_rows.append(row)
    shape = slide.shapes.add_table(len(checked_rows) + 1, len(columns), Inches(0.65), Inches(1.35), Inches(12.0), Inches(5.55))
    table = shape.table
    for column, text in enumerate(columns):
        table.cell(0, column).text = text
    for row_number, row in enumerate(checked_rows, start=1):
        for column, text in enumerate(row):
            table.cell(row_number, column).text = text


def _add_image(slide: Any, name: str, number: int, image_path: Callable[[str], Path]) -> None:
    try:
        path = image_path(name)
        if not path.is_file():
            raise PresentationError(f"slides[{number - 1}].image does not name a file: {name!r}.")
        slide.shapes.add_picture(str(path), Inches(6.65), Inches(1.3), width=Inches(6.0), height=Inches(5.65))
    except PresentationError:
        raise
    except Exception as failed:
        raise PresentationError(f"Could not add image for slides[{number - 1}]: {failed}") from failed


def _optional_text(specification: Mapping[str, object], field: str, number: int) -> str:
    value = specification.get(field, "")
    if not isinstance(value, str):
        raise PresentationError(f"slides[{number - 1}].{field} must be a string when supplied.")
    return value.strip()


def _text_list(value: object, field: str, number: int, maximum: int, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value) or len(value) > maximum:
        requirement = "a non-empty list" if nonempty else "a list"
        raise PresentationError(f"slides[{number - 1}].{field} must be {requirement} of at most {maximum} strings.")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise PresentationError(f"slides[{number - 1}].{field} must contain non-blank strings.")
    return [item.strip() for item in value]
