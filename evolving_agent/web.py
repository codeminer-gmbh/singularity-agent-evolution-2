"""Bounded HTTP retrieval for research tasks.

This deliberately uses the standard library: a web page is data, not
instructions, and the tool returns it as bounded text for the model to assess.
"""

from __future__ import annotations

import html
import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urlparse

_MAX_TIMEOUT: Final = 45
_DEFAULT_TIMEOUT: Final = 20
_DEFAULT_CHARACTERS: Final = 24_000
_MAX_CHARACTERS: Final = 80_000


class WebFetchError(Exception):
    """A URL could not be fetched as a bounded readable document."""


@dataclass(frozen=True)
class FetchedPage:
    """The useful, display-safe result of one HTTP request."""

    url: str
    status: int
    content_type: str
    text: str
    truncated: bool


class _TextExtractor(HTMLParser):
    """Extract visible HTML text without adding another parsing dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self._ignored_depth += 1
        elif tag.lower() in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif tag.lower() in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", "".join(self.parts))).strip()


def fetch(url: str, *, timeout_seconds: int = _DEFAULT_TIMEOUT, max_characters: int = _DEFAULT_CHARACTERS) -> FetchedPage:
    """Fetch an HTTP(S) URL, returning bounded plain text and useful metadata."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebFetchError("url must be an absolute http:// or https:// URL.")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= _MAX_TIMEOUT:
        raise WebFetchError(f"timeout_seconds must be an integer from 1 to {_MAX_TIMEOUT}.")
    if not isinstance(max_characters, int) or isinstance(max_characters, bool) or not 100 <= max_characters <= _MAX_CHARACTERS:
        raise WebFetchError(f"max_characters must be an integer from 100 to {_MAX_CHARACTERS}.")

    # Characters can expand under UTF-8 decoding; this bound still keeps a
    # response comfortably below a model-context-sized tool result.
    byte_limit = max_characters * 4 + 4096
    request = urllib.request.Request(url, headers={"User-Agent": "evolving-agent/1.0", "Accept": "text/html,text/plain,application/json,text/*;q=0.9,*/*;q=0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            data = response.read(byte_limit + 1)
            final_url = response.geturl()
            status = getattr(response, "status", 200)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, socket.timeout) as failure:
        raise WebFetchError(f"Could not fetch {url!r}: {failure}") from failure
    try:
        source = data[:byte_limit].decode(charset, errors="replace")
    except LookupError:
        source = data[:byte_limit].decode("utf-8", errors="replace")
    text = _render(source, content_type)
    truncated = len(data) > byte_limit or len(text) > max_characters
    return FetchedPage(final_url, int(status), content_type, text[:max_characters], truncated)


def _render(source: str, content_type: str) -> str:
    """Turn HTML into readable visible text; preserve text and JSON verbatim."""
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = _TextExtractor()
        parser.feed(source)
        parser.close()
        return html.unescape(parser.text())
    if content_type == "application/json":
        try:
            return json.dumps(json.loads(source), indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return source
