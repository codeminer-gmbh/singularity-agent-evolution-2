"""Bounded OCR for image and scanned-PDF task materials."""

from __future__ import annotations

from pathlib import Path

_MAX_INPUT_BYTES = 20_000_000
_MAX_PAGES = 12
_MAX_PIXELS_PER_IMAGE = 20_000_000
_MAX_OUTPUT_CHARACTERS = 60_000
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})


class ImageExtractionError(Exception):
    """The named image cannot safely be read with OCR."""


def extract_image_text(path: Path, *, max_characters: int = _MAX_OUTPUT_CHARACTERS) -> str:
    """OCR a raster image or up to twelve pages of a PDF.

    The input file is size-limited, malformed images are rejected as ordinary
    tool failures, and decompression-bomb-sized images are never handed to the
    OCR engine.  Page labels make multi-page results usable in a task answer.
    """
    if not path.is_file():
        raise ImageExtractionError(f"{path.name!r} is not a file.")
    if path.stat().st_size > _MAX_INPUT_BYTES:
        raise ImageExtractionError(f"{path.name!r} is larger than {_MAX_INPUT_BYTES:,} bytes.")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pages = _pdf_pages(path)
    elif suffix in _IMAGE_SUFFIXES:
        pages = [(1, _open_image(path))]
    else:
        supported = ", ".join(sorted(extension.lstrip(".").upper() for extension in _IMAGE_SUFFIXES))
        raise ImageExtractionError(f"Supported OCR formats are PDF, {supported}.")

    output: list[str] = []
    remaining = max_characters
    for number, image in pages:
        text = _ocr(image).strip()
        block = f"--- Page {number} ---\n{text or '[No text recognized.]'}"
        if len(block) > remaining:
            output.append(block[:remaining] + f"\n... [truncated at {max_characters} characters]")
            break
        output.append(block)
        remaining -= len(block) + 2
        if remaining <= 0:
            break
    return "\n\n".join(output) or "[No text recognized.]"


def _open_image(path: Path):
    try:
        from PIL import Image, UnidentifiedImageError
        with Image.open(path) as source:
            source.load()
            _check_size(source.width, source.height)
            # Tesseract handles RGB and grayscale reliably; copy detaches from
            # the closed input file and makes animated formats deterministic.
            return source.convert("RGB")
    except (OSError, UnidentifiedImageError) as error:
        raise ImageExtractionError(f"Could not read image: {error}") from error


def _pdf_pages(path: Path) -> list[tuple[int, object]]:
    try:
        import fitz
        document = fitz.open(path)
        try:
            count = min(document.page_count, _MAX_PAGES)
            if count == 0:
                raise ImageExtractionError("The PDF has no pages.")
            pages: list[tuple[int, object]] = []
            for index in range(count):
                # 200 dpi is sufficient for normal printed text without an
                # unbounded amount of raster memory.
                pixmap = document.load_page(index).get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72), alpha=False)
                _check_size(pixmap.width, pixmap.height)
                from PIL import Image
                pages.append((index + 1, Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)))
            return pages
        finally:
            document.close()
    except ImageExtractionError:
        raise
    except Exception as error:
        raise ImageExtractionError(f"Could not render PDF for OCR: {error}") from error


def _check_size(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or width * height > _MAX_PIXELS_PER_IMAGE:
        raise ImageExtractionError("Image dimensions are unsafe for OCR.")


def _ocr(image: object) -> str:
    try:
        import pytesseract
        return pytesseract.image_to_string(image, config="--psm 6")
    except Exception as error:
        raise ImageExtractionError(f"OCR failed: {error}") from error
