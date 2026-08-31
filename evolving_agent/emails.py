"""Bounded, read-only inspection of RFC 5322 email evidence.

The standard library parser handles MIME transfer encodings and charset labels without
executing active HTML, attachments, or message content.  Reports deliberately retain
metadata and a compact text preview rather than writing or opening attachments.
"""

from __future__ import annotations

import html
import json
import mailbox
import re
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Iterator

_MAX_INPUT_BYTES = 12_000_000
_MAX_MESSAGES = 100
_MAX_PARTS = 80
_MAX_BODY_CHARACTERS = 6_000
_MAX_ATTACHMENT_NAME = 300
_TAGS = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"[ \t\r\f\v]+")


class EmailError(ValueError):
    """The requested mailbox is unsupported, malformed, or too large."""


def inspect_email(path: Path, *, max_messages: int = 20, body_characters: int = 2_000) -> str:
    """Return JSON metadata and safe text previews from EML, EMLX, or MBOX files."""
    if not 1 <= max_messages <= _MAX_MESSAGES:
        raise EmailError(f"max_messages must be between 1 and {_MAX_MESSAGES}.")
    if not 0 <= body_characters <= _MAX_BODY_CHARACTERS:
        raise EmailError(f"body_characters must be between 0 and {_MAX_BODY_CHARACTERS}.")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise EmailError(f"could not inspect {path.name!r}: {error}") from error
    if size > _MAX_INPUT_BYTES:
        raise EmailError(f"{path.name!r} is over the 12 MB inspection limit.")
    suffix = path.suffix.lower()
    if suffix in {".eml", ".emlx"}:
        message = _parse_eml(path, suffix)
        messages = [_message_report(message, 1, body_characters)]
        total_known: int | None = 1
        truncated = False
        format_name = suffix[1:]
    elif suffix in {".mbox", ".mbx"}:
        messages, total_known, truncated = _parse_mbox(path, max_messages, body_characters)
        format_name = "mbox"
    else:
        raise EmailError("supported email formats are .eml, .emlx, .mbox, and .mbx.")
    return json.dumps(
        {
            "format": format_name,
            "messages": messages,
            "returned_messages": len(messages),
            "total_messages": total_known,
            "truncated": truncated,
            "note": "Bodies are decoded text previews only; HTML, attachments, links, and active content are never opened or executed.",
        },
        ensure_ascii=False,
        indent=2,
    )


def _parse_eml(path: Path, suffix: str) -> Message:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise EmailError(f"could not read email: {error}") from error
    if suffix == ".emlx":
        # Apple EMLX starts with an ASCII byte-count line before an ordinary EML.
        first, separator, remainder = raw.partition(b"\n")
        if separator and first.strip().isdigit():
            raw = remainder
    try:
        return BytesParser(policy=policy.default).parsebytes(raw)
    except (ValueError, UnicodeError) as error:
        raise EmailError(f"malformed email message: {error}") from error


def _parse_mbox(path: Path, maximum: int, body_characters: int) -> tuple[list[dict[str, object]], int | None, bool]:
    try:
        box = mailbox.mbox(path, factory=lambda file: BytesParser(policy=policy.default).parse(file))
        reports: list[dict[str, object]] = []
        truncated = False
        for number, message in enumerate(box, 1):
            if number > maximum:
                truncated = True
                break
            reports.append(_message_report(message, number, body_characters))
    except (OSError, ValueError, UnicodeError, mailbox.Error) as error:
        raise EmailError(f"malformed mbox: {error}") from error
    finally:
        try:
            box.close()
        except (UnboundLocalError, OSError):
            pass
    return reports, None if truncated else len(reports), truncated


def _message_report(message: Message, number: int, body_characters: int) -> dict[str, object]:
    headers = {name: _header(message.get(name, "")) for name in ("From", "To", "Cc", "Bcc", "Reply-To", "Date", "Subject", "Message-ID", "In-Reply-To") if message.get(name)}
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, object]] = []
    part_count = 0
    for part in message.walk():
        part_count += 1
        if part_count > _MAX_PARTS:
            break
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_type = part.get_content_type()
        if disposition == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append({"filename": _header(filename or "unnamed")[:_MAX_ATTACHMENT_NAME], "content_type": content_type, "bytes": len(payload)})
            continue
        if content_type in {"text/plain", "text/html"}:
            value = _part_text(part)
            (html_parts if content_type == "text/html" else text_parts).append(value)
    body = "\n\n".join(text_parts or [_html_text(value) for value in html_parts]).strip()
    if body_characters == 0:
        body = ""
    truncated = len(body) > body_characters or part_count > _MAX_PARTS
    if len(body) > body_characters:
        body = body[:body_characters] + "…"
    return {"number": number, "headers": headers, "body_preview": body, "attachments": attachments, "part_count": min(part_count, _MAX_PARTS), "truncated": truncated}


def _part_text(part: Message) -> str:
    try:
        value = part.get_content()
    except (LookupError, UnicodeError, ValueError):
        payload = part.get_payload(decode=True) or b""
        value = payload.decode("utf-8", errors="replace")
    return _SPACE.sub(" ", str(value).replace("\x00", "")).strip()


def _html_text(value: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", value))).strip()


def _header(value: object) -> str:
    try:
        return str(make_header(decode_header(str(value)))).replace("\r", " ").replace("\n", " ").strip()
    except (UnicodeError, ValueError):
        return str(value).replace("\r", " ").replace("\n", " ").strip()
