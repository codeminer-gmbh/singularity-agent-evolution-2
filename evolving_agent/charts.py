"""Create compact raster charts from bounded, explicitly supplied data.

The chart surface is deliberately declarative: labels and finite numeric values
are rendered with Pillow, with no spreadsheet formulas, scripts, URLs, or
untrusted drawing formats involved.  The resulting PNG or JPEG can be used as
a deliverable itself or embedded by the document and presentation tools.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


class ChartError(ValueError):
    """The requested chart cannot be rendered safely."""


_MAX_POINTS = 100
_MAX_LABEL_LENGTH = 80
_MAX_TITLE_LENGTH = 200
_MIN_DIMENSION = 200
_MAX_DIMENSION = 2400
_MAX_PIXELS = 4_000_000
_PALETTE = ("#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#4f46e5", "#be123c")


def create_chart(destination: Path, chart_type: object, labels: object, values: object, *, title: object = "", width: object = 1200, height: object = 720, colors: object = None) -> str:
    """Render a bar, line, or pie chart and return a short creation report."""
    kind = _kind(chart_type)
    checked_labels = _labels(labels)
    checked_values = _values(values, len(checked_labels))
    checked_title = _title(title)
    checked_width = _dimension(width, "width")
    checked_height = _dimension(height, "height")
    if checked_width * checked_height > _MAX_PIXELS:
        raise ChartError(f"width times height may not exceed {_MAX_PIXELS:,} pixels.")
    palette = _colors(colors)
    image_format = _format(destination)
    if kind == "pie" and (any(value < 0 for value in checked_values) or sum(checked_values) <= 0):
        raise ChartError("pie chart values must be non-negative and have a positive total.")

    image = Image.new("RGB", (checked_width, checked_height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    _header(draw, checked_title, checked_width, font)
    if kind == "bar":
        _bar(draw, checked_labels, checked_values, palette, checked_width, checked_height, font)
    elif kind == "line":
        _line(draw, checked_labels, checked_values, palette, checked_width, checked_height, font)
    else:
        _pie(draw, checked_labels, checked_values, palette, checked_width, checked_height, font)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Saving a complete temporary image before replacing the destination
        # avoids a half-written deliverable if Pillow reports an I/O failure.
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=destination.suffix, delete=False) as temporary:
            temporary_name = temporary.name
        try:
            image.save(temporary_name, format=image_format, quality=92 if image_format == "JPEG" else None)
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
    except (OSError, ValueError) as exc:
        raise ChartError(f"Could not save {destination.name!r}: {exc}") from exc
    return f"Created {kind} chart ({checked_width}x{checked_height}) at {destination.name}."


def _kind(value: object) -> str:
    if not isinstance(value, str) or value.lower() not in {"bar", "line", "pie"}:
        raise ChartError("'chart_type' must be one of: bar, line, pie.")
    return value.lower()


def _labels(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > _MAX_POINTS:
        raise ChartError(f"'labels' must be a non-empty list of at most {_MAX_POINTS} strings.")
    if not all(isinstance(label, str) and label.strip() and len(label.strip()) <= _MAX_LABEL_LENGTH for label in value):
        raise ChartError(f"Each label must be non-blank and at most {_MAX_LABEL_LENGTH} characters.")
    return [label.strip() for label in value]


def _values(value: object, expected: int) -> list[float]:
    if not isinstance(value, list) or len(value) != expected:
        raise ChartError("'values' must be a list with exactly one finite number for each label.")
    if any(isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) for number in value):
        raise ChartError("'values' must contain only finite numbers.")
    return [float(number) for number in value]


def _title(value: object) -> str:
    if not isinstance(value, str) or len(value.strip()) > _MAX_TITLE_LENGTH:
        raise ChartError(f"'title' must be a string of at most {_MAX_TITLE_LENGTH} characters.")
    return value.strip()


def _dimension(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not _MIN_DIMENSION <= value <= _MAX_DIMENSION:
        raise ChartError(f"'{name}' must be an integer from {_MIN_DIMENSION} through {_MAX_DIMENSION}.")
    return value


def _colors(value: object) -> Sequence[str]:
    if value is None:
        return _PALETTE
    if not isinstance(value, list) or not value or len(value) > _MAX_POINTS:
        raise ChartError(f"'colors' must be a non-empty list of at most {_MAX_POINTS} #RRGGBB colors.")
    if not all(isinstance(color, str) and len(color) == 7 and color.startswith("#") and all(char in "0123456789abcdefABCDEF" for char in color[1:]) for color in value):
        raise ChartError("Each color must be a hexadecimal #RRGGBB string.")
    return value


def _format(destination: Path) -> str:
    suffix = destination.suffix.lower()
    if suffix == ".png":
        return "PNG"
    if suffix in {".jpg", ".jpeg"}:
        return "JPEG"
    raise ChartError("'path' must end in .png, .jpg, or .jpeg.")


def _header(draw: ImageDraw.ImageDraw, title: str, width: int, font: ImageFont.ImageFont) -> None:
    if title:
        draw.text((45, 25), title, fill="#111827", font=font)
        draw.line((45, 48, width - 45, 48), fill="#d1d5db", width=1)


def _bounds(values: Sequence[float]) -> tuple[float, float]:
    low, high = min(0.0, min(values)), max(0.0, max(values))
    if low == high:
        return low - 1.0, high + 1.0
    padding = (high - low) * 0.08
    return low - padding, high + padding


def _bar(draw: ImageDraw.ImageDraw, labels: Sequence[str], values: Sequence[float], colors: Sequence[str], width: int, height: int, font: ImageFont.ImageFont) -> None:
    left, top, right, bottom = 70, 80, width - 35, height - 80
    low, high = _bounds(values)
    baseline = bottom - (0 - low) / (high - low) * (bottom - top)
    draw.line((left, top, left, bottom), fill="#374151")
    draw.line((left, baseline, right, baseline), fill="#374151")
    slot = (right - left) / len(values)
    for index, (label, value) in enumerate(zip(labels, values)):
        x1 = left + index * slot + max(2, slot * .14)
        x2 = left + (index + 1) * slot - max(2, slot * .14)
        y = bottom - (value - low) / (high - low) * (bottom - top)
        draw.rectangle((x1, min(y, baseline), x2, max(y, baseline)), fill=colors[index % len(colors)])
        if len(values) <= 20:
            draw.text((x1, bottom + 12), label[:14], fill="#374151", font=font)


def _line(draw: ImageDraw.ImageDraw, labels: Sequence[str], values: Sequence[float], colors: Sequence[str], width: int, height: int, font: ImageFont.ImageFont) -> None:
    left, top, right, bottom = 70, 80, width - 35, height - 80
    low, high = _bounds(values)
    draw.line((left, top, left, bottom), fill="#374151")
    draw.line((left, bottom, right, bottom), fill="#374151")
    step = (right - left) / max(1, len(values) - 1)
    points = [(left + index * step, bottom - (value - low) / (high - low) * (bottom - top)) for index, value in enumerate(values)]
    if len(points) > 1:
        draw.line(points, fill=colors[0], width=3)
    for index, (x, y) in enumerate(points):
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=colors[index % len(colors)])
        if len(values) <= 20:
            draw.text((x - 5, bottom + 12), labels[index][:12], fill="#374151", font=font)


def _pie(draw: ImageDraw.ImageDraw, labels: Sequence[str], values: Sequence[float], colors: Sequence[str], width: int, height: int, font: ImageFont.ImageFont) -> None:
    diameter = min(height - 135, width * 0.55)
    box = (45, 75, 45 + diameter, 75 + diameter)
    total, start = sum(values), -90.0
    for index, value in enumerate(values):
        end = start + value / total * 360
        draw.pieslice(box, start, end, fill=colors[index % len(colors)], outline="white")
        start = end
    x, y = int(65 + diameter), 95
    for index, label in enumerate(labels[:20]):
        color = colors[index % len(colors)]
        draw.rectangle((x, y + index * 22, x + 12, y + index * 22 + 12), fill=color)
        draw.text((x + 18, y + index * 22), f"{label[:35]} ({values[index]:g})", fill="#374151", font=font)
