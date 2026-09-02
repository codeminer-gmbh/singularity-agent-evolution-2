"""Bounded inspection of GDAL raster datasets through Rasterio."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:  # Keep module import diagnostic useful if an incomplete local install is used.
    import rasterio
    from rasterio.errors import RasterioError
    from rasterio.windows import Window
except ImportError:  # pragma: no cover - exercised only before image dependencies install
    rasterio = None  # type: ignore[assignment]
    RasterioError = Exception
    Window = Any  # type: ignore[misc,assignment]

MAX_RASTER_PIXELS = 10_000
DEFAULT_RASTER_PIXELS = 1_000


class RasterError(Exception):
    """A raster could not be inspected safely or is not a supported dataset."""


def inspect_raster(
    path: Path,
    *,
    band: int = 1,
    window: Sequence[int] | None = None,
    max_pixels: int = DEFAULT_RASTER_PIXELS,
) -> str:
    """Return metadata and a bounded 2-D pixel preview from a raster file.

    ``window`` is ``[column_offset, row_offset, width, height]`` in pixel
    coordinates.  When omitted a top-left window sized to the raster aspect
    ratio is used.  A single band is deliberately read, avoiding accidental
    materialisation of large multispectral products.
    """
    if rasterio is None:
        raise RasterError("Rasterio is unavailable; the raster reader dependency is not installed.")
    if isinstance(band, bool) or not isinstance(band, int) or band < 1:
        raise RasterError("band must be a positive integer.")
    _validate_max_pixels(max_pixels)
    requested = _parse_window(window)
    try:
        with rasterio.open(path) as dataset:
            if band > dataset.count:
                raise RasterError(f"band {band} is unavailable; this raster has {dataset.count} band(s).")
            selected = requested or _default_window(dataset.width, dataset.height, max_pixels)
            selected = _clip_window(selected, dataset.width, dataset.height)
            if selected[2] * selected[3] > max_pixels:
                raise RasterError(f"window contains more than max_pixels ({max_pixels}) pixels.")
            pixel_window = Window(*selected)
            pixels = dataset.read(band, window=pixel_window, masked=True)
            metadata: dict[str, Any] = {
                "driver": dataset.driver,
                "width": dataset.width,
                "height": dataset.height,
                "band_count": dataset.count,
                "dtypes": list(dataset.dtypes),
                "crs": dataset.crs.to_string() if dataset.crs else None,
                "bounds": _bounds(dataset.bounds),
                "transform": list(dataset.transform)[:6],
                "nodata_values": list(dataset.nodatavals),
                "color_interpretations": [str(item).replace("ColorInterp.", "") for item in dataset.colorinterp],
                "selected_band": band,
                "band_description": dataset.descriptions[band - 1],
                "overviews": dataset.overviews(band),
                "window": {"column_offset": selected[0], "row_offset": selected[1], "width": selected[2], "height": selected[3]},
                "window_bounds": _bounds(dataset.window_bounds(pixel_window)),
                "pixel_count": selected[2] * selected[3],
                "pixels": _masked_values(pixels),
                "pixel_summary": _pixel_summary(pixels),
            }
            return json.dumps(metadata, ensure_ascii=False, allow_nan=False, indent=2)
    except RasterError:
        raise
    except (RasterioError, OSError, ValueError) as error:
        raise RasterError(f"Could not read raster {path.name!r}: {error}") from error


def _validate_max_pixels(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RasterError("max_pixels must be an integer.")
    if not 1 <= value <= MAX_RASTER_PIXELS:
        raise RasterError(f"max_pixels must be between 1 and {MAX_RASTER_PIXELS}.")


def _parse_window(value: Sequence[int] | None) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 4:
        raise RasterError("window must be [column_offset, row_offset, width, height].")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise RasterError("window entries must be integers.")
    col, row, width, height = value
    if col < 0 or row < 0 or width < 1 or height < 1:
        raise RasterError("window offsets must be non-negative and width and height must be positive.")
    return col, row, width, height


def _default_window(width: int, height: int, maximum: int) -> tuple[int, int, int, int]:
    preview_width = min(width, max(1, int(math.sqrt(maximum * width / height))))
    preview_height = min(height, max(1, maximum // preview_width))
    return 0, 0, preview_width, preview_height


def _clip_window(window: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    col, row, requested_width, requested_height = window
    if col >= width or row >= height:
        raise RasterError("window starts outside raster dimensions.")
    return col, row, min(requested_width, width - col), min(requested_height, height - row)


def _bounds(bounds: Any) -> dict[str, float]:
    if hasattr(bounds, "left"):
        return {"left": float(bounds.left), "bottom": float(bounds.bottom), "right": float(bounds.right), "top": float(bounds.top)}
    left, bottom, right, top = bounds
    return {"left": float(left), "bottom": float(bottom), "right": float(right), "top": float(top)}


def _masked_values(array: Any) -> list[list[Any]]:
    values = array.data.tolist()
    mask = array.mask.tolist() if hasattr(array.mask, "tolist") else array.mask
    return _merge_mask(values, mask)


def _merge_mask(values: Any, mask: Any) -> Any:
    if isinstance(values, list):
        if isinstance(mask, list):
            return [_merge_mask(value, masked) for value, masked in zip(values, mask)]
        return [_merge_mask(value, mask) for value in values]
    if mask:
        return None
    if isinstance(values, float) and not math.isfinite(values):
        return None
    return values


def _pixel_summary(array: Any) -> dict[str, int | float | None]:
    """Summarise the returned window without another or unbounded raster read."""
    values = _masked_values(array)
    numeric: list[float] = []
    masked = 0
    for value in _flatten(values):
        if value is None:
            masked += 1
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric.append(float(value))
    if not numeric:
        return {"valid_count": 0, "masked_count": masked, "minimum": None, "maximum": None, "mean": None}
    return {
        "valid_count": len(numeric),
        "masked_count": masked,
        "minimum": min(numeric),
        "maximum": max(numeric),
        "mean": math.fsum(numeric) / len(numeric),
    }

def _flatten(values: Any) -> list[Any]:
    if not isinstance(values, list):
        return [values]
    flattened: list[Any] = []
    for value in values:
        flattened.extend(_flatten(value))
    return flattened
