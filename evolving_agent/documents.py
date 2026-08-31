"""Bounded text extraction for common office documents supplied to a task."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader

MAX_DOCUMENT_BYTES = 48 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_SHEETS = 40
MAX_ROWS_PER_SHEET = 5_000
MAX_COLUMNS_PER_SHEET = 100
MAX_TEXT_CHARACTERS = 120_000


class DocumentError(Exception):
    """A document cannot be safely or usefully read."""


def read_document(path: Path, *, max_characters: int = MAX_TEXT_CHARACTERS) -> str:
    """Extract readable text from a PDF, DOCX, or XLSX document.

    The returned text is deliberately bounded: office files are untrusted input
    and the caller is a language model with a finite context window.
    """
    if not path.is_file():
        raise DocumentError(f"{path.name!r} is not a document file.")
    size = path.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise DocumentError(
            f"{path.name!r} exceeds the {MAX_DOCUMENT_BYTES}-byte document limit."
        )
    if not isinstance(max_characters, int) or not 1 <= max_characters <= MAX_TEXT_CHARACTERS:
        raise DocumentError(
            f"max_characters must be an integer from 1 through {MAX_TEXT_CHARACTERS}."
        )
    suffix = path.suffix.lower()
    extractors: dict[str, Callable[[Path], str]] = {
        ".pdf": _pdf_text,
        ".docx": _docx_text,
        ".xlsx": _xlsx_text,
    }
    extractor = extractors.get(suffix)
    if extractor is None:
        raise DocumentError(
            f"Unsupported document type {suffix or '(no extension)'!r}. "
            "Supported types are PDF (.pdf), Word (.docx), and Excel (.xlsx)."
        )
    try:
        text = extractor(path)
    except DocumentError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise DocumentError(f"Could not read {path.name!r}: {error}") from error
    if len(text) > max_characters:
        return f"{text[:max_characters]}\n... [truncated at {max_characters} characters]"
    return text or "[The document contains no extractable text.]"


def _pdf_text(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
    except Exception as error:  # parser-specific errors vary by pypdf release
        raise DocumentError(f"Could not parse PDF {path.name!r}: {error}") from error
    if reader.is_encrypted:
        raise DocumentError(f"PDF {path.name!r} is encrypted and cannot be read without a password.")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise DocumentError(f"PDF has {len(reader.pages)} pages; limit is {MAX_PDF_PAGES}.")
    chunks: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as error:
            raise DocumentError(f"Could not extract page {number} of PDF: {error}") from error
        chunks.append(f"--- Page {number} ---\n{text.strip()}")
    return "\n\n".join(chunks)


def _docx_text(path: Path) -> str:
    try:
        document = Document(str(path))
    except Exception as error:
        raise DocumentError(f"Could not parse DOCX {path.name!r}: {error}") from error
    chunks = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    for table_number, table in enumerate(document.tables, start=1):
        chunks.append(f"--- Table {table_number} ---")
        for row in table.rows:
            chunks.append("\t".join(cell.text.replace("\n", " ") for cell in row.cells))
    return "\n".join(chunks)


def _xlsx_text(path: Path) -> str:
    try:
        workbook = load_workbook(str(path), read_only=True, data_only=False)
    except Exception as error:
        raise DocumentError(f"Could not parse XLSX {path.name!r}: {error}") from error
    try:
        if len(workbook.worksheets) > MAX_SHEETS:
            raise DocumentError(f"Workbook has {len(workbook.worksheets)} sheets; limit is {MAX_SHEETS}.")
        chunks: list[str] = []
        for sheet in workbook.worksheets:
            chunks.append(f"--- Sheet: {sheet.title} ---")
            row_count = 0
            for row in sheet.iter_rows(max_col=MAX_COLUMNS_PER_SHEET, values_only=True):
                row_count += 1
                if row_count > MAX_ROWS_PER_SHEET:
                    chunks.append(f"... [sheet truncated at {MAX_ROWS_PER_SHEET} rows]")
                    break
                values = ["" if value is None else str(value) for value in row]
                # Preserve interior empty cells but omit uninformative trailing cells.
                while values and not values[-1]:
                    values.pop()
                if values:
                    chunks.append("\t".join(values))
        return "\n".join(chunks)
    finally:
        workbook.close()
