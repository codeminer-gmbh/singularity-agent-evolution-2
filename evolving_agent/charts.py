"""Create bounded static charts with Pillow for report-style image deliverables."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageColor, ImageDraw, ImageFont


class ChartError(ValueError):
    """The requested chart is not a supported bounded chart."""


_MAX_PIXELS = 24_000_000
_MAX_POINTS = 60
_MAX_TEXT = 300
_DEFAULT_COLORS = ("#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7")


def create_chart(path: Path, *, chart_type: object, labels: object, values: object,
                 width: object = 1200, height: object = 800, title: object = None,
                 colors: object = None) -> str:
    """Create a PNG/JPEG bar, line, or pie chart at *path*.

    The deliberately small input surface makes quantitative charts available
    without exposing an arbitrary plotting interpreter.
    """
    suffix = path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise ChartError("'path' must end in .png, .jpg, or .jpeg.")
    if chart_type not in {"bar", "line", "pie"}:
        raise ChartError("'chart_type' must be one of: bar, line, pie.")
    if not _whole(width) or not 300 <= width <= 6000:
        raise ChartError("'width' must be a whole number from 300 to 6000.")
    if not _whole(height) or not 250 <= height <= 6000:
        raise ChartError("'height' must be a whole number from 250 to 6000.")
    if width * height > _MAX_PIXELS:
        raise ChartError(f"chart has too many pixels; limit is {_MAX_PIXELS}.")
    clean_labels = _labels(labels)
    numbers = _values(values, len(clean_labels))
    if chart_type == "pie" and (any(value < 0 for value in numbers) or not any(numbers)):
        raise ChartError("pie-chart values must be non-negative and have a positive total.")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > _MAX_TEXT):
        raise ChartError(f"'title' must be a non-blank string of at most {_MAX_TEXT} characters when supplied.")
    palette = _colors(colors, len(numbers))

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = _font(max(12, min(20, width // 55)), bold=False)
    small = _font(max(10, min(16, width // 75)), bold=False)
    heading = _font(max(18, min(32, width // 38)), bold=True)
    heading_text = title.strip() if isinstance(title, str) else _default_title(chart_type)
    draw.text((width // 2, 28), heading_text, fill="#1F2937", font=heading, anchor="ma")
    if chart_type == "pie":
        _pie(draw, width, height, clean_labels, numbers, palette, font, small)
    else:
        _cartesian(draw, width, height, clean_labels, numbers, palette, chart_type, font, small)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        image.save(path, format="JPEG" if suffix in {".jpg", ".jpeg"} else "PNG", quality=95)
    except OSError as exc:
        raise ChartError(f"Could not write {path.name!r}: {exc}") from exc
    return f"Created {chart_type} chart with {len(numbers)} values at {path.name}."


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _labels(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > _MAX_POINTS:
        raise ChartError(f"'labels' must be a non-empty list of at most {_MAX_POINTS} strings.")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or len(item) > _MAX_TEXT:
            raise ChartError(f"labels[{index}] must be a non-blank string of at most {_MAX_TEXT} characters.")
        result.append(item.strip())
    return result


def _values(value: object, count: int) -> list[float]:
    if not isinstance(value, list) or len(value) != count:
        raise ChartError("'values' must be a list of finite numbers with exactly one entry per label.")
    result: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise ChartError(f"values[{index}] must be a finite number.")
        result.append(float(item))
    return result


def _colors(value: object, count: int) -> list[str]:
    if value is None:
        return [_DEFAULT_COLORS[index % len(_DEFAULT_COLORS)] for index in range(count)]
    if not isinstance(value, list) or len(value) != count:
        raise ChartError("'colors' must have exactly one color string per value when supplied.")
    result: list[str] = []
    for index, color in enumerate(value):
        if not isinstance(color, str):
            raise ChartError(f"colors[{index}] must be a color string.")
        try:
            ImageColor.getrgb(color)
        except ValueError as exc:
            raise ChartError(f"colors[{index}] is not a recognized Pillow color.") from exc
        result.append(color)
    return result


def _font(size: int, *, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf") if bold else ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",)
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _cartesian(draw: ImageDraw.ImageDraw, width: int, height: int, labels: Sequence[str], values: Sequence[float], colors: Sequence[str], kind: str, font: Any, small: Any) -> None:
    left, top, right, bottom = 100, 100, width - 45, height - 115
    low, high = min(0.0, min(values)), max(0.0, max(values))
    if low == high:
        low, high = low - 1, high + 1
    padding = (high - low) * .08
    low, high = low - padding, high + padding
    def y(value: float) -> float: return bottom - (value - low) * (bottom - top) / (high - low)
    for step in range(6):
        value = low + (high - low) * step / 5
        ypos = y(value)
        draw.line((left, ypos, right, ypos), fill="#E5E7EB", width=1)
        draw.text((left - 10, ypos), f"{value:g}", font=small, fill="#4B5563", anchor="rm")
    zero = y(0)
    draw.line((left, top, left, bottom), fill="#6B7280", width=2)
    draw.line((left, zero, right, zero), fill="#6B7280", width=2)
    step = (right - left) / len(values)
    points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        center = left + step * (index + .5)
        ypos = y(value)
        if kind == "bar":
            draw.rectangle((center - step * .33, min(ypos, zero), center + step * .33, max(ypos, zero)), fill=colors[index])
        else:
            points.append((center, ypos))
        label = labels[index] if len(labels) <= 14 else str(index + 1)
        draw.text((center, bottom + 14), label, font=small, fill="#374151", anchor="ma")
    if kind == "line":
        draw.line(points, fill=colors[0], width=max(2, width // 400), joint="curve")
        for index, point in enumerate(points):
            radius = max(3, width // 250)
            draw.ellipse((point[0]-radius, point[1]-radius, point[0]+radius, point[1]+radius), fill=colors[index])


def _pie(draw: ImageDraw.ImageDraw, width: int, height: int, labels: Sequence[str], values: Sequence[float], colors: Sequence[str], font: Any, small: Any) -> None:
    line_height = 31 if len(labels) <= 10 else 23
    rows_per_column = max(1, max(line_height, height - 135) // line_height)
    legend_columns = math.ceil(len(labels) / rows_per_column)
    # Reserve enough horizontal space for all legend columns before sizing the
    # pie, rather than allowing a valid long legend to disappear off-canvas.
    legend_width = legend_columns * 205
    diameter = min(height - 170, int(width * .52), width - 65 - 45 - legend_width)
    diameter = max(40, diameter)
    left, top = 65, 95
    box = (left, top, left + diameter, top + diameter)
    total, angle = sum(values), -90.0
    for value, color in zip(values, colors):
        end = angle + 360 * value / total
        draw.pieslice(box, angle, end, fill=color, outline="white", width=2)
        angle = end
    # Keep every category identifiable rather than silently drawing a legend
    # below the canvas when a valid series has many slices.  A compact legend
    # flows into columns in the space to the right of the pie.
    legend_x = left + diameter + 45
    # ``rows_per_column`` was also used when sizing the pie above, keeping
    # these legend columns inside the reserved area.
    for index, (label, value, color) in enumerate(zip(labels, values, colors)):
        column, row = divmod(index, rows_per_column)
        x = legend_x + column * 205
        y = 125 + row * line_height
        draw.rectangle((x, y + 4, x + 16, y + 20), fill=color)
        draw.text((x + 27, y), f"{label} ({value:g})", font=font if len(labels) <= 10 else small, fill="#374151")


def _default_title(kind: str) -> str:
    return f"{kind.capitalize()} chart"
