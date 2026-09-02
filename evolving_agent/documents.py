"""Bounded extraction of evidence from PDFs, images, Office Open XML, and OpenDocument files."""

from __future__ import annotations

import posixpath
import re
import tempfile
import xml.etree.ElementTree as element_tree
import zipfile
from pathlib import Path
from typing import Callable

MAX_TEXT_CHARACTERS = 24_000
MAX_EPUB_MEMBER_BYTES = 1_000_000
MAX_ODF_CONTENT_BYTES = 4_000_000
MAX_XLSX_MEMBER_BYTES = 4_000_000
MAX_XLSX_SHEETS = 32
MAX_XLSX_CELLS = 4_000
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
    code, text, error = run(["tesseract", str(path), "stdout", "--psm", "3"])
    if code != 0:
        raise DocumentError(f"Tesseract could not read this image: {_reason(error)}")
    return f"Image OCR ({path.name})\n---\n{_bounded(text)}"


def _ooxml(path: Path, suffix: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            if suffix == ".docx":
                parts = ["word/document.xml"]
            elif suffix == ".pptx":
                parts = sorted(name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
            else:
                return _xlsx(archive, path.name)
            text = "\n".join(_xml_text(archive.read(part)) for part in parts if part in archive.namelist())
    except (OSError, zipfile.BadZipFile, element_tree.ParseError) as error:
        raise DocumentError(f"Could not read Office document: {error}") from error
    if not text.strip():
        raise DocumentError("No readable text was found in this Office document.")
    return f"{suffix[1:].upper()} extracted text ({path.name})\n---\n{_bounded(text)}"


def _xlsx(archive: zipfile.ZipFile, filename: str) -> str:
    """Render populated XLSX cells, not merely its shared-string dictionary.

    OOXML stores sheet names, relationship targets, strings, and cell values in
    separate parts.  Joining all XML would lose their association, so this
    follows only workbook-declared worksheet relationships and prints compact
    coordinate/value evidence.  It intentionally does not evaluate formulas.
    """
    names = set(archive.namelist())
    if "xl/workbook.xml" not in names:
        raise DocumentError("XLSX package has no workbook.xml member.")
    workbook = element_tree.fromstring(_xlsx_bytes(archive, "xl/workbook.xml"))
    relationships: dict[str, str] = {}
    rels_name = "xl/_rels/workbook.xml.rels"
    if rels_name in names:
        rels = element_tree.fromstring(_xlsx_bytes(archive, rels_name))
        for relation in rels:
            relation_id, target = relation.attrib.get("Id"), relation.attrib.get("Target", "")
            if relation_id and target and not target.startswith("/") and ".." not in target.split("/"):
                relationships[relation_id] = "xl/" + target.lstrip("/")
    shared = _xlsx_shared_strings(archive) if "xl/sharedStrings.xml" in names else []
    output: list[str] = []
    cells_left = MAX_XLSX_CELLS
    sheets = [node for node in workbook.iter() if node.tag.endswith("sheet")]
    for sheet in sheets[:MAX_XLSX_SHEETS]:
        relation_id = next((value for key, value in sheet.attrib.items() if key.endswith("}id")), None)
        target = relationships.get(relation_id or "")
        title = sheet.attrib.get("name", "(unnamed sheet)")
        if not target or target not in names:
            output.append(f"Sheet: {title} (worksheet data unavailable)")
            continue
        root = element_tree.fromstring(_xlsx_bytes(archive, target))
        output.append(f"Sheet: {title}")
        found = False
        for cell in (node for node in root.iter() if node.tag.endswith("c")):
            value = _xlsx_cell_value(cell, shared)
            if value is None:
                continue
            found = True
            coordinate = cell.attrib.get("r", "?")
            output.append(f"{coordinate}: {value}")
            cells_left -= 1
            if cells_left == 0:
                output.append(f"[cell preview truncated at {MAX_XLSX_CELLS} populated cells]")
                break
        if not found:
            output.append("(no populated cells)")
        if cells_left == 0:
            break
    if not output:
        raise DocumentError("XLSX workbook contains no worksheets.")
    return f"XLSX extracted cells ({filename})\n---\n{_bounded(chr(10).join(output))}"


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = element_tree.fromstring(_xlsx_bytes(archive, "xl/sharedStrings.xml"))
    return ["".join(node.itertext()) for node in root if node.tag.endswith("si")]


def _xlsx_cell_value(cell: element_tree.Element, shared: list[str]) -> str | None:
    kind = cell.attrib.get("t")
    formula = next(("".join(node.itertext()) for node in cell if node.tag.endswith("f")), None)
    raw = next(("".join(node.itertext()) for node in cell if node.tag.endswith("v")), None)
    if kind == "inlineStr":
        raw = "".join(text for node in cell if node.tag.endswith("is") for text in node.itertext())
    if raw is None and formula is None:
        return None
    if kind == "s" and raw is not None:
        try:
            value = shared[int(raw)]
        except (ValueError, IndexError):
            value = f"[invalid shared string index: {raw}]"
    elif kind == "b" and raw is not None:
        value = "TRUE" if raw == "1" else "FALSE" if raw == "0" else raw
    else:
        value = raw or ""
    return f"={formula} -> {value}" if formula is not None else value


def _xlsx_bytes(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as error:
        raise DocumentError(f"XLSX package has no {name} member.") from error
    if info.is_dir() or info.file_size > MAX_XLSX_MEMBER_BYTES:
        raise DocumentError(f"XLSX member {name} exceeds the {MAX_XLSX_MEMBER_BYTES}-byte inspection limit.")
    with archive.open(info) as source:
        data = source.read(MAX_XLSX_MEMBER_BYTES + 1)
    if len(data) > MAX_XLSX_MEMBER_BYTES:
        raise DocumentError(f"XLSX member {name} exceeds the {MAX_XLSX_MEMBER_BYTES}-byte inspection limit.")
    return data


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
