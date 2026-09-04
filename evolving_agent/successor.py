"""Whether what an improvement run left behind is a usable next iteration.

The orchestrator packages whatever the workspace holds and builds it. Nothing
between here and that build reads the tree for the model, so a syntax error
costs a whole cycle and fails a smoke test nobody learns anything from. The
checks below are the ones the tree will face later, applied while there is still
budget to fix them: it holds what a container needs, its Python parses, the
notes it leaves for its successor do not claim what the tree cannot show, and
the tests it ships pass. The last of these is the one a model most readily
reports as done when it is not: the first live round of this seed shipped a
record saying every gate was green beside a suite with one red test.

What the successor *does* is not judged here; that is what the experiment's
tests and evaluators are for.
"""

import ast
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Mapping

from evolving_agent.workspace import Workspace, WorkspaceError

REQUIRED_FILES: tuple[str, ...] = ("Dockerfile", "main.py")
"""What every iteration's tree carries, whatever else it holds.

The external runtime requires exactly these two names, and the container it
starts runs the entrypoint the first one names.
"""

NOTES_DIRECTORY = "memories/"
"""Where notes for the next iteration live."""

_UNGATED_NOTES = frozenset({"memories/README.md", "memories/round-plan.md"})
"""Notes that describe the directory or the round rather than the code."""

_NOTE_SUFFIX = ".md"

_MAX_SCANNED_BYTES = 1_000_000
_PYTHON_SUFFIX = ".py"

TESTS_DIRECTORY = "tests"
"""Where the tree keeps the tests a successor is held to."""

_TEST_TIMEOUT_SECONDS = 600
_NO_TESTS_COLLECTED = 5
"""pytest's exit status when the directory exists but holds no test."""
_REPORTED_TEST_LINES = 8

_VERIFICATION_CLAIM = re.compile(
    r"\b(?:tested|verified|verifies|verify|verification|passed|exercised|ran)\b"
    r"|\b(?:smoke|integration|end-to-end|e2e)[- ]tests?\b"
    r"|\bcommand results?\b",
    re.IGNORECASE,
)
"""The words with which a note claims that something was run and came out well."""

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_DURABLE_EVIDENCE_PARTS = frozenset({"test", "tests", "fixture", "fixtures"})


def successor_problems(workspace: Workspace) -> tuple[str, ...]:
    """Return everything that stops this tree being a usable next iteration.

    Nothing here raises. A tree the workspace itself refuses to read is a
    problem to report like any other: an improvement run that ended in an
    exception would skip both the repair it could still have made and the
    restore that keeps the cycle honest.

    Args:
        workspace: The tree an improvement run left behind.

    Returns:
        One sentence per problem, in a stable order; empty when the tree can be
        published, built and started.

    """
    entries = workspace.entries()
    if not entries:
        return ("the workspace is empty; a successor needs a complete source tree",)
    present = {entry.relative_path: entry.byte_size for entry in entries}
    problems = [
        f"{required} is missing from the workspace root"
        for required in REQUIRED_FILES
        if required not in present
    ]
    problems.extend(
        f"{required} is empty" for required in REQUIRED_FILES if present.get(required) == 0
    )
    problems.extend(_unparsable(workspace, path) for path in present)
    return tuple(problem for problem in problems if problem)


def note_snapshot(workspace: Workspace) -> dict[str, str]:
    """Fingerprint the notes as they stand before an improvement run changes them.

    Only notes the run adds or changes are held to the evidence rule, so an
    inherited note that was already audited cannot stop an otherwise usable
    successor from being published.

    Args:
        workspace: The tree before the session starts.

    Returns:
        A content digest per gated note.

    """
    snapshot: dict[str, str] = {}
    for entry in workspace.entries():
        if not _is_gated_note(entry.relative_path):
            continue
        content = _read(workspace, entry.relative_path)
        if content is not None:
            snapshot[entry.relative_path] = hashlib.sha256(content).hexdigest()
    return snapshot


def note_problems(workspace: Workspace, baseline: Mapping[str, str]) -> tuple[str, ...]:
    """Return the notes that claim a verification the tree does not carry.

    A note may say that something was tested or verified only when it names, in
    a code span, a test or fixture that exists in the tree it is shipped with.
    A claim about a command that merely ran belongs in the run's final reply,
    which is recorded separately; a note is inherited as fact by every later
    version, and the second experiment's audit found forty-eight of them
    describing checks that never happened.

    Args:
        workspace: The tree the run is about to leave behind.
        baseline: What :func:`note_snapshot` returned before the run.

    Returns:
        One sentence per offending note, in path order.

    """
    present = {entry.relative_path for entry in workspace.entries()}
    problems: list[str] = []
    for path in sorted(present):
        if not _is_gated_note(path):
            continue
        content = _read(workspace, path)
        if content is None or baseline.get(path) == hashlib.sha256(content).hexdigest():
            continue
        text = content.decode("utf-8", errors="replace")
        if _VERIFICATION_CLAIM.search(text) and not _names_durable_evidence(text, present):
            problems.append(
                f"{path} claims something was tested or verified without naming, in "
                "a code span, a test or fixture that exists in this tree; either name "
                "the shipped test, or keep the claim to the final reply"
            )
    return tuple(problems)


def suite_problems(
    workspace: Workspace, *, timeout_seconds: int = _TEST_TIMEOUT_SECONDS
) -> tuple[str, ...]:
    """Return why the tree's own tests do not pass, or nothing when they do.

    The suite is run against the tree in the workspace, not against the program
    that is running, so a successor is judged on what it ships. A tree with no
    tests directory owes nothing here; a tree whose tests cannot even be
    collected is reported like one whose tests fail.

    Args:
        workspace: The tree an improvement run left behind.
        timeout_seconds: How long the suite may take before it counts as failing.

    Returns:
        One sentence with the end of pytest's report, or nothing.

    """
    if not (workspace.root / TESTS_DIRECTORY).is_dir():
        return ()
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
        TESTS_DIRECTORY,
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - a fixed command in the tree's own directory
            command,
            cwd=workspace.root,
            env={**os.environ, "PYTHONPATH": str(workspace.root), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (f"the tree's tests did not finish within {timeout_seconds} seconds",)
    except OSError as unrunnable:
        return (f"the tree's tests could not be run: {unrunnable}",)
    if completed.returncode in (0, _NO_TESTS_COLLECTED):
        return ()
    report = (completed.stdout or completed.stderr).strip().splitlines()
    tail = " | ".join(line.strip() for line in report[-_REPORTED_TEST_LINES:] if line.strip())
    return (f"the tree's own tests fail (python -m pytest -q tests): {tail}",)


def _names_durable_evidence(text: str, present: set[str]) -> bool:
    """Report whether a note points at a test or fixture the tree holds."""
    for span in _CODE_SPAN.findall(text):
        path = span.strip().replace("\\", "/")
        parts = tuple(part.lower() for part in path.split("/") if part)
        if path in present and _DURABLE_EVIDENCE_PARTS.intersection(parts):
            return True
    return False


def _is_gated_note(path: str) -> bool:
    return (
        path.startswith(NOTES_DIRECTORY)
        and path.endswith(_NOTE_SUFFIX)
        and path not in _UNGATED_NOTES
    )


def _read(workspace: Workspace, relative_path: str) -> bytes | None:
    try:
        return workspace.resolve(relative_path).read_bytes()
    except (OSError, WorkspaceError):
        return None


def _unparsable(workspace: Workspace, relative_path: str) -> str:
    """Return why one Python file cannot be used, or nothing when it can.

    Only as much of a file as is scanned is read, so one enormous file cannot
    take the run's memory with it — and one that fills the scan is left alone
    rather than judged on the part that was read.
    """
    if not relative_path.endswith(_PYTHON_SUFFIX):
        return ""
    try:
        with workspace.resolve(relative_path).open("rb") as handle:
            source = handle.read(_MAX_SCANNED_BYTES)
    except (OSError, WorkspaceError) as unreadable:
        return f"{relative_path} could not be read: {unreadable}"
    if len(source) >= _MAX_SCANNED_BYTES:
        return ""
    try:
        ast.parse(source, filename=relative_path)
    except (SyntaxError, ValueError) as broken:
        return f"{relative_path} does not parse: {broken}"
    return ""
