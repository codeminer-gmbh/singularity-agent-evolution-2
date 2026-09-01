"""Bounded metadata and OCR inspection for image evidence.

Images are parsed locally with Pillow and, when requested, passed as a filename to
Tesseract.  No image is rendered, no embedded thumbnail or profile is opened, and
no network or shell is involved.  The output is JSON so screenshots and scans can
be used as ordinary evidence in a task.
"""

from __future__ import annotations

import json
import subprocess
import warnings
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, UnidentifiedImageError

_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_MAX_PIXELS = 80_000_000
_MAX_EXIF_FIELDS = 100
_MAX_VALUE_CHARACTERS = 1_000
_MAX_OCR_CHARACTERS = 20_000
_OCR_TIMEOUT_SECONDS = 30
_SUPPORTED_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "TIFF", "BMP", "GIF"})


class ImageError(ValueError):
    """An image could not be safely inspected."""


def inspect_image(path: Path, *, ocr: bool = True, max_characters: int = 8_000) -> str:
    """Return bounded image metadata, EXIF fields, and optional OCR text.

    The source is never modified.  Animated images expose their frame count but
    OCR only the first frame, which makes a request predictable and bounded.
    """
    if not isinstance(ocr, bool):
        raise ImageError("ocr must be true or false.")
    if not isinstance(max_characters, int) or isinstance(max_characters, bool):
        raise ImageError("max_characters must be an integer.")
    if not 1 <= max_characters <= _MAX_OCR_CHARACTERS:
        raise ImageError(f"max_characters must be between 1 and {_MAX_OCR_CHARACTERS}.")
    if not path.is_file():
        raise ImageError(f"{path.name!r} is not a readable image file.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImageError(f"Could not inspect {path.name!r}: {exc}") from exc
    if size > _MAX_SOURCE_BYTES:
        raise ImageError(
            f"{path.name!r} is {size} bytes; image inspection is limited to {_MAX_SOURCE_BYTES} bytes."
        )

    try:
        # Pillow otherwise merely warns before allocating an image whose pixel
        # count is hostile to an evidence-inspection request.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                if width * height > _MAX_PIXELS:
                    raise ImageError(
                        f"{path.name!r} has {width * height} pixels; image inspection is limited to {_MAX_PIXELS}."
                    )
                image.load()
                image_format = image.format or "unknown"
                if image_format.upper() not in _SUPPORTED_FORMATS:
                    raise ImageError(f"{path.name!r} has unsupported image format {image_format!r}.")
                report: dict[str, Any] = {
                    "format": image_format.lower(),
                    "size_bytes": size,
                    "width": width,
                    "height": height,
                    "mode": image.mode,
                    "frame_count": int(getattr(image, "n_frames", 1)),
                    "exif": _exif(image),
                }
    except ImageError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, SyntaxError, ValueError) as exc:
        raise ImageError(f"Could not parse image {path.name!r}: {exc}") from exc

    if ocr:
        text = _ocr(path)
        report["ocr_text_preview"] = text[:max_characters]
        report["ocr_text_truncated"] = len(text) > max_characters
        report["ocr_note"] = "OCR uses the first image frame and English Tesseract data."
    return json.dumps(report, ensure_ascii=False, indent=2)


def _exif(image: Image.Image) -> dict[str, str]:
    """Return selected, printable EXIF tags without decoding thumbnail data."""
    try:
        raw = image.getexif()
    except (OSError, ValueError, SyntaxError):
        return {}
    fields: dict[str, str] = {}
    for tag, value in raw.items():
        if len(fields) >= _MAX_EXIF_FIELDS:
            break
        # Binary tags (notably MakerNote) can be enormous and are not useful
        # textual evidence.  They are never materialized into the tool result.
        if isinstance(value, bytes):
            continue
        name = ExifTags.TAGS.get(tag, str(tag))
        text = " ".join(str(value).split())
        fields[str(name)] = text[:_MAX_VALUE_CHARACTERS]
    return fields


def _ocr(path: Path) -> str:
    """Use Tesseract directly with a short timeout and bounded captured output."""
    try:
        completed = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "eng"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_OCR_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ImageError("OCR is unavailable: the Tesseract executable is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ImageError(f"OCR exceeded the {_OCR_TIMEOUT_SECONDS}-second limit.") from exc
    except OSError as exc:
        raise ImageError(f"OCR could not start: {exc}") from exc
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ImageError(f"OCR failed for {path.name!r}: {detail[:500] or 'Tesseract returned an error.'}")
    return completed.stdout.decode("utf-8", errors="replace").strip()
