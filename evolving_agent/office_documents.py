"""Bounded, non-executing inspection of DOCX and PPTX evidence.

Office Open XML documents are ZIP containers.  This reader only parses selected
XML parts as data and inventories embedded media by archive metadata; it never
runs macros, follows relationships, opens external links, or extracts payloads.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_MAX_UNZIPPED_BYTES = 64 * 1024 * 1024
_MAX_MEMBERS = 4_000
_MAX_PART_BYTES = 8 * 1024 * 1024
_MAX_PARTS = 100
_MAX_TEXT = 12_000
_MAX_MEDIA = 100
_NUMBERED_SLIDE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


class OfficeDocumentError(ValueError):
    """An Office document was unsupported, malformed, or exceeded a bound."""


def inspect_office_document(path: Path, *, max_parts: int = 20, max_characters: int = 8_000) -> str:
    """Return bounded DOCX/PPTX text and a non-payload media inventory."""
    if not 1 <= max_parts <= _MAX_PARTS:
        raise OfficeDocumentError(f"max_parts must be between 1 and {_MAX_PARTS}.")
    if not 1 <= max_characters <= _MAX_TEXT:
        raise OfficeDocumentError(f"max_characters must be between 1 and {_MAX_TEXT}.")
    if not path.is_file():
        raise OfficeDocumentError(f"{path.name!r} is not a readable Office document.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise OfficeDocumentError(f"Could not inspect {path.name!r}: {exc}") from exc
    if size > _MAX_SOURCE_BYTES:
        raise OfficeDocumentError(f"{path.name!r} is over the { _MAX_SOURCE_BYTES // 1024 // 1024 } MB inspection limit.")
    suffix = path.suffix.lower()
    if suffix not in {".docx", ".pptx"}:
        raise OfficeDocumentError("supported Office formats are .docx and .pptx.")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_MEMBERS or sum(item.file_size for item in infos) > _MAX_UNZIPPED_BYTES:
                raise OfficeDocumentError("Office archive exceeds the safe inspection limit.")
            names = {item.filename for item in infos}
            required = "word/document.xml" if suffix == ".docx" else "ppt/presentation.xml"
            if required not in names:
                raise OfficeDocumentError(f"{path.name!r} is not a recognizable {suffix[1:].upper()} package.")
            parts = _docx_parts(names) if suffix == ".docx" else _pptx_parts(names)
            selected = parts[:max_parts]
            entries = [_part_report(archive, name, max_characters) for name in selected]
            media = [
                {"name": item.filename, "size_bytes": item.file_size}
                for item in infos if item.filename.startswith(("word/media/", "ppt/media/"))
            ][: _MAX_MEDIA]
    except OfficeDocumentError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise OfficeDocumentError(f"Could not parse Office archive {path.name!r}: {exc}") from exc
    return json.dumps({
        "format": suffix[1:], "size_bytes": size, "parts": entries,
        "part_count": len(parts), "parts_truncated": len(parts) > len(selected),
        "media": media, "media_count": sum(1 for item in infos if item.filename.startswith(("word/media/", "ppt/media/"))),
        "media_truncated": sum(1 for item in infos if item.filename.startswith(("word/media/", "ppt/media/"))) > len(media),
        "note": "Text is read from selected XML parts only; macros, external links, embedded objects, and media payloads are not opened.",
    }, ensure_ascii=False, indent=2)


def _docx_parts(names: set[str]) -> list[str]:
    order = ["word/document.xml"]
    for prefix in ("word/header", "word/footer"):
        order.extend(sorted(name for name in names if name.startswith(prefix) and name.endswith(".xml")))
    order.extend(name for name in ("word/footnotes.xml", "word/endnotes.xml", "word/comments.xml") if name in names)
    return order


def _pptx_parts(names: set[str]) -> list[str]:
    slides = []
    for name in names:
        found = _NUMBERED_SLIDE.match(name)
        if found:
            slides.append((int(found.group(1)), name))
    return [name for _, name in sorted(slides)]


def _part_report(archive: zipfile.ZipFile, name: str, maximum: int) -> dict[str, object]:
    info = archive.getinfo(name)
    if info.file_size > _MAX_PART_BYTES:
        return {"name": name, "error": "XML part exceeds the inspection limit."}
    try:
        with archive.open(info) as source:
            root = ET.parse(source).getroot()
    except (OSError, KeyError, ET.ParseError) as exc:
        return {"name": name, "error": f"Could not parse XML: {exc}"}
    paragraphs: list[str] = []
    # Both Word paragraphs and DrawingML paragraphs end in the local name p.
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "p":
            text = "".join(node.itertext()).strip()
            if text:
                paragraphs.append(" ".join(text.split()))
    if not paragraphs:
        text = " ".join(" ".join(root.itertext()).split())
        if text:
            paragraphs = [text]
    combined = "\n".join(paragraphs)
    return {"name": name, "text_preview": combined[:maximum], "text_preview_truncated": len(combined) > maximum}
