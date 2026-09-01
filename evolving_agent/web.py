"""Bounded retrieval of web evidence for the agent's research tool.

This module deliberately treats every response as untrusted data.  It performs
no JavaScript, stores no cookies, and follows only a small number of ordinary
HTTP(S) redirects.  The returned text is capped before it reaches the model.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_MAX_RESPONSE_BYTES: Final = 1_000_000
_DEFAULT_MAX_CHARACTERS: Final = 12_000
_MAX_CHARACTERS: Final = 20_000
_DEFAULT_TIMEOUT_SECONDS: Final = 20
_MAX_TIMEOUT_SECONDS: Final = 45
_MAX_REDIRECTS: Final = 5
_USER_AGENT: Final = "evolving-agent/1.0 (bounded research retrieval)"


class WebError(Exception):
    """The requested web resource could not be fetched safely."""


@dataclass
class _Redirects(HTTPRedirectHandler):
    """Redirect handler which rejects non-web destinations and redirect loops."""

    remaining: int = _MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if self.remaining <= 0:
            raise WebError(f"The URL redirected more than {_MAX_REDIRECTS} times.")
        _validate_url(urljoin(req.full_url, newurl))
        self.remaining -= 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(
    url: str, *, max_characters: int = _DEFAULT_MAX_CHARACTERS,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Fetch bounded textual HTTP(S) evidence and return useful response metadata.

    The body is read with a byte cap so a maliciously large response cannot
    consume unbounded memory.  Binary data is represented as decoded text with
    replacement characters rather than being interpreted or saved.
    """
    _validate_url(url)
    maximum = _bounded(max_characters, "max_characters", 1, _MAX_CHARACTERS)
    timeout = _bounded(timeout_seconds, "timeout_seconds", 1, _MAX_TIMEOUT_SECONDS)
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"})
    opener = build_opener(_Redirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            return _format_response(
                response.geturl(), response.status, response.headers.get_content_type(),
                response.headers.get_content_charset(), _read_limited(response), maximum,
            )
    except HTTPError as error:
        # HTTP errors still often contain the evidence a task needs (such as a
        # rate-limit explanation), but remain bounded exactly like successes.
        return _format_response(
            error.geturl(), error.code, error.headers.get_content_type() if error.headers else "unknown",
            error.headers.get_content_charset() if error.headers else None,
            _read_limited(error), maximum,
        )
    except WebError:
        raise
    except (URLError, socket.timeout, TimeoutError, OSError) as error:
        raise WebError(f"Could not fetch {url!r}: {error}") from error


def _validate_url(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WebError("'url' must be a non-blank HTTP or HTTPS URL.")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise WebError("Only absolute HTTP and HTTPS URLs with a hostname can be fetched.")
    if parsed.username or parsed.password:
        raise WebError("URLs with embedded credentials cannot be fetched.")


def _bounded(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise WebError(f"'{name}' must be a whole number from {minimum} to {maximum}.")
    return value


def _read_limited(response) -> bytes:  # type: ignore[no-untyped-def]
    data = response.read(_MAX_RESPONSE_BYTES + 1)
    return data[:_MAX_RESPONSE_BYTES]


def _format_response(url: str, status: int, content_type: str, charset: str | None, data: bytes, maximum: int) -> str:
    encoding = charset or "utf-8"
    try:
        body = data.decode(encoding, errors="replace")
    except LookupError:
        encoding = "utf-8"
        body = data.decode(encoding, errors="replace")
    if content_type == "application/json":
        try:
            body = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            pass
    suffix = ""
    if len(body) > maximum:
        body = body[:maximum]
        suffix = f"\n... [truncated at {maximum} characters]"
    return "\n".join((
        f"URL: {url}", f"Status: {status}", f"Content-Type: {content_type}; charset={encoding}",
        "--- body ---", f"{body}{suffix}",
    ))
