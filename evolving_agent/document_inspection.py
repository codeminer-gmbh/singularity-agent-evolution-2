"""Bounded, passive inspection of Office Open XML Word documents.

DOCX files are ZIP containers.  This reader opens only the core-properties and
main-document XML members, never macros, external relationships, or embedded
objects.  It applies ZIP and output limits before presenting JSON evidence to
the model.
"""

import json
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


class DocumentInspectionError(Exception):
    """A document cannot safely be inspected."""


_MAX_SOURCE_BYTES = 30_000_000
_MAX_MEMBERS = 10_000
_MAX_UNCOMPRESSED_BYTES = 50_000_000
_MAIN_DOCUMENT = "word/document.xml"
_CORE_PROPERTIES = "docProps/core.xml"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_DCTERMS = "{http://purl.org/dc/terms/}"


def inspect_document(path: Path, *, max_blocks: int = 100, max_characters: int = 8_000) -> str:
    """Return a bounded JSON preview of a standard DOCX's text and tables.

    ``max_characters`` bounds the serialized result, rather than merely the
    source text, so callers never receive a malformed clipped JSON object.
    """
    if path.suffix.lower() != ".docx":
        raise DocumentInspectionError("Document inspection supports only .docx files.")
    try:
        if not path.is_file():
            raise DocumentInspectionError(f"{path.name!r} is not a file.")
        if path.stat().st_size > _MAX_SOURCE_BYTES:
            raise DocumentInspectionError(
                f"{path.name!r} exceeds the { _MAX_SOURCE_BYTES:,}-byte DOCX inspection limit."
            )
        with zipfile.ZipFile(path) as archive:
            _validate_archive(archive, path.name)
            if _MAIN_DOCUMENT not in archive.namelist():
                raise DocumentInspectionError(f"{path.name!r} has no Word main-document XML member.")
            metadata = _metadata(archive)
            blocks = _blocks(_xml_member(archive, _MAIN_DOCUMENT, path.name))
    except DocumentInspectionError:
        raise
    except (OSError, zipfile.BadZipFile, ET.ParseError) as failed:
        raise DocumentInspectionError(f"Could not inspect {path.name!r}: {failed}") from failed

    result: dict[str, Any] = {"format": "DOCX", "metadata": metadata, "blocks": []}
    omitted = 0
    for block in blocks:
        if len(result["blocks"]) >= max_blocks or not _fits(result, block, max_characters):
            omitted += 1
            continue
        result["blocks"].append(block)
    if omitted:
        result["truncated"] = {"omitted_blocks": omitted}
    # Metadata can never make the normal result excessive: it is length-limited
    # while parsed.  A very small caller budget still returns valid evidence.
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if len(rendered) > max_characters:
        return json.dumps({"format": "DOCX", "truncated": {"reason": "max_characters"}}, indent=2)
    return rendered


def _validate_archive(archive: zipfile.ZipFile, name: str) -> None:
    members = archive.infolist()
    if len(members) > _MAX_MEMBERS:
        raise DocumentInspectionError(f"{name!r} has too many ZIP members to inspect safely.")
    if sum(member.file_size for member in members) > _MAX_UNCOMPRESSED_BYTES:
        raise DocumentInspectionError(f"{name!r} expands beyond the DOCX inspection limit.")


def _xml_member(archive: zipfile.ZipFile, member: str, name: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(member))
    except KeyError as absent:
        raise DocumentInspectionError(f"{name!r} is missing required member {member!r}.") from absent


def _metadata(archive: zipfile.ZipFile) -> dict[str, str]:
    if _CORE_PROPERTIES not in archive.namelist():
        return {}
    root = _xml_member(archive, _CORE_PROPERTIES, "DOCX")
    wanted = {
        "title": _DC + "title", "subject": _DC + "subject", "creator": _DC + "creator",
        "keywords": _CP + "keywords", "description": _DC + "description",
        "created": _DCTERMS + "created", "modified": _DCTERMS + "modified",
    }
    result: dict[str, str] = {}
    for label, tag in wanted.items():
        value = root.findtext(tag)
        if value and value.strip():
            result[label] = value.strip()[:1_000]
    return result


def _blocks(root: ET.Element) -> list[dict[str, Any]]:
    body = root.find(_W + "body")
    if body is None:
        return []
    extracted: list[dict[str, Any]] = []
    for child in body:
        if child.tag == _W + "p":
            text = _paragraph_text(child)
            if text:
                block: dict[str, Any] = {"type": "paragraph", "text": text}
                style = child.find("./" + _W + "pPr/" + _W + "pStyle")
                if style is not None and style.get(_W + "val"):
                    block["style"] = style.get(_W + "val")
                extracted.append(block)
        elif child.tag == _W + "tbl":
            rows = []
            for row in child.findall("./" + _W + "tr"):
                rows.append([_paragraph_text(cell) for cell in row.findall("./" + _W + "tc")])
            if rows:
                extracted.append({"type": "table", "rows": rows})
    return extracted


def _paragraph_text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == _W + "t" and node.text:
            parts.append(node.text)
        elif node.tag == _W + "tab":
            parts.append("\t")
        elif node.tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    return "".join(parts).strip()[:4_000]


def _fits(result: dict[str, Any], addition: dict[str, Any], maximum: int) -> bool:
    candidate = {**result, "blocks": [*result["blocks"], addition]}
    return len(json.dumps(candidate, ensure_ascii=False, indent=2)) <= maximum
