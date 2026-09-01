"""Bounded metadata and feature previews for common vector geodata files."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import fiona

MAX_GEODATA_FILE_BYTES = 512 * 1024 * 1024
MAX_GEODATA_FEATURES = 100
MAX_GEODATA_TEXT_CHARACTERS = 120_000
_MAX_PROPERTY_CHARACTERS = 2_000


class GeodataError(Exception):
    """A vector geodata source could not be inspected."""


def inspect_geodata(path: Path, *, layer: str | int | None = None, max_features: int = 20) -> str:
    """Return driver, layers, CRS, schema, extent, and a small feature preview."""
    if not path.is_file():
        raise GeodataError(f"{path.name!r} is not a vector geodata file.")
    if path.stat().st_size > MAX_GEODATA_FILE_BYTES:
        raise GeodataError(f"The file exceeds the {MAX_GEODATA_FILE_BYTES}-byte geodata limit.")
    if isinstance(max_features, bool) or not isinstance(max_features, int) or not 1 <= max_features <= MAX_GEODATA_FEATURES:
        raise GeodataError(f"max_features must be a whole number from 1 to {MAX_GEODATA_FEATURES}.")
    if layer is not None and (isinstance(layer, bool) or not isinstance(layer, (str, int))):
        raise GeodataError("layer must be a layer name or zero-based layer number.")
    try:
        layers = list(fiona.listlayers(path))
        with fiona.open(path, layer=layer) as source:
            schema = dict(source.schema)
            preview = [_feature_summary(feature) for _, feature in zip(range(max_features), source)]
            details: dict[str, Any] = {
                "driver": source.driver,
                "layers": layers or [source.name],
                "selected_layer": source.name,
                "feature_count": len(source),
                "geometry_type": schema.get("geometry"),
                "properties": schema.get("properties", {}),
                "bounds": list(source.bounds) if source.bounds else None,
                "crs": source.crs.to_string() if source.crs else None,
                "crs_wkt": source.crs_wkt,
                "preview": preview,
            }
    except (OSError, ValueError, TypeError, fiona.errors.FionaError) as error:
        raise GeodataError(f"Could not read vector geodata: {error}") from error
    text = json.dumps(details, ensure_ascii=False, default=str, indent=2)
    if len(text) > MAX_GEODATA_TEXT_CHARACTERS:
        return text[:MAX_GEODATA_TEXT_CHARACTERS] + f"\n... [truncated at {MAX_GEODATA_TEXT_CHARACTERS} characters]"
    return text


def _feature_summary(feature: Mapping[str, Any]) -> dict[str, Any]:
    """Keep properties and useful geometry facts without returning huge coordinates."""
    properties = {str(key): _short(value) for key, value in dict(feature.get("properties") or {}).items()}
    geometry = feature.get("geometry")
    result: dict[str, Any] = {"id": feature.get("id"), "properties": properties, "geometry": None}
    if geometry:
        result["geometry"] = {
            "type": geometry.get("type"),
            "coordinate_count": _coordinate_count(geometry.get("coordinates")),
        }
    return result


def _short(value: Any) -> Any:
    text = str(value)
    return text if len(text) <= _MAX_PROPERTY_CHARACTERS else text[:_MAX_PROPERTY_CHARACTERS] + "… [truncated]"


def _coordinate_count(value: Any) -> int:
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
            return 1
        return sum(_coordinate_count(item) for item in value)
    return 0
