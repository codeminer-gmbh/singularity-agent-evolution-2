"""Bounded retrieval and text extraction for task-referenced web resources.

Remote page contents are returned as untrusted data.  This module deliberately
has no authority to execute scripts, follow page instructions, or write files.
"""

from __future__ import annotations

import html
import json
import re
import socket
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_OUTPUT_CHARACTERS = 60_000
_DEFAULT_TIMEOUT_SECONDS = 20
_MAX_TIMEOUT_SECONDS = 45
_USER_AGENT = "evolving-agent/1.0 (task research)"


class WebError(Exception):
    """A web resource could not be retrieved as readable task data."""


class _VisibleText(HTMLParser):
    """Collect visible HTML text without executing or interpreting it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style", "noscript", "template", "svg"}:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag.lower() in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif self._ignored_depth == 0 and tag.lower() in {"p", "div", "li", "tr", "article", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)


def fetch(url: str, timeout_seconds: int | None = None) -> str:
    """Fetch an HTTP(S) URL and return its bounded, readable content.

    HTML is converted to visible text; JSON is indented when valid; other text
    media are decoded using the server charset or UTF-8 replacement.  Binary
    responses are rejected rather than emitted into a model context.
    """
    candidate = _url(url)
    timeout = _timeout(timeout_seconds)
    request = Request(candidate, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/json,text/plain,text/*;q=0.9,*/*;q=0.1"})
    try:
        with build_opener().open(request, timeout=timeout) as response:
            raw = _read_bounded(response)
            content_type = response.headers.get_content_type().lower()
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = response.geturl()
            status = getattr(response, "status", 200)
    except HTTPError as failure:
        raise WebError(f"The server returned HTTP {failure.code} for {candidate!r}.") from failure
    except (URLError, TimeoutError, socket.timeout, OSError) as failure:
        raise WebError(f"Could not fetch {candidate!r}: {failure.reason if isinstance(failure, URLError) else failure}.") from failure

    if not _textual(content_type):
        raise WebError(f"{candidate!r} returned unsupported content type {content_type!r}; fetch_web_page reads HTML, JSON, XML, and text.")
    try:
        decoded = raw.decode(charset, errors="replace")
    except LookupError:
        decoded = raw.decode("utf-8", errors="replace")
    rendered = _render(decoded, content_type)
    rendered = _clean(rendered)
    if len(rendered) > _MAX_OUTPUT_CHARACTERS:
        rendered = rendered[:_MAX_OUTPUT_CHARACTERS] + f"\n\n[truncated at {_MAX_OUTPUT_CHARACTERS} characters]"
    return f"# Web resource\nURL: {final_url}\nStatus: {status}\nContent-Type: {content_type}\n\n{rendered or '[No readable text found]'}"


def _url(value: str) -> str:
    candidate = value.strip()
    if not candidate.startswith(("http://", "https://")):
        raise WebError("'url' must be an absolute http:// or https:// URL.")
    return candidate


def _timeout(value: int | None) -> int:
    if value is None:
        return _DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_TIMEOUT_SECONDS:
        raise WebError(f"'timeout_seconds' must be an integer from 1 to {_MAX_TIMEOUT_SECONDS}.")
    return value


def _read_bounded(response: object) -> bytes:
    # urllib HTTPResponse supports read(size); keeping the extra byte detects a
    # too-large body without allocating or passing it to the model.
    payload = response.read(_MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise WebError(f"The response exceeds the {_MAX_RESPONSE_BYTES // (1024 * 1024)} MiB retrieval limit.")
    return payload


def _textual(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in {"application/json", "application/ld+json", "application/xml", "application/xhtml+xml"} or content_type.endswith("+json") or content_type.endswith("+xml")


def _render(content: str, content_type: str) -> str:
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = _VisibleText()
        parser.feed(content)
        parser.close()
        return "".join(parser.parts)
    if content_type.endswith("json") or content_type == "application/json":
        try:
            return json.dumps(json.loads(content), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    return html.unescape(content)


def _clean(content: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t\f\v]+", " ", content)).strip()
