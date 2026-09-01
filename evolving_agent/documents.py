"""Bounded extraction of evidence from PDFs, images, Office Open XML, and OpenDocument files."""

from __future__ import annotations

import posixpath
import re
import tempfile
import xml.etree.ElementTree as element_tree
import zipfile
from pathlib import Path
from typing import Any, Callable

from PIL import ExifTags, Image, UnidentifiedImageError

MAX_TEXT_CHARACTERS = 24_000
MAX_EPUB_MEMBER_BYTES = 1_000_000
MAX_ODF_CONTENT_BYTES = 4_000_000
MAX_IMAGE_METADATA_FIELDS = 64
MAX_IMAGE_METADATA_VALUE_CHARACTERS = 500
"""Enough evidence for a model, without one attachment consuming a turn."""


class DocumentError(Exception):
    """A document is unsupported, malformed, or could not be inspected."""


def inspect_document(
    path: Path,
    *,
    page: int = 1,
    ocr: bool = False,
    run: Callable[[list[str]], tuple[int | None, str, str]],
) -> str:
    """Return text and useful context from one document without modifying it.

    ``run`` is supplied by the workspace tool so every external program shares
    its command timeout and process cleanup.  A PDF page first gets its embedded
    text; rendering/OCR is used when requested or when that page is scanned.
    """
    if page < 1:
        raise DocumentError("'page' must be a positive integer.")
    suffix = path.suffix.lower()
    if suffix == ".pdf" or _magic(path, b"%PDF-"):
        return _pdf(path, page, ocr, run)
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        return _image(path, run)
    if suffix in {".docx", ".pptx", ".xlsx"}:
        return _ooxml(path, suffix)
    if suffix in {".odt", ".ods", ".odp"}:
        return _odf(path, suffix)
    if suffix == ".epub":
        return _epub(path, page)
    raise DocumentError(
        "Supported document formats are PDF, PNG/JPEG/TIFF/BMP/WebP images, DOCX/PPTX/XLSX, ODT/ODS/ODP, and EPUB."
    )


def _pdf(path: Path, page: int, ocr: bool, run: Callable[[list[str]], tuple[int | None, str, str]]) -> str:
    code, info, error = run(["pdfinfo", str(path)])
    if code != 0:
        raise DocumentError(f"pdfinfo could not read the PDF: {_reason(error)}")
    pages = _pdf_pages(info)
    if pages is not None and page > pages:
        raise DocumentError(f"The PDF has {pages} page(s); page {page} does not exist.")
    code, text, error = run(["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(path), "-"])
    if code != 0:
        raise DocumentError(f"pdftotext could not extract page {page}: {_reason(error)}")
    text = _bounded(text)
    header = f"PDF page {page}" + (f" of {pages}" if pages is not None else "")
    # A scanned page commonly emits whitespace only. The automatic fallback is
    # important because a model cannot infer from a file listing that it needs OCR.
    if ocr or len(text.strip()) < 20:
        return _ocr_pdf(path, page, header, text, run)
    return f"{header} (embedded text)\n---\n{text}"


def _ocr_pdf(path: Path, page: int, header: str, embedded: str, run: Callable[[list[str]], tuple[int | None, str, str]]) -> str:
    # Render into a private temporary directory, never beside a read-only task
    # attachment.  The command runner still owns timeout and child cleanup.
    with tempfile.TemporaryDirectory(prefix="agent-pdf-ocr-") as directory:
        prefix = str(Path(directory) / "page")
        code, _, error = run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "200", "-png", "-singlefile", str(path), prefix])
        image = Path(prefix + ".png")
        if code != 0 or not image.is_file():
            raise DocumentError(f"Could not render PDF page {page} for OCR: {_reason(error)}")
        code, text, error = run(["tesseract", str(image), "stdout", "--psm", "3"])
        if code != 0:
            raise DocumentError(f"Tesseract could not OCR PDF page {page}: {_reason(error)}")
    label = "OCR" if not embedded.strip() else "embedded text plus OCR"
    return f"{header} ({label})\n---\n{_bounded(text)}"


def _image(path: Path, run: Callable[[list[str]], tuple[int | None, str, str]]) -> str:
    """Read both visible image text and provenance stored in its container."""
    metadata = _image_metadata(path)
    code, text, error = run(["tesseract", str(path), "stdout", "--psm", "3"])
    if code != 0:
        raise DocumentError(f"Tesseract could not read this image: {_reason(error)}")
    return f"Image evidence ({path.name})\nMetadata:\n{metadata}\n---\nOCR text:\n{_bounded(text)}"


def _image_metadata(path: Path) -> str:
    """Render bounded, human-readable container and EXIF evidence without pixels."""
    try:
        with Image.open(path) as image:
            lines = [
                f"- Format: {image.format or 'unknown'}",
                f"- Dimensions: {image.width} x {image.height}",
                f"- Color mode: {image.mode}",
            ]
            exif = image.getexif()
            lines.extend(_image_provenance(exif))
            values: list[tuple[str, str]] = []
            for tag, value in exif.items():
                # GPS is rendered below as coordinates instead of opaque rationals.
                if tag == 34853:
                    continue
                label = ExifTags.TAGS.get(tag, f"EXIF tag {tag}")
                values.append((str(label), _metadata_value(value)))
            for label, value in sorted(values, key=lambda item: item[0])[:MAX_IMAGE_METADATA_FIELDS]:
                lines.append(f"- EXIF {label}: {value}")
            if len(values) > MAX_IMAGE_METADATA_FIELDS:
                lines.append(f"- EXIF: {len(values) - MAX_IMAGE_METADATA_FIELDS} additional field(s) omitted")
            lines.extend(_gps_metadata(exif))
    except (OSError, SyntaxError, UnidentifiedImageError) as error:
        raise DocumentError(f"Could not read image metadata: {error}") from error
    return "\n".join(lines)


def _image_provenance(exif: Any) -> list[str]:
    """Render common EXIF facts in terms useful to an evidence reader."""
    lines: list[str] = []
    orientation = exif.get(274)
    orientation_labels = {
        1: "normal", 2: "mirrored horizontally", 3: "rotated 180 degrees",
        4: "mirrored vertically", 5: "mirrored horizontally, rotated 270 degrees",
        6: "rotated 90 degrees clockwise", 7: "mirrored horizontally, rotated 90 degrees",
        8: "rotated 270 degrees clockwise",
    }
    if orientation is not None:
        lines.append(f"- Display orientation: {orientation_labels.get(orientation, _metadata_value(orientation))}")
    # DateTimeOriginal is the camera's capture time; its label makes it less
    # likely that a reader mistakes an edit/export timestamp for capture time.
    captured = exif.get(36867)
    if captured is not None:
        lines.append(f"- Capture time (EXIF, timezone may be absent): {_metadata_value(captured)}")
    return lines


def _gps_metadata(exif: Any) -> list[str]:
    """Return decimal GPS evidence when an EXIF GPS IFD is present."""
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except (AttributeError, KeyError, TypeError, ValueError):
        gps = {}
    if not gps:
        return []
    lines: list[str] = []
    latitude = _gps_coordinate(gps.get(2), gps.get(1))
    longitude = _gps_coordinate(gps.get(4), gps.get(3))
    if latitude is not None and longitude is not None:
        lines.append(f"- GPS coordinates: {latitude:.6f}, {longitude:.6f}")
    for tag, value in sorted(gps.items()):
        if tag in {1, 2, 3, 4}:
            continue
        label = ExifTags.GPSTAGS.get(tag, f"GPS tag {tag}")
        lines.append(f"- EXIF GPS {label}: {_metadata_value(value)}")
    return lines[:MAX_IMAGE_METADATA_FIELDS]


def _gps_coordinate(value: Any, reference: Any) -> float | None:
    try:
        degrees, minutes, seconds = value
        coordinate = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
        if str(reference).upper() in {"S", "W"}:
            coordinate = -coordinate
        return coordinate
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _metadata_value(value: Any, depth: int = 0) -> str:
    """Avoid unbounded binary, nested, or control-character metadata output."""
    if depth >= 3:
        return "…"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, (tuple, list)):
        rendered = [_metadata_value(item, depth + 1) for item in value[:16]]
        if len(value) > 16:
            rendered.append("…")
        return "[" + ", ".join(rendered) + "]"
    if isinstance(value, dict):
        rendered = [f"{key}: {_metadata_value(item, depth + 1)}" for key, item in list(value.items())[:16]]
        if len(value) > 16:
            rendered.append("…")
        return "{" + ", ".join(rendered) + "}"
    text = str(value).replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    if len(text) > MAX_IMAGE_METADATA_VALUE_CHARACTERS:
        return text[:MAX_IMAGE_METADATA_VALUE_CHARACTERS] + "… [truncated]"
    return text


def _ooxml(path: Path, suffix: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            if suffix == ".docx":
                parts = ["word/document.xml"]
            elif suffix == ".pptx":
                parts = sorted(name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
            else:
                parts = ["xl/sharedStrings.xml"]
            text = "\n".join(_xml_text(archive.read(part)) for part in parts if part in archive.namelist())
    except (OSError, zipfile.BadZipFile, element_tree.ParseError) as error:
        raise DocumentError(f"Could not read Office document: {error}") from error
    if not text.strip():
        raise DocumentError("No readable text was found in this Office document.")
    return f"{suffix[1:].upper()} extracted text ({path.name})\n---\n{_bounded(text)}"


def _odf(path: Path, suffix: str) -> str:
    """Extract visible text from an OpenDocument package without unpacking it.

    ODT, ODS, and ODP all keep their principal document content in content.xml.
    Reading just that member avoids executing macros or following package links.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                info = archive.getinfo("content.xml")
            except KeyError as error:
                raise DocumentError("OpenDocument package has no content.xml member.") from error
            if info.is_dir() or info.file_size > MAX_ODF_CONTENT_BYTES:
                raise DocumentError(
                    f"OpenDocument content.xml exceeds the {MAX_ODF_CONTENT_BYTES}-byte inspection limit."
                )
            with archive.open(info) as source:
                data = source.read(MAX_ODF_CONTENT_BYTES + 1)
            if len(data) > MAX_ODF_CONTENT_BYTES:
                raise DocumentError(
                    f"OpenDocument content.xml exceeds the {MAX_ODF_CONTENT_BYTES}-byte inspection limit."
                )
            text = _xml_text(data)
    except (OSError, zipfile.BadZipFile, element_tree.ParseError) as error:
        raise DocumentError(f"Could not read OpenDocument file: {error}") from error
    if not text.strip():
        raise DocumentError("No readable text was found in this OpenDocument file.")
    return f"{suffix[1:].upper()} extracted text ({path.name})\n---\n{_bounded(text)}"


def _epub(path: Path, chapter: int) -> str:
    """Extract one spine chapter from an EPUB without unpacking its ZIP package."""
    try:
        with zipfile.ZipFile(path) as archive:
            container = _epub_xml(archive, "META-INF/container.xml")
            rootfile = next((node.attrib.get("full-path") for node in container.iter() if node.tag.endswith("rootfile")), None)
            if not rootfile or not _safe_epub_name(rootfile):
                raise DocumentError("EPUB container has no safe package document path.")
            package = _epub_xml(archive, rootfile)
            base = posixpath.dirname(rootfile)
            manifest = {
                item.attrib.get("id"): _epub_join(base, item.attrib.get("href", ""))
                for item in package.iter() if item.tag.endswith("item")
            }
            spine = [manifest.get(item.attrib.get("idref")) for item in package.iter() if item.tag.endswith("itemref")]
            chapters = [name for name in spine if name]
            if not chapters:
                raise DocumentError("EPUB package contains no readable spine chapters.")
            if chapter > len(chapters):
                raise DocumentError(f"The EPUB has {len(chapters)} spine chapter(s); chapter {chapter} does not exist.")
            name = chapters[chapter - 1]
            text = _xml_text(_epub_bytes(archive, name))
    except (OSError, zipfile.BadZipFile, element_tree.ParseError) as error:
        raise DocumentError(f"Could not read EPUB: {error}") from error
    if not text.strip():
        raise DocumentError(f"No readable text was found in EPUB chapter {chapter}.")
    return f"EPUB chapter {chapter} of {len(chapters)} ({path.name}: {name})\n---\n{_bounded(text)}"


def _epub_xml(archive: zipfile.ZipFile, name: str) -> element_tree.Element:
    return element_tree.fromstring(_epub_bytes(archive, name))


def _epub_bytes(archive: zipfile.ZipFile, name: str) -> bytes:
    if not _safe_epub_name(name):
        raise DocumentError("EPUB refers to an unsafe package member.")
    try:
        info = archive.getinfo(name)
    except KeyError as error:
        raise DocumentError(f"EPUB member {name!r} is missing.") from error
    if info.is_dir() or info.file_size > MAX_EPUB_MEMBER_BYTES:
        raise DocumentError(f"EPUB member {name!r} exceeds the {MAX_EPUB_MEMBER_BYTES}-byte inspection limit.")
    with archive.open(info) as source:
        data = source.read(MAX_EPUB_MEMBER_BYTES + 1)
    if len(data) > MAX_EPUB_MEMBER_BYTES:
        raise DocumentError(f"EPUB member {name!r} exceeds the {MAX_EPUB_MEMBER_BYTES}-byte inspection limit.")
    return data


def _epub_join(base: str, href: str) -> str | None:
    name = posixpath.normpath(posixpath.join(base, href.split("#", 1)[0]))
    within_package = not base or name.startswith(base + "/")
    return name if within_package and _safe_epub_name(name) else None


def _safe_epub_name(name: str) -> bool:
    return bool(name) and not name.startswith("/") and "\x00" not in name and not any(part == ".." for part in name.split("/"))


def _xml_text(data: bytes) -> str:
    root = element_tree.fromstring(data)
    # OOXML stores visible words in <w:t>, <a:t>, and <t>; joining descendants
    # also handles spreadsheet shared strings without format-specific namespaces.
    return " ".join(node.text.strip() for node in root.iter() if node.text and node.text.strip())


def _pdf_pages(info: str) -> int | None:
    match = re.search(r"^Pages:\s*(\d+)", info, re.MULTILINE)
    return int(match.group(1)) if match else None


def _magic(path: Path, wanted: bytes) -> bool:
    try:
        with path.open("rb") as source:
            return source.read(len(wanted)) == wanted
    except OSError as error:
        raise DocumentError(f"Could not read document: {error}") from error


def _reason(error: str) -> str:
    return error.strip() or "no diagnostic was returned"


def _bounded(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARACTERS:
        return text
    return text[:MAX_TEXT_CHARACTERS] + f"\n... [truncated at {MAX_TEXT_CHARACTERS} characters]"
