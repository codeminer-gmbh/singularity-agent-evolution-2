"""Bounded inspection and conversion of common vector geodata task inputs.

The agent does not need a full GIS engine merely to make an opaque Shapefile
usable.  This module accepts GeoJSON and ESRI Shapefiles, emits ordinary
GeoJSON, and computes a small, JSON-safe summary for a model to reason over.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import shapefile

MAX_GEODATA_BYTES = 64 * 1024 * 1024
MAX_FEATURES = 20_000
MAX_SUMMARY_FEATURES = 500
MAX_PROPERTY_FIELDS = 100


class GeodataError(Exception):
    """A vector dataset cannot be safely read or converted."""


def inspect_geodata(path: Path, *, max_features: int = 100) -> str:
    """Return a concise JSON summary of a GeoJSON or Shapefile dataset."""
    _check_limit(max_features, ceiling=MAX_SUMMARY_FEATURES, name="max_features")
    features = list(_features(path, maximum=MAX_FEATURES))
    types: Counter[str] = Counter()
    fields: set[str] = set()
    bounds: list[float] | None = None
    samples: list[dict[str, Any]] = []
    for number, feature in enumerate(features):
        geometry = feature.get("geometry")
        if isinstance(geometry, dict):
            kind = geometry.get("type")
            if isinstance(kind, str):
                types[kind] += 1
            bounds = _extend_bounds(bounds, _geometry_bounds(geometry))
        props = feature.get("properties")
        if isinstance(props, dict):
            fields.update(str(key) for key in props)
        if number < max_features:
            samples.append({"id": feature.get("id"), "properties": props or {}, "geometry_type": geometry.get("type") if isinstance(geometry, dict) else None})
    summary: dict[str, Any] = {
        "format": "ESRI Shapefile" if path.suffix.lower() == ".shp" else "GeoJSON",
        "feature_count": len(features),
        "geometry_types": dict(sorted(types.items())),
        "property_fields": sorted(fields)[:MAX_PROPERTY_FIELDS],
        "bounds_west_south_east_north": bounds,
        "samples": samples,
    }
    if len(fields) > MAX_PROPERTY_FIELDS:
        summary["property_fields_truncated"] = len(fields) - MAX_PROPERTY_FIELDS
    return json.dumps(summary, ensure_ascii=False, indent=2, default=str)


def convert_to_geojson(source: Path, destination: Path, *, max_features: int = MAX_FEATURES) -> int:
    """Convert GeoJSON or a Shapefile to a FeatureCollection at ``destination``."""
    _check_limit(max_features, ceiling=MAX_FEATURES, name="max_features")
    if destination.suffix.lower() not in {".geojson", ".json"}:
        raise GeodataError("destination must end in .geojson or .json.")
    features = list(_features(source, maximum=max_features))
    collection = {"type": "FeatureCollection", "features": features}
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except OSError as error:
        raise GeodataError(f"Could not write {destination.name!r}: {error}") from error
    return len(features)


def _features(path: Path, *, maximum: int) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        raise GeodataError(f"{path.name!r} is not a geodata file.")
    if path.stat().st_size > MAX_GEODATA_BYTES:
        raise GeodataError(f"{path.name!r} exceeds the {MAX_GEODATA_BYTES}-byte geodata limit.")
    suffix = path.suffix.lower()
    if suffix in {".geojson", ".json"}:
        yield from _geojson_features(path, maximum)
    elif suffix == ".shp":
        yield from _shapefile_features(path, maximum)
    else:
        raise GeodataError("Unsupported geodata type. Supported types are GeoJSON (.geojson, .json) and ESRI Shapefile (.shp).")


def _geojson_features(path: Path, maximum: int) -> Iterable[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GeodataError(f"Could not parse GeoJSON {path.name!r}: {error}") from error
    if not isinstance(data, dict):
        raise GeodataError("GeoJSON root must be an object.")
    kind = data.get("type")
    raw = data.get("features") if kind == "FeatureCollection" else [data] if kind == "Feature" else [{"type": "Feature", "properties": {}, "geometry": data}]
    if not isinstance(raw, list):
        raise GeodataError("GeoJSON FeatureCollection features must be an array.")
    if len(raw) > maximum:
        raise GeodataError(f"Dataset has more than the {maximum}-feature limit.")
    for item in raw:
        if not isinstance(item, dict):
            raise GeodataError("Every GeoJSON feature must be an object.")
        geometry = item.get("geometry")
        if geometry is not None and not isinstance(geometry, dict):
            raise GeodataError("A GeoJSON feature geometry must be an object or null.")
        props = item.get("properties")
        yield {"type": "Feature", "id": item.get("id"), "properties": props if isinstance(props, dict) else {}, "geometry": geometry}


def _shapefile_features(path: Path, maximum: int) -> Iterable[dict[str, Any]]:
    try:
        reader = shapefile.Reader(str(path))
        if len(reader) > maximum:
            raise GeodataError(f"Dataset has more than the {maximum}-feature limit.")
        fields = [field[0] for field in reader.fields[1:]]
        for number, record in enumerate(reader.iterShapeRecords()):
            properties = dict(zip(fields, list(record.record)))
            geometry = record.shape.__geo_interface__ if record.shape.shapeType != shapefile.NULL else None
            yield {"type": "Feature", "id": number, "properties": properties, "geometry": geometry}
    except GeodataError:
        raise
    except (OSError, ValueError, shapefile.ShapefileException) as error:
        raise GeodataError(f"Could not parse Shapefile {path.name!r}: {error}") from error


def _check_limit(value: int, *, ceiling: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
        raise GeodataError(f"{name} must be an integer from 1 through {ceiling}.")


def _extend_bounds(current: list[float] | None, candidate: list[float] | None) -> list[float] | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return [min(current[0], candidate[0]), min(current[1], candidate[1]), max(current[2], candidate[2]), max(current[3], candidate[3])]


def _geometry_bounds(geometry: dict[str, Any]) -> list[float] | None:
    values: list[tuple[float, float]] = []
    def visit(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
                values.append((float(value[0]), float(value[1])))
            else:
                for child in value:
                    visit(child)
    visit(geometry.get("coordinates"))
    if not values:
        return None
    return [min(v[0] for v in values), min(v[1] for v in values), max(v[0] for v in values), max(v[1] for v in values)]
