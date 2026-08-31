"""Bounded text extraction for common office documents supplied to a task."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader
from pptx import Presentation

MAX_DOCUMENT_BYTES = 48 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_SLIDES = 200
MAX_PRESENTATION_SHAPES = 10_000
MAX_PRESENTATION_MEMBERS = 10_000
MAX_PRESENTATION_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_SHEETS = 40
MAX_ROWS_PER_SHEET = 5_000
MAX_COLUMNS_PER_SHEET = 100
MAX_TEXT_CHARACTERS = 120_000
MAX_EBOOK_MEMBERS = 10_000
MAX_EBOOK_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_EBOOK_CHAPTERS = 1_000
MAX_EBOOK_CHAPTER_BYTES = 8 * 1024 * 1024


class DocumentError(Exception):
    """A document cannot be safely or usefully read."""


def read_document(path: Path, *, max_characters: int = MAX_TEXT_CHARACTERS) -> str:
    """Extract readable text from a PDF, DOCX, XLSX, PPTX, or EPUB document.

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
        ".pptx": _pptx_text,
        ".epub": _epub_text,
    }
    extractor = extractors.get(suffix)
    if extractor is None:
        raise DocumentError(
            f"Unsupported document type {suffix or '(no extension)'!r}. "
            "Supported types are PDF (.pdf), Word (.docx), Excel (.xlsx), PowerPoint (.pptx), and EPUB (.epub)."
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


def _pptx_text(path: Path) -> str:
    """Extract slide text, tables, and presenter notes in slide order.

    PPTX is a ZIP container. Check its declared expansion before handing it to
    python-pptx so a small hostile input cannot make the reader allocate an
    unbounded presentation.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            expanded = sum(info.file_size for info in infos)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise DocumentError(f"Could not parse PPTX {path.name!r}: {error}") from error
    if len(infos) > MAX_PRESENTATION_MEMBERS:
        raise DocumentError(
            f"PowerPoint has {len(infos)} package members; limit is {MAX_PRESENTATION_MEMBERS}."
        )
    if expanded > MAX_PRESENTATION_UNCOMPRESSED_BYTES:
        raise DocumentError(
            "PowerPoint expands to "
            f"{expanded} bytes; limit is {MAX_PRESENTATION_UNCOMPRESSED_BYTES}."
        )
    try:
        presentation = Presentation(str(path))
    except Exception as error:
        raise DocumentError(f"Could not parse PPTX {path.name!r}: {error}") from error
    if len(presentation.slides) > MAX_SLIDES:
        raise DocumentError(
            f"PowerPoint has {len(presentation.slides)} slides; limit is {MAX_SLIDES}."
        )

    chunks: list[str] = []
    shape_count = 0
    for number, slide in enumerate(presentation.slides, start=1):
        chunks.append(f"--- Slide {number} ---")
        for shape in _walk_shapes(slide.shapes):
            shape_count += 1
            if shape_count > MAX_PRESENTATION_SHAPES:
                raise DocumentError(
                    "PowerPoint has more than "
                    f"{MAX_PRESENTATION_SHAPES} shapes; limit is {MAX_PRESENTATION_SHAPES}."
                )
            if getattr(shape, "has_table", False):
                chunks.append("--- Table ---")
                for row in shape.table.rows:
                    chunks.append("\t".join(cell.text.replace("\n", " ") for cell in row.cells))
            elif getattr(shape, "has_text_frame", False):
                text = shape.text.strip()
                if text:
                    chunks.append(text)
        # python-pptx exposes a notes text frame when a notes slide exists.
        # It can be absent in malformed or minimal packages, so treat it as
        # optional rather than making slide text unreadable.
        try:
            notes_frame = slide.notes_slide.notes_text_frame
            notes = notes_frame.text.strip() if notes_frame is not None else ""
        except Exception:
            notes = ""
        if notes:
            chunks.append(f"--- Speaker Notes ---\n{notes}")
    return "\n".join(chunks)


def _walk_shapes(shapes: Iterable[object]) -> Iterable[object]:
    """Yield shapes in visual tree order, including shapes inside groups."""
    for shape in shapes:
        yield shape
        # GroupShape is iterable; normal shapes are not. Avoid relying on an
        # implementation-specific type so this remains compatible with
        # python-pptx releases.
        if getattr(shape, "shape_type", None) == 6:  # MSO_SHAPE_TYPE.GROUP
            yield from _walk_shapes(shape)


class _EpubTextParser(HTMLParser):
    """Collect visible text from tolerant XHTML/HTML EPUB chapters."""

    _BLOCK_TAGS = frozenset({"address", "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section", "tr"})
    _SKIP_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skipping += 1
        if tag in self._BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skipping:
            self._skipping -= 1
        if tag in self._BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts).strip()


def _epub_member_path(base: str, href: str) -> str:
    """Resolve an EPUB package-relative href without touching the filesystem."""
    candidate = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(href)))
    if candidate.startswith("../") or candidate == ".." or candidate.startswith("/"):
        raise DocumentError("EPUB package contains an invalid chapter path.")
    return candidate


def _epub_text(path: Path) -> str:
    """Read EPUB XHTML in the publication spine order, under ZIP bounds."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_EBOOK_MEMBERS:
                raise DocumentError(f"EPUB has {len(infos)} package members; limit is {MAX_EBOOK_MEMBERS}.")
            expanded = sum(info.file_size for info in infos)
            if expanded > MAX_EBOOK_UNCOMPRESSED_BYTES:
                raise DocumentError(f"EPUB expands to {expanded} bytes; limit is {MAX_EBOOK_UNCOMPRESSED_BYTES}.")
            names = {info.filename for info in infos}
            if "META-INF/container.xml" not in names:
                raise DocumentError("EPUB has no META-INF/container.xml package descriptor.")
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next((node.get("full-path") for node in container.iter() if node.tag.endswith("rootfile") and node.get("full-path")), None)
            if not rootfile or rootfile not in names:
                raise DocumentError("EPUB package descriptor does not name a readable OPF file.")
            package = ET.fromstring(archive.read(rootfile))
            manifest = {node.get("id"): node.get("href") for node in package.iter() if node.tag.endswith("item") and node.get("id") and node.get("href")}
            spine = [node.get("idref") for node in package.iter() if node.tag.endswith("itemref") and node.get("idref")]
            if not spine:
                raise DocumentError("EPUB package has no reading-order spine.")
            if len(spine) > MAX_EBOOK_CHAPTERS:
                raise DocumentError(f"EPUB has {len(spine)} spine chapters; limit is {MAX_EBOOK_CHAPTERS}.")
            chunks: list[str] = []
            for number, item_id in enumerate(spine, 1):
                href = manifest.get(item_id)
                if not href:
                    continue
                member = _epub_member_path(rootfile, href)
                if member not in names:
                    continue
                info = archive.getinfo(member)
                if info.file_size > MAX_EBOOK_CHAPTER_BYTES:
                    raise DocumentError(f"EPUB chapter {number} exceeds the {MAX_EBOOK_CHAPTER_BYTES}-byte limit.")
                parser = _EpubTextParser()
                parser.feed(archive.read(member).decode("utf-8", errors="replace"))
                parser.close()
                text = parser.text()
                if text:
                    chunks.append(f"--- Chapter {number} ---\n{text}")
            return "\n\n".join(chunks)
    except DocumentError:
        raise
    except (ET.ParseError, OSError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
        raise DocumentError(f"Could not parse EPUB {path.name!r}: {error}") from error
