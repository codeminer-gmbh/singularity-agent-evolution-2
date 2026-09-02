"""Bounded, passive inspection of PowerPoint Open XML presentations.

PPTX files are ZIP containers.  This reader reads only core properties,
presentation relationships, slide XML, and optional speaker-notes XML.  It does
not load external relationships, macros, media, or embedded objects.
"""

from __future__ import annotations

import json
from posixpath import normpath
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


class PresentationInspectionError(Exception):
    """A presentation cannot safely be inspected."""


_MAX_SOURCE_BYTES = 30_000_000
_MAX_MEMBERS = 10_000
_MAX_UNCOMPRESSED_BYTES = 50_000_000
_MAX_TEXT = 4_000
_MAIN = "ppt/presentation.xml"
_RELS = "ppt/_rels/presentation.xml.rels"
_CORE = "docProps/core.xml"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_DCTERMS = "{http://purl.org/dc/terms/}"


def inspect_presentation(path: Path, *, max_slides: int = 30, max_characters: int = 8_000) -> str:
    """Return bounded JSON evidence from a standard PPTX file.

    Slide order follows ``presentation.xml`` relationships, not ZIP member
    order. Text includes ordinary text shapes and table cells; speaker notes
    are previewed separately when present. The JSON response is never sliced.
    """
    if not 1 <= max_slides <= 100:
        raise PresentationInspectionError("'max_slides' must be between 1 and 100.")
    if not 100 <= max_characters <= 20_000:
        raise PresentationInspectionError("'max_characters' must be between 100 and 20000.")
    if path.suffix.lower() != ".pptx":
        raise PresentationInspectionError("Presentation inspection supports only .pptx files.")
    try:
        if not path.is_file():
            raise PresentationInspectionError(f"{path.name!r} is not a file.")
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            raise PresentationInspectionError(f"{path.name!r} exceeds the {_MAX_SOURCE_BYTES:,}-byte PPTX inspection limit.")
        with zipfile.ZipFile(path) as archive:
            _validate(archive, path.name)
            names = set(archive.namelist())
            if _MAIN not in names or _RELS not in names:
                raise PresentationInspectionError(f"{path.name!r} has no PowerPoint presentation XML members.")
            metadata = _metadata(archive, names)
            slide_paths = _slide_paths(archive, path.name)
            slides = [_slide(archive, member, index + 1, names) for index, member in enumerate(slide_paths)]
    except PresentationInspectionError:
        raise
    except (OSError, zipfile.BadZipFile, ET.ParseError) as failed:
        raise PresentationInspectionError(f"Could not inspect {path.name!r}: {failed}") from failed

    result: dict[str, Any] = {"format": "PPTX", "metadata": metadata, "slide_count": len(slides), "slides": []}
    omitted = 0
    for slide in slides:
        if len(result["slides"]) >= max_slides or not _fits(result, slide, max_characters):
            omitted += 1
        else:
            result["slides"].append(slide)
    if omitted:
        result["truncated"] = {"omitted_slides": omitted}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if len(rendered) > max_characters:
        return json.dumps({"format": "PPTX", "truncated": {"reason": "max_characters"}}, indent=2)
    return rendered


def _validate(archive: zipfile.ZipFile, name: str) -> None:
    members = archive.infolist()
    if len(members) > _MAX_MEMBERS:
        raise PresentationInspectionError(f"{name!r} has too many ZIP members to inspect safely.")
    if sum(member.file_size for member in members) > _MAX_UNCOMPRESSED_BYTES:
        raise PresentationInspectionError(f"{name!r} expands beyond the PPTX inspection limit.")


def _xml(archive: zipfile.ZipFile, member: str, name: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(member))
    except KeyError as absent:
        raise PresentationInspectionError(f"{name!r} is missing required member {member!r}.") from absent


def _metadata(archive: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    if _CORE not in names:
        return {}
    root = _xml(archive, _CORE, "PPTX")
    wanted = {"title": _DC + "title", "subject": _DC + "subject", "creator": _DC + "creator", "keywords": _CP + "keywords", "description": _DC + "description", "created": _DCTERMS + "created", "modified": _DCTERMS + "modified"}
    return {label: value.strip()[:1000] for label, tag in wanted.items() if (value := root.findtext(tag)) and value.strip()}


def _slide_paths(archive: zipfile.ZipFile, name: str) -> list[str]:
    relationships = _xml(archive, _RELS, name)
    targets = {relation.get("Id"): relation.get("Target", "") for relation in relationships.findall(_REL + "Relationship") if relation.get("Type", "").endswith("/slide")}
    presentation = _xml(archive, _MAIN, name)
    paths: list[str] = []
    for identifier in presentation.findall("./" + _P + "sldIdLst/" + _P + "sldId"):
        target = targets.get(identifier.get(_R + "id"))
        if target:
            member = "ppt/" + target.lstrip("/")
            # Presentation relationships normally use relative targets.
            if member.startswith("ppt/ppt/"):
                member = member[4:]
            paths.append(member)
    return paths


def _slide(archive: zipfile.ZipFile, member: str, number: int, names: set[str]) -> dict[str, Any]:
    root = _xml(archive, member, "PPTX")
    shapes: list[dict[str, Any]] = []
    for shape in root.findall("./" + _P + "cSld/" + _P + "spTree/*"):
        if shape.tag == _P + "sp":
            text = _text(shape)
            if text:
                shapes.append({"type": "text", "text": text})
        elif shape.tag == _P + "graphicFrame":
            rows = [[_text(cell) for cell in row.findall("./" + _A + "tc")] for row in shape.findall(".//" + _A + "tr")]
            if rows:
                shapes.append({"type": "table", "rows": rows})
    result: dict[str, Any] = {"slide_number": number, "content": shapes}
    notes = _notes(archive, member, names)
    if notes:
        result["notes"] = notes
    return result


def _notes(archive: zipfile.ZipFile, slide_member: str, names: set[str]) -> str:
    """Read notes only via this slide's internal OOXML relationship.

    Notes-slide filenames are not required to track slide filenames.  Following
    the package relationship preserves that association while deliberately
    ignoring external targets.
    """
    parent, filename = slide_member.rsplit("/", 1)
    rels_member = f"{parent}/_rels/{filename}.rels"
    if rels_member not in names:
        return ""
    for relation in _xml(archive, rels_member, "PPTX").findall(_REL + "Relationship"):
        if not relation.get("Type", "").endswith("/notesSlide"):
            continue
        if relation.get("TargetMode", "").lower() == "external":
            return ""
        target = relation.get("Target")
        if not target:
            return ""
        member = normpath(f"{parent}/{target}")
        if not member.startswith("ppt/") or member not in names:
            return ""
        return _text(_xml(archive, member, "PPTX"))
    return ""


def _text(element: ET.Element) -> str:
    return "\n".join("".join(run.text or "" for run in paragraph.findall(".//" + _A + "t")).strip() for paragraph in element.findall(".//" + _A + "p") if "".join(run.text or "" for run in paragraph.findall(".//" + _A + "t")).strip())[:_MAX_TEXT]


def _fits(result: dict[str, Any], addition: dict[str, Any], maximum: int) -> bool:
    candidate = {**result, "slides": [*result["slides"], addition]}
    return len(json.dumps(candidate, ensure_ascii=False, indent=2)) <= maximum
