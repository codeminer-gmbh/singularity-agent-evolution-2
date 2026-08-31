"""Bounded text extraction from common office document formats.

These parsers let a task's supplied binary attachment become model-readable
text while leaving the attachment itself untouched. Extraction is deliberately
text-only: image OCR is not silently guessed, and output has a fixed budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader
from pptx import Presentation

_MAX_DOCUMENT_BYTES: Final = 40 * 1024 * 1024
_MAX_ARCHIVE_CONTENT_BYTES: Final = 80 * 1024 * 1024
_MAX_CHARACTERS: Final = 80_000
_SUPPORTED_SUFFIXES: Final = frozenset({".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"})


class DocumentError(Exception):
    """A document was unsupported, unsafe to process, or unreadable."""


def extract_text(path: Path, *, max_characters: int = _MAX_CHARACTERS) -> str:
    """Extract text from a supported office document within fixed bounds."""
    if not isinstance(max_characters, int) or isinstance(max_characters, bool) or not 100 <= max_characters <= _MAX_CHARACTERS:
        raise DocumentError(f"max_characters must be an integer from 100 to {_MAX_CHARACTERS}.")
    if not path.is_file():
        raise DocumentError("The document path is not a regular file.")
    try:
        size = path.stat().st_size
    except OSError as failure:
        raise DocumentError(f"Could not inspect the document: {failure}") from failure
    if size > _MAX_DOCUMENT_BYTES:
        raise DocumentError(f"The document is too large to extract (maximum {_MAX_DOCUMENT_BYTES} bytes).")
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(_SUPPORTED_SUFFIXES))
        raise DocumentError(f"Unsupported document type {suffix or '(none)'}; supported types: {supported}.")
    try:
        if suffix == ".pdf":
            chunks = _pdf_text(path)
        elif suffix == ".docx":
            chunks = _docx_text(path)
        elif suffix == ".xlsx":
            chunks = _xlsx_text(path)
        elif suffix == ".pptx":
            chunks = _pptx_text(path)
        else:
            chunks = _opendocument_text(path, suffix)
    except DocumentError:
        raise
    except Exception as failure:
        raise DocumentError(f"Could not extract text from {path.name}: {failure}") from failure
    text = "\n\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    if len(text) > max_characters:
        return text[:max_characters] + f"\n\n... [truncated at {max_characters} characters]"
    return text or "[No extractable text found in this document.]"


def _pdf_text(path: Path) -> list[str]:
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise DocumentError("The PDF is encrypted and cannot be read without its password.")
    return [f"[Page {number}]\n{page.extract_text() or ''}" for number, page in enumerate(reader.pages, 1)]


def _docx_text(path: Path) -> list[str]:
    document = Document(str(path))
    chunks = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            chunks.append(" | ".join(cell.text.replace("\n", " ") for cell in row.cells))
    return chunks


def _xlsx_text(path: Path) -> list[str]:
    workbook = load_workbook(str(path), read_only=True, data_only=True)
    try:
        chunks: list[str] = []
        for sheet in workbook.worksheets:
            chunks.append(f"[Sheet: {sheet.title}]")
            for row in sheet.iter_rows(values_only=True):
                values = [str(value) for value in row if value is not None]
                if values:
                    chunks.append(" | ".join(values))
        return chunks
    finally:
        workbook.close()


def _pptx_text(path: Path) -> list[str]:
    presentation = Presentation(str(path))
    chunks: list[str] = []
    for number, slide in enumerate(presentation.slides, 1):
        lines = [shape.text for shape in slide.shapes if hasattr(shape, "text") and shape.text.strip()]
        if lines:
            chunks.append(f"[Slide {number}]\n" + "\n".join(lines))
    return chunks


def _opendocument_text(path: Path, suffix: str) -> list[str]:
    """Read ``content.xml`` from ODT/ODS/ODP without expanding archive bombs."""
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 10_000 or sum(info.file_size for info in infos) > _MAX_ARCHIVE_CONTENT_BYTES:
                raise DocumentError("The OpenDocument archive expands beyond the extraction limit.")
            try:
                content = archive.read("content.xml")
            except KeyError as failure:
                raise DocumentError("The OpenDocument archive has no content.xml.") from failure
    except BadZipFile as failure:
        raise DocumentError("The OpenDocument file is not a valid ZIP archive.") from failure
    if len(content) > _MAX_ARCHIVE_CONTENT_BYTES:
        raise DocumentError("The OpenDocument content is too large to extract.")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as failure:
        raise DocumentError("The OpenDocument content.xml is not valid XML.") from failure
    if suffix == ".ods":
        return _ods_text(root)
    return _od_text(root, "Slide" if suffix == ".odp" else None)


def _local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _od_text(root: ElementTree.Element, label: str | None) -> list[str]:
    chunks: list[str] = []
    page = 0
    for element in root.iter():
        name = _local_name(element)
        if label and name == "page":
            page += 1
        if name in {"p", "h", "list-item"}:
            text = "".join(element.itertext()).strip()
            if text:
                prefix = f"[{label} {page}]\n" if label and page else ""
                chunks.append(prefix + text)
    return chunks


def _ods_text(root: ElementTree.Element) -> list[str]:
    chunks: list[str] = []
    for table in root.iter():
        if _local_name(table) != "table":
            continue
        name = next((value for key, value in table.attrib.items() if key.rsplit("}", 1)[-1] == "name"), "Untitled")
        chunks.append(f"[Sheet: {name}]")
        for row in table:
            if _local_name(row) != "table-row":
                continue
            cells: list[str] = []
            for cell in row:
                if _local_name(cell) not in {"table-cell", "covered-table-cell"}:
                    continue
                value = "".join(cell.itertext()).strip()
                # Repeated empty cells only represent layout; do not turn one XML
                # attribute into an unbounded Python list.
                if value:
                    cells.append(value)
            if cells:
                chunks.append(" | ".join(cells))
    return chunks
