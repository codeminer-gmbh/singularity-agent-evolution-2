"""Create small, dependable chart images from declarative numeric series."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageDraw

from evolving_agent.images import ImageError, _font

_MAX_POINTS = 100
_MAX_TEXT = 200
_PALETTE = ("#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d")


def create_chart(path: Path, *, chart_type: object, data: object, width: object = 1000,
                 height: object = 650, title: object = None, background: object = "white") -> str:
    """Save a bar, line, or pie chart as PNG/JPEG; numbers are never evaluated."""
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise ImageError("destination must end in .png, .jpg, or .jpeg.")
    if chart_type not in {"bar", "line", "pie"}:
        raise ImageError("chart_type must be one of 'bar', 'line', or 'pie'.")
    if not isinstance(width, int) or isinstance(width, bool) or not 300 <= width <= 6000:
        raise ImageError("width must be an integer between 300 and 6000.")
    if not isinstance(height, int) or isinstance(height, bool) or not 250 <= height <= 6000:
        raise ImageError("height must be an integer between 250 and 6000.")
    if width * height > 24_000_000:
        raise ImageError("chart has too many pixels; limit is 24000000.")
    if title is not None and (not isinstance(title, str) or len(title) > _MAX_TEXT):
        raise ImageError("title must be a string of at most 200 characters when supplied.")
    if not isinstance(background, str) or not background.strip():
        raise ImageError("background must be a non-blank color string.")
    try:
        ImageColor.getrgb(background)
    except ValueError as exc:
        raise ImageError("background must be a valid Pillow color.") from exc
    points = _points(data, chart_type)
    try:
        image = Image.new("RGBA", (width, height), background)
        draw = ImageDraw.Draw(image)
        if title:
            draw.text((42, 22), title, fill="#111827", font=_font(28))
        if chart_type == "pie":
            _pie(draw, points, width, height)
        else:
            _axes_chart(draw, points, width, height, chart_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            image.convert("RGB").save(path, "JPEG", quality=95)
        else:
            image.save(path, "PNG")
    except (OSError, ValueError, TypeError) as exc:
        raise ImageError(f"Could not create chart {path.name!r}: {exc}") from exc
    return f"Created {chart_type} chart {path.name} ({width}x{height} {path.suffix[1:].upper()}) with {len(points)} data points."


def _points(data: object, chart_type: object) -> list[tuple[str, float, str | None]]:
    if not isinstance(data, list) or not data or len(data) > _MAX_POINTS:
        raise ImageError("data must be a non-empty list of at most 100 data-point objects.")
    result = []
    for index, point in enumerate(data):
        if not isinstance(point, dict):
            raise ImageError(f"data[{index}] must be an object.")
        label, value, color = point.get("label"), point.get("value"), point.get("color")
        if not isinstance(label, str) or not label.strip() or len(label) > _MAX_TEXT:
            raise ImageError(f"data[{index}].label must be a non-blank string of at most 200 characters.")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ImageError(f"data[{index}].value must be a finite number.")
        if chart_type == "pie" and value < 0:
            raise ImageError(f"data[{index}].value must be non-negative for a pie chart.")
        if color is not None:
            if not isinstance(color, str) or not color.strip():
                raise ImageError(f"data[{index}].color must be a non-blank color string when supplied.")
            try: ImageColor.getrgb(color)
            except ValueError as exc: raise ImageError(f"data[{index}].color must be a valid Pillow color.") from exc
        result.append((label.strip(), float(value), color))
    if chart_type == "pie" and not any(value > 0 for _, value, _ in result):
        raise ImageError("a pie chart needs at least one positive value.")
    return result


def _axes_chart(draw: Any, points: list[tuple[str, float, str | None]], width: int, height: int, kind: str) -> None:
    left, top, right, bottom = 85, 85, width - 45, height - 100
    values = [p[1] for p in points]
    low, high = min(0.0, min(values)), max(0.0, max(values))
    if high == low: high = low + 1
    def y(value: float) -> int: return round(bottom - (value - low) / (high - low) * (bottom - top))
    zero = y(0)
    draw.line((left, top, left, bottom), fill="#374151", width=2)
    draw.line((left, zero, right, zero), fill="#374151", width=2)
    for tick in range(5):
        value = low + (high - low) * tick / 4
        py = y(value); draw.line((left - 5, py, right, py), fill="#e5e7eb", width=1)
        draw.text((8, py - 8), f"{value:g}", fill="#374151", font=_font(14))
    step = (right - left) / len(points)
    if kind == "bar":
        for i, (label, value, color) in enumerate(points):
            x = left + i * step + step * .15; x2 = left + (i + 1) * step - step * .15
            draw.rectangle((round(x), min(zero, y(value)), round(x2), max(zero, y(value))), fill=color or _PALETTE[i % len(_PALETTE)])
            draw.text((round(x), bottom + 12), label[:16], fill="#374151", font=_font(13))
    else:
        coords = [(round(left + (i + .5) * step), y(value)) for i, (_, value, _) in enumerate(points)]
        draw.line(coords, fill="#2563eb", width=4, joint="curve")
        for i, ((x, py), (label, _, color)) in enumerate(zip(coords, points)):
            draw.ellipse((x - 5, py - 5, x + 5, py + 5), fill=color or "#2563eb")
            draw.text((x - 20, bottom + 12), label[:16], fill="#374151", font=_font(13))


def _pie(draw: Any, points: list[tuple[str, float, str | None]], width: int, height: int) -> None:
    side = min(height - 140, width * 3 // 5); box = (50, 80, 50 + side, 80 + side)
    total, start = sum(p[1] for p in points), -90.0
    for i, (label, value, color) in enumerate(points):
        end = start + value / total * 360
        draw.pieslice(box, start, end, fill=color or _PALETTE[i % len(_PALETTE)], outline="white", width=2)
        y = 100 + i * 30; draw.rectangle((width * 3 // 5 + 20, y, width * 3 // 5 + 36, y + 16), fill=color or _PALETTE[i % len(_PALETTE)])
        draw.text((width * 3 // 5 + 44, y - 2), f"{label[:22]} ({value:g})", fill="#374151", font=_font(15)); start = end
