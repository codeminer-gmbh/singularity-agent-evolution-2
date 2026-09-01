"""Bounded, read-only inspection of RFC 5322 email and Unix mbox evidence."""

from __future__ import annotations

import html
import mailbox
from email import policy
from email.headerregistry import AddressHeader
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

MAX_SOURCE_BYTES = 25_000_000
MAX_MESSAGES = 500
MAX_TEXT_CHARACTERS = 24_000
MAX_ATTACHMENT_PREVIEW = 12_000


class EmailError(Exception):
    """An email evidence file cannot be safely inspected."""


class _HTMLText(HTMLParser):
    """Small dependency-free HTML fallback for HTML-only email bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []

    def handle_data(self, data: str) -> None:
        self.pieces.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.pieces.append("\n")

    def text(self) -> str:
        return "".join(self.pieces)


def inspect_email(path: Path, message_number: int = 1, attachment_number: int | None = None) -> str:
    """Render one message from an EML or mbox file without modifying it.

    Message and attachment numbers are one-based.  Normal inspection includes
    decoded headers, a readable body, and attachment metadata.  Selecting an
    attachment additionally previews its textual bytes, never writes it out.
    """
    if message_number < 1:
        raise EmailError("'message' must be at least 1.")
    if attachment_number is not None and attachment_number < 1:
        raise EmailError("'attachment' must be at least 1 when supplied.")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise EmailError(f"Cannot read email file: {error}.") from error
    if size > MAX_SOURCE_BYTES:
        raise EmailError(f"Email file is {size} bytes; limit is {MAX_SOURCE_BYTES} bytes.")

    if _is_mbox(path):
        email, total = _read_mbox(path, message_number)
        source = f"mbox message {message_number} of {total}"
    else:
        try:
            email = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        except (OSError, ValueError) as error:
            raise EmailError(f"Cannot parse EML file: {error}.") from error
        source = "EML message"
    return _render(email, source, attachment_number)


def _is_mbox(path: Path) -> bool:
    """Recognize conventional mbox extensions or its required separator line."""
    if path.suffix.lower() in {".mbox", ".mbx"}:
        return True
    try:
        with path.open("rb") as evidence:
            return evidence.readline().startswith(b"From ")
    except OSError as error:
        raise EmailError(f"Cannot read email file: {error}.") from error


def _read_mbox(path: Path, requested: int) -> tuple[Message, int]:
    """Read only a selected message from a bounded mbox index."""
    try:
        box = mailbox.mbox(path, create=False)
        keys = list(box.iterkeys())
        try:
            if len(keys) > MAX_MESSAGES:
                raise EmailError(f"Mailbox has more than {MAX_MESSAGES} messages; it is too large to inspect.")
            if not keys:
                raise EmailError("Mailbox contains no messages.")
            if requested > len(keys):
                raise EmailError(f"Mailbox has {len(keys)} messages; requested message {requested}.")
            return box.get_message(keys[requested - 1]), len(keys)
        finally:
            box.close()
    except EmailError:
        raise
    except (OSError, ValueError, mailbox.Error) as error:
        raise EmailError(f"Cannot parse mbox file: {error}.") from error


def _render(email: Message, source: str, wanted_attachment: int | None) -> str:
    lines = [source, "Headers:"]
    for name in ("From", "To", "Cc", "Reply-To", "Date", "Subject", "Message-ID"):
        value = email.get(name)
        if value:
            lines.append(f"{name}: {_header_text(value)}")

    body = _body(email)
    lines.extend(("", "Body:", _truncate(body, MAX_TEXT_CHARACTERS)))
    attachments = _attachments(email)
    if attachments:
        lines.extend(("", "Attachments:"))
        for number, part in enumerate(attachments, 1):
            filename = part.get_filename() or "(unnamed attachment)"
            content_type = part.get_content_type()
            decoded = _decoded_bytes(part)
            lines.append(f"{number}. {filename} — {content_type}, {len(decoded)} bytes")
        if wanted_attachment is not None:
            if wanted_attachment > len(attachments):
                raise EmailError(f"Message has {len(attachments)} attachments; requested attachment {wanted_attachment}.")
            part = attachments[wanted_attachment - 1]
            lines.extend(("", f"Attachment {wanted_attachment} preview:"))
            lines.append(_attachment_preview(part))
    elif wanted_attachment is not None:
        raise EmailError("Message has no attachments.")
    return "\n".join(lines)


def _header_text(value: object) -> str:
    # HeaderRegistry objects have a decoded str representation; unfold malicious
    # line breaks so evidence remains one header per output line.
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def _body(email: Message) -> str:
    plain: list[str] = []
    html_parts: list[str] = []
    for part in email.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() == "text/plain":
            plain.append(_part_text(part))
        elif part.get_content_type() == "text/html":
            parser = _HTMLText()
            parser.feed(_part_text(part))
            html_parts.append(html.unescape(parser.text()))
    text = "\n\n".join(plain or html_parts).strip()
    return text or "(No readable text body.)"


def _attachments(email: Message) -> list[Message]:
    return [part for part in email.walk() if not part.is_multipart() and (
        part.get_content_disposition() == "attachment" or part.get_filename() is not None
    )]


def _decoded_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw.encode("utf-8", "replace") if isinstance(raw, str) else b""
    return payload


def _part_text(part: Message) -> str:
    raw = _decoded_bytes(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _attachment_preview(part: Message) -> str:
    if part.get_content_maintype() != "text":
        return "(Binary attachment; metadata is listed but binary bytes are not rendered.)"
    return _truncate(_part_text(part), MAX_ATTACHMENT_PREVIEW)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated at {limit} characters]"
