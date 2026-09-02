"""Bounded metadata and pixel previews for GDAL-supported raster files."""

from __future__ import annotations

import json
from math import isfinite
from pathlib import Path
from typing import Any

MAX_RASTER_PIXELS = 10_000
"""Largest selected-band window a caller may read."""
DEFAULT_RASTER_PIXELS = 1_000


class RasterError(Exception):
    """A requested raster preview is invalid or cannot be opened."""


def _integer(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RasterError(f"{name} must be an integer at least {minimum}.")
    return value


def _window_spec(value: Any, width: int, height: int, max_pixels: int) -> tuple[int, int, int, int]:
    if value is None:
        # A full image may be huge. Read a deterministic, top-left preview that
        # remains under the caller's cap, preserving the image's aspect ratio.
        preview_width = min(width, max_pixels)
        preview_height = min(height, max(1, max_pixels // preview_width))
        return 0, 0, preview_width, preview_height
    if not isinstance(value, dict):
        raise RasterError("window must be an object with col_off, row_off, width, and height.")
    expected = {"col_off", "row_off", "width", "height"}
    if set(value) != expected:
        raise RasterError("window must contain exactly col_off, row_off, width, and height.")
    col_off = _integer(value["col_off"], "window.col_off", minimum=0)
    row_off = _integer(value["row_off"], "window.row_off", minimum=0)
    window_width = _integer(value["width"], "window.width", minimum=1)
    window_height = _integer(value["height"], "window.height", minimum=1)
    if col_off + window_width > width or row_off + window_height > height:
        raise RasterError("window lies outside raster dimensions.")
    if window_width * window_height > max_pixels:
        raise RasterError(f"window contains more than max_pixels ({max_pixels}).")
    return col_off, row_off, window_width, window_height


def _json_number(value: Any) -> int | float | None:
    """Convert numpy scalar values into strict JSON scalars, masking non-finite values."""
    numeric = value.item() if hasattr(value, "item") else value
    if isinstance(numeric, (int, float)):
        result = int(numeric) if isinstance(numeric, int) else float(numeric)
        return result if not isinstance(result, float) or isfinite(result) else None
    return None


def inspect_raster(path: Path, *, band: Any = 1, window: Any = None, max_pixels: Any = DEFAULT_RASTER_PIXELS) -> str:
    """Return JSON spatial metadata and a bounded, masked pixel preview.

    Rasterio is imported lazily so an informative tool error is returned should
    an image omit a readable GDAL driver rather than making agent startup fail.
    """
    selected_band = _integer(band, "band", minimum=1)
    pixel_cap = _integer(max_pixels, "max_pixels", minimum=1)
    if pixel_cap > MAX_RASTER_PIXELS:
        raise RasterError(f"max_pixels must not exceed {MAX_RASTER_PIXELS}.")
    try:
        import rasterio
        from rasterio.windows import Window
    except ImportError as error:  # pragma: no cover - dependency is pinned
        raise RasterError("Raster inspection support is unavailable in this image.") from error

    try:
        with rasterio.open(path) as dataset:
            if selected_band > dataset.count:
                raise RasterError(f"band must be between 1 and {dataset.count}.")
            col_off, row_off, read_width, read_height = _window_spec(
                window, dataset.width, dataset.height, pixel_cap,
            )
            values = dataset.read(
                selected_band,
                window=Window(col_off, row_off, read_width, read_height),
                masked=True,
            )
            mask = values.mask
            scalar_mask = getattr(mask, "ndim", 0) == 0
            preview = [
                [
                    None if (bool(mask) if scalar_mask else bool(mask[row, column]))
                    else _json_number(values[row, column])
                    for column in range(read_width)
                ]
                for row in range(read_height)
            ]
            transform = dataset.transform
            result = {
                "driver": dataset.driver,
                "width": dataset.width,
                "height": dataset.height,
                "band_count": dataset.count,
                "bounds": [dataset.bounds.left, dataset.bounds.bottom, dataset.bounds.right, dataset.bounds.top],
                "crs": str(dataset.crs) if dataset.crs else None,
                "transform": list(transform),
                "pixel_size": [transform.a, transform.e],
                "nodata": [_json_number(item) for item in dataset.nodatavals],
                "dtypes": list(dataset.dtypes),
                "color_interpretations": [item.name for item in dataset.colorinterp],
                "selected_band": selected_band,
                "window": {"col_off": col_off, "row_off": row_off, "width": read_width, "height": read_height},
                "preview": preview,
            }
    except RasterError:
        raise
    except Exception as error:
        raise RasterError(f"Could not inspect raster {path.name}: {error}") from error
    return json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
