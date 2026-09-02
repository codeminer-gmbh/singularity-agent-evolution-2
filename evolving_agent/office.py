"""Bounded, non-executing extraction from OOXML Word and PowerPoint files.

DOCX and PPTX files are ZIP containers.  This module reads only the XML parts
that carry visible text and table cells; it never opens relationships, embedded
objects, macros, external links, or media.  It intentionally uses the standard
library rather than an Office application, so inspecting evidence cannot run it.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_MAX_MEMBERS = 2_000
_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_SLIDES = 100
_MAX_CHARACTERS = 20_000

_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")


class OfficeError(ValueError):
    """An Office evidence file could not be safely inspected."""


def inspect_office(path: Path, *, max_slides: int = 10, max_characters: int = 8_000) -> str:
    """Return a bounded JSON preview of a DOCX or PPTX package.

    ``max_slides`` applies only to PPTX; DOCX block extraction is globally
    bounded by ``max_characters``. The routine does not resolve OOXML
    relationship targets, avoiding both external fetches and attachment reads.
    """
    if not 1 <= max_slides <= _MAX_SLIDES:
        raise OfficeError(f"max_slides must be between 1 and {_MAX_SLIDES}.")
    if not 1 <= max_characters <= _MAX_CHARACTERS:
        raise OfficeError(f"max_characters must be between 1 and {_MAX_CHARACTERS}.")
    if not path.is_file():
        raise OfficeError(f"{path.name!r} is not a readable Office file.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise OfficeError(f"Could not inspect {path.name!r}: {exc}") from exc
    if size > _MAX_SOURCE_BYTES:
        raise OfficeError(f"{path.name!r} is {size} bytes; Office inspection is limited to {_MAX_SOURCE_BYTES} bytes.")
    try:
        with zipfile.ZipFile(path) as package:
            _validate_package(package)
            names = set(package.namelist())
            if "word/document.xml" in names:
                report = _docx(package, max_characters)
            elif any(_SLIDE_RE.match(name) for name in names):
                report = _pptx(package, max_slides, max_characters)
            else:
                raise OfficeError("The ZIP package is not a DOCX or PPTX file with readable document parts.")
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise OfficeError(f"Could not parse Office file {path.name!r}: {exc}") from exc
    report["size_bytes"] = size
    report["note"] = "Only bounded visible XML text and table cells were read; macros, links, embedded files, media, and relationships were not opened."
    return json.dumps(report, ensure_ascii=False, indent=2)


def _validate_package(package: zipfile.ZipFile) -> None:
    infos = package.infolist()
    if len(infos) > _MAX_MEMBERS:
        raise OfficeError(f"Office package has {len(infos)} members; limit is {_MAX_MEMBERS}.")
    total = sum(info.file_size for info in infos)
    if total > _MAX_UNCOMPRESSED_BYTES:
        raise OfficeError(f"Office package expands to {total} bytes; limit is {_MAX_UNCOMPRESSED_BYTES} bytes.")


def _xml(package: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(package.read(name))


def _text(node: ET.Element, namespace: str) -> str:
    """Extract text in XML order, retaining visible breaks and tabs.

    OOXML represents a line break or tab as an element rather than a text run.
    Ignoring those elements joined distinct values (particularly multi-paragraph
    Word table cells), which made a bounded evidence preview misleading.
    """
    text: list[str] = []
    for item in node.iter():
        if item.tag == namespace + "t":
            text.append(item.text or "")
        elif namespace == _WORD and item.tag == _WORD + "tab":
            text.append("\t")
        elif namespace == _WORD and item.tag in (_WORD + "br", _WORD + "cr"):
            text.append("\n")
        elif namespace == _DRAWING and item.tag == _DRAWING + "br":
            text.append("\n")
    return "".join(text)


def _cell_text(cell: ET.Element) -> str:
    """Return a Word table cell with its visible paragraph boundaries intact."""
    paragraphs = cell.findall(_WORD + "p")
    return "\n".join(_text(paragraph, _WORD) for paragraph in paragraphs)


def _docx(package: zipfile.ZipFile, maximum: int) -> dict[str, Any]:
    root = _xml(package, "word/document.xml")
    body = root.find(_WORD + "body")
    blocks: list[dict[str, Any]] = []
    used = 0
    truncated = False
    if body is not None:
        for child in body:
            if child.tag == _WORD + "p":
                block: dict[str, Any] = {"type": "paragraph", "text": _text(child, _WORD)}
            elif child.tag == _WORD + "tbl":
                rows = []
                for row in child.findall(_WORD + "tr"):
                    rows.append([_cell_text(cell) for cell in row.findall(_WORD + "tc")])
                block = {"type": "table", "rows": rows}
            else:
                continue
            encoded = json.dumps(block, ensure_ascii=False)
            if used + len(encoded) > maximum:
                truncated = True
                break
            blocks.append(block)
            used += len(encoded)
    return {"format": "docx", "blocks": blocks, "blocks_truncated": truncated}


def _pptx(package: zipfile.ZipFile, max_slides: int, maximum: int) -> dict[str, Any]:
    slides = sorted(((int(match.group(1)), name) for name in package.namelist() if (match := _SLIDE_RE.match(name))), key=lambda item: item[0])
    result: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for number, name in slides[:max_slides]:
        root = _xml(package, name)
        texts = []
        # A paragraph is a readable unit, including text inside a table cell.
        for paragraph in root.iter(_DRAWING + "p"):
            value = _text(paragraph, _DRAWING)
            if value:
                texts.append(value)
        slide = {"slide_number": number, "text": texts}
        encoded = json.dumps(slide, ensure_ascii=False)
        if used + len(encoded) > maximum:
            truncated = True
            break
        result.append(slide)
        used += len(encoded)
    if len(slides) > len(result):
        truncated = True
    return {"format": "pptx", "slide_count": len(slides), "slides": result, "slides_truncated": truncated}
