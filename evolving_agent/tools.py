"""The capabilities this agent has: reading, writing and running things.

The tools are declared here, once, as data — a name, a description and a JSON
Schema for the arguments — and are implemented against the workspace and the
command runner. The MCP server publishes exactly this list, so what a model is
told it can do and what the agent can actually do cannot drift apart.

Every failure is a :class:`ToolFailureError` carrying a sentence the model can act
on. That is the difference between a tool that teaches and one that merely
refuses: "that path leaves the workspace" gets a corrected second attempt, an
unhandled exception ends the run.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
import tarfile
import zipfile
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from typing import Any, BinaryIO

from evolving_agent.commands import CommandRunner
from evolving_agent.databases import DatabaseError, MAX_QUERY_ROWS, query_sqlite
from evolving_agent.documents import DocumentError, MAX_TEXT_CHARACTERS, read_document
from evolving_agent.geodata import GeodataError, MAX_SUMMARY_FEATURES, convert_to_geojson, inspect_geodata
from evolving_agent.workspace import Workspace, WorkspaceError

_MAX_LISTING_CHARACTERS = 8_000
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 1_000
_MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_ARCHIVE_EXTRACTED_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_COMPRESSION_RATIO = 1_000
"""Maximum expanded-to-compressed ratio accepted from an archive stream."""
_MIN_ARCHIVE_RATIO_INPUT_BYTES = 1_024
"""Small inputs get this floor so normal tiny files are not rejected by metadata overhead."""
_ARCHIVE_CHUNK_BYTES = 64 * 1024
_MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_HTTP_TIMEOUT_SECONDS = 30
_MAX_HTTP_REDIRECTS = 5
_HTTP_CHUNK_BYTES = 64 * 1024
_MAX_SEARCH_RESULTS = 10
_MAX_SEARCH_QUERY_CHARACTERS = 500
_MAX_SEARCH_RESPONSE_BYTES = 512 * 1024

MATERIALS_PREFIX = "materials/"
"""How a path names the read-only input files a task was given."""

OUTPUT_PREFIX = "output/"
"""How a path names the directory a task's deliverables are left in."""


class ToolFailureError(Exception):
    """A tool could not do what it was asked, for a reason worth reporting."""


@dataclass(frozen=True)
class ToolDefinition:
    """One capability, as it is published to a model."""

    name: str
    description: str
    input_schema: dict[str, Any]


class WorkspaceTools:
    """Reads, writes and runs things, all inside one workspace.

    Two further trees may sit beside it. ``materials/`` is what a task handed
    in: readable, listed, never written. ``output/`` is where a task's
    deliverables go: written, listed, collected afterwards. Both are named by a
    path prefix, so the model has one set of tools and three places to point
    them at, and the containment story is the same for each — a path is
    resolved against the tree its prefix names and refused if it leaves it.
    """

    def __init__(
        self,
        workspace: Workspace,
        commands: CommandRunner,
        *,
        materials: Workspace | None = None,
        output: Workspace | None = None,
    ) -> None:
        """Hold the trees the tools work in and the runner they execute through.

        Args:
            workspace: The contained directory every path is resolved against.
            commands: How a command is run, bounded and cleaned.
            materials: The read-only inputs a task was given, if any.
            output: Where a task's deliverables are left, if anywhere.

        """
        self._workspace = workspace
        self._commands = commands
        self._materials = materials
        self._output = output

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return every tool this agent publishes, in a stable order."""
        return (
            ToolDefinition(
                name="list_files",
                description=(
                    "List every file in the workspace with its size in bytes, "
                    "followed by the task's input files under materials/ and "
                    "the deliverables written so far under output/. Takes no "
                    "arguments."
                ),
                input_schema={"type": "object", "properties": {}, "required": []},
            ),
            ToolDefinition(
                name="read_file",
                description=(
                    "Read one file as text. Paths are relative to the workspace; "
                    "a path starting with materials/ reads one of the task's "
                    "input files, and one starting with output/ reads a "
                    "deliverable. Large files are truncated."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace root.",
                        }
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="write_file",
                description=(
                    "Write one file, replacing it if it exists and creating "
                    "the directories it needs. The content is the whole new "
                    "file, not a patch. A path starting with output/ writes a "
                    "deliverable the task asked for; materials/ is read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace root.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The complete new content of the file.",
                        },
                    },
                    "required": ["path", "content"],
                },
            ),
            ToolDefinition(
                name="read_document",
                description=(
                    "Extract headers, bodies, and supported attachments from RFC 822 (.eml) or Outlook (.msg) email; text, tables, speaker notes, and OCR text from scanned PDFs or PNG/JPEG/TIFF/WebP/BMP images; and text from PDF, Word DOCX, Excel XLSX, PowerPoint PPTX, or EPUB "
                    "file. Paths may be under materials/ or output/. Output is bounded; "
                    "use max_characters to request a smaller excerpt."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Document path relative to the workspace root."},
                        "max_characters": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_TEXT_CHARACTERS,
                            "description": "Maximum extracted characters (default 120000).",
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="inspect_geodata",
                description=(
                    "Inspect GeoJSON (.geojson, .json) or ESRI Shapefile (.shp) "
                    "vector data. Returns bounded JSON with feature count, geometry "
                    "types, fields, coordinate bounds, and property samples."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Geodata path relative to the workspace root."},
                        "max_features": {"type": "integer", "minimum": 1, "maximum": MAX_SUMMARY_FEATURES, "description": "Maximum sample features (default 100)."},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="convert_to_geojson",
                description=(
                    "Convert a GeoJSON or ESRI Shapefile vector dataset to a GeoJSON "
                    "FeatureCollection. Destination must be a .geojson or .json file "
                    "in the workspace or output/."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "source_path": {"type": "string", "description": "GeoJSON or .shp source path."},
                        "destination": {"type": "string", "description": "Writable .geojson or .json destination path."},
                        "max_features": {"type": "integer", "minimum": 1, "maximum": 20000, "description": "Maximum features to convert (default 20000)."},
                    },
                    "required": ["source_path", "destination"],
                },
            ),
            ToolDefinition(
                name="query_sqlite",
                description=(
                    "Run a read-only SELECT, WITH, or EXPLAIN query against a SQLite "
                    "database (.db, .sqlite, .sqlite3). Paths may be under materials/ "
                    "or output/. Results are bounded and returned as TSV."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "SQLite database path relative to the workspace root."},
                        "query": {"type": "string", "description": "One read-only SQL query."},
                        "max_rows": {"type": "integer", "minimum": 1, "maximum": MAX_QUERY_ROWS, "description": "Maximum result rows (default 200)."},
                    },
                    "required": ["path", "query"],
                },
            ),
            ToolDefinition(
                name="extract_archive",
                description=(
                    "Extract regular files from a ZIP or TAR-family archive into "
                    "a directory in the workspace or output/. The archive may be "
                    "read from the workspace, materials/, or output/. Extraction is "
                    "bounded and rejects links and member paths that could escape the "
                    "destination. Omit members to extract every regular file."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "archive_path": {
                            "type": "string",
                            "description": "ZIP or TAR archive to read.",
                        },
                        "destination": {
                            "type": "string",
                            "description": "Directory under the workspace or output/ to receive files.",
                        },
                        "members": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional exact archive member names to extract.",
                        },
                    },
                    "required": ["archive_path", "destination"],
                },
            ),
            ToolDefinition(
                name="web_search",
                description=(
                    "Search the live web for current sources when a task gives no URL. "
                    "Returns normalized result titles, destination URLs, and snippets; "
                    "use http_fetch to read a selected source."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms or a research question."},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": _MAX_SEARCH_RESULTS, "description": "Maximum results (default 5)."},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": _MAX_HTTP_TIMEOUT_SECONDS, "description": "Network timeout (default 20 seconds)."},
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name="http_fetch",
                description=(
                    "Fetch a live HTTP or HTTPS URL with a bounded GET request. "
                    "Returns the final URL, status, response headers, and decoded text "
                    "or JSON body. Supports optional request headers, redirects, and limits."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "HTTP or HTTPS URL to fetch."},
                        "headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Optional request headers, for example Accept."
                        },
                        "timeout_seconds": {"type": "integer", "description": "Network timeout, from 1 to 30 seconds."},
                        "max_bytes": {"type": "integer", "description": "Maximum response bytes to read, from 1 to 2097152."}
                    },
                    "required": ["url"],
                },
            ),
            ToolDefinition(
                name="delete_path",
                description="Remove one file or directory from the workspace.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace root.",
                        }
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="run_command",
                description=(
                    "Run one command in the workspace and return its exit "
                    "code and output. There is no shell: give the program and "
                    'its arguments as a list, e.g. ["python", "-m", '
                    '"unittest", "discover"]. The container has no network '
                    "Command networking depends on the deployment; use http_fetch for bounded HTTP or HTTPS requests."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The program and its arguments.",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": (
                                "How long the command may run before it is stopped."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            ),
        )

    def call(self, name: str, arguments: Mapping[str, Any]) -> str:
        """Perform one published tool call and return what it produced.

        Args:
            name: Name of the tool, as published.
            arguments: The arguments the caller supplied.

        Returns:
            What the tool produced, as the text a model reads.

        Raises:
            ToolFailureError: If the tool is unknown, an argument is missing
                or of the wrong shape, or the operation itself was refused.

        """
        handlers = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "read_document": self._read_document,
            "inspect_geodata": self._inspect_geodata,
            "convert_to_geojson": self._convert_to_geojson,
            "query_sqlite": self._query_sqlite,
            "extract_archive": self._extract_archive,
            "web_search": self._web_search,
            "http_fetch": self._http_fetch,
            "delete_path": self._delete_path,
            "run_command": self._run_command,
        }
        handler = handlers.get(name)
        if handler is None:
            published = ", ".join(sorted(handlers))
            raise ToolFailureError(
                f"There is no tool called {name!r}. Tools: {published}."
            )
        try:
            return handler(arguments)
        except WorkspaceError as refused:
            raise ToolFailureError(str(refused)) from refused

    def _list_files(self, arguments: Mapping[str, Any]) -> str:
        """Return every tree's files, one per line, with their sizes."""
        del arguments
        lines = [
            f"{entry.relative_path} ({entry.byte_size} bytes)"
            for entry in self._workspace.entries()
        ]
        for prefix, tree in (
            (MATERIALS_PREFIX, self._materials),
            (OUTPUT_PREFIX, self._output),
        ):
            if tree is None:
                continue
            lines.extend(
                f"{prefix}{entry.relative_path} ({entry.byte_size} bytes)"
                for entry in tree.entries()
            )
        if not lines:
            return "The workspace is empty."
        listing = "\n".join(lines)
        if len(listing) <= _MAX_LISTING_CHARACTERS:
            return listing
        return (
            f"{listing[:_MAX_LISTING_CHARACTERS]}\n"
            f"... [truncated at {_MAX_LISTING_CHARACTERS} characters]"
        )

    def _read_file(self, arguments: Mapping[str, Any]) -> str:
        """Return one file's text, from whichever tree the path names."""
        tree, relative = self._located(_text_argument(arguments, "path"))
        return tree.read_text(relative)

    def _write_file(self, arguments: Mapping[str, Any]) -> str:
        """Write one whole file and report what was written."""
        path = _text_argument(arguments, "path")
        tree, relative = self._located(path, writing=True)
        written = tree.write_text(
            relative, _text_argument(arguments, "content", allow_empty=True)
        )
        return f"Wrote {written} bytes to {path}."

    def _read_document(self, arguments: Mapping[str, Any]) -> str:
        """Extract bounded readable content from a supported office document."""
        path = _text_argument(arguments, "path")
        tree, relative = self._located(path)
        maximum = arguments.get("max_characters", MAX_TEXT_CHARACTERS)
        # Keep argument diagnostics consistent with the rest of this tool surface.
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise ToolFailureError("max_characters must be an integer.")
        try:
            return read_document(tree.resolve(relative), max_characters=maximum)
        except DocumentError as unreadable:
            raise ToolFailureError(str(unreadable)) from unreadable

    def _inspect_geodata(self, arguments: Mapping[str, Any]) -> str:
        """Summarize a vector dataset without asking the model to parse coordinates."""
        path = _text_argument(arguments, "path")
        tree, relative = self._located(path)
        maximum = arguments.get("max_features", 100)
        try:
            return inspect_geodata(tree.resolve(relative), max_features=maximum)
        except GeodataError as unreadable:
            raise ToolFailureError(str(unreadable)) from unreadable

    def _convert_to_geojson(self, arguments: Mapping[str, Any]) -> str:
        """Write a portable GeoJSON FeatureCollection from a vector source."""
        source_path = _text_argument(arguments, "source_path")
        source_tree, source_relative = self._located(source_path)
        destination_path = _text_argument(arguments, "destination")
        destination_tree, destination_relative = self._located(destination_path, writing=True)
        maximum = arguments.get("max_features", 20_000)
        try:
            count = convert_to_geojson(
                source_tree.resolve(source_relative),
                destination_tree.resolve(destination_relative),
                max_features=maximum,
            )
        except GeodataError as unusable:
            raise ToolFailureError(str(unusable)) from unusable
        return f"Converted {count} features from {source_path} to {destination_path}."

    def _query_sqlite(self, arguments: Mapping[str, Any]) -> str:
        """Run a bounded read-only query against a task SQLite database."""
        path = _text_argument(arguments, "path")
        query = _text_argument(arguments, "query")
        tree, relative = self._located(path)
        maximum = arguments.get("max_rows", 200)
        try:
            return query_sqlite(tree.resolve(relative), query, max_rows=maximum)
        except DatabaseError as unreadable:
            raise ToolFailureError(str(unreadable)) from unreadable

    def _extract_archive(self, arguments: Mapping[str, Any]) -> str:
        """Extract selected safe regular files from one archive."""
        archive_path = _text_argument(arguments, "archive_path")
        source, source_relative = self._located(archive_path)
        destination_path = _text_argument(arguments, "destination")
        destination, destination_relative = self._located(destination_path, writing=True)
        selected = _members_argument(arguments)
        try:
            archive = source.resolve(source_relative)
            if not archive.is_file():
                raise WorkspaceError(f"{archive_path!r} is not an archive file.")
            if archive.stat().st_size > _MAX_ARCHIVE_BYTES:
                raise WorkspaceError(
                    f"{archive_path!r} exceeds the {_MAX_ARCHIVE_BYTES}-byte archive limit."
                )
            destination_root = destination.resolve(destination_relative)
            destination_root.mkdir(parents=True, exist_ok=True)
            if not destination_root.is_dir():
                raise WorkspaceError(f"{destination_path!r} is not a directory.")
            if zipfile.is_zipfile(archive):
                count, total = _extract_zip(archive, destination, destination_relative, selected)
            else:
                count, total = _extract_tar(archive, destination, destination_relative, selected)
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as unusable:
            raise ToolFailureError(f"Could not extract {archive_path!r}: {unusable}") from unusable
        return f"Extracted {count} files ({total} bytes) to {destination_path}."

    def _web_search(self, arguments: Mapping[str, Any]) -> str:
        """Discover public web sources through DuckDuckGo's lightweight HTML page.

        Search responses are deliberately reduced to untrusted factual leads rather
        than presented as an answer.  This keeps research iterative: the model can
        select a result and inspect its primary source with ``http_fetch``.
        """
        query = _search_query_argument(arguments)
        maximum = _http_bound_argument(arguments, "max_results", _MAX_SEARCH_RESULTS, 5)
        timeout = _http_bound_argument(arguments, "timeout_seconds", _MAX_HTTP_TIMEOUT_SECONDS, 20)
        url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
        request = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; evolving-agent/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip, deflate",
        })
        try:
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                body = _read_http_body(response, _MAX_SEARCH_RESPONSE_BYTES)
                page = _http_body_text(
                    _decode_http_body(body, response.headers, _MAX_SEARCH_RESPONSE_BYTES),
                    response.headers,
                )
        except HTTPError as error:
            raise ToolFailureError(f"Search service returned HTTP {error.code}; try a narrower query or a direct URL.") from error
        except (URLError, OSError, ValueError) as unavailable:
            raise ToolFailureError(f"Could not search the web: {unavailable}") from unavailable
        results = _DuckDuckGoResults(page).results()
        if not results:
            return f"No web results found for {query!r}. Try different terms or use a direct URL."
        lines = [f"Web results for {query!r}:"]
        for number, result in enumerate(results[:maximum], start=1):
            lines.extend((f"{number}. {result.title}", f"   URL: {result.url}", f"   {result.snippet}" if result.snippet else ""))
        return "\n".join(line for line in lines if line)

    def _http_fetch(self, arguments: Mapping[str, Any]) -> str:
        """Fetch a web resource without delegating networking to a shell command."""
        url = _http_url_argument(arguments)
        headers = _http_headers_argument(arguments)
        timeout = _http_bound_argument(arguments, "timeout_seconds", _MAX_HTTP_TIMEOUT_SECONDS, 20)
        maximum = _http_bound_argument(arguments, "max_bytes", _MAX_HTTP_RESPONSE_BYTES, _MAX_HTTP_RESPONSE_BYTES)
        opener = build_opener(_NoRedirect())
        for _ in range(_MAX_HTTP_REDIRECTS + 1):
            request = Request(
                url,
                headers={"User-Agent": "evolving-agent/1.0", "Accept-Encoding": "gzip, deflate", **headers},
            )
            try:
                response = opener.open(request, timeout=timeout)
            except HTTPError as error:
                if error.code not in (301, 302, 303, 307, 308):
                    body = _read_http_body(error, maximum)
                    return _format_http_response(
                        error.geturl(), error.code, error.headers, _decode_http_body(body, error.headers, maximum)
                    )
                location = error.headers.get("Location")
                if not location:
                    raise ToolFailureError(f"HTTP {error.code} response has no Location header.") from error
                url = _http_url(urljoin(url, location))
                continue
            except (URLError, OSError, ValueError) as unavailable:
                raise ToolFailureError(f"Could not fetch {url!r}: {unavailable}") from unavailable
            with response:
                body = _read_http_body(response, maximum)
                return _format_http_response(
                    response.geturl(), response.status, response.headers,
                    _decode_http_body(body, response.headers, maximum),
                )
        raise ToolFailureError(f"Too many redirects (maximum {_MAX_HTTP_REDIRECTS}) while fetching {url!r}.")

    def _delete_path(self, arguments: Mapping[str, Any]) -> str:
        """Remove one path and report that it is gone."""
        path = _text_argument(arguments, "path")
        tree, relative = self._located(path, writing=True)
        tree.delete(relative)
        return f"Removed {path}."

    def _located(self, path: str, *, writing: bool = False) -> tuple[Workspace, str]:
        """Return the tree one path names and the path relative to it.

        Raises:
            WorkspaceError: If the path names a tree this run was not given,
                or asks to write into the read-only materials.

        """
        stripped = path.strip()
        if stripped.startswith(MATERIALS_PREFIX):
            if writing:
                raise WorkspaceError(
                    f"{stripped!r} is under materials/, which is read-only: the "
                    "task's input files cannot be changed."
                )
            if self._materials is None:
                raise WorkspaceError(
                    f"{stripped!r} names materials/, but this run was given no "
                    "input files."
                )
            return self._materials, stripped[len(MATERIALS_PREFIX) :]
        if stripped.startswith(OUTPUT_PREFIX):
            if self._output is None:
                raise WorkspaceError(
                    f"{stripped!r} names output/, but this run has nowhere to "
                    "leave deliverables."
                )
            return self._output, stripped[len(OUTPUT_PREFIX) :]
        return self._workspace, stripped

    def _run_command(self, arguments: Mapping[str, Any]) -> str:
        """Run one command and report how it ended and what it printed.

        Raises:
            ToolFailureError: If the command is not a list of strings, or the
                timeout is not a whole number.

        """
        command = _command_argument(arguments)
        try:
            result = self._commands.run(
                command, timeout_seconds=_timeout_argument(arguments)
            )
        except (TypeError, ValueError) as unusable:
            raise ToolFailureError(str(unusable)) from unusable
        status = (
            "timed out"
            if result.timed_out
            else f"exit code {result.exit_code}"
            if result.exit_code is not None
            else "did not start"
        )
        return "\n".join(
            (
                f"$ {' '.join(result.command)}",
                f"[{status}]",
                f"--- stdout ---\n{result.stdout}",
                f"--- stderr ---\n{result.stderr}",
            )
        )


class _NoRedirect(HTTPRedirectHandler):
    """Expose redirects to the fetcher so every destination is revalidated."""

    def redirect_request(self, request: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        del request, fp, code, msg, headers, newurl
        return None


def _search_query_argument(arguments: Mapping[str, Any]) -> str:
    """Return a short nonempty query that cannot turn one call into a bulk crawl."""
    query = _text_argument(arguments, "query").strip()
    if len(query) > _MAX_SEARCH_QUERY_CHARACTERS:
        raise ToolFailureError(
            f"'query' must be at most {_MAX_SEARCH_QUERY_CHARACTERS} characters."
        )
    return query


@dataclass
class _SearchResult:
    """One normalized external lead returned by the HTML search service."""

    title: str
    url: str
    snippet: str = ""


class _DuckDuckGoResults(HTMLParser):
    """Extract result anchors and snippets without trusting search-page markup."""

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self._found: list[_SearchResult] = []
        self._capture: str | None = None
        self._depth = 0
        self._parts: list[str] = []
        self.feed(page)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            target = _search_destination(attributes.get("href") or "")
            if target:
                self._capture, self._depth, self._parts = target, 1, []
            return
        if self._capture is not None:
            self._depth += 1
            return
        if ("result__snippet" in classes or "result-snippet" in classes) and self._found:
            self._capture, self._depth, self._parts = "snippet", 1, []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag, attrs

    def handle_endtag(self, tag: str) -> None:
        del tag
        if self._capture is None:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        text = " ".join("".join(self._parts).split())
        if self._capture == "snippet":
            self._found[-1].snippet = text
        elif text:
            self._found.append(_SearchResult(title=text, url=self._capture))
        self._capture, self._parts = None, []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._parts.append(data)

    def results(self) -> tuple[_SearchResult, ...]:
        return tuple(self._found)


def _search_destination(href: str) -> str | None:
    """Unwrap DuckDuckGo's redirect URL and retain only usable HTTP targets."""
    candidate = href.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlsplit(candidate)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        candidate = parse_qs(parsed.query).get("uddg", [""])[0]
    try:
        return _http_url(candidate)
    except ToolFailureError:
        return None


def _http_url_argument(arguments: Mapping[str, Any]) -> str:
    """Return a syntactically usable web URL."""
    return _http_url(_text_argument(arguments, "url"))


def _http_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ToolFailureError("'url' must be an absolute HTTP or HTTPS URL without embedded credentials.")
    return value.strip()


def _http_headers_argument(arguments: Mapping[str, Any]) -> dict[str, str]:
    """Return bounded optional request headers."""
    value = arguments.get("headers", {})
    if not isinstance(value, Mapping) or len(value) > 20:
        raise ToolFailureError("'headers' must be an object with at most 20 string headers.")
    headers: dict[str, str] = {}
    for name, header_value in value.items():
        if not isinstance(name, str) or not isinstance(header_value, str) or not name or len(name) > 128 or len(header_value) > 4096 or "\r" in name + header_value or "\n" in name + header_value:
            raise ToolFailureError("Every request header must be a short single-line string.")
        headers[name] = header_value
    return headers


def _http_bound_argument(arguments: Mapping[str, Any], name: str, ceiling: int, default: int) -> int:
    """Read one positive bounded HTTP integer option."""
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
        raise ToolFailureError(f"{name!r} must be a whole number from 1 to {ceiling}.")
    return value


def _read_http_body(response: Any, maximum: int) -> bytes:
    """Read at most the requested response budget, refusing an oversized body."""
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > maximum:
        raise ToolFailureError(f"Response declares {declared} bytes, above the {maximum}-byte limit.")
    body = bytearray()
    while chunk := response.read(min(_HTTP_CHUNK_BYTES, maximum - len(body) + 1)):
        body.extend(chunk)
        if len(body) > maximum:
            raise ToolFailureError(f"Response exceeds the {maximum}-byte limit.")
    return bytes(body)


def _decode_http_body(body: bytes, headers: Any, maximum: int) -> bytes:
    """Decode supported HTTP content encodings without permitting a zip bomb."""
    encoding = headers.get("Content-Encoding", "").strip().lower()
    if not encoding or encoding == "identity":
        return body
    if encoding == "gzip":
        return _bounded_zlib_decode(body, 16 + zlib.MAX_WBITS, maximum)
    if encoding == "deflate":
        # Servers disagree over whether deflate means a zlib wrapper or raw DEFLATE.
        try:
            return _bounded_zlib_decode(body, zlib.MAX_WBITS, maximum)
        except zlib.error:
            return _bounded_zlib_decode(body, -zlib.MAX_WBITS, maximum)
    raise ToolFailureError(
        f"Response uses unsupported Content-Encoding {encoding!r}; request identity encoding."
    )


def _bounded_zlib_decode(body: bytes, wbits: int, maximum: int) -> bytes:
    """Inflate incrementally, refusing decoded data beyond the tool's budget."""
    decoder = zlib.decompressobj(wbits)
    output = bytearray()
    remaining = body
    while remaining:
        chunk = decoder.decompress(remaining, maximum - len(output) + 1)
        output.extend(chunk)
        if len(output) > maximum:
            raise ToolFailureError(f"Decoded response exceeds the {maximum}-byte limit.")
        remaining = decoder.unconsumed_tail
    output.extend(decoder.flush(maximum - len(output) + 1))
    if len(output) > maximum:
        raise ToolFailureError(f"Decoded response exceeds the {maximum}-byte limit.")
    if not decoder.eof:
        raise ToolFailureError("Compressed response ended before its stream was complete.")
    return bytes(output)


def _http_body_text(body: bytes, headers: Any) -> str:
    """Decode an HTTP entity body using its declared charset when usable."""
    charset = headers.get_content_charset() if hasattr(headers, "get_content_charset") else None
    try:
        return body.decode(charset or "utf-8")
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


def _format_http_response(url: str, status: int, headers: Any, body: bytes) -> str:
    """Render a bounded fetched response as useful model-readable text."""
    content_type = headers.get("Content-Type", "")
    text = _http_body_text(body, headers)
    shown_headers = "\n".join(f"{name}: {value}" for name, value in list(headers.items())[:20])
    return f"URL: {url}\nStatus: {status}\nContent-Type: {content_type}\nHeaders:\n{shown_headers}\n\n{text}"


def _text_argument(
    arguments: Mapping[str, Any], name: str, *, allow_empty: bool = False
) -> str:
    """Return one string argument.

    Raises:
        ToolFailureError: If the argument is absent, not a string, or blank when a
            value is required.

    """
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolFailureError(f"{name!r} is required and must be a string.")
    if not allow_empty and not value.strip():
        raise ToolFailureError(f"{name!r} must not be blank.")
    return value


def _command_argument(arguments: Mapping[str, Any]) -> Sequence[str]:
    """Return the argument vector a command call named.

    Raises:
        ToolFailureError: If it is not a non-empty list of strings.

    """
    value = arguments.get("command")
    if isinstance(value, str) or not isinstance(value, list):
        raise ToolFailureError(
            "'command' must be a list of strings, e.g. ['python', '--version']."
        )
    if not value:
        raise ToolFailureError("'command' needs at least the program to run.")
    if not all(isinstance(argument, str) for argument in value):
        raise ToolFailureError("Every element of 'command' must be a string.")
    return [str(argument) for argument in value]


def _timeout_argument(arguments: Mapping[str, Any]) -> int | None:
    """Return the bound a command call asked for, if it asked for one.

    Raises:
        ToolFailureError: If the bound is not a positive whole number.

    """
    value = arguments.get("timeout_seconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolFailureError("'timeout_seconds' must be a whole number of seconds.")
    if value <= 0:
        raise ToolFailureError("'timeout_seconds' must be greater than zero.")
    return int(value)


def _members_argument(arguments: Mapping[str, Any]) -> frozenset[str] | None:
    """Return optional exact archive member names after validating their shape."""
    value = arguments.get("members")
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ToolFailureError("'members' must be an array of non-blank member names.")
    return frozenset(value)


def _safe_member_path(member_name: str) -> str:
    """Return a member name that is safe to place below an extraction root."""
    if not member_name or "\\" in member_name:
        raise WorkspaceError(f"Archive member {member_name!r} has an unsafe path.")
    # Workspace.resolve is also applied at write time, but reject traversal here
    # before a partially extracted archive can be left behind.
    if member_name.startswith("/") or ".." in member_name.split("/"):
        raise WorkspaceError(f"Archive member {member_name!r} has an unsafe path.")
    return member_name


class _CountingReader:
    """File-like wrapper which records compressed bytes consumed by tarfile."""

    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self.count += len(data)
        return data

    def __getattr__(self, name: str) -> object:
        return getattr(self._raw, name)


def _validate_zip_member_compression(info: zipfile.ZipInfo, name: str) -> None:
    """Refuse a ZIP entry whose declared expansion is bomb-like."""
    if info.file_size and info.compress_size == 0:
        raise WorkspaceError(f"Archive member {name!r} has no compressed data.")
    _validate_stream_compression(info.file_size, info.compress_size)


def _validate_stream_compression(expanded: int, compressed: int) -> None:
    """Keep decompression work proportionate to input bytes.

    A small floor avoids treating ZIP/TAR headers as a hostile ratio, while
    large repetitive payloads cannot consume substantial resources from a
    tiny archive.
    """
    allowed = max(
        _MIN_ARCHIVE_RATIO_INPUT_BYTES * _MAX_ARCHIVE_COMPRESSION_RATIO,
        compressed * _MAX_ARCHIVE_COMPRESSION_RATIO,
    )
    if expanded > allowed:
        raise WorkspaceError(
            f"Archive exceeds the {_MAX_ARCHIVE_COMPRESSION_RATIO}:1 compression-ratio limit."
        )


def _extract_zip(
    archive: object, destination: Workspace, destination_relative: str,
    selected: frozenset[str] | None,
) -> tuple[int, int]:
    """Extract safe selected ZIP entries, enforcing declared and streamed limits."""
    count = total = 0
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > _MAX_ARCHIVE_MEMBERS:
            raise WorkspaceError(f"Archive exceeds the {_MAX_ARCHIVE_MEMBERS}-member limit.")
        for info in entries:
            name = _safe_member_path(info.filename)
            if selected is not None and name not in selected:
                continue
            if info.is_dir():
                continue
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise WorkspaceError(f"Archive member {name!r} is a link and cannot be extracted.")
            _validate_zip_member_compression(info, name)
            count, total = _copy_archive_member(
                destination, destination_relative, name, zipped.open(info), info.file_size, count, total
            )
    return count, total


def _extract_tar(
    archive: object, destination: Workspace, destination_relative: str,
    selected: frozenset[str] | None,
) -> tuple[int, int]:
    """Extract safe selected TAR entries, including compressed TAR variants."""
    count = total = 0
    # Tar compression wraps the whole stream, so it has no trustworthy
    # per-member compressed-size field. Count bytes drawn from that outer
    # stream while extracting and enforce the same expansion ratio.
    with open(archive, "rb") as raw:
        compressed = _CountingReader(raw)
        with tarfile.open(fileobj=compressed, mode="r|*") as tarred:
            for seen, info in enumerate(tarred, start=1):
                if seen > _MAX_ARCHIVE_MEMBERS:
                    raise WorkspaceError(f"Archive exceeds the {_MAX_ARCHIVE_MEMBERS}-member limit.")
                name = _safe_member_path(info.name)
                if selected is not None and name not in selected:
                    continue
                if info.isdir():
                    continue
                if not info.isreg():
                    raise WorkspaceError(f"Archive member {name!r} is not a regular file.")
                reader = tarred.extractfile(info)
                if reader is None:
                    raise WorkspaceError(f"Archive member {name!r} could not be read.")
                count, total = _copy_archive_member(
                    destination, destination_relative, name, reader, info.size, count, total,
                    compressed_bytes=compressed,
                )
    return count, total


def _copy_archive_member(
    destination: Workspace, destination_relative: str, member_name: str,
    reader: BinaryIO, expected_size: int, count: int, total: int,
    *, compressed_bytes: "_CountingReader | None" = None,
) -> tuple[int, int]:
    """Copy a bounded member after resolving its target beneath the destination."""
    if count >= _MAX_ARCHIVE_MEMBERS:
        raise WorkspaceError(f"Archive exceeds the {_MAX_ARCHIVE_MEMBERS}-file extraction limit.")
    if expected_size > _MAX_ARCHIVE_MEMBER_BYTES or total + expected_size > _MAX_ARCHIVE_EXTRACTED_BYTES:
        raise WorkspaceError("Archive exceeds the permitted extracted-data limit.")
    relative = f"{destination_relative.rstrip('/')}/{member_name}"
    target = destination.resolve(relative)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating parents, so an existing symlink cannot turn
        # a member path into a write outside the chosen tree.
        target = destination.resolve(relative)
        written = 0
        with reader, target.open("wb") as output:
            while chunk := reader.read(_ARCHIVE_CHUNK_BYTES):
                written += len(chunk)
                if written > _MAX_ARCHIVE_MEMBER_BYTES or total + written > _MAX_ARCHIVE_EXTRACTED_BYTES:
                    raise WorkspaceError("Archive exceeds the permitted extracted-data limit.")
                if compressed_bytes is not None:
                    _validate_stream_compression(total + written, compressed_bytes.count)
                output.write(chunk)
    except OSError as unwritable:
        raise WorkspaceError(f"Archive member {member_name!r} could not be written: {unwritable}") from unwritable
    return count + 1, total + written
