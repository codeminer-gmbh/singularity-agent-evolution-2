"""Bounded extraction of evidence from documents, images, and local HTML."""

from __future__ import annotations

import posixpath
import re
from html.parser import HTMLParser
import tempfile
import xml.etree.ElementTree as element_tree
import zipfile
from pathlib import Path
from typing import Callable

MAX_TEXT_CHARACTERS = 24_000
MAX_EPUB_MEMBER_BYTES = 1_000_000
MAX_ODF_CONTENT_BYTES = 4_000_000
MAX_OOXML_PART_BYTES = 4_000_000
MAX_OOXML_COMMENT_PARTS = 128
MAX_HTML_BYTES = 4_000_000
MAX_HTML_LINKS = 1_000
MAX_HTML_ROWS = 1_000
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
    if suffix in {".html", ".htm", ".xhtml"}:
        return _html(path)
    raise DocumentError(
        "Supported document formats are PDF, PNG/JPEG/TIFF/BMP/WebP images, DOCX/PPTX/XLSX, ODT/ODS/ODP, EPUB, and HTML/HTM/XHTML."
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
    """Extract visible OOXML text together with reviewer comments.

    Comments often hold the actual requested decision or correction.  They are
    optional package parts, so a document with comments but little body text is
    still useful evidence.  Read package members by declared uncompressed size
    rather than extracting them, keeping a hostile attachment bounded.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if suffix == ".docx":
                body_parts = ["word/document.xml"]
                comments = _docx_comments(archive, names)
            elif suffix == ".pptx":
                body_parts = sorted(name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
                comments = _pptx_comments(archive, names)
            else:
                body_parts = ["xl/sharedStrings.xml"]
                comments = _xlsx_comments(archive, names)
            text = "\n".join(_xml_text(_ooxml_bytes(archive, part)) for part in body_parts if part in names)
    except (OSError, zipfile.BadZipFile, element_tree.ParseError) as error:
        raise DocumentError(f"Could not read Office document: {error}") from error
    evidence = "\n".join(part for part in (text, comments) if part.strip())
    if not evidence.strip():
        raise DocumentError("No readable text or comments were found in this Office document.")
    return f"{suffix[1:].upper()} extracted text and comments ({path.name})\n---\n{_bounded(evidence)}"


def _ooxml_bytes(archive: zipfile.ZipFile, name: str) -> bytes:
    """Read one OOXML XML part under the document-inspection size limit."""
    info = archive.getinfo(name)
    if info.is_dir() or info.file_size > MAX_OOXML_PART_BYTES:
        raise DocumentError(f"Office document member {name!r} exceeds the {MAX_OOXML_PART_BYTES}-byte inspection limit.")
    with archive.open(info) as source:
        data = source.read(MAX_OOXML_PART_BYTES + 1)
    if len(data) > MAX_OOXML_PART_BYTES:
        raise DocumentError(f"Office document member {name!r} exceeds the {MAX_OOXML_PART_BYTES}-byte inspection limit.")
    return data


def _local(node: element_tree.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _comment_text(node: element_tree.Element) -> str:
    return " ".join(child.text.strip() for child in node.iter() if child.text and child.text.strip())


def _attr(node: element_tree.Element, name: str, default: str = "") -> str:
    """Return an XML attribute whether its producer namespaces attributes or not."""
    return next((value for key, value in node.attrib.items() if key.rsplit("}", 1)[-1] == name), default)


def _docx_comments(archive: zipfile.ZipFile, names: set[str]) -> str:
    if "word/comments.xml" not in names:
        return ""
    root = element_tree.fromstring(_ooxml_bytes(archive, "word/comments.xml"))
    comments = []
    for node in root.iter():
        if _local(node) != "comment":
            continue
        text = _comment_text(node)
        if text:
            author = _attr(node, "author", "unknown author")
            identifier = _attr(node, "id", "?")
            comments.append(f"DOCX comment {identifier} by {author}: {text}")
    return "\n".join(comments)


def _pptx_comments(archive: zipfile.ZipFile, names: set[str]) -> str:
    authors: dict[str, str] = {}
    if "ppt/commentAuthors.xml" in names:
        root = element_tree.fromstring(_ooxml_bytes(archive, "ppt/commentAuthors.xml"))
        authors = {_attr(node, "id"): _attr(node, "name", "unknown author") for node in root.iter() if _local(node) == "cmAuthor"}
    parts = sorted(name for name in names if re.fullmatch(r"ppt/comments/comment\d+\.xml", name))
    if len(parts) > MAX_OOXML_COMMENT_PARTS:
        raise DocumentError(f"Office document has more than {MAX_OOXML_COMMENT_PARTS} comment parts.")
    comments = []
    for part in parts:
        root = element_tree.fromstring(_ooxml_bytes(archive, part))
        for node in root.iter():
            if _local(node) == "cm":
                text = _comment_text(node)
                if text:
                    comments.append(f"PPTX comment by {authors.get(_attr(node, 'authorId'), 'unknown author')}: {text}")
    return "\n".join(comments)


def _xlsx_comments(archive: zipfile.ZipFile, names: set[str]) -> str:
    authors: list[str] = []
    legacy_parts = sorted(name for name in names if re.fullmatch(r"xl/comments\d+\.xml", name))
    threaded_parts = sorted(name for name in names if re.fullmatch(r"xl/threadedComments/threadedComment\d+\.xml", name))
    if len(legacy_parts) + len(threaded_parts) > MAX_OOXML_COMMENT_PARTS:
        raise DocumentError(f"Office document has more than {MAX_OOXML_COMMENT_PARTS} comment parts.")
    comments = []
    for part in legacy_parts:
        root = element_tree.fromstring(_ooxml_bytes(archive, part))
        authors = [node.text.strip() for node in root.iter() if _local(node) == "author" and node.text and node.text.strip()]
        for node in root.iter():
            if _local(node) == "comment":
                text = _comment_text(node)
                if text:
                    try:
                        author = authors[int(_attr(node, "authorId"))]
                    except (ValueError, IndexError):
                        author = "unknown author"
                    comments.append(f"XLSX comment {_attr(node, 'ref', '?')} by {author}: {text}")
    people: dict[str, str] = {}
    for part in sorted(name for name in names if re.fullmatch(r"xl/persons/person\d*\.xml", name)):
        root = element_tree.fromstring(_ooxml_bytes(archive, part))
        people.update({_attr(node, "id"): _attr(node, "displayName", _attr(node, "userId", "unknown author")) for node in root.iter() if _local(node) == "person"})
    for part in threaded_parts:
        root = element_tree.fromstring(_ooxml_bytes(archive, part))
        for node in root.iter():
            if _local(node) == "threadedComment":
                text = _comment_text(node)
                if text:
                    comments.append(f"XLSX threaded comment {_attr(node, 'ref', '?')} by {people.get(_attr(node, 'personId'), 'unknown author')}: {text}")
    return "\n".join(comments)


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



class _EvidenceHTMLParser(HTMLParser):
    """Collect human-readable HTML evidence without rendering or fetching URLs."""

    _HIDDEN = {"script", "style", "template", "noscript", "head"}
    _BREAKS = {"address", "article", "aside", "blockquote", "br", "div", "dt", "dd", "figcaption", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main", "p", "section", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.rows: list[list[str]] = []
        self._hidden_depth = 0
        self._title_depth = 0
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag in self._HIDDEN:
            self._hidden_depth += 1
        if tag == "title":
            self._title_depth += 1
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth and self._row is None and len(self.rows) < MAX_HTML_ROWS:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None and self._cell is None:
            self._cell = []
        if tag == "a" and len(self.links) < MAX_HTML_LINKS:
            href = attributes.get("href")
            if href:
                label = attributes.get("aria-label") or attributes.get("title")
                self.links.append(f"{href} ({label})" if label else href)
        if tag in self._BREAKS:
            self.text_parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None:
            cell = _html_clean(" ".join(self._cell))
            if cell and self._row is not None:
                self._row.append(cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag in self._HIDDEN and self._hidden_depth:
            self._hidden_depth -= 1
        if tag in self._BREAKS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self.title_parts.append(data)
        if not self._hidden_depth:
            self.text_parts.append(data)
            if self._cell is not None:
                self._cell.append(data)


def _html(path: Path) -> str:
    """Extract visible text and structural evidence from a local HTML attachment."""
    try:
        size = path.stat().st_size
        if size > MAX_HTML_BYTES:
            raise DocumentError(f"HTML attachment exceeds the {MAX_HTML_BYTES}-byte inspection limit.")
        data = path.read_bytes()
    except OSError as error:
        raise DocumentError(f"Could not read HTML attachment: {error}") from error
    if len(data) > MAX_HTML_BYTES:
        raise DocumentError(f"HTML attachment exceeds the {MAX_HTML_BYTES}-byte inspection limit.")
    parser = _EvidenceHTMLParser()
    try:
        parser.feed(data.decode("utf-8", errors="replace"))
        parser.close()
    except (ValueError, RuntimeError) as error:
        raise DocumentError(f"Could not parse HTML attachment: {error}") from error
    sections = []
    title = _html_clean(" ".join(parser.title_parts))
    text = _html_clean(" ".join(parser.text_parts))
    if title:
        sections.append(f"Title: {title}")
    if text:
        sections.append(f"Visible text:\n{text}")
    if parser.links:
        sections.append("Links:\n" + "\n".join(f"- {link}" for link in parser.links))
    if parser.rows:
        sections.append("Table rows:\n" + "\n".join(" | ".join(row) for row in parser.rows))
    if not sections:
        raise DocumentError("No readable text, links, or table rows were found in this HTML attachment.")
    return f"HTML extracted evidence ({path.name})\n---\n{_bounded('\n\n'.join(sections))}"


def _html_clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

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
