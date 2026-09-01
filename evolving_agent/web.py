"""Bounded, readable retrieval of public HTML pages for research tasks.

The extractor deliberately returns page evidence rather than executing JavaScript or
writing downloads to the task workspace.  Network and response limits make a bad
URL a recoverable tool failure instead of a stalled agent turn.
"""
from __future__ import annotations

import base64
from html.parser import HTMLParser
import ipaddress
import re
import socket
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES: Final = 1_500_000
MAX_TEXT_CHARACTERS: Final = 24_000
MAX_LINKS: Final = 80
MAX_SEARCH_RESULTS: Final = 12
TIMEOUT_SECONDS: Final = 20
MAX_REDIRECTS: Final = 5
_USER_AGENT: Final = "EvolvingAgent/1.0 (bounded research fetch)"


class WebError(Exception):
    """A URL could not safely be retrieved as readable web evidence."""


class _Redirects(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise WebError(f"The URL redirected more than {MAX_REDIRECTS} times.")
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ReadableHTML(HTMLParser):
    """Small dependency-free HTML-to-text extractor, retaining useful links."""
    _SKIP = frozenset({"script", "style", "noscript", "template", "svg", "canvas", "iframe"})
    _BREAK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section", "blockquote", "pre"})

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._link_url: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in self._BREAK:
            self.parts.append("\n")
        if tag == "a" and not self._skip_depth:
            href = dict(attrs).get("href")
            if href:
                absolute = urljoin(self.base_url, href)
                if urlsplit(absolute).scheme in {"http", "https"}:
                    self._link_url, self._link_text = absolute, []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in self._BREAK:
            self.parts.append("\n")
        if tag == "a" and self._link_url:
            label = _clean(" ".join(self._link_text)) or self._link_url
            if len(self.links) < MAX_LINKS and (self._link_url, label) not in self.links:
                self.links.append((self._link_url, label[:300]))
            self._link_url = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)
            if self._link_url:
                self._link_text.append(data)


def fetch_web_page(url: str) -> str:
    """Fetch one HTTP(S) page and return bounded text, metadata, and outgoing links."""
    _validate_url(url)
    redirects = _Redirects()
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1"})
    try:
        with build_opener(redirects).open(request, timeout=TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            _validate_url(final_url)
            content_type = response.headers.get_content_type().lower()
            declared_length = response.headers.get("Content-Length")
            if declared_length and declared_length.isdigit() and int(declared_length) > MAX_RESPONSE_BYTES:
                raise WebError(f"The response exceeds the {MAX_RESPONSE_BYTES}-byte retrieval limit.")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise WebError(f"The response exceeds the {MAX_RESPONSE_BYTES}-byte retrieval limit.")
            charset = response.headers.get_content_charset() or _charset_from_html(raw) or "utf-8"
            try:
                body = raw.decode(charset, errors="replace")
            except LookupError:
                body = raw.decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
    except WebError:
        raise
    except HTTPError as error:
        raise WebError(f"The server returned HTTP {error.code} {error.reason}.") from error
    except (URLError, OSError, socket.timeout) as error:
        raise WebError(f"Could not fetch the URL: {getattr(error, 'reason', error)}") from error
    if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise WebError(f"The URL returned {content_type or 'an unknown type'}, not an HTML or text page.")
    if content_type == "text/plain":
        text, links = _clean(body), []
    else:
        parser = _ReadableHTML(final_url)
        try:
            parser.feed(body)
            parser.close()
        except Exception as error:
            raise WebError(f"Could not parse the HTML response: {error}") from error
        text, links = _clean(" ".join(parser.parts)), parser.links
    shown = text[:MAX_TEXT_CHARACTERS]
    if len(text) > MAX_TEXT_CHARACTERS:
        shown += f"\n... [truncated at {MAX_TEXT_CHARACTERS} characters]"
    link_lines = "\n".join(f"- {label}: {target}" for target, label in links) or "(no HTTP(S) links found)"
    return f"Web page: {final_url}\nHTTP {status}; {content_type}; {len(raw)} bytes\n---\n{shown or '(no readable text found)'}\n---\nLinks\n{link_lines}"


def search_web(query: str) -> str:
    """Search the public web and return a small, citation-ready result list.

    Bing's server-rendered HTML endpoint needs neither a browser nor an API
    credential. Search pages are parsed separately so result redirect wrappers
    are unwrapped before being shown to the model.
    """
    if not isinstance(query, str) or not query.strip():
        raise WebError("'query' must be a non-empty search phrase.")
    phrase = query.strip()
    if len(phrase) > 500:
        raise WebError("The search phrase exceeds the 500-character limit.")
    search_url = "https://www.bing.com/search?q=" + quote_plus(phrase)
    try:
        # Reuse the bounded public fetcher; its links give us the result targets.
        evidence = fetch_web_page(search_url)
    except WebError as error:
        raise WebError(f"Search failed: {error}") from error
    rows: list[str] = []
    seen: set[str] = set()
    # The generic extraction already emits each anchor as '- label: target'.
    links = evidence.partition("\nLinks\n")[2].splitlines()
    for line in links:
        if not line.startswith("- ") or ": " not in line:
            continue
        label, target = line[2:].rsplit(": ", 1)
        parsed = urlsplit(target)
        # Search engines wrap result URLs. DDG uses uddg; Bing's ck URL
        # carries a URL-safe base64 destination after its a1 marker.
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            target = parse_qs(parsed.query).get("uddg", [target])[0]
        elif parsed.hostname and parsed.hostname.endswith("bing.com") and parsed.path.startswith("/ck/"):
            encoded = parse_qs(parsed.query).get("u", [""])[0]
            if encoded.startswith("a1"):
                try:
                    target = base64.urlsafe_b64decode(encoded[2:] + "=" * (-len(encoded[2:]) % 4)).decode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    pass
        candidate = urlsplit(target)
        if candidate.scheme not in {"http", "https"} or not candidate.hostname:
            continue
        canonical = urlunsplit((candidate.scheme, candidate.netloc, candidate.path, candidate.query, ""))
        # Generic page extraction also sees the search engine's navigation
        # controls; wrapped result destinations have already been decoded.
        result_host = candidate.hostname.lower()
        if result_host == "bing.com" or result_host.endswith(".bing.com"):
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        rows.append(f"{len(rows) + 1}. {label[:300]}\n   {canonical}")
        if len(rows) >= MAX_SEARCH_RESULTS:
            break
    if not rows:
        # Keeping bounded page text makes a temporary markup change actionable.
        preview = evidence.partition("\n---\n")[2].partition("\n---\nLinks")[0][:4_000]
        return f"Web search: {phrase}\nNo result links were recognized. Search-page text:\n{preview}"
    return f"Web search: {phrase}\nResults ({len(rows)}):\n" + "\n".join(rows)


def _validate_url(url: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise WebError("'url' must be a non-empty HTTP(S) URL.")
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise WebError("Use an absolute HTTP(S) URL without embedded credentials.")
    # This tool is for public research, not probing local services or cloud metadata.
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise WebError("Localhost URLs are not permitted.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise WebError("Private, loopback, and reserved IP addresses are not permitted.")


def _charset_from_html(raw: bytes) -> str | None:
    match = re.search(br"<meta[^>]+charset=[\"']?([A-Za-z0-9._-]+)", raw[:8192], re.I)
    return match.group(1).decode("ascii", "ignore") if match else None


def _clean(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
