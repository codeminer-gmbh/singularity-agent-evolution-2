"""Bounded, non-executing inspection of PDF evidence.

PDFs can contain JavaScript, launch actions, embedded files, forms, and media.  This
module does none of those things: it asks :mod:`pypdf` only for document
information and the text drawing operators of a small number of pages.  The
result is JSON, rather than a rendered document, so a task can examine evidence
without starting a viewer or retrieving an attachment payload.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_MAX_PAGES = 25
_MAX_PAGE_CHARACTERS = 12_000
_MAX_METADATA_VALUE = 1_000
_MAX_OCR_PAGES = 5
_RENDER_TIMEOUT_SECONDS = 25
_OCR_TIMEOUT_SECONDS = 30
_RENDER_MAX_DIMENSION = 2_400


class PdfError(ValueError):
    """A PDF could not be safely inspected."""


def inspect_pdf(
    path: Path,
    *,
    page: int | None = None,
    max_pages: int = 5,
    max_characters: int = 8_000,
    ocr: bool = False,
) -> str:
    """Return metadata, extracted text, and optional bounded rendered-page OCR.

    ``page`` is one-based when supplied.  Encrypted PDFs are identified but not
    decrypted: this tool has no password parameter and never attempts to bypass
    document access controls.
    """
    if not isinstance(ocr, bool):
        raise PdfError("ocr must be true or false.")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= _MAX_PAGES:
        raise PdfError(f"max_pages must be between 1 and {_MAX_PAGES}.")
    if (not isinstance(max_characters, int) or isinstance(max_characters, bool)
            or not 1 <= max_characters <= _MAX_PAGE_CHARACTERS):
        raise PdfError(
            f"max_characters must be between 1 and {_MAX_PAGE_CHARACTERS}."
        )
    if page is not None and (not isinstance(page, int) or isinstance(page, bool) or page < 1):
        raise PdfError("page must be one or greater (PDF pages are one-based).")
    if not path.is_file():
        raise PdfError(f"{path.name!r} is not a readable PDF file.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PdfError(f"Could not inspect {path.name!r}: {exc}") from exc
    if size > _MAX_SOURCE_BYTES:
        raise PdfError(
            f"{path.name!r} is {size} bytes; PDF inspection is limited to "
            f"{_MAX_SOURCE_BYTES} bytes."
        )
    try:
        reader = PdfReader(path, strict=False)
        encrypted = bool(reader.is_encrypted)
        # Accessing the page tree can itself require decryption.
        page_count = None if encrypted else len(reader.pages)
    except (OSError, PdfReadError, ValueError, KeyError) as exc:
        raise PdfError(f"Could not parse PDF {path.name!r}: {exc}") from exc

    report: dict[str, Any] = {
        "format": "pdf",
        "size_bytes": size,
        "page_count": page_count,
        "encrypted": encrypted,
    }
    # Do not call decrypt, even with an empty password.  It changes what a
    # document reveals and makes the tool's behavior depend on a password.
    if encrypted:
        report["note"] = "Encrypted PDF: metadata and page text were not inspected."
        return json.dumps(report, ensure_ascii=False, indent=2)

    report["metadata"] = _metadata(reader)
    report["embedded_files"] = _embedded_files_metadata(reader)
    if page is not None:
        if page > page_count:
            raise PdfError(f"page {page} is outside this PDF ({page_count} pages).")
        indices = range(page - 1, page)
    else:
        indices = range(min(page_count, max_pages))
    page_indices = list(indices)
    pages: list[dict[str, Any]] = []
    for index in page_indices:
        try:
            text = reader.pages[index].extract_text(extraction_mode="layout") or ""
        except (PdfReadError, ValueError, KeyError, TypeError) as exc:
            pages.append({"page_number": index + 1, "error": f"Text extraction failed: {exc}"})
            continue
        pages.append(
            {
                "page_number": index + 1,
                "text_preview": text[:max_characters],
                "text_preview_truncated": len(text) > max_characters,
            }
        )
    if ocr:
        # Rendering is deliberately opt-in: native text extraction remains the
        # inexpensive default, while scans become useful evidence on request.
        ocr_indices = page_indices[:_MAX_OCR_PAGES]
        recognized = _ocr_rendered_pages(path, ocr_indices)
        for item in pages:
            if item["page_number"] in recognized:
                text = recognized[item["page_number"]]
                item["ocr_text_preview"] = text[:max_characters]
                item["ocr_text_truncated"] = len(text) > max_characters
        report["ocr_note"] = (
            "OCR renders pages locally at a bounded resolution with English "
            "Tesseract; only the first five selected pages are OCRed."
        )
        report["ocr_pages_truncated"] = len(page_indices) > len(ocr_indices)
    report["pages"] = pages
    report["pages_truncated"] = page is None and page_count > len(pages)
    report["note"] = (
        "Text is extracted from PDF content streams only; JavaScript, forms, "
        "attachments, links, and other active content are not opened."
    )
    return json.dumps(report, ensure_ascii=False, indent=2)


def _ocr_rendered_pages(path: Path, indices: list[int]) -> dict[int, str]:
    """Render selected pages into a private temporary directory and OCR them.

    Poppler and Tesseract are invoked with argument vectors, not a shell.  The
    rendered files never enter the workspace and are removed immediately.
    """
    recognized: dict[int, str] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="pdf-ocr-") as temporary:
            directory = Path(temporary)
            for index in indices:
                image_stem = directory / f"page-{index + 1}"
                _run_bounded(
                    [
                        "pdftoppm", "-f", str(index + 1), "-l", str(index + 1),
                        "-singlefile", "-png", "-r", "144", "-scale-to",
                        str(_RENDER_MAX_DIMENSION), str(path), str(image_stem),
                    ],
                    _RENDER_TIMEOUT_SECONDS,
                    "PDF rendering",
                )
                image = image_stem.with_suffix(".png")
                if not image.is_file():
                    raise PdfError("PDF rendering did not produce a page image.")
                recognized[index + 1] = _run_bounded(
                    ["tesseract", str(image), "stdout", "-l", "eng"],
                    _OCR_TIMEOUT_SECONDS,
                    "PDF OCR",
                ).strip()
    except OSError as exc:
        raise PdfError(f"PDF OCR could not start: {exc}") from exc
    return recognized


def _run_bounded(command: list[str], timeout: int, label: str) -> str:
    """Run one local conversion stage with a bounded error message."""
    try:
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise PdfError(f"{label} is unavailable: {command[0]} is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PdfError(f"{label} exceeded the {timeout}-second limit.") from exc
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PdfError(f"{label} failed: {detail[:500] or 'conversion tool returned an error.'}")
    return completed.stdout.decode("utf-8", errors="replace")

def _metadata(reader: PdfReader) -> dict[str, str]:
    """Return a short, predictable subset of document information fields."""
    try:
        info = reader.metadata
    except (PdfReadError, ValueError, KeyError):
        return {}
    if info is None:
        return {}
    fields = {
        "title": "/Title",
        "author": "/Author",
        "subject": "/Subject",
        "creator": "/Creator",
        "producer": "/Producer",
        "creation_date": "/CreationDate",
        "modification_date": "/ModDate",
    }
    result: dict[str, str] = {}
    for name, key in fields.items():
        value = info.get(key)
        if value is not None:
            result[name] = _bounded(str(value), _MAX_METADATA_VALUE)
    return result


def _bounded(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit] + "…"


def _embedded_files_metadata(reader: PdfReader) -> dict[str, Any]:
    """List bounded embedded-file *metadata* without retrieving any payload.

    PDF portfolios store file specifications below the catalog Names/EmbeddedFiles
    tree.  Walking the tree is useful evidence (a task can discover that an
    attachment exists) but deliberately never accesses ``/EF`` stream bytes.
    """
    result: dict[str, Any] = {"count": 0, "files": []}
    try:
        root = reader.trailer["/Root"]
        names = root.get("/Names")
        embedded = names.get("/EmbeddedFiles") if names else None
    except (PdfReadError, ValueError, KeyError, TypeError):
        return result
    if not embedded:
        return result

    files: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(node: Any) -> None:
        # Both the number of name-tree nodes and reported files are bounded:
        # crafted PDFs must not turn a metadata request into an unbounded walk.
        if not node or len(seen) >= 1_000 or len(files) >= 50:
            return
        marker = id(node)
        if marker in seen:
            return
        seen.add(marker)
        try:
            entries = node.get("/Names", [])
            for offset in range(0, len(entries) - 1, 2):
                if len(files) >= 50:
                    return
                display_name, specification = entries[offset], entries[offset + 1]
                item: dict[str, Any] = {"name": _bounded(str(display_name), 1_000)}
                if specification:
                    description = specification.get("/Desc")
                    if description is not None:
                        item["description"] = _bounded(str(description), 1_000)
                    # /Params belongs to the embedded-file stream below /EF,
                    # not ordinarily to the file specification itself.  Reading
                    # its dictionary does not call get_data() on the stream.
                    files_dict = specification.get("/EF")
                    stream = files_dict.get("/F") if files_dict else None
                    params = stream.get("/Params") if stream else None
                    if params and params.get("/Size") is not None:
                        try:
                            item["declared_size_bytes"] = int(params.get("/Size"))
                        except (TypeError, ValueError):
                            pass
                files.append(item)
            for child in node.get("/Kids", []):
                visit(child)
        except (PdfReadError, ValueError, KeyError, TypeError, AttributeError):
            return

    visit(embedded)
    result["count"] = len(files)
    result["files"] = files
    if len(files) >= 50:
        result["truncated"] = True
    return result
