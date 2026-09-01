"""Bounded, non-executing inspection of DOCX, PPTX, and ODT evidence.

Office Open XML and OpenDocument files are ZIP containers whose XML can contain
links, macros, and other active document features.  This reader opens only a
small, named set of XML parts and extracts their text nodes and descriptive
metadata.  It never follows relationships, evaluates fields/formulas, renders
documents, or opens embedded objects.
"""

from __future__ import annotations

import json
import re
import stat
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 96 * 1024 * 1024
_MAX_MEMBERS = 2_000
_MAX_PARTS = 50
_MAX_TEXT_CHARACTERS = 20_000
_MAX_METADATA_VALUE = 1_000

_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_ODT_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_META = "{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}"
_SLIDE_NUMBER = re.compile(r"^ppt/slides/slide([1-9][0-9]*)\.xml$")


class OfficeDocumentError(ValueError):
    """The office document is unsupported, malformed, or too large."""


def inspect_office_document(
    path: Path, *, max_parts: int = 10, max_characters: int = 8_000
) -> str:
    """Return metadata and bounded text from a DOCX, PPTX, or ODT file."""
    if not 1 <= max_parts <= _MAX_PARTS:
        raise OfficeDocumentError(f"max_parts must be between 1 and {_MAX_PARTS}.")
    if not 1 <= max_characters <= _MAX_TEXT_CHARACTERS:
        raise OfficeDocumentError(
            f"max_characters must be between 1 and {_MAX_TEXT_CHARACTERS}."
        )
    if not path.is_file():
        raise OfficeDocumentError(f"{path.name!r} is not a readable office document.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise OfficeDocumentError(f"Could not inspect {path.name!r}: {exc}") from exc
    if size > _MAX_SOURCE_BYTES:
        raise OfficeDocumentError(
            f"{path.name!r} is {size} bytes; office inspection is limited to {_MAX_SOURCE_BYTES} bytes."
        )
    try:
        with zipfile.ZipFile(path) as archive:
            _check_archive(archive)
            names = frozenset(archive.namelist())
            document_type = _document_type(names)
            metadata = _metadata(archive, document_type)
            selected = _text_parts(names, document_type)
            parts = [
                _part(archive, name, document_type, max_characters)
                for name in selected[:max_parts]
            ]
    except OfficeDocumentError:
        raise
    except (OSError, EOFError, ET.ParseError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OfficeDocumentError(f"Could not parse office document {path.name!r}: {exc}") from exc

    return json.dumps(
        {
            "format": document_type,
            "size_bytes": size,
            "metadata": metadata,
            "part_count": len(selected),
            "parts": parts,
            "parts_truncated": len(selected) > len(parts),
            "note": (
                "Only document XML text and descriptive metadata were read; macros, "
                "external links, embedded files, fields, formulas, and active content were not opened."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


def _check_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > _MAX_MEMBERS:
        raise OfficeDocumentError(f"office document has more than {_MAX_MEMBERS} ZIP members.")
    if sum(info.file_size for info in infos) > _MAX_UNCOMPRESSED_BYTES:
        raise OfficeDocumentError(
            f"office document expands beyond the {_MAX_UNCOMPRESSED_BYTES}-byte inspection limit."
        )
    if any(stat.S_ISLNK(info.external_attr >> 16) for info in infos):
        raise OfficeDocumentError("office document contains a symbolic-link ZIP member.")


def _document_type(names: frozenset[str]) -> str:
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "content.xml" in names and "mimetype" in names:
        return "odt"
    raise OfficeDocumentError("unsupported office document; supported formats are DOCX, PPTX, and ODT.")


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        with archive.open(name) as source:
            return ET.parse(source).getroot()
    except KeyError:
        return None


def _metadata(archive: zipfile.ZipFile, document_type: str) -> dict[str, str]:
    name = "meta.xml" if document_type == "odt" else "docProps/core.xml"
    root = _read_xml(archive, name)
    if root is None:
        return {}
    tags = (
        {
            "title": _DC + "title",
            "subject": _DC + "subject",
            "creator": _DC + "creator",
            "description": _DC + "description",
            "created": _META + "creation-date",
            "modified": _META + "date",
        }
        if document_type == "odt"
        else {
            "title": _DC + "title",
            "subject": _DC + "subject",
            "creator": _DC + "creator",
            "description": _DC + "description",
            "created": "{http://purl.org/dc/terms/}created",
            "modified": "{http://purl.org/dc/terms/}modified",
        }
    )
    result: dict[str, str] = {}
    for label, tag in tags.items():
        element = root.find(".//" + tag)
        if element is not None and element.text:
            result[label] = _clean(element.text)[:_MAX_METADATA_VALUE]
    return result


def _text_parts(names: frozenset[str], document_type: str) -> list[str]:
    if document_type == "docx":
        primary = ["word/document.xml"]
        extras = sorted(
            name
            for name in names
            if re.fullmatch(r"word/(?:header|footer)[0-9]+\.xml", name)
        )
        return primary + extras
    if document_type == "pptx":
        slides = []
        for name in names:
            match = _SLIDE_NUMBER.fullmatch(name)
            if match:
                slides.append((int(match.group(1)), name))
        return [name for _, name in sorted(slides)]
    return ["content.xml"]


def _part(
    archive: zipfile.ZipFile, name: str, document_type: str, limit: int
) -> dict[str, object]:
    root = _read_xml(archive, name)
    if root is None:
        raise OfficeDocumentError(f"office document is missing text part {name!r}.")
    if document_type == "docx":
        text = "\n".join(
            _paragraph_text(element, _WORD + "t")
            for element in root.iter(_WORD + "p")
        )
    elif document_type == "pptx":
        text = "\n".join(
            _clean("".join(element.itertext())) for element in root.iter(_DRAWING + "p")
        )
    else:
        text = "\n".join(
            _clean("".join(element.itertext()))
            for element in root.iter()
            if element.tag in {_ODT_TEXT + "p", _ODT_TEXT + "h"}
        )
    text = "\n".join(line for line in text.splitlines() if line)
    return {
        "name": _part_name(name, document_type),
        "text_preview": text[:limit],
        "text_preview_truncated": len(text) > limit,
    }


def _paragraph_text(element: ET.Element, text_tag: str) -> str:
    return _clean("".join(node.text or "" for node in element.iter(text_tag)))


def _part_name(name: str, document_type: str) -> str:
    if document_type == "pptx":
        match = _SLIDE_NUMBER.fullmatch(name)
        return f"slide {match.group(1)}" if match else name
    if name == "word/document.xml":
        return "document"
    return name.rsplit("/", 1)[-1]


def _clean(value: str) -> str:
    return " ".join(value.split())
