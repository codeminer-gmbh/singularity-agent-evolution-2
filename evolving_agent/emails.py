"""Bounded, non-executing inspection of common email evidence formats.

Email is treated as untrusted data: MIME parts are never opened, attachments
are not written anywhere, and HTML is reduced to readable text rather than
rendered.  The result is deliberately JSON so a model can quote or compare it
without spending a tool call decoding headers itself.
"""

from __future__ import annotations

import email
import email.policy
import html
import json
import mailbox
import re
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any

_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_MESSAGES = 50
_MAX_HEADER_VALUE = 1_000
_MAX_ATTACHMENTS = 100
_MAX_PREVIEW_CHARACTERS = 12_000


class EmailError(Exception):
    """An email evidence file could not be inspected."""


def inspect_email(
    path: Path, *, message_index: int = 0, max_messages: int = 20
) -> str:
    """Return a bounded JSON summary and selected-message preview for *path*.

    ``message_index`` is zero-based for an MBOX.  EML and EMLX contain exactly
    one message and only accept index zero.  ``max_messages`` bounds the MBOX
    envelope list independently from the selected message.
    """
    if not path.is_file():
        raise EmailError(f"{path.name!r} is not a readable email file.")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EmailError(f"Could not inspect {path.name!r}: {exc}") from exc
    if size > _MAX_SOURCE_BYTES:
        raise EmailError(
            f"{path.name!r} is {size} bytes; email inspection is limited to "
            f"{_MAX_SOURCE_BYTES} bytes."
        )
    if message_index < 0:
        raise EmailError("message_index must be zero or greater.")
    if not 1 <= max_messages <= _MAX_MESSAGES:
        raise EmailError(f"max_messages must be between 1 and {_MAX_MESSAGES}.")

    if path.suffix.lower() in {".mbox", ".mbx"}:
        return _inspect_mbox(path, message_index, max_messages)
    if message_index:
        raise EmailError("EML and EMLX files contain one message; message_index must be 0.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise EmailError(f"Could not read {path.name!r}: {exc}") from exc
    if path.suffix.lower() == ".emlx":
        data = _emlx_message(data)
    return json.dumps(
        {"format": path.suffix.lower().lstrip(".") or "eml", "message": _message_info(_parse(data))},
        ensure_ascii=False,
        indent=2,
    )


def _inspect_mbox(path: Path, message_index: int, max_messages: int) -> str:
    """Read an MBOX without locking, changing, or extracting any of its parts."""
    try:
        box = mailbox.mbox(path, factory=_parse_stream, create=False)
        summaries: list[dict[str, Any]] = []
        selected: Message | None = None
        total = 0
        for index, message in enumerate(box):
            total += 1
            if index < max_messages:
                summaries.append(_envelope(index, message))
            if index == message_index:
                selected = message
        box.close()
    except (OSError, mailbox.Error, ValueError) as exc:
        raise EmailError(f"Could not parse MBOX {path.name!r}: {exc}") from exc
    if selected is None:
        raise EmailError(f"message_index {message_index} is outside this MBOX ({total} messages).")
    return json.dumps(
        {
            "format": "mbox",
            "message_count": total,
            "listed_messages": summaries,
            "selected_message_index": message_index,
            "message": _message_info(selected),
        },
        ensure_ascii=False,
        indent=2,
    )


def _parse(data: bytes) -> Message:
    try:
        return BytesParser(policy=email.policy.default).parsebytes(data)
    except (ValueError, UnicodeError) as exc:
        raise EmailError(f"The email message could not be parsed: {exc}") from exc


def _parse_stream(stream: Any) -> Message:
    """Parse mailbox's bounded message stream without retaining its file handle."""
    try:
        return BytesParser(policy=email.policy.default).parse(stream)
    except (ValueError, UnicodeError) as exc:
        raise EmailError(f"The email message could not be parsed: {exc}") from exc


def _emlx_message(data: bytes) -> bytes:
    """Remove Apple's optional EMLX byte-count line without trusting it blindly."""
    first, separator, rest = data.partition(b"\n")
    if not separator:
        return data
    try:
        declared = int(first.strip())
    except ValueError:
        return data
    if declared < 0:
        return data
    return rest[:declared]


def _envelope(index: int, message: Message) -> dict[str, Any]:
    return {
        "index": index,
        "from": _header(message, "From"),
        "to": _header(message, "To"),
        "date": _header(message, "Date"),
        "subject": _header(message, "Subject"),
    }


def _message_info(message: Message) -> dict[str, Any]:
    headers = {
        name: _header(message, name)
        for name in ("From", "To", "Cc", "Bcc", "Reply-To", "Date", "Subject", "Message-ID", "In-Reply-To")
        if message.get(name) is not None
    }
    attachments: list[dict[str, Any]] = []
    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type().lower()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        is_attachment = disposition == "attachment" or filename is not None
        if is_attachment:
            if len(attachments) < _MAX_ATTACHMENTS:
                attachments.append(
                    {
                        "filename": _bounded(filename or "", 500) or None,
                        "content_type": content_type,
                        "disposition": disposition or None,
                        "size_bytes": _part_size(part),
                    }
                )
            continue
        text = _part_text(part)
        if text is None:
            continue
        if content_type == "text/plain":
            text_parts.append(text)
        elif content_type == "text/html":
            html_parts.append(_html_to_text(text))
    preview = _join_preview(text_parts or html_parts)
    return {
        "headers": headers,
        "body_preview": preview,
        "body_preview_truncated": len(preview) >= _MAX_PREVIEW_CHARACTERS,
        "attachments": attachments,
        "attachment_count": sum(1 for part in message.walk() if not part.is_multipart() and ((part.get_content_disposition() or "").lower() == "attachment" or part.get_filename() is not None)),
    }


def _header(message: Message, name: str) -> str:
    value = str(message.get(name, ""))
    return _bounded(" ".join(value.split()), _MAX_HEADER_VALUE)


def _part_size(part: Message) -> int | None:
    payload = part.get_payload(decode=True)
    return len(payload) if isinstance(payload, bytes) else None


def _part_text(part: Message) -> str | None:
    if part.get_content_maintype().lower() != "text":
        return None
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        raw = part.get_payload()
        return raw if isinstance(raw, str) else None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", "", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(p|div|li|tr|h[1-6])\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]*>", "", value)
    return html.unescape(value)


def _join_preview(parts: list[str]) -> str:
    return _bounded("\n\n".join(part.strip() for part in parts if part.strip()), _MAX_PREVIEW_CHARACTERS)


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}… [truncated]"
