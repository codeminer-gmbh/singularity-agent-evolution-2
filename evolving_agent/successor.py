"""Whether what an improvement run left behind is a usable next iteration.

The orchestrator packages whatever the workspace holds and builds it. Nothing
between here and that build reads the tree for the model, so a syntax error
costs a whole cycle and fails a smoke test nobody learns anything from. The
checks below are the ones the tree will face later, applied while there is still
budget to fix them: it holds what a container needs, its Python parses, and new
memory claims do not misrepresent transient work as shipped evidence. What the
successor *does* is not judged here; that is what the experiment's tests and
evaluators are for.
"""

import ast
import re
from pathlib import Path

from evolving_agent.workspace import Workspace, WorkspaceError

REQUIRED_FILES: tuple[str, ...] = ("Dockerfile", "main.py")
"""What every iteration's tree carries, whatever else it holds.

The external runtime requires exactly these two names, and the container it
starts runs the entrypoint the first one names.
"""

_MAX_SCANNED_BYTES = 1_000_000
_PYTHON_SUFFIX = ".py"
_MEMORY_DIRECTORY = "memories/"
# Execution-result wording belongs in a note only when that note points to a
# real durable test or fixture.  Include ordinary outcome words too: previous
# audits found prose such as "a check covers" just as misleading as "tested".
_VERIFICATION_LANGUAGE = re.compile(
    r"\b(?:tested|verified|verifies|verify|verification|passed|ran|"
    r"checked|checks|check|covers|covered|coverage|exercised|exercise|"
    r"validated|validates|validation|confirmed|confirms|successful|success|succeeded|succeeds)\b"
    r"|\bcommand results?\b"
    r"|\b(?:smoke|integration|end[- ]to[- ]end|e2e)\s+(?:test|check|exercise|validation)\b",
    re.IGNORECASE,
)
_CODE_SPAN = re.compile(r"`([^`\n]+)`")


def successor_problems(
    workspace: Workspace, *, baseline_root: Path | None = None
) -> tuple[str, ...]:
    """Return everything that stops this tree being a usable next iteration.

    ``baseline_root`` is the immutable source from which an improvement began.
    It lets the memory gate examine only notes introduced or edited this round;
    old records cannot make an unrelated successor impossible to publish.

    Nothing here raises. A tree the workspace itself refuses to read is a
    problem to report like any other: an improvement run that ended in an
    exception would skip both the repair it could still have made and the
    restore that keeps the cycle honest.
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
    if baseline_root is not None:
        problems.extend(_memory_claim_problems(workspace, baseline_root, present))
    return tuple(problem for problem in problems if problem)


def _memory_claim_problems(
    workspace: Workspace, baseline_root: Path, present: dict[str, int]
) -> tuple[str, ...]:
    """Reject changed execution claims with no named durable evidence.

    A cited path must be an actual test module (rather than, for example,
    ``tests/README.md``) or a file below a fixture directory.  Mere existence
    of a path with a convenient directory name is not durable evidence.
    """
    baseline = Workspace(baseline_root)
    baseline_sizes = {entry.relative_path: entry.byte_size for entry in baseline.entries()}
    problems: list[str] = []
    for path in sorted(present):
        if not path.startswith(_MEMORY_DIRECTORY) or not path.endswith(".md"):
            continue
        if not _changed(workspace, baseline, path, baseline_sizes):
            continue
        try:
            text = workspace.read_text(path, max_bytes=_MAX_SCANNED_BYTES)
        except WorkspaceError as unreadable:
            problems.append(f"{path} could not be read for memory evidence: {unreadable}")
            continue
        if _VERIFICATION_LANGUAGE.search(text) and not _names_durable_evidence(text, present):
            problems.append(
                f"{path} claims execution evidence but names no shipped test or fixture"
            )
    return tuple(problems)


def _changed(
    workspace: Workspace, baseline: Workspace, path: str, baseline_sizes: dict[str, int]
) -> bool:
    """Return whether a candidate note is new or differs from its baseline."""
    if path not in baseline_sizes:
        return True
    try:
        candidate = workspace.resolve(path).read_bytes()
        original = baseline.resolve(path).read_bytes()
    except (OSError, WorkspaceError):
        return True
    return candidate != original


def _names_durable_evidence(text: str, present: dict[str, int]) -> bool:
    """Return whether a code span names a shipped test module or fixture file."""
    for raw_path in _CODE_SPAN.findall(text):
        path = raw_path.strip().replace("\\", "/")
        if path not in present:
            continue
        parts = tuple(part.lower() for part in path.split("/") if part)
        leaf = parts[-1] if parts else ""
        is_test_module = (
            "tests" in parts
            and path.endswith(_PYTHON_SUFFIX)
            and (leaf == "test.py" or leaf.startswith("test_") or leaf.endswith("_test.py"))
        )
        if is_test_module or "fixture" in parts or "fixtures" in parts:
            return True
    return False


def _unparsable(workspace: Workspace, relative_path: str) -> str:
    """Return why one Python file cannot be used, or nothing when it can."""
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
