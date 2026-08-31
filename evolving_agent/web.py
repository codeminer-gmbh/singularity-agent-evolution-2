"""Bounded retrieval and source discovery for online research tasks."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class WebFetchError(Exception):
    """A URL could not be retrieved as bounded readable text."""


@dataclass(frozen=True)
class FetchedPage:
    """The useful, inspectable portion of one HTTP response."""

    url: str
    status: int
    content_type: str
    text: str
    truncated: bool

    def render(self) -> str:
        heading = f"URL: {self.url}\nHTTP status: {self.status}\nContent-Type: {self.content_type or 'unknown'}"
        suffix = "\n... [response truncated]" if self.truncated else ""
        return f"{heading}\n\n{self.text}{suffix}"


class _LimitedRedirects(HTTPRedirectHandler):
    """Follow normal HTTP redirects, but never an unbounded redirect loop."""

    max_redirections = 5
    max_repeats = 2


def fetch_url(url: str, *, max_characters: int = 20_000, timeout_seconds: int = 20) -> str:
    """Fetch an HTTP(S) URL and return decoded, character-bounded response text."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebFetchError("url must be an absolute http:// or https:// URL.")
    if parsed.username or parsed.password:
        raise WebFetchError("url must not contain embedded credentials.")
    if not 100 <= max_characters <= 100_000:
        raise WebFetchError("max_characters must be an integer from 100 through 100000.")
    if not 1 <= timeout_seconds <= 60:
        raise WebFetchError("timeout_seconds must be an integer from 1 through 60.")

    byte_limit = max_characters * 4
    request = Request(url, headers={"User-Agent": "evolving-agent/1.0 (+bounded research tool)"})
    try:
        with build_opener(_LimitedRedirects()).open(request, timeout=timeout_seconds) as response:
            payload = response.read(byte_limit + 1)
            final_url, status, headers = response.geturl(), getattr(response, "status", response.getcode()), response.headers
    except HTTPError as error:
        try:
            payload = error.read(byte_limit + 1)
        finally:
            error.close()
        final_url, status, headers = error.geturl(), error.code, error.headers
    except (URLError, ValueError, OSError) as error:
        raise WebFetchError(f"could not fetch {url!r}: {error}") from error

    truncated = len(payload) > byte_limit
    text = _decode(payload[:byte_limit], headers)
    if len(text) > max_characters:
        text, truncated = text[:max_characters], True
    return FetchedPage(final_url, int(status), headers.get_content_type(), text, truncated).render()


def search_web(query: str, *, max_results: int = 5, timeout_seconds: int = 20) -> str:
    """Discover public sources with DuckDuckGo's server-rendered result page.

    Search results are deliberately a compact index, not a browser dump.  The
    returned URLs are direct destinations where DuckDuckGo provides one, so a
    model can inspect evidence with ``fetch_url`` in a subsequent call.
    """
    query = query.strip()
    if not 1 <= len(query) <= 500:
        raise WebFetchError("query must contain from 1 through 500 characters.")
    if not 1 <= max_results <= 10:
        raise WebFetchError("max_results must be an integer from 1 through 10.")
    if not 1 <= timeout_seconds <= 60:
        raise WebFetchError("timeout_seconds must be an integer from 1 through 60.")
    page = fetch_url(
        "https://html.duckduckgo.com/html/?" + urlencode({"q": query}),
        max_characters=60_000,
        timeout_seconds=timeout_seconds,
    )
    results = _DuckDuckGoResults()
    results.feed(page)
    results.close()
    entries = results.entries[:max_results]
    if not entries:
        return f"Search query: {query}\nNo parseable results were returned. Try a more specific query or fetch a known source URL."
    lines = [f"Search query: {query}", f"Results: {len(entries)}"]
    for index, entry in enumerate(entries, 1):
        lines.append(f"\n{index}. {entry[0]}\nURL: {entry[1]}" + (f"\nSnippet: {entry[2]}" if entry[2] else ""))
    return "\n".join(lines)


class _DuckDuckGoResults(HTMLParser):
    """Small dependency-free parser for the stable HTML search-result markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[list[str]] = []
        self._field: str | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            destination = _direct_result_url(values.get("href", ""))
            if destination:
                self.entries.append(["", destination, ""])
                self._field, self._depth = "title", 1
        elif self.entries and "result__snippet" in classes:
            self._field, self._depth = "snippet", 1
        elif self._field:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._field:
            self._depth -= 1
            if self._depth == 0:
                self._field = None

    def handle_data(self, data: str) -> None:
        if self._field and self.entries:
            index = 0 if self._field == "title" else 2
            self.entries[-1][index] += data

    def close(self) -> None:
        super().close()
        for entry in self.entries:
            entry[0] = " ".join(entry[0].split()) or "Untitled result"
            entry[2] = " ".join(entry[2].split())


def _direct_result_url(href: str) -> str:
    """Unwrap DuckDuckGo's redirect target without accepting other schemes."""
    absolute = urljoin("https://duckduckgo.com", href)
    parsed = urlsplit(absolute)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        absolute = parse_qs(parsed.query).get("uddg", [""])[0]
        parsed = urlsplit(absolute)
    return absolute if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _decode(payload: bytes, headers: Message) -> str:
    """Decode using the declared charset, with a readable UTF-8 fallback."""
    charset = headers.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")
