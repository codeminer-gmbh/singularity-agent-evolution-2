"""Bounded, read-only retrieval of text evidence from web pages."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import os
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

_MAX_DOWNLOAD_BYTES = 1_000_000
_MAX_FILE_DOWNLOAD_BYTES = 25_000_000
_MAX_RESULT_CHARACTERS = 30_000
_MAX_LINKS = 100
_USER_AGENT = "evolving-agent/1.0 (+read-only evidence retrieval)"


class WebError(Exception):
    """A page could not be fetched or is not readable text."""


class _PageParser(HTMLParser):
    """Collect visible HTML text, title, and anchors without executing content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._ignored_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if not self._ignored_depth:
            self.text_parts.append(data)


def fetch_web_page(url: str, timeout_seconds: int = 20) -> str:
    """Fetch one HTTP(S) document and return a bounded, readable evidence view.

    The request is a GET with no cookies or credentials.  It does not save a
    response to disk, execute page content, or fetch linked resources.
    """
    _validate_url(url)
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310: scheme is validated above
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > _MAX_DOWNLOAD_BYTES:
                raise WebError(f"The response declares {declared} bytes; the limit is {_MAX_DOWNLOAD_BYTES} bytes.")
            raw = response.read(_MAX_DOWNLOAD_BYTES + 1)
            if len(raw) > _MAX_DOWNLOAD_BYTES:
                raise WebError(f"The response exceeds the {_MAX_DOWNLOAD_BYTES}-byte download limit.")
            final_url = response.geturl()
            content_type = response.headers.get_content_type().lower()
            charset = response.headers.get_content_charset() or "utf-8"
            status = getattr(response, "status", 200)
    except HTTPError as exc:
        raise WebError(f"The server returned HTTP {exc.code} for {url!r}.") from exc
    except (URLError, OSError, ValueError) as exc:
        raise WebError(f"Could not fetch {url!r}: {exc.reason if isinstance(exc, URLError) else exc}") from exc

    if content_type not in {"text/html", "text/plain", "application/json", "application/xml", "text/xml"}:
        raise WebError(f"The response content type is {content_type!r}, not a readable web page.")
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")

    lines = [f"URL: {final_url}", f"Status: {status}", f"Content-Type: {content_type}"]
    if content_type == "text/html":
        parser = _PageParser()
        parser.feed(body)
        parser.close()
        title = _normalise(" ".join(parser.title_parts))
        text = _normalise(" ".join(parser.text_parts))
        if title:
            lines.append(f"Title: {title}")
        lines.extend(("", "Text:", text or "[The page contains no readable text.]"))
        links = _web_links(parser.links, final_url)
        if links:
            lines.extend(("", "Links:"))
            lines.extend(f"- {link}" for link in links)
    else:
        lines.extend(("", "Text:", body.strip() or "[The response body is empty.]"))
    return _bounded("\n".join(lines))



def download_web_file(url: str, destination: Path, timeout_seconds: int = 30) -> str:
    """Download one bounded HTTP(S) resource atomically to *destination*.

    Unlike :func:`fetch_web_page`, this accepts binary content so a caller can
    pass a web-hosted primary-source file to the local evidence inspectors.
    No credentials, cookies, request body, or linked resources are used.
    """
    _validate_url(url)
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    temporary_name: str | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # The target has already been containment-checked by Workspace.resolve.
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310: scheme validated
            final_url = response.geturl()
            _validate_url(final_url)
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > _MAX_FILE_DOWNLOAD_BYTES:
                raise WebError(f"The response declares {declared} bytes; the file limit is {_MAX_FILE_DOWNLOAD_BYTES} bytes.")
            with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, prefix=".web-download-", delete=False) as temporary:
                temporary_name = temporary.name
                total = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_FILE_DOWNLOAD_BYTES:
                        raise WebError(f"The response exceeds the {_MAX_FILE_DOWNLOAD_BYTES}-byte file limit.")
                    temporary.write(chunk)
            os.replace(temporary_name, destination)
            temporary_name = None
            content_type = response.headers.get_content_type().lower()
            return f"Downloaded {total} bytes from {final_url} to {destination.name}. Content-Type: {content_type}."
    except HTTPError as exc:
        raise WebError(f"The server returned HTTP {exc.code} for {url!r}.") from exc
    except (URLError, OSError, ValueError) as exc:
        raise WebError(f"Could not download {url!r}: {exc.reason if isinstance(exc, URLError) else exc}") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass

def _validate_url(url: str) -> None:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise WebError("'url' must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password:
        raise WebError("URLs with embedded credentials are not accepted.")


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _web_links(hrefs: list[str], base_url: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        absolute = urljoin(base_url, href)
        parsed = urlsplit(absolute)
        if parsed.scheme.lower() not in {"http", "https"} or absolute in seen:
            continue
        seen.add(absolute)
        result.append(absolute)
        if len(result) == _MAX_LINKS:
            break
    return result


def _bounded(value: str) -> str:
    if len(value) <= _MAX_RESULT_CHARACTERS:
        return value
    return value[:_MAX_RESULT_CHARACTERS] + f"\n... [truncated at {_MAX_RESULT_CHARACTERS} characters]"
