"""Bounded text and metadata extraction for common task artifacts.

Office files are ZIP/XML containers, so DOCX, XLSX, PPTX, and ODT can be read
without launching an office suite.  PDF and image decoding use maintained
libraries.  This module only reads a caller-provided, already-contained path.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path
from xml.etree import ElementTree as ET

_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 2_000
_MAX_MEMBER_BYTES = 30 * 1024 * 1024
_OCR_TIMEOUT_SECONDS = 45
_PDF_RENDER_TIMEOUT_SECONDS = 30
_MAX_PDF_OCR_PAGES = 10


class DocumentError(Exception):
    """An artifact could not be interpreted as the requested document type."""


def extract_document(path: Path, *, max_characters: int = 20_000, max_pages: int = 100) -> str:
    """Return useful, bounded text or metadata from one document.

    Args:
        path: A resolved regular file inside one of the agent's allowed trees.
        max_characters: Largest returned body, excluding the truncation marker.
        max_pages: Largest number of PDF pages or presentation slides to inspect.
    """
    if not path.is_file():
        raise DocumentError(f"{path.name!r} is not a file.")
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise DocumentError(f"{path.name!r} is larger than the 100 MiB document limit.")
    if max_characters < 100 or max_characters > 100_000:
        raise DocumentError("max_characters must be between 100 and 100000.")
    if max_pages < 1 or max_pages > 1_000:
        raise DocumentError("max_pages must be between 1 and 1000.")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        body = _pdf(path, max_pages)
        kind = "PDF"
    elif suffix == ".docx":
        body = _docx(path)
        kind = "DOCX"
    elif suffix == ".xlsx":
        body = _xlsx(path)
        kind = "XLSX"
    elif suffix == ".pptx":
        body = _pptx(path, max_pages)
        kind = "PPTX"
    elif suffix in {".odt", ".ods", ".odp"}:
        body = _opendocument(path)
        kind = suffix[1:].upper()
    elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tiff", ".bmp"}:
        body = _image(path)
        kind = "image"
    else:
        raise DocumentError(
            "Unsupported document type. Supported: PDF, DOCX, XLSX, PPTX, "
            "ODT/ODS/ODP, PNG, JPEG, GIF, WebP, TIFF, BMP."
        )
    return _bounded(f"{kind} extraction: {path.name}\n\n{body}", max_characters)


def _safe_zip(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise DocumentError(f"{path.name!r} is not a readable office document: {error}") from error
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_MEMBERS or any(info.file_size > _MAX_MEMBER_BYTES for info in infos):
        archive.close()
        raise DocumentError("Document archive exceeds safe extraction limits.")
    return archive


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(name))
    except KeyError as error:
        raise DocumentError(f"Document is missing required part {name!r}.") from error
    except ET.ParseError as error:
        raise DocumentError(f"Document part {name!r} is invalid XML.") from error


def _text(nodes: Iterable[ET.Element]) -> str:
    return "".join(node.text or "" for node in nodes)


def _docx(path: Path) -> str:
    with _safe_zip(path) as archive:
        root = _xml(archive, "word/document.xml")
    paragraphs = []
    for paragraph in root.findall(".//{*}p"):
        value = _text(paragraph.findall(".//{*}t")).strip()
        if value:
            paragraphs.append(value)
    return "\n".join(paragraphs) or "[No text was found in this document.]"


def _xlsx(path: Path) -> str:
    with _safe_zip(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = _xml(archive, "xl/sharedStrings.xml")
            shared = [_text(item.findall(".//{*}t")) for item in shared_root.findall(".//{*}si")]
        sheets = sorted(name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
        if not sheets:
            raise DocumentError("Workbook has no readable worksheets.")
        output = []
        for name in sheets:
            root = _xml(archive, name)
            output.append(f"## {Path(name).stem}")
            for row in root.findall(".//{*}row"):
                cells = []
                for cell in row.findall("{*}c"):
                    value = cell.findtext("{*}v", default="")
                    if cell.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    elif cell.get("t") == "inlineStr":
                        value = _text(cell.findall(".//{*}t"))
                    cells.append(f"{cell.get('r', '?')}={value}")
                if cells:
                    output.append(" | ".join(cells))
    return "\n".join(output)


def _pptx(path: Path, max_slides: int) -> str:
    with _safe_zip(path) as archive:
        slides = sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)), key=lambda n: int(re.search(r"\d+", Path(n).stem).group()))
        output = []
        for number, name in enumerate(slides[:max_slides], 1):
            root = _xml(archive, name)
            value = _text(root.findall(".//{*}t")).strip()
            output.append(f"## Slide {number}\n{value or '[No text]'}")
    return "\n\n".join(output) or "[No slides were found.]"


def _opendocument(path: Path) -> str:
    with _safe_zip(path) as archive:
        root = _xml(archive, "content.xml")
    paragraphs = [_text(node.findall(".//{*}span")).strip() or _text(node).strip() for node in root.findall(".//{*}p")]
    return "\n".join(value for value in paragraphs if value) or "[No text was found.]"


def _pdf(path: Path, max_pages: int) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:  # Helpful if someone runs source without its image.
        raise DocumentError("PDF support is unavailable because pypdf is not installed.") from error
    try:
        reader = PdfReader(path)
        extracted = [(number, page.extract_text() or "") for number, page in enumerate(reader.pages[:max_pages], 1)]
    except Exception as error:
        raise DocumentError(f"Could not read PDF: {error}") from error

    output = []
    ocr_remaining = _MAX_PDF_OCR_PAGES
    for number, text in extracted:
        if not text.strip() and ocr_remaining:
            text = _ocr_pdf_page(path, number)
            ocr_remaining -= 1
        elif not text.strip():
            text = "[No extractable text; OCR page limit reached.]"
        output.append(f"## Page {number}\n{text.strip() or '[No readable text was found on this page.]'}")
    return "\n\n".join(output) or "[The PDF has no pages.]"


def _ocr_pdf_page(path: Path, page_number: int) -> str:
    """Rasterize one known PDF page in a private directory, then OCR it."""
    with tempfile.TemporaryDirectory(prefix="agent-pdf-ocr-") as directory:
        prefix = Path(directory) / "page"
        try:
            rendered = subprocess.run(
                ["pdftoppm", "-f", str(page_number), "-l", str(page_number), "-r", "200", "-png", "-singlefile", str(path), str(prefix)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=_PDF_RENDER_TIMEOUT_SECONDS, check=False,
            )
        except FileNotFoundError as error:
            raise DocumentError("Scanned-PDF OCR is unavailable because Poppler is not installed.") from error
        except subprocess.TimeoutExpired as error:
            raise DocumentError(f"PDF rendering exceeded the {_PDF_RENDER_TIMEOUT_SECONDS}-second limit.") from error
        image = prefix.with_suffix(".png")
        if rendered.returncode or not image.is_file():
            detail = rendered.stderr.strip() or f"exit status {rendered.returncode}"
            raise DocumentError(f"Could not render PDF page for OCR: {detail}")
        return _ocr_image(image)


def _image(path: Path) -> str:
    try:
        from PIL import Image
    except ImportError as error:
        raise DocumentError("Image support is unavailable because Pillow is not installed.") from error
    try:
        with Image.open(path) as image:
            details = [f"format: {image.format}", f"dimensions: {image.width} x {image.height}", f"mode: {image.mode}", f"frames: {getattr(image, 'n_frames', 1)}"]
            if image.info.get("dpi"):
                details.append(f"dpi: {image.info['dpi']}")
    except Exception as error:
        raise DocumentError(f"Could not read image: {error}") from error

    details.append("OCR text:")
    text = _ocr_image(path)
    details.append(text or "[No readable text was found in this image.]")
    return "\n".join(details)


def _ocr_image(path: Path) -> str:
    """Recognize a validated raster image with the image-packaged engine."""
    try:
        completed = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "3"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=_OCR_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError as error:
        raise DocumentError("Image OCR is unavailable because Tesseract is not installed.") from error
    except subprocess.TimeoutExpired as error:
        raise DocumentError(f"Image OCR exceeded the {_OCR_TIMEOUT_SECONDS}-second limit.") from error
    text = completed.stdout.strip()
    if completed.returncode and not text:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise DocumentError(f"Could not OCR image: {detail}")
    return text


def _bounded(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    return f"{text[:maximum]}\n... [truncated at {maximum} characters]"
