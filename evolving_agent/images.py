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

# Creation intentionally has a small declarative surface.  It lets the model make
# portable visual deliverables without granting a general graphics interpreter.
_MAX_CREATED_PIXELS = 24_000_000
_MAX_DRAW_OPERATIONS = 200
_MAX_TEXT_LENGTH = 4_000


def create_image(
    path: Path,
    *,
    width: int,
    height: int,
    background: str = "white",
    operations: list[dict[str, Any]] | None = None,
    image_resolver: Any = None,
) -> str:
    """Create a PNG or JPEG from bounded declarative drawing operations.

    Operations are ``rectangle``, ``ellipse``, ``line``, ``text``, and
    ``image``. Coordinates are pixels; colors use normal Pillow CSS-style
    color strings.  Image operations may only name paths resolved by the
    caller, keeping composition inside the agent's readable trees.
    """
    if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= 6000:
        raise ImageError("width must be an integer between 1 and 6000.")
    if not isinstance(height, int) or isinstance(height, bool) or not 1 <= height <= 6000:
        raise ImageError("height must be an integer between 1 and 6000.")
    if width * height > _MAX_CREATED_PIXELS:
        raise ImageError(f"image has too many pixels; limit is {_MAX_CREATED_PIXELS}.")
    if not isinstance(background, str) or not background.strip():
        raise ImageError("background must be a non-blank color string.")
    if operations is None:
        operations = []
    if not isinstance(operations, list) or len(operations) > _MAX_DRAW_OPERATIONS:
        raise ImageError(f"operations must be a list with at most {_MAX_DRAW_OPERATIONS} items.")
    suffix = path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise ImageError("destination must end in .png, .jpg, or .jpeg.")
    try:
        from PIL import ImageColor, ImageDraw, ImageFont
        # Validate separately so invalid colors report a useful input error.
        ImageColor.getrgb(background)
        canvas = Image.new("RGBA", (width, height), background)
        draw = ImageDraw.Draw(canvas)
        for index, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                raise ImageError(f"operation {index} must be an object.")
            kind = operation.get("type")
            if kind == "rectangle":
                draw.rectangle(_box(operation, index), fill=operation.get("fill"), outline=operation.get("outline"), width=_positive(operation.get("stroke_width", 1), "stroke_width", index))
            elif kind == "ellipse":
                draw.ellipse(_box(operation, index), fill=operation.get("fill"), outline=operation.get("outline"), width=_positive(operation.get("stroke_width", 1), "stroke_width", index))
            elif kind == "line":
                points = operation.get("points")
                if not isinstance(points, list) or len(points) < 2 or len(points) > 100:
                    raise ImageError(f"operation {index} line needs 2 to 100 [x, y] points.")
                draw.line([_point(point, index) for point in points], fill=operation.get("fill", "black"), width=_positive(operation.get("stroke_width", 1), "stroke_width", index), joint="curve")
            elif kind == "text":
                text = operation.get("text")
                if not isinstance(text, str) or len(text) > _MAX_TEXT_LENGTH:
                    raise ImageError(f"operation {index} text must be a string up to {_MAX_TEXT_LENGTH} characters.")
                font_size = _positive(operation.get("font_size", 16), "font_size", index)
                if font_size > 200:
                    raise ImageError(f"operation {index} font_size must be at most 200.")
                draw.multiline_text(_point(operation.get("position"), index), text, fill=operation.get("fill", "black"), font=_font(font_size), spacing=4)
            elif kind == "image":
                source = operation.get("path")
                if not isinstance(source, str) or image_resolver is None:
                    raise ImageError(f"operation {index} image needs a readable path.")
                with _open_composition_image(image_resolver(source)) as overlay:
                    box = _box(operation, index)
                    target = (box[2] - box[0], box[3] - box[1])
                    if target[0] <= 0 or target[1] <= 0:
                        raise ImageError(f"operation {index} image box must have positive dimensions.")
                    overlay.thumbnail(target)
                    canvas.alpha_composite(overlay.convert("RGBA"), (box[0], box[1]))
            else:
                raise ImageError(f"operation {index} has unsupported type {kind!r}.")
        path.parent.mkdir(parents=True, exist_ok=True)
        if suffix in {".jpg", ".jpeg"}:
            canvas.convert("RGB").save(path, "JPEG", quality=95)
        else:
            canvas.save(path, "PNG")
    except ImageError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ImageError(f"Could not create image {path.name!r}: {exc}") from exc
    return f"Created {path.name} ({width}x{height} {suffix[1:].upper()})."


def _open_composition_image(path: Path) -> Any:
    """Open a local source only after applying the same basic decode bounds."""
    if not path.is_file():
        raise ImageError(f"{path.name!r} is not a readable image file.")
    try:
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            raise ImageError(f"{path.name!r} is too large to compose; limit is {_MAX_SOURCE_BYTES} bytes.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(path)
        pixels = image.width * image.height
        if pixels > _MAX_CREATED_PIXELS:
            image.close()
            raise ImageError(f"{path.name!r} has too many pixels to compose; limit is {_MAX_CREATED_PIXELS}.")
        return image
    except ImageError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, SyntaxError, ValueError) as exc:
        raise ImageError(f"Could not open composition image {path.name!r}: {exc}") from exc


def _point(value: Any, index: int) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or any(not isinstance(v, int) or isinstance(v, bool) for v in value):
        raise ImageError(f"operation {index} needs a two-integer position or point.")
    return value[0], value[1]


def _box(operation: dict[str, Any], index: int) -> tuple[int, int, int, int]:
    value = operation.get("box")
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(not isinstance(v, int) or isinstance(v, bool) for v in value):
        raise ImageError(f"operation {index} needs a four-integer box [left, top, right, bottom].")
    return value[0], value[1], value[2], value[3]


def _positive(value: Any, name: str, index: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ImageError(f"operation {index} {name} must be a positive integer.")
    return value


def _font(size: int) -> Any:
    from PIL import ImageFont
    for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default()
