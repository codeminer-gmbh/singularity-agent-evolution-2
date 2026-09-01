"""Bounded, read-only inspection of common geospatial evidence attachments."""

from __future__ import annotations

import json
import math
import zipfile
import xml.etree.ElementTree as element_tree
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

try:
    import shapefile
except ImportError:  # pragma: no cover - dependency is part of the image
    shapefile = None  # type: ignore[assignment]

MAX_INPUT_BYTES = 20_000_000
MAX_FEATURES = 100
MAX_OUTPUT_CHARACTERS = 24_000
MAX_PROPERTY_VALUE_CHARACTERS = 500


class GeospatialError(Exception):
    """A geospatial attachment is unsupported, malformed, or too large."""


def inspect_geospatial(path: Path, *, feature_index: int | None = None, max_features: int = 20) -> str:
    """Summarize features and return a compact, bounded preview.

    ``feature_index`` is one-based, matching the labels in the result.  The
    inspector deliberately never renders maps or follows linked resources:
    coordinates and attributes are evidence, and a model can reason over them
    without a browser or an extraction directory.
    """
    if feature_index is not None and feature_index < 1:
        raise GeospatialError("'feature_index' must be a positive integer.")
    if not 1 <= max_features <= MAX_FEATURES:
        raise GeospatialError(f"'max_features' must be from 1 through {MAX_FEATURES}.")
    suffix = path.suffix.lower()
    try:
        if suffix in {".geojson", ".json"}:
            kind, features = _geojson(_read(path))
        elif suffix in {".kml", ".gpx"}:
            kind, features = _xml_features(_read(path), suffix)
        elif suffix == ".kmz":
            kind, features = _kmz(path)
        elif suffix == ".shp":
            kind, features = _shapefile(path)
        else:
            raise GeospatialError("Supported formats are GeoJSON/JSON, KML/KMZ, GPX, and Shapefile (.shp).")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, element_tree.ParseError, zipfile.BadZipFile) as error:
        raise GeospatialError(f"Could not read geospatial evidence: {error}") from error
    return _report(kind, path.name, features, feature_index, max_features)


def _read(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise GeospatialError(f"{path.name!r} is {size} bytes, above the {MAX_INPUT_BYTES}-byte inspection limit.")
    return path.read_bytes()


def _geojson(data: bytes) -> tuple[str, list[dict[str, Any]]]:
    root = json.loads(data.decode("utf-8-sig"))
    if not isinstance(root, dict):
        raise GeospatialError("GeoJSON top level must be an object.")
    root_type = root.get("type")
    if root_type == "FeatureCollection":
        raw = root.get("features")
        if not isinstance(raw, list):
            raise GeospatialError("GeoJSON FeatureCollection has no features array.")
        return "GeoJSON FeatureCollection", [_normal_feature(item) for item in raw if isinstance(item, dict)]
    if root_type == "Feature":
        return "GeoJSON Feature", [_normal_feature(root)]
    if isinstance(root_type, str):
        return f"GeoJSON {root_type}", [{"geometry": root, "properties": {}}]
    raise GeospatialError("The JSON file is not recognizable GeoJSON (missing type).")


def _normal_feature(item: dict[str, Any]) -> dict[str, Any]:
    geometry = item.get("geometry")
    return {"geometry": geometry if isinstance(geometry, dict) else None, "properties": item.get("properties") if isinstance(item.get("properties"), dict) else {}, "id": item.get("id")}


def _xml_features(data: bytes, suffix: str) -> tuple[str, list[dict[str, Any]]]:
    root = element_tree.fromstring(data)
    if suffix == ".gpx":
        features: list[dict[str, Any]] = []
        for node in root.iter():
            if _local(node.tag) not in {"wpt", "rtept", "trkpt"}:
                continue
            try:
                coordinate = [float(node.attrib["lon"]), float(node.attrib["lat"])]
            except (KeyError, ValueError):
                continue
            props = { _local(child.tag): (child.text or "").strip() for child in node if (child.text or "").strip() }
            props["point_type"] = _local(node.tag)
            features.append({"geometry": {"type": "Point", "coordinates": coordinate}, "properties": props})
        return "GPX", features
    features = []
    for node in root.iter():
        if _local(node.tag) != "Placemark":
            continue
        props: dict[str, Any] = {}
        for child in node:
            name = _local(child.tag)
            if name in {"name", "description", "address"} and (child.text or "").strip():
                props[name] = (child.text or "").strip()
        coordinates = next((child.text for child in node.iter() if _local(child.tag) == "coordinates" and child.text), None)
        geometry_name = next((_local(child.tag) for child in node.iter() if _local(child.tag) in {"Point", "LineString", "Polygon", "MultiGeometry"}), None)
        parsed = _kml_coordinates(coordinates or "")
        geometry = _kml_geometry(geometry_name, parsed)
        features.append({"geometry": geometry, "properties": props})
    return "KML", features


def _kmz(path: Path) -> tuple[str, list[dict[str, Any]]]:
    with zipfile.ZipFile(path) as archive:
        choices = [info for info in archive.infolist() if not info.is_dir() and info.filename.lower().endswith(".kml")]
        if not choices:
            raise GeospatialError("KMZ contains no KML member.")
        info = next((item for item in choices if item.filename.lower() == "doc.kml"), choices[0])
        if info.file_size > MAX_INPUT_BYTES:
            raise GeospatialError(f"KMZ KML member is above the {MAX_INPUT_BYTES}-byte inspection limit.")
        with archive.open(info) as source:
            data = source.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise GeospatialError(f"KMZ KML member is above the {MAX_INPUT_BYTES}-byte inspection limit.")
    _, features = _xml_features(data, ".kml")
    return f"KMZ ({info.filename})", features


def _shapefile(path: Path) -> tuple[str, list[dict[str, Any]]]:
    if shapefile is None:
        raise GeospatialError("Shapefile support is unavailable in this image.")
    _validate_shapefile_sidecars(path)
    try:
        reader = shapefile.Reader(str(path))
        fields = [field[0] for field in reader.fields[1:]]
        features = []
        for record in reader.iterShapeRecords():
            geometry = record.shape.__geo_interface__
            features.append({"geometry": geometry, "properties": dict(zip(fields, record.record))})
        return "Shapefile", features
    except Exception as error:
        raise GeospatialError(f"Could not read Shapefile (its .shx and .dbf sidecars may be required): {error}") from error


def _validate_shapefile_sidecars(path: Path) -> None:
    """Keep companion reads contained and individually bounded too."""
    parent = path.parent.resolve()
    for suffix in (".shx", ".dbf", ".cpg", ".prj"):
        sidecar = path.with_suffix(suffix)
        if not sidecar.exists():
            continue
        resolved = sidecar.resolve()
        if resolved != parent and parent not in resolved.parents:
            raise GeospatialError(f"Shapefile sidecar {sidecar.name!r} resolves outside its evidence directory.")
        if not resolved.is_file():
            raise GeospatialError(f"Shapefile sidecar {sidecar.name!r} is not a regular file.")
        if resolved.stat().st_size > MAX_INPUT_BYTES:
            raise GeospatialError(f"Shapefile sidecar {sidecar.name!r} is above the {MAX_INPUT_BYTES}-byte inspection limit.")


def _kml_coordinates(text: str) -> list[list[float]]:
    result = []
    for token in text.replace("\n", " ").split():
        try:
            values = [float(value) for value in token.split(",")]
            if len(values) >= 2:
                result.append(values[:3])
        except ValueError:
            continue
    return result


def _kml_geometry(name: str | None, coordinates: list[list[float]]) -> dict[str, Any] | None:
    if not coordinates:
        return None
    if name == "Point": return {"type": "Point", "coordinates": coordinates[0]}
    if name == "Polygon": return {"type": "Polygon", "coordinates": [coordinates]}
    return {"type": "LineString", "coordinates": coordinates}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _report(kind: str, name: str, features: list[dict[str, Any]], selected: int | None, maximum: int) -> str:
    if selected is not None:
        if selected > len(features):
            raise GeospatialError(f"This attachment has {len(features)} feature(s); feature_index {selected} does not exist.")
        shown = [(selected, features[selected - 1])]
    else:
        shown = list(enumerate(features[:maximum], 1))
    geometries = Counter(_geometry_type(feature.get("geometry")) for feature in features)
    keys = sorted({str(key) for feature in features for key in feature.get("properties", {}).keys()})
    bbox = _bbox(point for feature in features for point in _points(feature.get("geometry")))
    lines = [f"{kind} geospatial evidence ({name})", f"Features: {len(features)}", "Geometry types: " + (", ".join(f"{key}={value}" for key, value in sorted(geometries.items())) or "none"), "Property fields: " + (", ".join(keys) if keys else "none"), "Bounding box [min_lon, min_lat, max_lon, max_lat]: " + (json.dumps(bbox) if bbox else "unavailable"), "---"]
    for index, feature in shown:
        compact = {"geometry_type": _geometry_type(feature.get("geometry")), "coordinates": _coordinate_preview(feature.get("geometry")), "properties": {str(key): _safe_value(value) for key, value in feature.get("properties", {}).items()}}
        if feature.get("id") is not None: compact["id"] = _safe_value(feature["id"])
        lines.append(f"Feature {index}: {json.dumps(compact, ensure_ascii=False, default=str)}")
    if selected is None and len(features) > len(shown): lines.append(f"... [{len(features) - len(shown)} more features omitted; use feature_index to inspect one]")
    result = "\n".join(lines)
    return result[:MAX_OUTPUT_CHARACTERS] + ("\n... [result truncated]" if len(result) > MAX_OUTPUT_CHARACTERS else "")


def _geometry_type(geometry: Any) -> str:
    return str(geometry.get("type", "none")) if isinstance(geometry, dict) else "none"

def _points(geometry: Any) -> Iterable[tuple[float, float]]:
    if not isinstance(geometry, dict): return []
    if geometry.get("type") == "GeometryCollection":
        for child in geometry.get("geometries", []):
            yield from _points(child)
        return
    def walk(value: Any) -> Iterable[tuple[float, float]]:
        if isinstance(value, (list, tuple)) and len(value) >= 2 and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in value[:2]):
            yield float(value[0]), float(value[1])
        elif isinstance(value, (list, tuple)):
            for item in value: yield from walk(item)
    yield from walk(geometry.get("coordinates"))

def _bbox(points: Iterable[tuple[float, float]]) -> list[float] | None:
    valid = [(x, y) for x, y in points if math.isfinite(x) and math.isfinite(y)]
    return [min(x for x, _ in valid), min(y for _, y in valid), max(x for x, _ in valid), max(y for _, y in valid)] if valid else None

def _coordinate_preview(geometry: Any) -> Any:
    if not isinstance(geometry, dict): return None
    coordinates = geometry.get("coordinates")
    text = json.dumps(coordinates, ensure_ascii=False, default=str)
    return coordinates if len(text) <= 1000 else text[:1000] + "... [coordinates truncated]"

def _safe_value(value: Any) -> Any:
    text = str(value)
    return text if len(text) <= MAX_PROPERTY_VALUE_CHARACTERS else text[:MAX_PROPERTY_VALUE_CHARACTERS] + "... [truncated]"
