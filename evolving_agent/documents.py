"""Bounded text extraction for common office documents supplied to a task."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import tempfile
from typing import Callable, Iterable
from urllib.parse import unquote
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from docx import Document
from openpyxl import load_workbook
from PIL import Image, ImageSequence
from pypdf import PdfReader
from pptx import Presentation

MAX_DOCUMENT_BYTES = 48 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_OCR_PAGES = 30
MAX_OCR_PIXELS_PER_PAGE = 20_000_000
MAX_OCR_OUTPUT_BYTES = 1_000_000
_OCR_TIMEOUT_SECONDS = 45
MAX_SLIDES = 200
MAX_PRESENTATION_SHAPES = 10_000
MAX_PRESENTATION_MEMBERS = 10_000
MAX_PRESENTATION_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_SHEETS = 40
MAX_ROWS_PER_SHEET = 5_000
MAX_COLUMNS_PER_SHEET = 100
MAX_TEXT_CHARACTERS = 120_000
MAX_EBOOK_MEMBERS = 10_000
MAX_EBOOK_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_EBOOK_CHAPTERS = 1_000
MAX_EBOOK_CHAPTER_BYTES = 8 * 1024 * 1024
MAX_ODF_MEMBERS = 10_000
MAX_ODF_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ODF_CONTENT_BYTES = 32 * 1024 * 1024
MAX_EMAIL_PARTS = 200
MAX_EMAIL_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_EMAIL_DOCUMENT_ATTACHMENTS = 20
# Speech recognition is intentionally bounded more tightly than document parsing:
# decoding compressed media and acoustic inference both consume CPU proportional
# to duration, not just the on-disk input size.
MAX_AUDIO_DURATION_SECONDS = 15 * 60
MAX_AUDIO_PCM_BYTES = MAX_AUDIO_DURATION_SECONDS * 16_000 * 2
_AUDIO_CONVERT_TIMEOUT_SECONDS = 90
_AUDIO_CHUNK_BYTES = 64 * 1024
_AUDIO_SUFFIXES = frozenset({
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
})


class DocumentError(Exception):
    """A document cannot be safely or usefully read."""


def read_document(path: Path, *, max_characters: int = MAX_TEXT_CHARACTERS) -> str:
    """Extract readable text from a PDF, office documents, email, or an image.

    Scanned PDF pages and PNG, JPEG, TIFF, WebP, and BMP images are read with
    Tesseract OCR. OCR has deliberately tighter page and pixel limits than
    native extraction because raster recognition is computationally expensive.

    The returned text is deliberately bounded: office files are untrusted input
    and the caller is a language model with a finite context window.
    """
    if not path.is_file():
        raise DocumentError(f"{path.name!r} is not a document file.")
    size = path.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise DocumentError(
            f"{path.name!r} exceeds the {MAX_DOCUMENT_BYTES}-byte document limit."
        )
    if not isinstance(max_characters, int) or not 1 <= max_characters <= MAX_TEXT_CHARACTERS:
        raise DocumentError(
            f"max_characters must be an integer from 1 through {MAX_TEXT_CHARACTERS}."
        )
    suffix = path.suffix.lower()
    extractors: dict[str, Callable[[Path], str]] = {
        ".pdf": _pdf_text,
        ".docx": _docx_text,
        ".xlsx": _xlsx_text,
        ".pptx": _pptx_text,
        ".epub": _epub_text,
        ".odt": _odf_text,
        ".ods": _odf_text,
        ".odp": _odf_text,
        ".eml": _eml_text,
        ".msg": _msg_text,
        ".png": _image_text,
        ".jpg": _image_text,
        ".jpeg": _image_text,
        ".tif": _image_text,
        ".tiff": _image_text,
        ".webp": _image_text,
        ".bmp": _image_text,
        **{suffix: _audio_text for suffix in _AUDIO_SUFFIXES},
    }
    extractor = extractors.get(suffix)
    if extractor is None:
        raise DocumentError(
            f"Unsupported document type {suffix or '(no extension)'!r}. "
            "Supported types are PDF (.pdf), Word (.docx), Excel (.xlsx), PowerPoint (.pptx), OpenDocument Text/Spreadsheet/Presentation (.odt, .ods, .odp), EPUB (.epub), RFC 822 email (.eml), Outlook email (.msg), PNG/JPEG/TIFF/WebP/BMP images, and common audio or video files (including WAV, MP3, M4A, FLAC, OGG, MP4, MOV, MKV, and WebM)."
        )
    try:
        text = extractor(path)
    except DocumentError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise DocumentError(f"Could not read {path.name!r}: {error}") from error
    if len(text) > max_characters:
        return f"{text[:max_characters]}\n... [truncated at {max_characters} characters]"
    return text or "[The document contains no extractable text.]"



def _decode_email_bytes(part: object) -> str:
    """Decode one MIME text part without trusting its claimed charset."""
    payload = part.get_payload(decode=True)
    if payload is None:
        payload = b""
    if len(payload) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise DocumentError(f"Email part exceeds the {MAX_EMAIL_ATTACHMENT_BYTES}-byte limit.")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        return payload.decode("utf-8", errors="replace")


def _email_html_text(value: str) -> str:
    parser = _EpubTextParser()
    parser.feed(value)
    parser.close()
    return parser.text()


def _email_attachment_text(data: bytes, filename: str) -> str:
    """Read a bounded, supported document attached to an email."""
    suffix = Path(filename).suffix.lower()
    extractor = {
        ".pdf": _pdf_text, ".docx": _docx_text, ".xlsx": _xlsx_text,
        ".pptx": _pptx_text, ".epub": _epub_text, ".odt": _odf_text, ".ods": _odf_text, ".odp": _odf_text, ".png": _image_text,
        ".jpg": _image_text, ".jpeg": _image_text, ".tif": _image_text,
        ".tiff": _image_text, ".webp": _image_text, ".bmp": _image_text,
        **{extension: _audio_text for extension in _AUDIO_SUFFIXES},
    }.get(suffix)
    if extractor is None:
        return ""
    if len(data) > MAX_EMAIL_ATTACHMENT_BYTES:
        return f"[Attachment omitted: exceeds {MAX_EMAIL_ATTACHMENT_BYTES}-byte limit.]"
    # The generated filename is never derived from untrusted attachment names.
    with tempfile.TemporaryDirectory(prefix="evolving-agent-email-") as temporary:
        attached = Path(temporary) / f"attachment{suffix}"
        attached.write_bytes(data)
        try:
            return extractor(attached)
        except DocumentError as error:
            return f"[Attachment could not be read: {error}]"


def _eml_text(path: Path) -> str:
    """Extract headers, readable bodies, and supported attachments from RFC 822 mail."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    except Exception as error:
        raise DocumentError(f"Could not parse email {path.name!r}: {error}") from error
    chunks = ["--- Email ---"]
    for header in ("From", "To", "Cc", "Bcc", "Date", "Subject"):
        value = str(message.get(header, "")).strip()
        if value:
            chunks.append(f"{header}: {value}")
    document_attachments = 0
    for number, part in enumerate(message.walk(), start=1):
        if number > MAX_EMAIL_PARTS:
            raise DocumentError(f"Email has more than {MAX_EMAIL_PARTS} MIME parts.")
        if part.is_multipart():
            continue
        content_type = part.get_content_type().lower()
        filename = part.get_filename() or ""
        disposition = (part.get_content_disposition() or "").lower()
        if content_type.startswith("text/") and not (filename or disposition == "attachment"):
            text = _decode_email_bytes(part)
            if content_type == "text/html":
                text = _email_html_text(text)
            if text.strip():
                chunks.append(f"--- Body part {number} ({content_type}) ---\n{text.strip()}")
            continue
        label = filename or f"part-{number}"
        chunks.append(f"--- Attachment: {label} ({content_type}) ---")
        data = part.get_payload(decode=True) or b""
        if len(data) > MAX_EMAIL_ATTACHMENT_BYTES:
            chunks.append(f"[Attachment omitted: exceeds {MAX_EMAIL_ATTACHMENT_BYTES}-byte limit.]")
            continue
        if document_attachments < MAX_EMAIL_DOCUMENT_ATTACHMENTS:
            extracted = _email_attachment_text(data, filename)
            if extracted:
                document_attachments += 1
                chunks.append(extracted)
        elif Path(filename).suffix.lower() in {".pdf", ".docx", ".xlsx", ".pptx", ".epub", ".odt", ".ods", ".odp", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", *_AUDIO_SUFFIXES}:
            chunks.append(f"[Document attachments stopped at {MAX_EMAIL_DOCUMENT_ATTACHMENTS}.]")
    return "\n".join(chunks)


def _msg_text(path: Path) -> str:
    """Extract Outlook MSG metadata, body, and supported binary attachments."""
    try:
        import extract_msg
        message = extract_msg.Message(str(path))
    except Exception as error:
        raise DocumentError(f"Could not parse Outlook email {path.name!r}: {error}") from error
    try:
        chunks = ["--- Outlook Email ---"]
        for label, value in (("From", message.sender), ("To", message.to), ("Cc", message.cc), ("Date", message.date), ("Subject", message.subject)):
            if value:
                chunks.append(f"{label}: {value}")
        body = message.body or ""
        if body.strip():
            chunks.append(f"--- Body ---\n{body.strip()}")
        for number, attachment in enumerate(message.attachments, start=1):
            if number > MAX_EMAIL_PARTS:
                raise DocumentError(f"Outlook email has more than {MAX_EMAIL_PARTS} attachments.")
            filename = attachment.getFilename() or f"attachment-{number}"
            chunks.append(f"--- Attachment: {filename} ({getattr(attachment, 'mimetype', None) or 'unknown'}) ---")
            data = attachment.data
            if not isinstance(data, bytes):
                chunks.append("[Attachment is not a binary file and was not extracted.]")
                continue
            if len(data) > MAX_EMAIL_ATTACHMENT_BYTES:
                chunks.append(f"[Attachment omitted: exceeds {MAX_EMAIL_ATTACHMENT_BYTES}-byte limit.]")
                continue
            extracted = _email_attachment_text(data, filename)
            if extracted:
                chunks.append(extracted)
        return "\n".join(chunks)
    finally:
        message.close()

def _pdf_text(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
    except Exception as error:  # parser-specific errors vary by pypdf release
        raise DocumentError(f"Could not parse PDF {path.name!r}: {error}") from error
    if reader.is_encrypted:
        raise DocumentError(f"PDF {path.name!r} is encrypted and cannot be read without a password.")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise DocumentError(f"PDF has {len(reader.pages)} pages; limit is {MAX_PDF_PAGES}.")
    chunks: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as error:
            raise DocumentError(f"Could not extract page {number} of PDF: {error}") from error
        if not text.strip():
            text = _ocr_pdf_page(path, number)
        chunks.append(f"--- Page {number} ---\n{text.strip()}")
    return "\n\n".join(chunks)


def _ocr_pdf_page(path: Path, number: int) -> str:
    """Rasterize one textless PDF page only when native extraction failed."""
    if number > MAX_OCR_PAGES:
        return f"[OCR skipped: page {number} exceeds the {MAX_OCR_PAGES}-page OCR limit.]"
    try:
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(str(path))
        page = document[number - 1]
        image = page.render(scale=2).to_pil()
        try:
            return _ocr_image(image, f"PDF page {number}")
        finally:
            image.close()
            page.close()
            document.close()
    except DocumentError:
        raise
    except Exception as error:
        raise DocumentError(f"Could not rasterize page {number} for OCR: {error}") from error


def _image_text(path: Path) -> str:
    try:
        with Image.open(path) as opened:
            chunks: list[str] = []
            for number, frame in enumerate(ImageSequence.Iterator(opened), start=1):
                if number > MAX_OCR_PAGES:
                    chunks.append(f"[OCR stopped at {MAX_OCR_PAGES} image frames.]")
                    break
                chunks.append(f"--- Image {number} ---\n{_ocr_image(frame.copy(), f'image frame {number}').strip()}")
            return "\n\n".join(chunks)
    except DocumentError:
        raise
    except (OSError, ValueError) as error:
        raise DocumentError(f"Could not open image {path.name!r}: {error}") from error


def _ocr_image(image: Image.Image, label: str) -> str:
    """Recognize a bounded raster using Tesseract without shell interpolation."""
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_OCR_PIXELS_PER_PAGE:
        raise DocumentError(f"{label} has {width * height} pixels; OCR limit is {MAX_OCR_PIXELS_PER_PAGE}.")
    with tempfile.TemporaryDirectory(prefix="evolving-agent-ocr-") as temporary:
        source = Path(temporary) / "image.png"
        output_base = Path(temporary) / "recognized"
        image.convert("RGB").save(source, format="PNG")
        try:
            completed = subprocess.run(
                ["tesseract", str(source), str(output_base), "-l", "eng", "--psm", "3"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=_OCR_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as error:
            raise DocumentError("OCR engine is unavailable in this image.") from error
        except subprocess.TimeoutExpired as error:
            raise DocumentError(f"OCR of {label} exceeded {_OCR_TIMEOUT_SECONDS} seconds.") from error
        if completed.returncode:
            detail = completed.stderr.strip()[:500]
            raise DocumentError(f"OCR of {label} failed: {detail or 'Tesseract returned an error.'}")
        result = output_base.with_suffix(".txt")
        if not result.is_file():
            raise DocumentError(f"OCR of {label} produced no text output.")
        if result.stat().st_size > MAX_OCR_OUTPUT_BYTES:
            raise DocumentError(f"OCR output for {label} exceeds the {MAX_OCR_OUTPUT_BYTES}-byte limit.")
        return result.read_text(encoding="utf-8", errors="replace")


def _audio_text(path: Path) -> str:
    """Transcribe the first audio stream with PocketSphinx, via ffmpeg.

    ffmpeg gives one carefully constrained decoder path for both audio files and
    movie containers.  PocketSphinx ships a compact English acoustic/language
    model, avoiding a network download or a cloud credential while a task is
    running.  Its output is best-effort recognition, not a verbatim guarantee.
    """
    duration = _media_duration(path)
    if duration <= 0:
        raise DocumentError(f"Could not determine a positive duration for {path.name!r}.")
    if duration > MAX_AUDIO_DURATION_SECONDS:
        raise DocumentError(
            f"{path.name!r} is {duration:.1f} seconds long; audio limit is "
            f"{MAX_AUDIO_DURATION_SECONDS} seconds."
        )
    with tempfile.TemporaryDirectory(prefix="evolving-agent-audio-") as temporary:
        pcm = Path(temporary) / "audio.s16le"
        _decode_media_to_pcm(path, pcm)
        transcript = _recognize_pcm(pcm)
    return f"--- Audio transcript ({duration:.1f} seconds; English, offline recognition) ---\n{transcript}"


def _media_duration(path: Path) -> float:
    """Ask ffprobe for duration without accepting arbitrary probe output."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
    except FileNotFoundError as error:
        raise DocumentError("Audio support is unavailable because ffprobe is not installed.") from error
    except subprocess.TimeoutExpired as error:
        raise DocumentError(f"Timed out reading media metadata for {path.name!r}.") from error
    try:
        duration = float(result.stdout.decode("ascii", errors="strict").strip())
    except (UnicodeError, ValueError) as error:
        raise DocumentError(f"Could not read media duration for {path.name!r}.") from error
    if result.returncode or duration == float("inf") or duration != duration:
        raise DocumentError(f"Could not inspect audio in {path.name!r}.")
    return duration


def _decode_media_to_pcm(source: Path, destination: Path) -> None:
    """Decode only one mono, 16 kHz stream, with output and wall-clock bounds."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-y", str(destination)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=_AUDIO_CONVERT_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError as error:
        raise DocumentError("Audio support is unavailable because ffmpeg is not installed.") from error
    except subprocess.TimeoutExpired as error:
        raise DocumentError(f"Timed out decoding audio from {source.name!r}.") from error
    if result.returncode or not destination.is_file():
        detail = result.stderr.decode("utf-8", errors="replace").strip().replace("\n", " ")[:300]
        raise DocumentError(f"Could not decode an audio stream from {source.name!r}{': ' + detail if detail else ''}")
    if destination.stat().st_size > MAX_AUDIO_PCM_BYTES:
        raise DocumentError(f"Decoded audio from {source.name!r} exceeds the audio duration limit.")


def _recognize_pcm(path: Path) -> str:
    """Run the bundled PocketSphinx English model on bounded signed PCM."""
    try:
        from pocketsphinx import Decoder, get_model_path
    except ImportError as error:
        raise DocumentError("Audio support is unavailable because PocketSphinx is not installed.") from error
    try:
        model_root = Path(get_model_path()) / "en-us"
        config = Decoder.default_config()
        config.set_string("-hmm", str(model_root / "en-us"))
        config.set_string("-lm", str(model_root / "en-us.lm.bin"))
        config.set_string("-dict", str(model_root / "cmudict-en-us.dict"))
        decoder = Decoder(config)
        decoder.start_utt()
        with path.open("rb") as stream:
            while chunk := stream.read(_AUDIO_CHUNK_BYTES):
                decoder.process_raw(chunk, False, False)
        decoder.end_utt()
        hypothesis = decoder.hyp()
    except Exception as error:
        raise DocumentError(f"Could not recognize audio: {error}") from error
    if hypothesis is None or not hypothesis.hypstr.strip():
        return "[No intelligible speech was recognized.]"
    return hypothesis.hypstr.strip()


def _docx_text(path: Path) -> str:
    try:
        document = Document(str(path))
    except Exception as error:
        raise DocumentError(f"Could not parse DOCX {path.name!r}: {error}") from error
    chunks = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    for table_number, table in enumerate(document.tables, start=1):
        chunks.append(f"--- Table {table_number} ---")
        for row in table.rows:
            chunks.append("\t".join(cell.text.replace("\n", " ") for cell in row.cells))
    return "\n".join(chunks)


def _xlsx_text(path: Path) -> str:
    try:
        workbook = load_workbook(str(path), read_only=True, data_only=False)
    except Exception as error:
        raise DocumentError(f"Could not parse XLSX {path.name!r}: {error}") from error
    try:
        if len(workbook.worksheets) > MAX_SHEETS:
            raise DocumentError(f"Workbook has {len(workbook.worksheets)} sheets; limit is {MAX_SHEETS}.")
        chunks: list[str] = []
        for sheet in workbook.worksheets:
            chunks.append(f"--- Sheet: {sheet.title} ---")
            row_count = 0
            for row in sheet.iter_rows(max_col=MAX_COLUMNS_PER_SHEET, values_only=True):
                row_count += 1
                if row_count > MAX_ROWS_PER_SHEET:
                    chunks.append(f"... [sheet truncated at {MAX_ROWS_PER_SHEET} rows]")
                    break
                values = ["" if value is None else str(value) for value in row]
                # Preserve interior empty cells but omit uninformative trailing cells.
                while values and not values[-1]:
                    values.pop()
                if values:
                    chunks.append("\t".join(values))
        return "\n".join(chunks)
    finally:
        workbook.close()


def _pptx_text(path: Path) -> str:
    """Extract slide text, tables, and presenter notes in slide order.

    PPTX is a ZIP container. Check its declared expansion before handing it to
    python-pptx so a small hostile input cannot make the reader allocate an
    unbounded presentation.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            expanded = sum(info.file_size for info in infos)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise DocumentError(f"Could not parse PPTX {path.name!r}: {error}") from error
    if len(infos) > MAX_PRESENTATION_MEMBERS:
        raise DocumentError(
            f"PowerPoint has {len(infos)} package members; limit is {MAX_PRESENTATION_MEMBERS}."
        )
    if expanded > MAX_PRESENTATION_UNCOMPRESSED_BYTES:
        raise DocumentError(
            "PowerPoint expands to "
            f"{expanded} bytes; limit is {MAX_PRESENTATION_UNCOMPRESSED_BYTES}."
        )
    try:
        presentation = Presentation(str(path))
    except Exception as error:
        raise DocumentError(f"Could not parse PPTX {path.name!r}: {error}") from error
    if len(presentation.slides) > MAX_SLIDES:
        raise DocumentError(
            f"PowerPoint has {len(presentation.slides)} slides; limit is {MAX_SLIDES}."
        )

    chunks: list[str] = []
    shape_count = 0
    for number, slide in enumerate(presentation.slides, start=1):
        chunks.append(f"--- Slide {number} ---")
        for shape in _walk_shapes(slide.shapes):
            shape_count += 1
            if shape_count > MAX_PRESENTATION_SHAPES:
                raise DocumentError(
                    "PowerPoint has more than "
                    f"{MAX_PRESENTATION_SHAPES} shapes; limit is {MAX_PRESENTATION_SHAPES}."
                )
            if getattr(shape, "has_table", False):
                chunks.append("--- Table ---")
                for row in shape.table.rows:
                    chunks.append("\t".join(cell.text.replace("\n", " ") for cell in row.cells))
            elif getattr(shape, "has_text_frame", False):
                text = shape.text.strip()
                if text:
                    chunks.append(text)
        # python-pptx exposes a notes text frame when a notes slide exists.
        # It can be absent in malformed or minimal packages, so treat it as
        # optional rather than making slide text unreadable.
        try:
            notes_frame = slide.notes_slide.notes_text_frame
            notes = notes_frame.text.strip() if notes_frame is not None else ""
        except Exception:
            notes = ""
        if notes:
            chunks.append(f"--- Speaker Notes ---\n{notes}")
    return "\n".join(chunks)


def _walk_shapes(shapes: Iterable[object]) -> Iterable[object]:
    """Yield shapes in visual tree order, including shapes inside groups."""
    for shape in shapes:
        yield shape
        # GroupShape is iterable; normal shapes are not. Avoid relying on an
        # implementation-specific type so this remains compatible with
        # python-pptx releases.
        if getattr(shape, "shape_type", None) == 6:  # MSO_SHAPE_TYPE.GROUP
            yield from _walk_shapes(shape)


class _EpubTextParser(HTMLParser):
    """Collect visible text from tolerant XHTML/HTML EPUB chapters."""

    _BLOCK_TAGS = frozenset({"address", "article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section", "tr"})
    _SKIP_TAGS = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skipping += 1
        if tag in self._BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skipping:
            self._skipping -= 1
        if tag in self._BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts).strip()


_ODF_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_ODF_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_ODF_DRAW_NS = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
_ODF_OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def _odf_paragraphs(node: ET.Element) -> list[str]:
    """Return visible paragraph/headline text below one ODF element."""
    paragraphs: list[str] = []
    for element in node.iter():
        if element.tag in {f"{{{_ODF_TEXT_NS}}}p", f"{{{_ODF_TEXT_NS}}}h"}:
            value = "".join(element.itertext()).strip()
            if value:
                paragraphs.append(value)
    return paragraphs


def _odf_word_text(root: ET.Element) -> str:
    body = root.find(f".//{{{_ODF_OFFICE_NS}}}text")
    if body is None:
        raise DocumentError("OpenDocument text file has no office:text body.")
    return "\n".join(_odf_paragraphs(body))


def _odf_spreadsheet_text(root: ET.Element) -> str:
    body = root.find(f".//{{{_ODF_OFFICE_NS}}}spreadsheet")
    if body is None:
        raise DocumentError("OpenDocument spreadsheet has no office:spreadsheet body.")
    chunks: list[str] = []
    tables = list(body.iter(f"{{{_ODF_TABLE_NS}}}table"))
    if len(tables) > MAX_SHEETS:
        raise DocumentError(f"OpenDocument spreadsheet has {len(tables)} sheets; limit is {MAX_SHEETS}.")
    for table in tables:
        name = table.get(f"{{{_ODF_TABLE_NS}}}name", "Untitled")
        chunks.append(f"--- Sheet: {name} ---")
        emitted_rows = 0
        for row in table:
            if row.tag != f"{{{_ODF_TABLE_NS}}}table-row":
                continue
            repetitions = _odf_repeat(row, "number-rows-repeated")
            for _ in range(min(repetitions, MAX_ROWS_PER_SHEET - emitted_rows)):
                values: list[str] = []
                for cell in row:
                    if cell.tag not in {f"{{{_ODF_TABLE_NS}}}table-cell", f"{{{_ODF_TABLE_NS}}}covered-table-cell"}:
                        continue
                    value = " ".join(_odf_paragraphs(cell))
                    if not value:
                        value = cell.get(f"{{{_ODF_OFFICE_NS}}}value", "")
                    repeat = min(_odf_repeat(cell, "number-columns-repeated"), MAX_COLUMNS_PER_SHEET - len(values))
                    values.extend([value] * repeat)
                    if len(values) >= MAX_COLUMNS_PER_SHEET:
                        break
                while values and not values[-1]:
                    values.pop()
                if values:
                    chunks.append("\t".join(values))
                emitted_rows += 1
            if emitted_rows >= MAX_ROWS_PER_SHEET:
                chunks.append(f"... [sheet truncated at {MAX_ROWS_PER_SHEET} rows]")
                break
    return "\n".join(chunks)


def _odf_repeat(element: ET.Element, name: str) -> int:
    """Read a non-negative ODF repeat attribute without expanding hostile counts."""
    try:
        return max(1, int(element.get(f"{{{_ODF_TABLE_NS}}}{name}", "1")))
    except ValueError:
        return 1


def _odf_presentation_text(root: ET.Element) -> str:
    body = root.find(f".//{{{_ODF_OFFICE_NS}}}presentation")
    if body is None:
        raise DocumentError("OpenDocument presentation has no office:presentation body.")
    pages = list(body.iter(f"{{{_ODF_DRAW_NS}}}page"))
    if len(pages) > MAX_SLIDES:
        raise DocumentError(f"OpenDocument presentation has {len(pages)} slides; limit is {MAX_SLIDES}.")
    chunks: list[str] = []
    for number, page in enumerate(pages, 1):
        chunks.append(f"--- Slide {number} ---")
        chunks.extend(_odf_paragraphs(page))
    return "\n".join(chunks)


def _odf_text(path: Path) -> str:
    """Read ODT, ODS, or ODP ``content.xml`` with ZIP and logical-size bounds."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ODF_MEMBERS:
                raise DocumentError(f"OpenDocument has {len(infos)} package members; limit is {MAX_ODF_MEMBERS}.")
            if sum(info.file_size for info in infos) > MAX_ODF_UNCOMPRESSED_BYTES:
                raise DocumentError(f"OpenDocument expands beyond the {MAX_ODF_UNCOMPRESSED_BYTES}-byte limit.")
            try:
                content = archive.getinfo("content.xml")
            except KeyError as error:
                raise DocumentError("OpenDocument package has no content.xml.") from error
            if content.file_size > MAX_ODF_CONTENT_BYTES:
                raise DocumentError(f"OpenDocument content.xml exceeds the {MAX_ODF_CONTENT_BYTES}-byte limit.")
            root = ET.fromstring(archive.read(content))
    except DocumentError:
        raise
    except (ET.ParseError, OSError, ValueError, zipfile.BadZipFile) as error:
        raise DocumentError(f"Could not parse OpenDocument {path.name!r}: {error}") from error
    extractors = {".odt": _odf_word_text, ".ods": _odf_spreadsheet_text, ".odp": _odf_presentation_text}
    return extractors[path.suffix.lower()](root)


def _epub_member_path(base: str, href: str) -> str:
    """Resolve an EPUB package-relative href without touching the filesystem."""
    candidate = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(href)))
    if candidate.startswith("../") or candidate == ".." or candidate.startswith("/"):
        raise DocumentError("EPUB package contains an invalid chapter path.")
    return candidate


def _epub_text(path: Path) -> str:
    """Read EPUB XHTML in the publication spine order, under ZIP bounds."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_EBOOK_MEMBERS:
                raise DocumentError(f"EPUB has {len(infos)} package members; limit is {MAX_EBOOK_MEMBERS}.")
            expanded = sum(info.file_size for info in infos)
            if expanded > MAX_EBOOK_UNCOMPRESSED_BYTES:
                raise DocumentError(f"EPUB expands to {expanded} bytes; limit is {MAX_EBOOK_UNCOMPRESSED_BYTES}.")
            names = {info.filename for info in infos}
            if "META-INF/container.xml" not in names:
                raise DocumentError("EPUB has no META-INF/container.xml package descriptor.")
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next((node.get("full-path") for node in container.iter() if node.tag.endswith("rootfile") and node.get("full-path")), None)
            if not rootfile or rootfile not in names:
                raise DocumentError("EPUB package descriptor does not name a readable OPF file.")
            package = ET.fromstring(archive.read(rootfile))
            manifest = {node.get("id"): node.get("href") for node in package.iter() if node.tag.endswith("item") and node.get("id") and node.get("href")}
            spine = [node.get("idref") for node in package.iter() if node.tag.endswith("itemref") and node.get("idref")]
            if not spine:
                raise DocumentError("EPUB package has no reading-order spine.")
            if len(spine) > MAX_EBOOK_CHAPTERS:
                raise DocumentError(f"EPUB has {len(spine)} spine chapters; limit is {MAX_EBOOK_CHAPTERS}.")
            chunks: list[str] = []
            for number, item_id in enumerate(spine, 1):
                href = manifest.get(item_id)
                if not href:
                    continue
                member = _epub_member_path(rootfile, href)
                if member not in names:
                    continue
                info = archive.getinfo(member)
                if info.file_size > MAX_EBOOK_CHAPTER_BYTES:
                    raise DocumentError(f"EPUB chapter {number} exceeds the {MAX_EBOOK_CHAPTER_BYTES}-byte limit.")
                parser = _EpubTextParser()
                parser.feed(archive.read(member).decode("utf-8", errors="replace"))
                parser.close()
                text = parser.text()
                if text:
                    chunks.append(f"--- Chapter {number} ---\n{text}")
            return "\n\n".join(chunks)
    except DocumentError:
        raise
    except (ET.ParseError, OSError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
        raise DocumentError(f"Could not parse EPUB {path.name!r}: {error}") from error
