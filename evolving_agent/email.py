"""Bounded, read-only inspection of RFC 5322 and Outlook email evidence."""

from __future__ import annotations

from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any

MAX_EMAIL_BYTES = 8_000_000
MAX_ATTACHMENTS = 100
MAX_ATTACHMENT_BYTES = 256_000
MAX_TEXT_CHARACTERS = 24_000


class EmailError(Exception):
    """An email is unsupported, malformed, or cannot be inspected."""


def inspect_email(path: Path, attachment: str | None = None) -> str:
    """Return bounded headers, readable body, or one attachment preview.

    Parsing never creates files.  EML is handled by Python's standards-based
    parser; Outlook MSG is read with ``extract-msg`` when that optional format
    is requested.  Attachments are identified by their zero-based index or
    displayed filename, avoiding any path interpretation of their names.
    """
    suffix = path.suffix.lower()
    if suffix in {".eml", ".emlx"} or _looks_like_email(path):
        return _eml(path, attachment)
    if suffix == ".msg":
        return _msg(path, attachment)
    raise EmailError("Supported email formats are EML/EMLX and Outlook MSG.")


def _eml(path: Path, requested: str | None) -> str:
    data = _read_limited(path)
    if path.suffix.lower() == ".emlx":
        data = _emlx_message_bytes(data)
    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
    except (OSError, ValueError) as error:
        raise EmailError(f"Could not parse EML message: {error}") from error
    attachments = _eml_attachments(message)
    if requested is not None:
        item = _find_attachment(attachments, requested)
        return _attachment_preview(item[0], item[1], item[2])
    headers = _headers(message)
    body = _eml_body(message)
    return _message_result("EML", path.name, headers, body, attachments)


def _msg(path: Path, requested: str | None) -> str:
    try:
        import extract_msg
        with extract_msg.Message(str(path)) as message:
            attachments = []
            for index, item in enumerate(message.attachments[:MAX_ATTACHMENTS]):
                name = str(getattr(item, "longFilename", None) or getattr(item, "shortFilename", None) or f"attachment-{index}")
                data = getattr(item, "data", b"")
                if not isinstance(data, bytes):
                    data = str(data).encode("utf-8", errors="replace")
                attachments.append((name, _content_type(name), data))
            if requested is not None:
                item = _find_attachment(attachments, requested)
                return _attachment_preview(item[0], item[1], item[2])
            headers = [("From", str(getattr(message, "sender", "") or "")), ("To", str(getattr(message, "to", "") or "")), ("Cc", str(getattr(message, "cc", "") or "")), ("Date", str(getattr(message, "date", "") or "")), ("Subject", str(getattr(message, "subject", "") or ""))]
            return _message_result("MSG", path.name, headers, str(getattr(message, "body", "") or ""), attachments)
    except ImportError as error:
        raise EmailError("Outlook MSG support is unavailable because extract-msg is not installed.") from error
    except Exception as error:
        raise EmailError(f"Could not read Outlook MSG message: {error}") from error


def _read_limited(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_EMAIL_BYTES:
            raise EmailError(f"Email is above the {MAX_EMAIL_BYTES}-byte inspection limit.")
        data = path.read_bytes()
    except OSError as error:
        raise EmailError(f"Could not read email: {error}") from error
    if len(data) > MAX_EMAIL_BYTES:
        raise EmailError(f"Email is above the {MAX_EMAIL_BYTES}-byte inspection limit.")
    return data


def _emlx_message_bytes(data: bytes) -> bytes:
    """Remove the EMLX byte-count line and optional metadata trailer.

    Apple Mail stores a decimal count, a newline, RFC 5322 bytes, then may
    append a plist metadata trailer.  Limiting parsing to the declared bytes
    prevents that trailer from becoming part of the evidence body.
    """
    count_line, newline, remainder = data.partition(b"\n")
    if not newline:
        return data
    try:
        count = int(count_line.strip())
    except ValueError:
        return data
    if count < 0:
        return data
    return remainder[:count]


def _looks_like_email(path: Path) -> bool:
    try:
        return b":" in path.read_bytes()[:4096]
    except OSError as error:
        raise EmailError(f"Could not read email: {error}") from error


def _headers(message: Message) -> list[tuple[str, str]]:
    wanted = ("From", "To", "Cc", "Bcc", "Reply-To", "Date", "Subject", "Message-ID", "In-Reply-To")
    return [(name, _decode_header(str(message[name]))) for name in wanted if message[name] is not None]


def _eml_attachments(message: Message) -> list[tuple[str, str, bytes]]:
    found: list[tuple[str, str, bytes]] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() != "attachment":
            continue
        name = _decode_header(part.get_filename() or f"attachment-{len(found)}")
        payload = part.get_payload(decode=True) or b""
        found.append((name, part.get_content_type(), payload))
        if len(found) >= MAX_ATTACHMENTS:
            break
    return found


def _eml_body(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        (plain if content_type == "text/plain" else html).append(text)
    return "\n\n".join(plain or html) or "[No readable text body]"


def _message_result(kind: str, filename: str, headers: list[tuple[str, str]], body: str, attachments: list[tuple[str, str, bytes]]) -> str:
    lines = [f"{kind} email ({filename})", "--- Headers ---"]
    lines.extend(f"{name}: {value}" for name, value in headers if value)
    lines.extend(["--- Body ---", _bounded(body), "--- Attachments ---"])
    if attachments:
        lines.extend(f"[{index}] {name} ({len(data)} bytes, {content_type})" for index, (name, content_type, data) in enumerate(attachments))
    else:
        lines.append("[none]")
    return _bounded("\n".join(lines))


def _find_attachment(items: list[tuple[str, str, bytes]], requested: str) -> tuple[str, str, bytes]:
    try:
        index = int(requested)
        if str(index) == requested and 0 <= index < len(items):
            return items[index]
    except ValueError:
        pass
    matches = [item for item in items if item[0] == requested]
    if len(matches) == 1:
        return matches[0]
    raise EmailError(f"No attachment named or indexed {requested!r}. List the email first to see available attachments.")


def _attachment_preview(name: str, content_type: str, data: bytes) -> str:
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise EmailError(f"Attachment {name!r} is above the {MAX_ATTACHMENT_BYTES}-byte preview limit.")
    if content_type.startswith("text/") or _content_type(name).startswith("text/"):
        return f"Attachment {name} ({len(data)} bytes, {content_type})\n---\n{_bounded(data.decode('utf-8', errors='replace'))}"
    return f"Attachment {name} ({len(data)} bytes, {content_type}) is binary; use inspect_document, inspect_archive, or another appropriate inspector on the original attachment when separately available."


def _decode_header(value: str) -> str:
    try:
        return "".join(fragment.decode(charset or "utf-8", errors="replace") if isinstance(fragment, bytes) else fragment for fragment, charset in decode_header(value))
    except (LookupError, ValueError):
        return value


def _content_type(name: str) -> str:
    import mimetypes
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _bounded(value: str) -> str:
    if len(value) <= MAX_TEXT_CHARACTERS:
        return value
    return value[:MAX_TEXT_CHARACTERS] + f"\n... [truncated at {MAX_TEXT_CHARACTERS} characters]"
