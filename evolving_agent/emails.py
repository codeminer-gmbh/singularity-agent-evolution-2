"""Bounded, read-only inspection of RFC 5322, mbox, and Outlook MSG email evidence."""

from __future__ import annotations

import html
import mailbox
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

import extract_msg

MAX_SOURCE_BYTES = 25_000_000
MAX_MESSAGES = 500
MAX_TEXT_CHARACTERS = 24_000
MAX_ATTACHMENT_PREVIEW = 12_000
_MSG_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_TEXT_SUFFIXES = frozenset({".txt", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".log", ".md", ".ics", ".vcf"})


class EmailError(Exception):
    """An email evidence file cannot be safely inspected."""


@dataclass(frozen=True)
class _MsgAttachment:
    filename: str
    content_type: str
    data: bytes | None
    embedded_message: object | None = None


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
    """Render a selected EML/mbox message or one Outlook MSG message read-only.

    Message and attachment numbers are one-based. Normal inspection returns
    decoded headers, a readable body, and attachment metadata. Selecting an
    attachment previews textual bytes in memory and never extracts an artifact.
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

    if _is_outlook_msg(path):
        if message_number != 1:
            raise EmailError("An Outlook MSG file contains one message; requested message %d." % message_number)
        return _inspect_msg(path, attachment_number)
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


def extract_email_attachment(path: Path, message_number: int, attachment_number: int) -> bytes:
    """Return one decoded byte attachment for follow-on evidence inspection.

    The source is bounded exactly as normal email inspection is.  Embedded MSG
    objects have no stable raw byte representation and remain inspectable via
    :func:`inspect_email` rather than being silently serialized.
    """
    if message_number < 1 or attachment_number < 1:
        raise EmailError("'message' and 'attachment' must be at least 1.")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise EmailError(f"Cannot read email file: {error}.") from error
    if size > MAX_SOURCE_BYTES:
        raise EmailError(f"Email file is {size} bytes; limit is {MAX_SOURCE_BYTES} bytes.")
    if _is_outlook_msg(path):
        if message_number != 1:
            raise EmailError("An Outlook MSG file contains one message; requested message %d." % message_number)
        try:
            msg = extract_msg.Message(str(path))
        except (OSError, ValueError) as error:
            raise EmailError(f"Cannot parse Outlook MSG file: {error}.") from error
        try:
            attachments = [_msg_attachment(item) for item in getattr(msg, "attachments", ())]
            if attachment_number > len(attachments):
                raise EmailError(f"Message has {len(attachments)} attachments; requested attachment {attachment_number}.")
            data = attachments[attachment_number - 1].data
            if data is None:
                raise EmailError("The selected attachment is an embedded Outlook message and cannot be extracted as raw bytes.")
        finally:
            msg.close()
    else:
        if _is_mbox(path):
            email, _ = _read_mbox(path, message_number)
        else:
            try:
                email = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            except (OSError, ValueError) as error:
                raise EmailError(f"Cannot parse EML file: {error}.") from error
        attachments = _attachments(email)
        if attachment_number > len(attachments):
            raise EmailError(f"Message has {len(attachments)} attachments; requested attachment {attachment_number}.")
        data = _decoded_bytes(attachments[attachment_number - 1])
    if len(data) > MAX_SOURCE_BYTES:
        raise EmailError(f"Decoded attachment is {len(data)} bytes; limit is {MAX_SOURCE_BYTES} bytes.")
    return data


def _is_outlook_msg(path: Path) -> bool:
    if path.suffix.lower() == ".msg":
        return True
    try:
        with path.open("rb") as evidence:
            return evidence.read(len(_MSG_MAGIC)) == _MSG_MAGIC
    except OSError as error:
        raise EmailError(f"Cannot read email file: {error}.") from error


def _inspect_msg(path: Path, wanted_attachment: int | None) -> str:
    """Read MSG properties through extract-msg without invoking its save API."""
    try:
        message = extract_msg.Message(str(path))
    except (OSError, ValueError) as error:
        raise EmailError(f"Cannot parse Outlook MSG file: {error}.") from error
    try:
        headers = {
            "From": getattr(message, "sender", None),
            "To": getattr(message, "to", None),
            "Cc": getattr(message, "cc", None),
            "Reply-To": getattr(message, "replyTo", None),
            "Date": getattr(message, "date", None),
            "Subject": getattr(message, "subject", None),
            "Message-ID": getattr(message, "messageId", None),
        }
        body = getattr(message, "body", None) or _msg_html_body(message)
        attachments = [_msg_attachment(item) for item in getattr(message, "attachments", ())]
        return _render_msg(headers, str(body or ""), attachments, wanted_attachment)
    finally:
        message.close()


def _msg_html_body(message: object) -> str:
    raw = getattr(message, "htmlBody", None)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return ""
    parser = _HTMLText()
    parser.feed(raw)
    return html.unescape(parser.text())


def _msg_attachment(item: object) -> _MsgAttachment:
    filename = getattr(item, "longFilename", None) or getattr(item, "shortFilename", None) or "(unnamed attachment)"
    content_type = getattr(item, "mimetype", None) or "application/octet-stream"
    data = getattr(item, "data", None)
    # Embedded Outlook messages are represented as MSGFile objects, not bytes.
    embedded = data if data is not None and not isinstance(data, bytes) and hasattr(data, "attachments") else None
    return _MsgAttachment(str(filename), str(content_type), data if isinstance(data, bytes) else None, embedded)


def _render_msg(headers: dict[str, object], body: str, attachments: list[_MsgAttachment], wanted_attachment: int | None) -> str:
    lines = ["Outlook MSG message", "Headers:"]
    for name, value in headers.items():
        if value:
            lines.append(f"{name}: {_header_text(value)}")
    lines.extend(("", "Body:", _truncate(body.strip() or "(No readable text body.)", MAX_TEXT_CHARACTERS)))
    if attachments:
        lines.extend(("", "Attachments:"))
        for number, item in enumerate(attachments, 1):
            size = len(item.data) if item.data is not None else "unknown"
            lines.append(f"{number}. {item.filename} — {item.content_type}, {size} bytes")
        if wanted_attachment is not None:
            if wanted_attachment > len(attachments):
                raise EmailError(f"Message has {len(attachments)} attachments; requested attachment {wanted_attachment}.")
            item = attachments[wanted_attachment - 1]
            lines.extend(("", f"Attachment {wanted_attachment} preview:", _msg_attachment_preview(item)))
    elif wanted_attachment is not None:
        raise EmailError("Message has no attachments.")
    return "\n".join(lines)


def _msg_attachment_preview(item: _MsgAttachment) -> str:
    if item.embedded_message is not None:
        return _render_embedded_msg(item.embedded_message)
    if item.data is None or (not item.content_type.lower().startswith("text/") and Path(item.filename).suffix.lower() not in _TEXT_SUFFIXES):
        return "(Binary attachment; metadata is listed but binary bytes are not rendered.)"
    return _truncate(item.data.decode("utf-8", "replace"), MAX_ATTACHMENT_PREVIEW)


def _render_embedded_msg(message: object) -> str:
    """Render one forwarded MSG entirely in memory."""
    headers = {
        "From": getattr(message, "sender", None), "To": getattr(message, "to", None),
        "Cc": getattr(message, "cc", None), "Date": getattr(message, "date", None),
        "Subject": getattr(message, "subject", None), "Message-ID": getattr(message, "messageId", None),
    }
    body = getattr(message, "body", None) or _msg_html_body(message)
    attachments = [_msg_attachment(child) for child in getattr(message, "attachments", ())]
    rendered = _render_msg(headers, str(body or ""), attachments, None)
    return _truncate(rendered, MAX_ATTACHMENT_PREVIEW)


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
    lines.extend(("", "Body:", _truncate(_body(email), MAX_TEXT_CHARACTERS)))
    attachments = _attachments(email)
    if attachments:
        lines.extend(("", "Attachments:"))
        for number, part in enumerate(attachments, 1):
            filename = part.get_filename() or "(unnamed attachment)"
            lines.append(f"{number}. {filename} — {part.get_content_type()}, {len(_decoded_bytes(part))} bytes")
        if wanted_attachment is not None:
            if wanted_attachment > len(attachments):
                raise EmailError(f"Message has {len(attachments)} attachments; requested attachment {wanted_attachment}.")
            lines.extend(("", f"Attachment {wanted_attachment} preview:", _attachment_preview(attachments[wanted_attachment - 1])))
    elif wanted_attachment is not None:
        raise EmailError("Message has no attachments.")
    return "\n".join(lines)


def _header_text(value: object) -> str:
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
    return "\n\n".join(plain or html_parts).strip() or "(No readable text body.)"


def _attachments(email: Message) -> list[Message]:
    return [part for part in email.walk() if not part.is_multipart() and (part.get_content_disposition() == "attachment" or part.get_filename() is not None)]


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
