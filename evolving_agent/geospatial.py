"""Bounded, read-only inspection of vector geospatial evidence.

GeoJSON, KML, GPX, ESRI Shapefile, and ZIPped Shapefile attachments are decoded
in process.  Their feature properties are exposed through a sole DuckDB ``data``
view, allowing useful filtering and aggregation without allowing a query to open
other files.  Geometry stays JSON evidence and the inspection reports a common
WGS84-style coordinate bounding box when coordinates are present.
"""
from __future__ import annotations

import io
import json
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import duckdb
import shapefile

MAX_BYTES = 25 * 1024 * 1024
MAX_FEATURES = 50_000
MAX_ROWS = 1_000
DEFAULT_ROWS = 200
MAX_COLUMNS = 500
MAX_CELL_CHARACTERS = 1_000
MAX_OUTPUT_CHARACTERS = 24_000
QUERY_SECONDS = 8.0
_FORBIDDEN = re.compile(r"\b(?:attach|copy|create|delete|drop|export|import|insert|install|load|merge|pragma|replace|update|vacuum|call|read_[a-z0-9_]*|parquet_[a-z0-9_]*|csv_scan|glob|httpfs|query_table|sqlite_scan)\b", re.I)


class GeospatialError(Exception):
    """A vector geospatial attachment could not be safely inspected."""


def inspect_geospatial(path: Path, query: str | None = None, max_rows: int = DEFAULT_ROWS) -> str:
    """Describe vector evidence, optionally executing one bounded query on ``data``."""
    if not 1 <= max_rows <= MAX_ROWS:
        raise GeospatialError(f"'max_rows' must be between 1 and {MAX_ROWS}.")
    if not path.is_file():
        raise GeospatialError("The geospatial path is not a file.")
    try:
        if path.stat().st_size > MAX_BYTES:
            raise GeospatialError(f"Geospatial attachments are limited to {MAX_BYTES // (1024 * 1024)} MB.")
        fmt, features = _load(path)
        if len(features) > MAX_FEATURES:
            raise GeospatialError(f"Attachment has more than {MAX_FEATURES} features.")
        properties = _property_columns(features)
        bounds = _bounds(features)
        geometry_types = dict(sorted(Counter(str(item.get("geometry_type") or "Unknown") for item in features).items()))
        summary: dict[str, Any] = {
            "file": path.name, "format": fmt, "feature_count": len(features),
            "bounds": bounds, "geometry_types": geometry_types,
            "schema": _schema(features, properties),
        }
        if query is None:
            return _render(summary)
        _check_query(query)
        connection = duckdb.connect(database=":memory:")
        try:
            _make_view(connection, features, properties)
            rows, columns, truncated = _query(connection, query, max_rows)
        finally:
            connection.close()
        summary.update({"query": query, "columns": columns, "rows": rows, "truncated": truncated})
        return _render(summary)
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile, shapefile.ShapefileException) as error:
        raise GeospatialError(f"Could not inspect geospatial file: {error}") from error
    except duckdb.Error as error:
        raise GeospatialError(f"Could not query geospatial file: {error}") from error


def _load(path: Path) -> tuple[str, list[dict[str, Any]]]:
    suffix = path.suffix.lower()
    if suffix in {".geojson", ".json"}:
        return "GeoJSON", _geojson(json.loads(path.read_text(encoding="utf-8")))
    if suffix == ".kml":
        return "KML", _kml(path)
    if suffix == ".gpx":
        return "GPX", _gpx(path)
    if suffix == ".shp":
        return "Shapefile", _shapefile(shapefile.Reader(str(path)))
    if suffix == ".zip":
        return "Zipped Shapefile", _zipped_shapefile(path)
    raise GeospatialError("Supported formats are GeoJSON, KML, GPX, Shapefile (.shp), and ZIPped Shapefile.")


def _geojson(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        raise GeospatialError("GeoJSON must be a JSON object.")
    kind = value.get("type")
    if kind == "FeatureCollection":
        raw = value.get("features")
    elif kind == "Feature":
        raw = [value]
    elif isinstance(kind, str):
        raw = [{"type": "Feature", "geometry": value, "properties": {}}]
    else:
        raise GeospatialError("GeoJSON must be a FeatureCollection, Feature, or geometry object.")
    if not isinstance(raw, list):
        raise GeospatialError("GeoJSON features must be an array.")
    return [_feature(item.get("geometry"), item.get("properties"), item.get("id")) for item in raw if isinstance(item, dict)]


def _kml(path: Path) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot()
    result = []
    for placemark in root.iter():
        if _local(placemark.tag) != "Placemark":
            continue
        props = {"name": _child_text(placemark, "name"), "description": _child_text(placemark, "description")}
        props = {key: value for key, value in props.items() if value is not None}
        geometries = [_kml_geometry(child) for child in placemark.iter() if _local(child.tag) in {"Point", "LineString", "Polygon"}]
        result.append(_feature(geometries[0] if geometries else None, props, None))
    return result


def _kml_geometry(element: ET.Element) -> dict[str, Any] | None:
    coordinates = _child_text(element, "coordinates")
    if not coordinates:
        return None
    points = []
    for token in coordinates.split():
        bits = token.split(",")
        try: points.append([float(bits[0]), float(bits[1])] + ([float(bits[2])] if len(bits) > 2 else []))
        except (ValueError, IndexError): continue
    if not points: return None
    kind = _local(element.tag)
    if kind == "Point": return {"type": "Point", "coordinates": points[0]}
    if kind == "LineString": return {"type": "LineString", "coordinates": points}
    return {"type": "Polygon", "coordinates": [points]}


def _gpx(path: Path) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot(); result = []
    for element in root.iter():
        kind = _local(element.tag)
        if kind not in {"wpt", "rte", "trk"}: continue
        props = {"name": _child_text(element, "name"), "type": kind}
        props = {key: value for key, value in props.items() if value is not None}
        points = []
        candidates = [element] if kind == "wpt" else [x for x in element.iter() if _local(x.tag) == "trkpt" or _local(x.tag) == "rtept"]
        for point in candidates:
            try: points.append([float(point.attrib["lon"]), float(point.attrib["lat"])])
            except (KeyError, ValueError): continue
        geometry = {"type": "Point", "coordinates": points[0]} if kind == "wpt" and points else ({"type": "LineString", "coordinates": points} if points else None)
        result.append(_feature(geometry, props, None))
    return result


def _shapefile(reader: shapefile.Reader) -> list[dict[str, Any]]:
    fields = [field[0] for field in reader.fields[1:]]
    return [_feature(item.shape.__geo_interface__, dict(zip(fields, item.record)), index) for index, item in enumerate(reader.iterShapeRecords(), 1)]


def _zipped_shapefile(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if sum(info.file_size for info in members) > MAX_BYTES * 4:
            raise GeospatialError("Uncompressed ZIP shapefile contents are too large.")
        shp = next((info for info in members if info.filename.lower().endswith(".shp")), None)
        if shp is None: raise GeospatialError("ZIP does not contain a .shp file.")
        stem = shp.filename[:-4].lower()
        by_extension = {Path(info.filename).suffix.lower(): info for info in members if info.filename[:-len(Path(info.filename).suffix)].lower() == stem}
        if ".dbf" not in by_extension: raise GeospatialError("ZIP shapefile is missing its .dbf member.")
        return _shapefile(shapefile.Reader(shp=io.BytesIO(archive.read(shp)), shx=io.BytesIO(archive.read(by_extension[".shx"])) if ".shx" in by_extension else None, dbf=io.BytesIO(archive.read(by_extension[".dbf"]))))


def _feature(geometry: Any, properties: Any, identifier: Any) -> dict[str, Any]:
    geometry = geometry if isinstance(geometry, dict) else None
    return {"id": identifier, "geometry": geometry, "geometry_type": geometry.get("type") if geometry else None, "properties": properties if isinstance(properties, dict) else {}}


def _property_columns(features: list[dict[str, Any]]) -> list[str]:
    source_keys = sorted({str(key) for item in features for key in item["properties"]})[:MAX_COLUMNS - 3]
    reserved = {"feature_id", "geometry_type", "geometry_json"}
    columns: list[str] = []
    used = set(reserved)
    for key in source_keys:
        column = key
        while column in used:
            column = f"property_{column}"
        columns.append(column)
        used.add(column)
    return columns


def _schema(features: list[dict[str, Any]], columns: list[str]) -> list[dict[str, str]]:
    keys = sorted({str(key) for item in features for key in item["properties"]})[:len(columns)]
    return ([{"name": "feature_id", "type": "BIGINT"}, {"name": "geometry_type", "type": "VARCHAR"}, {"name": "geometry_json", "type": "JSON"}] + [{"name": column, "type": _type([item["properties"].get(key) for item in features])} for key, column in zip(keys, columns)])


def _type(values: Iterable[Any]) -> str:
    known = [value for value in values if value is not None]
    if known and all(isinstance(value, bool) for value in known): return "BOOLEAN"
    if known and all(isinstance(value, int) and not isinstance(value, bool) for value in known): return "BIGINT"
    if known and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in known): return "DOUBLE"
    return "VARCHAR"


def _make_view(connection: duckdb.DuckDBPyConnection, features: list[dict[str, Any]], columns: list[str]) -> None:
    source_keys = sorted({str(key) for item in features for key in item["properties"]})[:len(columns)]
    types = ["BIGINT", "VARCHAR", "JSON"] + [_type([item["properties"].get(key) for item in features]) for key in source_keys]
    names = ["feature_id", "geometry_type", "geometry_json"] + columns
    connection.execute("CREATE TABLE data (" + ", ".join(f'{_quote(name)} {kind}' for name, kind in zip(names, types)) + ")")
    marks = ", ".join("?" for _ in names)
    rows = []
    for index, item in enumerate(features, 1):
        props = item["properties"]
        values = [index, item["geometry_type"], json.dumps(item["geometry"], ensure_ascii=False)]
        for key, kind in zip(source_keys, types[3:]):
            value = props.get(key)
            values.append(json.dumps(value, ensure_ascii=False) if kind == "VARCHAR" and isinstance(value, (dict, list)) else value)
        rows.append(values)
    if rows: connection.executemany(f"INSERT INTO data VALUES ({marks})", rows)


def _bounds(features: list[dict[str, Any]]) -> dict[str, float] | None:
    points = []
    for item in features: points.extend(_coordinates(item["geometry"]))
    if not points: return None
    return {"min_lon": min(p[0] for p in points), "min_lat": min(p[1] for p in points), "max_lon": max(p[0] for p in points), "max_lat": max(p[1] for p in points)}


def _coordinates(value: Any) -> list[tuple[float, float]]:
    if not isinstance(value, dict): return []
    found = []
    def visit(item: Any) -> None:
        if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[0], (int, float)) and isinstance(item[1], (int, float)): found.append((float(item[0]), float(item[1])))
        elif isinstance(item, (list, tuple)):
            for child in item: visit(child)
    visit(value.get("coordinates"))
    return found


def _check_query(query: str) -> None:
    compact = query.strip()
    if not compact or compact.split(None, 1)[0].upper() not in {"SELECT", "WITH", "EXPLAIN"} or ";" in compact or _FORBIDDEN.search(compact):
        raise GeospatialError("Only one read-only SELECT, WITH, or EXPLAIN query against data is allowed.")


def _query(connection: duckdb.DuckDBPyConnection, query: str, max_rows: int) -> tuple[list[list[Any]], list[str], bool]:
    started = time.monotonic(); cursor = connection.execute(query)
    if time.monotonic() - started > QUERY_SECONDS: raise GeospatialError(f"Query exceeded the {QUERY_SECONDS:g}-second inspection limit.")
    fetched = cursor.fetchmany(max_rows + 1)
    if time.monotonic() - started > QUERY_SECONDS: raise GeospatialError(f"Query exceeded the {QUERY_SECONDS:g}-second inspection limit.")
    return [[_value(value) for value in row] for row in fetched[:max_rows]], [item[0] for item in cursor.description], len(fetched) > max_rows


def _value(value: Any) -> Any:
    if isinstance(value, str): return value if len(value) <= MAX_CELL_CHARACTERS else value[:MAX_CELL_CHARACTERS] + f"… [truncated at {MAX_CELL_CHARACTERS} characters]"
    return value

def _quote(value: str) -> str: return '"' + value.replace('"', '""') + '"'
def _local(tag: str) -> str: return tag.rsplit("}", 1)[-1]
def _child_text(element: ET.Element, name: str) -> str | None:
    child = next((node for node in element.iter() if _local(node.tag) == name), None)
    return child.text.strip() if child is not None and child.text else None
def _render(value: dict[str, Any]) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return text if len(text) <= MAX_OUTPUT_CHARACTERS else text[:MAX_OUTPUT_CHARACTERS] + f"… [truncated at {MAX_OUTPUT_CHARACTERS} characters]"
