"""Bounded text extraction for common task-material document formats."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_MAX_INPUT_BYTES = 12_000_000
_MAX_OUTPUT_CHARACTERS = 60_000
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


class DocumentExtractionError(Exception):
    """The named document cannot safely be extracted."""


def extract_document_text(path: Path, *, max_characters: int = _MAX_OUTPUT_CHARACTERS) -> str:
    """Extract readable text from PDF or Office Open XML documents.

    The caller supplies a path already confined to one of the agent's trees.
    Extraction is deliberately bounded both before parsing and in its returned
    text, so a supplied archive cannot consume the whole model context.
    """
    if not path.is_file():
        raise DocumentExtractionError(f"{path.name!r} is not a file.")
    if path.stat().st_size > _MAX_INPUT_BYTES:
        raise DocumentExtractionError(
            f"{path.name!r} is larger than {_MAX_INPUT_BYTES:,} bytes; use a smaller document."
        )
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = _pdf(path)
    elif suffix == ".docx":
        text = _docx(path)
    elif suffix == ".xlsx":
        text = _xlsx(path)
    elif suffix == ".pptx":
        text = _pptx(path)
    else:
        raise DocumentExtractionError(
            "Supported document formats are PDF, DOCX, XLSX, and PPTX. "
            "Use read_file for plain-text formats."
        )
    if len(text) > max_characters:
        return f"{text[:max_characters]}\n... [truncated at {max_characters} characters]"
    return text or "[No extractable text was found.]"


def _pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        pages = [f"--- Page {number} ---\n{page.extract_text() or ''}" for number, page in enumerate(reader.pages, 1)]
    except Exception as error:
        raise DocumentExtractionError(f"Could not extract PDF text: {error}") from error
    return "\n\n".join(pages)


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise DocumentExtractionError(f"Could not read Office document: {error}") from error


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(name))
    except (KeyError, ET.ParseError) as error:
        raise DocumentExtractionError(f"Office document is missing or has invalid {name}: {error}") from error


def _docx(path: Path) -> str:
    with _open_zip(path) as archive:
        root = _xml(archive, "word/document.xml")
    blocks: list[str] = []
    body = root.find(_WORD_NS + "body")
    for item in list(body) if body is not None else []:
        if item.tag == _WORD_NS + "p":
            value = "".join(node.text or "" for node in item.iter(_WORD_NS + "t"))
            if value:
                blocks.append(value)
        elif item.tag == _WORD_NS + "tbl":
            for row in item.findall(_WORD_NS + "tr"):
                cells = ["".join(node.text or "" for node in cell.iter(_WORD_NS + "t")) for cell in row.findall(_WORD_NS + "tc")]
                if any(cells):
                    blocks.append(" | ".join(cells))
    return "\n".join(blocks)


def _xlsx(path: Path) -> str:
    with _open_zip(path) as archive:
        shared = _shared_strings(archive)
        workbook = _xml(archive, "xl/workbook.xml")
        relationships = _relationships(archive)
        sheets: list[str] = []
        for sheet in workbook.findall(".//" + _SHEET_NS + "sheet"):
            relation = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
            target = relationships.get(relation)
            if not target:
                continue
            sheet_path = "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
            rows = _sheet_rows(_xml(archive, sheet_path), shared)
            title = sheet.attrib.get("name", "Sheet")
            sheets.append(f"--- Sheet: {title} ---\n" + "\n".join(rows))
    return "\n\n".join(sheets)


def _relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    root = _xml(archive, "xl/_rels/workbook.xml.rels")
    return {item.attrib.get("Id", ""): item.attrib.get("Target", "") for item in root}


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml(archive, "xl/sharedStrings.xml")
    return ["".join(node.text or "" for node in item.iter(_SHEET_NS + "t")) for item in root.findall(_SHEET_NS + "si")]


def _sheet_rows(root: ET.Element, shared: list[str]) -> list[str]:
    """Return rows while retaining omitted cells and formula-only cells.

    Spreadsheet XML commonly omits empty cells.  Their references are needed
    to keep later values aligned with their headers, and formula cells written
    by generators often have no cached ``v`` result at all.
    """
    output: list[str] = []
    for row in root.findall(".//" + _SHEET_NS + "row"):
        values: list[str] = []
        for position, cell in enumerate(row.findall(_SHEET_NS + "c")):
            column = _column_index(cell.attrib.get("r", ""), position)
            if column >= len(values):
                values.extend("" for _ in range(column + 1 - len(values)))
            value = cell.findtext(_SHEET_NS + "v", "")
            if cell.attrib.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                value = shared[int(value)]
            elif cell.attrib.get("t") == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(_SHEET_NS + "t"))
            elif not value:
                formula = cell.findtext(_SHEET_NS + "f", "")
                if formula:
                    value = "=" + formula
            values[column] = value
        if values:
            output.append(" | ".join(values))
    return output


def _column_index(reference: str, fallback: int) -> int:
    """Convert the column letters in an A1 reference to a zero-based index."""
    match = re.match(r"([A-Za-z]+)", reference)
    if not match:
        return fallback
    index = 0
    for letter in match.group(1).upper():
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def _pptx(path: Path) -> str:
    with _open_zip(path) as archive:
        names = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)), key=lambda name: int(re.search(r"\d+", name).group()))
        slides = []
        for number, name in enumerate(names, 1):
            root = _xml(archive, name)
            text = "\n".join(node.text or "" for node in root.iter(_DRAWING_NS + "t") if node.text)
            slides.append(f"--- Slide {number} ---\n{text}")
    return "\n\n".join(slides)
