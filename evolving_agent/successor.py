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
import json
import os
import subprocess
import sys
from pathlib import Path

from evolving_agent.workspace import Workspace, WorkspaceError

REQUIRED_FILES: tuple[str, ...] = ("Dockerfile", "main.py")
"""What every iteration's tree carries, whatever else it holds.

The external runtime requires exactly these two names, and the container it
starts runs the entrypoint the first one names.
"""

_MAX_SCANNED_BYTES = 1_000_000
_PYTHON_SUFFIX = ".py"
_DESCRIBE_TIMEOUT_SECONDS = 30


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
    if not problems:
        problems.append(_describe_problem(workspace))
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


def _describe_problem(workspace: Workspace) -> str:
    """Return why the candidate cannot start its model-free describe mode.

    Parsing catches damaged Python but not broken imports, entry points, or a
    tool registry that fails during initialization. Describe mode exercises all
    of those without requiring a model, credentials, or task data.
    """
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_MODE": "describe",
            "AGENT_WORKSPACE": str(workspace.root),
        }
    )
    try:
        result = subprocess.run(
            [sys.executable, "main.py"],
            cwd=workspace.root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_DESCRIBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as failed:
        return f"the successor describe-mode smoke check could not run: {failed}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return (
            "the successor describe-mode smoke check failed "
            f"(exit {result.returncode}): {_short(detail)}"
        )
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as invalid:
        return f"the successor describe-mode smoke check emitted invalid JSON: {invalid}"
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tools"), list):
        return "the successor describe-mode smoke check emitted no tool manifest"
    return ""


def _short(value: str, limit: int = 500) -> str:
    """Keep a child-process failure useful without making reports enormous."""
    return value[:limit] + ("..." if len(value) > limit else "")
