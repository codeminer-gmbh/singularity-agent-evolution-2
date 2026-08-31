"""Structured reading and creation of common office deliverables.

The workspace itself is intentionally text-only.  This module adds a small,
contained bridge for task documents, so a model need not decode ZIP containers
or PDF bytes by hand.  It does not interpret instructions in documents: it
returns their content as data to the caller.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import Workbook, load_workbook
from odf import table as odf_table
from odf import teletype
from odf import text as odf_text
from odf.opendocument import OpenDocumentText, load as load_odt
from pptx import Presentation
from PIL import Image
from pypdf import PdfReader
import pytesseract
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

_SUPPORTED = frozenset({"pdf", "docx", "xlsx", "pptx", "odt", "png", "jpg", "jpeg", "tif", "tiff", "webp", "zip", "tar", "tgz", "tar.gz", "tar.bz2", "tar.xz"})
_ARCHIVE_FORMATS = frozenset({"zip", "tar", "tgz", "tar.gz", "tar.bz2", "tar.xz"})
_TEXT_MEMBER_SUFFIXES = frozenset({".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".html", ".htm", ".log", ".yaml", ".yml"})
_IMAGE_FORMATS = frozenset({"png", "jpg", "jpeg", "tif", "tiff", "webp"})
_MAX_EXTRACTED_CHARACTERS = 60_000
_MAX_OCR_PAGES = 12
_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 60
_MAX_ARCHIVE_UNPACKED_BYTES = 50 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_BYTES = 12 * 1024 * 1024


class DocumentError(Exception):
    """A requested document operation cannot be completed."""


def document_format(path: str, supplied: str | None = None) -> str:
    """Return and validate a document format from an explicit value or suffix."""
    candidate = (supplied or _format_from_path(path)).lower().strip()
    if candidate not in _SUPPORTED:
        choices = ", ".join(sorted(_SUPPORTED))
        raise DocumentError(f"Unsupported document format {candidate!r}; use one of {choices}.")
    return candidate


def _format_from_path(path: str) -> str:
    """Recognize compound archive suffixes before falling back to the last suffix."""
    lowered = path.lower().strip()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lowered.endswith(suffix):
            return suffix.removeprefix(".")
    return Path(path).suffix.removeprefix(".")


def extract(path: Path, kind: str) -> str:
    """Extract readable text and simple structure from one office file."""
    try:
        if path.stat().st_size > _MAX_DOCUMENT_BYTES:
            raise DocumentError(f"{path.name!r} is too large to extract safely (limit: 50 MiB).")
        if kind in _ARCHIVE_FORMATS:
            result = _extract_archive(path, kind)
        elif kind == "pdf":
            result = _extract_pdf(path)
        elif kind in _IMAGE_FORMATS:
            result = _extract_image(path)
        elif kind == "docx":
            result = _extract_docx(path)
        elif kind == "xlsx":
            result = _extract_xlsx(path)
        elif kind == "pptx":
            result = _extract_pptx(path)
        else:
            result = _extract_odt(path)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as failure:
        raise DocumentError(f"Could not read {path.name!r} as {kind}: {failure}") from failure
    return _truncate(result)


def create(path: Path, kind: str, content: str, title: str = "") -> None:
    """Create one document from text (or CSV/JSON rows for a spreadsheet)."""
    if kind in _ARCHIVE_FORMATS:
        raise DocumentError(f"Archives are readable inputs, not create_document formats: {kind}.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "pdf":
            _create_pdf(path, content, title)
        elif kind == "docx":
            _create_docx(path, content, title)
        elif kind == "xlsx":
            _create_xlsx(path, content, title)
        elif kind == "pptx":
            _create_pptx(path, content, title)
        else:
            _create_odt(path, content, title)
    except (OSError, ValueError, TypeError) as failure:
        raise DocumentError(f"Could not create {path.name!r} as {kind}: {failure}") from failure



def _extract_archive(path: Path, kind: str) -> str:
    """Return a bounded manifest plus readable members of an archive.

    Members are never extracted to their declared paths: this avoids archive path
    traversal entirely while still allowing office parsers to consume a temporary
    file with the member's suffix.
    """
    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as archive:
                members = [(item.filename, item.file_size, lambda item=item: archive.read(item))
                           for item in archive.infolist() if not item.is_dir()]
                return _render_archive_members(path.name, members)
        with tarfile.open(path, mode="r:*") as archive:
            members = []
            for item in archive.getmembers():
                if not item.isfile():
                    continue
                def read_item(item: tarfile.TarInfo = item) -> bytes:
                    stream = archive.extractfile(item)
                    return b"" if stream is None else stream.read()
                members.append((item.name, item.size, read_item))
            return _render_archive_members(path.name, members)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as failure:
        raise DocumentError(f"Could not read {path.name!r} as an archive: {failure}") from failure


def _render_archive_members(archive_name: str, members: list[tuple[str, int, Any]]) -> str:
    """Read supported archive members within count and decompression budgets."""
    parts = [f"# Archive: {archive_name}"]
    total = 0
    for number, (name, size, read_member) in enumerate(members, 1):
        if number > _MAX_ARCHIVE_MEMBERS:
            parts.append(f"[member limit reached: first {_MAX_ARCHIVE_MEMBERS} files shown]")
            break
        if size < 0 or size > _MAX_ARCHIVE_MEMBER_BYTES or total + size > _MAX_ARCHIVE_UNPACKED_BYTES:
            parts.append(f"## {name}\n[skipped: exceeds archive extraction size budget]")
            continue
        total += size
        try:
            payload = read_member()
            if len(payload) > _MAX_ARCHIVE_MEMBER_BYTES:
                parts.append(f"## {name}\n[skipped: exceeds archive extraction size budget]")
                continue
            parts.append(f"## {name}\n{_extract_archive_member(name, payload)}")
        except (OSError, ValueError, DocumentError) as failure:
            parts.append(f"## {name}\n[unreadable member: {failure}]")
    return "\n\n".join(parts) if len(parts) > 1 else f"# Archive: {archive_name}\n[Archive contains no files]"


def _extract_archive_member(name: str, payload: bytes) -> str:
    """Decode a textual member or delegate an office member to its extractor."""
    suffix = Path(name).suffix.lower()
    if suffix in _TEXT_MEMBER_SUFFIXES:
        return payload[:_MAX_DOCUMENT_BYTES].decode("utf-8", errors="replace") or "[empty text file]"
    kind = _format_from_path(name)
    if kind not in _SUPPORTED:
        return f"[binary or unsupported member ({suffix or 'no extension'})]"
    with tempfile.TemporaryDirectory(prefix="agent-archive-") as directory:
        member_path = Path(directory) / f"member.{kind.replace('.', '_')}"
        # Keep a meaningful final suffix for libraries which inspect it.
        member_path = member_path.with_suffix("." + kind.split(".")[-1])
        member_path.write_bytes(payload)
        return extract(member_path, kind)


def _extract_pdf(path: Path) -> str:
    """Read native PDF text, OCRing only pages whose text layer is empty."""
    reader = PdfReader(str(path))
    parts: list[str] = []
    for number, page in enumerate(reader.pages, 1):
        native = (page.extract_text() or "").strip()
        # OCR is deliberately a fallback: native PDF text is more accurate and
        # avoids needless rasterization of ordinary, digitally-created files.
        if len(native) < 20 and number <= _MAX_OCR_PAGES:
            ocr = _ocr_pdf_page(path, number)
            if ocr:
                native = f"[OCR]\n{ocr}"
        parts.append(f"## Page {number}\n{native}")
    if len(reader.pages) > _MAX_OCR_PAGES:
        parts.append(f"[OCR limited to the first {_MAX_OCR_PAGES} scanned pages]")
    return "\n\n".join(parts) or "[PDF contains no extractable text]"


def _extract_image(path: Path) -> str:
    """OCR a supported raster image without treating its embedded text as instructions."""
    with Image.open(path) as image:
        image.load()
        # Avoid pathological image dimensions consuming the agent's task budget.
        if image.width * image.height > 40_000_000:
            raise DocumentError("Image has more than 40 million pixels.")
        text = pytesseract.image_to_string(image).strip()
    return text or "[Image contains no text recognized by OCR]"


def _ocr_pdf_page(path: Path, page_number: int) -> str:
    """Rasterize one scan page at readable resolution, then feed it to Tesseract."""
    with tempfile.TemporaryDirectory(prefix="agent-ocr-") as directory:
        rendered = Path(directory) / "page"
        subprocess.run(
            ["pdftoppm", "-f", str(page_number), "-l", str(page_number), "-r", "200", "-png", "-singlefile", str(path), str(rendered)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=45,
        )
        image_path = rendered.with_suffix(".png")
        if not image_path.is_file():
            return ""
        return _extract_image(image_path)


def _extract_docx(path: Path) -> str:
    document = Document(path)
    parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    for number, table in enumerate(document.tables, 1):
        parts.append(f"## Table {number}")
        parts.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
    return "\n".join(parts) or "[Document contains no paragraphs or tables]"


def _extract_xlsx(path: Path) -> str:
    book = load_workbook(path, data_only=False, read_only=True)
    parts: list[str] = []
    for sheet in book.worksheets:
        parts.append(f"## Sheet: {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            if any(value is not None for value in row):
                parts.append(" | ".join("" if value is None else str(value) for value in row))
    return "\n".join(parts) or "[Workbook contains no populated cells]"


def _extract_pptx(path: Path) -> str:
    presentation = Presentation(path)
    parts: list[str] = []
    for number, slide in enumerate(presentation.slides, 1):
        texts = [shape.text for shape in slide.shapes if hasattr(shape, "text") and shape.text.strip()]
        parts.append(f"## Slide {number}\n" + "\n".join(texts))
    return "\n\n".join(parts) or "[Presentation contains no text]"


def _extract_odt(path: Path) -> str:
    document = load_odt(str(path))
    parts: list[str] = []
    # Paragraphs and headings retain the document's readable narrative; tables
    # are separately labelled so a caller can distinguish their cell values.
    for element in document.getElementsByType(odf_text.H):
        value = teletype.extractText(element).strip()
        if value:
            parts.append(f"# {value}")
    for element in document.getElementsByType(odf_text.P):
        value = teletype.extractText(element).strip()
        if value:
            parts.append(value)
    for number, table in enumerate(document.getElementsByType(odf_table.Table), 1):
        parts.append(f"## Table {number}")
        for row in table.getElementsByType(odf_table.TableRow):
            cells = row.getElementsByType(odf_table.TableCell)
            values = [teletype.extractText(cell).strip() for cell in cells]
            if any(values):
                parts.append(" | ".join(values))
    return "\n".join(parts) or "[OpenDocument contains no paragraphs or tables]"


def _create_odt(path: Path, content: str, title: str) -> None:
    document = OpenDocumentText()
    if title:
        document.text.addElement(odf_text.H(outlinelevel=1, text=title))
    for line in content.splitlines():
        if line.startswith("# "):
            document.text.addElement(odf_text.H(outlinelevel=1, text=line[2:]))
        elif line.startswith("## "):
            document.text.addElement(odf_text.H(outlinelevel=2, text=line[3:]))
        elif line:
            document.text.addElement(odf_text.P(text=line))
    document.save(str(path))


def _create_pdf(path: Path, content: str, title: str) -> None:
    canvas = Canvas(str(path), pagesize=letter)
    canvas.setTitle(title or path.stem)
    width, height = letter
    y = height - 54
    for line in ([title] if title else []) + content.splitlines():
        for wrapped in _wrap(line, 95) or [""]:
            if y < 54:
                canvas.showPage()
                y = height - 54
            canvas.drawString(54, y, wrapped)
            y -= 14
    canvas.save()


def _create_docx(path: Path, content: str, title: str) -> None:
    document = Document()
    if title:
        document.add_heading(title, level=0)
    for line in content.splitlines():
        if line.startswith("# "):
            document.add_heading(line[2:], level=1)
        elif line.startswith("## "):
            document.add_heading(line[3:], level=2)
        elif line:
            document.add_paragraph(line)
    document.save(path)


def _rows(content: str) -> list[list[Any]]:
    try:
        decoded = json.loads(content)
        if isinstance(decoded, list) and all(isinstance(row, list) for row in decoded):
            return decoded
    except json.JSONDecodeError:
        pass
    return list(csv.reader(io.StringIO(content)))


def _create_xlsx(path: Path, content: str, title: str) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = (title or "Sheet1")[:31]
    for row in _rows(content):
        sheet.append(row)
    book.save(path)


def _create_pptx(path: Path, content: str, title: str) -> None:
    presentation = Presentation()
    chunks = content.split("\n---\n") or [""]
    for number, chunk in enumerate(chunks):
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        heading, _, body = chunk.partition("\n")
        slide.shapes.title.text = heading or title or f"Slide {number + 1}"
        slide.placeholders[1].text = body
    presentation.save(path)


def _wrap(text: str, width: int) -> list[str]:
    return [text[index : index + width] for index in range(0, len(text), width)]


def _truncate(text: str) -> str:
    if len(text) <= _MAX_EXTRACTED_CHARACTERS:
        return text
    return f"{text[:_MAX_EXTRACTED_CHARACTERS]}\n... [truncated at {_MAX_EXTRACTED_CHARACTERS} characters]"
