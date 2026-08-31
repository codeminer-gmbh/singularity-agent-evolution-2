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
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

_MAX_TIMEOUT: Final = 45
_DEFAULT_TIMEOUT: Final = 20
_DEFAULT_CHARACTERS: Final = 24_000
_MAX_CHARACTERS: Final = 80_000
_MAX_SEARCH_RESULTS: Final = 10
_MAX_SEARCH_QUERY_CHARACTERS: Final = 500


class WebFetchError(Exception):
    """A URL could not be fetched as a bounded readable document."""


@dataclass(frozen=True)
class SearchResult:
    """One independently discoverable web result."""

    title: str
    url: str
    snippet: str = ""


class _SearchExtractor(HTMLParser):
    """Extract result links from DuckDuckGo's deliberately simple HTML view."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        href = values.get("href")
        if href and ({"result__a", "result-link"} & classes):
            self._href, self._parts = href, []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        title = " ".join("".join(self._parts).split())
        url = _search_target(self._href)
        if title and url:
            self.results.append(SearchResult(title=title, url=url))
        self._href, self._parts = None, []


def _search_target(href: str) -> str | None:
    """Turn a DuckDuckGo redirect (or direct link) into an HTTP(S) target."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        href = unquote(target)
        parsed = urlparse(href)
    return href if parsed.scheme in {"http", "https"} and parsed.netloc else None


def search(query: str, *, max_results: int = 5, timeout_seconds: int = _DEFAULT_TIMEOUT) -> list[SearchResult]:
    """Find public pages matching ``query`` without requiring an API key.

    DuckDuckGo's HTML endpoint is used because it returns ordinary server-rendered
    links, unlike a browser-only search UI.  Search is intentionally bounded;
    callers fetch the small number of sources they decide are relevant.
    """
    if not isinstance(query, str) or not query.strip():
        raise WebFetchError("query must be a non-blank string.")
    if len(query) > _MAX_SEARCH_QUERY_CHARACTERS:
        raise WebFetchError(f"query must be at most {_MAX_SEARCH_QUERY_CHARACTERS} characters.")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= _MAX_SEARCH_RESULTS:
        raise WebFetchError(f"max_results must be an integer from 1 to {_MAX_SEARCH_RESULTS}.")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= _MAX_TIMEOUT:
        raise WebFetchError(f"timeout_seconds must be an integer from 1 to {_MAX_TIMEOUT}.")
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query.strip())
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; evolving-agent/1.0)", "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            source = response.read(1_000_000).decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, socket.timeout) as failure:
        raise WebFetchError(f"Could not search the web: {failure}") from failure
    parser = _SearchExtractor()
    parser.feed(source)
    parser.close()
    unique: list[SearchResult] = []
    seen: set[str] = set()
    for result in parser.results:
        if result.url not in seen:
            unique.append(result)
            seen.add(result.url)
        if len(unique) >= max_results:
            break
    if not unique:
        raise WebFetchError("The search service returned no readable results; try a more specific query.")
    return unique


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
