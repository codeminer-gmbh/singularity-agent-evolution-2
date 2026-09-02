"""Bounded, line-oriented discovery of text evidence across a contained tree."""

from __future__ import annotations

from dataclasses import dataclass

from evolving_agent.workspace import Workspace, WorkspaceError

_MAX_FILE_BYTES = 256_000
_MAX_TOTAL_BYTES = 8_000_000
_MAX_LINE_CHARACTERS = 360


@dataclass(frozen=True)
class TextMatch:
    """One readable occurrence found while scanning a tree."""

    path: str
    line: int
    text: str


@dataclass(frozen=True)
class SearchReport:
    """The bounded evidence and coverage information from one tree search."""

    matches: tuple[TextMatch, ...]
    files_scanned: int
    bytes_scanned: int
    skipped_binary: int
    truncated_files: int
    budget_exhausted: bool


def search_text(
    workspace: Workspace, query: str, *, case_sensitive: bool, max_results: int
) -> SearchReport:
    """Find literal query occurrences in readable text files without following links.

    Search is deliberately literal rather than a regular-expression evaluator: task
    evidence may be arbitrary text, and a bounded discovery operation must not let a
    pathological pattern consume its entire turn.  File and total byte limits make
    the coverage reported below meaningful even for an unexpectedly large bundle.
    """
    needle = query if case_sensitive else query.lower()
    matches: list[TextMatch] = []
    files_scanned = bytes_scanned = skipped_binary = truncated_files = 0
    budget_exhausted = False
    for entry in workspace.entries():
        remaining = _MAX_TOTAL_BYTES - bytes_scanned
        if remaining <= 0:
            budget_exhausted = True
            break
        path = workspace.resolve(entry.relative_path)
        try:
            with path.open("rb") as source:
                raw = source.read(min(_MAX_FILE_BYTES, remaining))
        except OSError as error:
            raise WorkspaceError(f"{entry.relative_path!r} could not be read: {error}") from error
        # Account for every byte read, including binary probes: otherwise a bundle
        # of binary files could silently evade the advertised total scan limit.
        bytes_scanned += len(raw)
        if b"\0" in raw:
            skipped_binary += 1
            continue
        files_scanned += 1
        if entry.byte_size > len(raw):
            truncated_files += 1
        text = raw.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            haystack = line if case_sensitive else line.lower()
            match_start = haystack.find(needle)
            if match_start >= 0:
                matches.append(TextMatch(entry.relative_path, line_number, _snippet(line, match_start, len(query))))
                if len(matches) >= max_results:
                    return SearchReport(tuple(matches), files_scanned, bytes_scanned, skipped_binary, truncated_files, False)
    return SearchReport(tuple(matches), files_scanned, bytes_scanned, skipped_binary, truncated_files, budget_exhausted)


def _snippet(line: str, match_start: int, query_length: int) -> str:
    """Return bounded context which still contains the discovered occurrence."""
    if len(line) <= _MAX_LINE_CHARACTERS:
        return line
    # Center the matching text where possible.  A prefix-only preview can claim a
    # match while hiding it entirely when it occurs near the end of a long row.
    focus_width = min(max(query_length, 1), _MAX_LINE_CHARACTERS)
    start = max(0, match_start - (_MAX_LINE_CHARACTERS - focus_width) // 2)
    end = min(len(line), start + _MAX_LINE_CHARACTERS)
    start = max(0, end - _MAX_LINE_CHARACTERS)
    prefix = "… " if start else ""
    suffix = " … [line truncated]" if end < len(line) else ""
    return prefix + line[start:end] + suffix
