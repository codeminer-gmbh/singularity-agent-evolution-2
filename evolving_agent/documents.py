"""Bounded extraction of evidence from PDFs, images, and Office Open XML files."""

from __future__ import annotations

import re
import tempfile
import xml.etree.ElementTree as element_tree
import zipfile
from pathlib import Path
from typing import Callable

MAX_TEXT_CHARACTERS = 24_000
"""Enough evidence for a model, without one attachment consuming a turn."""


class DocumentError(Exception):
    """A document is unsupported, malformed, or could not be inspected."""


def inspect_document(
    path: Path,
    *,
    page: int = 1,
    ocr: bool = False,
    run: Callable[[list[str]], tuple[int | None, str, str]],
) -> str:
    """Return text and useful context from one document without modifying it.

    ``run`` is supplied by the workspace tool so every external program shares
    its command timeout and process cleanup.  A PDF page first gets its embedded
    text; rendering/OCR is used when requested or when that page is scanned.
    """
    if page < 1:
        raise DocumentError("'page' must be a positive integer.")
    suffix = path.suffix.lower()
    if suffix == ".pdf" or _magic(path, b"%PDF-"):
        return _pdf(path, page, ocr, run)
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        return _image(path, run)
    if suffix in {".docx", ".pptx", ".xlsx"}:
        return _ooxml(path, suffix)
    raise DocumentError(
        "Supported document formats are PDF, PNG/JPEG/TIFF/BMP/WebP images, and DOCX/PPTX/XLSX."
    )


def _pdf(path: Path, page: int, ocr: bool, run: Callable[[list[str]], tuple[int | None, str, str]]) -> str:
    code, info, error = run(["pdfinfo", str(path)])
    if code != 0:
        raise DocumentError(f"pdfinfo could not read the PDF: {_reason(error)}")
    pages = _pdf_pages(info)
    if pages is not None and page > pages:
        raise DocumentError(f"The PDF has {pages} page(s); page {page} does not exist.")
    code, text, error = run(["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(path), "-"])
    if code != 0:
        raise DocumentError(f"pdftotext could not extract page {page}: {_reason(error)}")
    text = _bounded(text)
    header = f"PDF page {page}" + (f" of {pages}" if pages is not None else "")
    # A scanned page commonly emits whitespace only. The automatic fallback is
    # important because a model cannot infer from a file listing that it needs OCR.
    if ocr or len(text.strip()) < 20:
        return _ocr_pdf(path, page, header, text, run)
    return f"{header} (embedded text)\n---\n{text}"


def _ocr_pdf(path: Path, page: int, header: str, embedded: str, run: Callable[[list[str]], tuple[int | None, str, str]]) -> str:
    # Render into a private temporary directory, never beside a read-only task
    # attachment.  The command runner still owns timeout and child cleanup.
    with tempfile.TemporaryDirectory(prefix="agent-pdf-ocr-") as directory:
        prefix = str(Path(directory) / "page")
        code, _, error = run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "200", "-png", "-singlefile", str(path), prefix])
        image = Path(prefix + ".png")
        if code != 0 or not image.is_file():
            raise DocumentError(f"Could not render PDF page {page} for OCR: {_reason(error)}")
        code, text, error = run(["tesseract", str(image), "stdout", "--psm", "3"])
        if code != 0:
            raise DocumentError(f"Tesseract could not OCR PDF page {page}: {_reason(error)}")
    label = "OCR" if not embedded.strip() else "embedded text plus OCR"
    return f"{header} ({label})\n---\n{_bounded(text)}"


def _image(path: Path, run: Callable[[list[str]], tuple[int | None, str, str]]) -> str:
    code, text, error = run(["tesseract", str(path), "stdout", "--psm", "3"])
    if code != 0:
        raise DocumentError(f"Tesseract could not read this image: {_reason(error)}")
    return f"Image OCR ({path.name})\n---\n{_bounded(text)}"


def _ooxml(path: Path, suffix: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            if suffix == ".docx":
                parts = ["word/document.xml"]
            elif suffix == ".pptx":
                parts = sorted(name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
            else:
                parts = ["xl/sharedStrings.xml"]
            text = "\n".join(_xml_text(archive.read(part)) for part in parts if part in archive.namelist())
    except (OSError, zipfile.BadZipFile, element_tree.ParseError) as error:
        raise DocumentError(f"Could not read Office document: {error}") from error
    if not text.strip():
        raise DocumentError("No readable text was found in this Office document.")
    return f"{suffix[1:].upper()} extracted text ({path.name})\n---\n{_bounded(text)}"


def _xml_text(data: bytes) -> str:
    root = element_tree.fromstring(data)
    # OOXML stores visible words in <w:t>, <a:t>, and <t>; joining descendants
    # also handles spreadsheet shared strings without format-specific namespaces.
    return " ".join(node.text.strip() for node in root.iter() if node.text and node.text.strip())


def _pdf_pages(info: str) -> int | None:
    match = re.search(r"^Pages:\s*(\d+)", info, re.MULTILINE)
    return int(match.group(1)) if match else None


def _magic(path: Path, wanted: bytes) -> bool:
    try:
        with path.open("rb") as source:
            return source.read(len(wanted)) == wanted
    except OSError as error:
        raise DocumentError(f"Could not read document: {error}") from error


def _reason(error: str) -> str:
    return error.strip() or "no diagnostic was returned"


def _bounded(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARACTERS:
        return text
    return text[:MAX_TEXT_CHARACTERS] + f"\n... [truncated at {MAX_TEXT_CHARACTERS} characters]"
