"""Bounded HTTP retrieval for research tasks.

This module deliberately returns data rather than following any instructions in a
page.  HTML is reduced to its visible text, while JSON, feeds, and other text
responses remain verbatim, so an agent can use one small tool for documents and
web APIs alike.
"""

from __future__ import annotations

from html.parser import HTMLParser
import gzip
import re
import socket
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
import zlib

_MAX_RESPONSE_BYTES: Final = 2_000_000
_DEFAULT_MAX_CHARACTERS: Final = 20_000
_MAX_CHARACTERS: Final = 100_000
_TIMEOUT_SECONDS: Final = 20


class WebFetchError(Exception):
    """A URL could not be fetched in a form useful to the caller."""


class _LimitedRedirects(HTTPRedirectHandler):
    """Keep redirect chains bounded even for a malicious or broken endpoint."""

    max_redirections = 5
    max_repeats = 2


class _VisibleText(HTMLParser):
    """Turn HTML into compact readable text without executing or retaining markup."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BREAK = {
        "address", "article", "blockquote", "br", "div", "dd", "dl", "dt",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "li", "main", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        elif not self._skip_depth and tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", "".join(self.parts))).strip()


def fetch_url(url: str, *, max_characters: int = _DEFAULT_MAX_CHARACTERS) -> str:
    """Download one HTTP(S) URL and return a bounded, readable representation."""
    if not isinstance(url, str) or not url.strip():
        raise WebFetchError("'url' must be a non-empty HTTP or HTTPS URL.")
    if isinstance(max_characters, bool) or not isinstance(max_characters, int):
        raise WebFetchError("'max_characters' must be a whole number.")
    if not 100 <= max_characters <= _MAX_CHARACTERS:
        raise WebFetchError("'max_characters' must be between 100 and 100000.")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebFetchError("'url' must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise WebFetchError("URLs with embedded credentials are not supported.")

    request = Request(url, headers={"User-Agent": "evolving-agent/1.0", "Accept-Encoding": "gzip, deflate"})
    try:
        with build_opener(_LimitedRedirects()).open(request, timeout=_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            encoding = response.headers.get("Content-Encoding", "").lower()
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise WebFetchError(f"The server returned HTTP {error.code} for {url!r}.") from error
    except (URLError, TimeoutError, socket.timeout, OSError) as error:
        raise WebFetchError(f"Could not fetch {url!r}: {error}.") from error
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise WebFetchError(f"The response from {url!r} exceeds the 2000000-byte download limit.")
    try:
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw)
    except (OSError, zlib.error) as error:
        raise WebFetchError(f"The response from {url!r} had invalid {encoding!r} compression.") from error
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise WebFetchError(f"The decompressed response from {url!r} exceeds the 2000000-byte limit.")
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = _VisibleText()
        parser.feed(body)
        parser.close()
        body = parser.text()
    result = f"Source: {final_url}\nContent-Type: {content_type}\n\n{body}"
    if len(result) > max_characters:
        result = result[:max_characters] + f"\n... [truncated at {max_characters} characters]"
    return result
