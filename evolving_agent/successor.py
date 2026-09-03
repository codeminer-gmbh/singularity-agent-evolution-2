"""Whether what an improvement run left behind is a usable next iteration.

The orchestrator packages whatever the workspace holds and builds it. Nothing
between here and that build reads the tree for the model, so a syntax error
costs a whole cycle and fails a smoke test nobody learns anything from. The
checks below are the ones the tree will face later, applied while there is still
budget to fix them: it holds what a container needs, and its Python parses. What
the successor *does* is not judged here; that is what the experiment's tests and
evaluators are for.
"""

import ast
import hashlib
import re

from collections.abc import Mapping

from evolving_agent.workspace import Workspace, WorkspaceError

REQUIRED_FILES: tuple[str, ...] = ("Dockerfile", "main.py")
"""What every iteration's tree carries, whatever else it holds.

The external runtime requires exactly these two names, and the container it
starts runs the entrypoint the first one names.
"""


_MEMORY_PREFIX = "memories/"
_MEMORY_EXCLUSIONS = frozenset({"memories/README.md", "memories/round-plan.md"})
_RESULT_CLAIM = re.compile(
    r"\b(?:verif(?:y|ies|ied)|tested|exercised|smoke[- ]tested)\b"
    r"|\b(?:tests?|checks?|fixtures?|commands?)\s+(?:passed|succeeded)\b"
    r"|\b(?:ran|executed)\s+(?:an?\s+)?(?:test|check|fixture|command)\b",
    re.IGNORECASE,
)


def durable_note_snapshot(workspace: Workspace) -> dict[str, str]:
    """Fingerprint durable memory notes before an improvement session changes them."""
    snapshot: dict[str, str] = {}
    for entry in workspace.entries():
        path = entry.relative_path
        if not _is_durable_note(path):
            continue
        try:
            content = workspace.resolve(path).read_bytes()
        except (OSError, WorkspaceError):
            continue
        snapshot[path] = hashlib.sha256(content).hexdigest()
    return snapshot


def durable_note_problems(
    workspace: Workspace, baseline: Mapping[str, str]
) -> tuple[str, ...]:
    """Reject transient result claims in notes added or changed this round.

    Durable notes may name shipped tests and describe what code does.  Whether a
    command happened to pass belongs in the final run report, which is retained
    separately.  Only changed notes are checked so inherited audited prose does
    not make an otherwise useful successor impossible to publish.
    """
    problems: list[str] = []
    for entry in workspace.entries():
        path = entry.relative_path
        if not _is_durable_note(path):
            continue
        try:
            content = workspace.resolve(path).read_bytes()
        except (OSError, WorkspaceError):
            continue
        if baseline.get(path) == hashlib.sha256(content).hexdigest():
            continue
        text = content.decode("utf-8", errors="replace")
        if _RESULT_CLAIM.search(text):
            problems.append(
                f"{path} contains a transient test or execution-result claim; "
                "remove that claim from the durable note and report it only "
                "in the final answer"
            )
    return tuple(problems)


def _is_durable_note(path: str) -> bool:
    return (
        path.startswith(_MEMORY_PREFIX)
        and path.endswith(".md")
        and path not in _MEMORY_EXCLUSIONS
    )

_MAX_SCANNED_BYTES = 1_000_000
_PYTHON_SUFFIX = ".py"


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
        f"{required} is empty"
        for required in REQUIRED_FILES
        if present.get(required) == 0
    )
    problems.extend(_unparsable(workspace, path) for path in present)
    return tuple(problem for problem in problems if problem)


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
