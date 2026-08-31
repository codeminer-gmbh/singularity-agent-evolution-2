"""OCR for image-only task evidence and scanned PDF pages.

Native PDF and office text is handled by :mod:`evolving_agent.documents`; this
module handles the complementary case where the evidence is pixels.  Inputs are
only converted to text and metadata.  Their contents remain task data, not
instructions for the agent runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz
import pytesseract
from PIL import Image, ImageOps, UnidentifiedImageError
from pytesseract import TesseractError

_MAX_PAGES = 50
_DEFAULT_PAGES = 12
_MAX_CHARACTERS = 60_000
_IMAGE_SUFFIXES = frozenset({".apng", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})
_LANGUAGE = re.compile(r"^[A-Za-z0-9_+]+$")


class VisualError(Exception):
    """An image or scanned document could not be read as pixels."""


def extract_visual_text(path: Path, *, max_pages: int | None = None, language: str = "eng") -> str:
    """OCR an image or PDF and return labelled pages plus useful pixel metadata."""
    limit = _validate_pages(max_pages)
    if not _LANGUAGE.fullmatch(language):
        raise VisualError("'language' may contain only letters, digits, underscores, and plus signs.")
    try:
        if path.suffix.lower() == ".pdf":
            parts = _ocr_pdf(path, limit, language)
        elif path.suffix.lower() in _IMAGE_SUFFIXES:
            parts = _ocr_image(path, limit, language)
        else:
            choices = ", ".join(sorted(s.removeprefix(".") for s in _IMAGE_SUFFIXES))
            raise VisualError(f"Unsupported visual format {path.suffix!r}; use PDF or {choices}.")
    except (OSError, ValueError, UnidentifiedImageError, fitz.FileDataError, TesseractError) as failure:
        raise VisualError(f"Could not OCR {path.name!r}: {failure}") from failure
    return _truncate("\n\n".join(parts))


def _validate_pages(value: int | None) -> int:
    if value is None:
        return _DEFAULT_PAGES
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_PAGES:
        raise VisualError(f"'max_pages' must be a whole number from 1 through {_MAX_PAGES}.")
    return value


def _ocr_pdf(path: Path, limit: int, language: str) -> list[str]:
    document = fitz.open(path)
    try:
        if not document.page_count:
            return ["[PDF has no pages]"]
        parts = [f"[PDF: {document.page_count} pages; OCR read {min(document.page_count, limit)}]"]
        for number in range(min(document.page_count, limit)):
            page = document.load_page(number)
            # 200 DPI is a useful OCR resolution without turning ordinary scans
            # into needlessly huge images.
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            with Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples) as image:
                parts.append(_labelled_ocr(image, f"Page {number + 1}", language))
        if document.page_count > limit:
            parts.append(f"[Stopped after {limit} pages; call again with a larger max_pages to continue.]" )
        return parts
    finally:
        document.close()


def _ocr_image(path: Path, limit: int, language: str) -> list[str]:
    with Image.open(path) as source:
        frames = getattr(source, "n_frames", 1)
        parts = [f"[Image: {source.format or path.suffix} {source.width}x{source.height}, {frames} frame(s); OCR read {min(frames, limit)}]"]
        for number in range(min(frames, limit)):
            source.seek(number)
            with source.copy() as frame:
                parts.append(_labelled_ocr(frame, f"Frame {number + 1}", language))
        if frames > limit:
            parts.append(f"[Stopped after {limit} frames; call again with a larger max_pages to continue.]")
        return parts


def _labelled_ocr(image: Image.Image, label: str, language: str) -> str:
    prepared = ImageOps.exif_transpose(image).convert("L")
    # Small screenshots benefit disproportionately from upscaling; retain a
    # bounded raster so a maliciously large image cannot create a second copy
    # far beyond its source size.
    if max(prepared.size) < 1800:
        prepared = prepared.resize((prepared.width * 2, prepared.height * 2), Image.Resampling.LANCZOS)
    text = pytesseract.image_to_string(prepared, lang=language, config="--psm 6").strip()
    return f"## {label}\n{text or '[No text recognized]'}"


def _truncate(text: str) -> str:
    if len(text) <= _MAX_CHARACTERS:
        return text
    return f"{text[:_MAX_CHARACTERS]}\n... [OCR truncated at {_MAX_CHARACTERS} characters]"
