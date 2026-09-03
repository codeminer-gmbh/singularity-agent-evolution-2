"""Validate the durable proof record an improvement leaves for its successor.

The experiment cannot rerun a model's one-off commands.  A compact record of
requirements, concrete inputs, expected results, interactions, and observed
results makes the claimed capability reviewable by the next round instead of
turning a final reply into the only evidence.
"""

import json
from pathlib import Path

from evolving_agent.workspace import Workspace, WorkspaceError

VERIFICATION_RECORD = "memories/verification.json"
"""Workspace-relative location of the evidence required for a changed tree."""

_REQUIRED_TOP_LEVEL = ("audit", "matrix", "candidate_digest")
_REQUIRED_AUDIT = ("costly_failure", "evidence")
_REQUIRED_ROW = ("requirement", "input", "expected", "interaction", "observed")
_MAX_RECORD_BYTES = 100_000


def verification_record_bytes(workspace: Workspace) -> bytes | None:
    """Return the exact current evidence receipt, or ``None`` if unavailable.

    Improvement mode snapshots this before the model edits.  Byte identity is
    deliberate: a valid receipt inherited from the parent says nothing about
    a different candidate, while the first receipt in a previously recordless
    workspace has no predecessor to compare against.
    """
    try:
        record_path = workspace.resolve(VERIFICATION_RECORD)
        return record_path.read_bytes() if record_path.is_file() else None
    except (WorkspaceError, OSError):
        return None


def verification_update_problem(
    workspace: Workspace, inherited_record: bytes | None
) -> str | None:
    """Say when a changed candidate retained its parent's evidence receipt."""
    if inherited_record is None:
        return None
    if verification_record_bytes(workspace) == inherited_record:
        return f"{VERIFICATION_RECORD} was not updated for this changed successor"
    return None


def verification_problems(workspace: Workspace) -> tuple[str, ...]:
    """Return stable reasons a changed successor lacks reviewable proof.

    The record deliberately reports observations rather than a boolean
    ``passed`` flag: a reviewer can distinguish a test that was run and failed
    from one that was never run.  It is intentionally a publication check,
    not a claim that JSON alone proves a capability.
    """
    try:
        record_path = workspace.resolve(VERIFICATION_RECORD)
    except WorkspaceError as invalid_workspace:
        return (f"verification record cannot be located: {invalid_workspace}",)
    if not record_path.is_file():
        return (f"{VERIFICATION_RECORD} is missing",)
    try:
        raw = record_path.read_bytes()
    except OSError as unreadable:
        return (f"{VERIFICATION_RECORD} could not be read: {unreadable}",)
    if len(raw) > _MAX_RECORD_BYTES:
        return (f"{VERIFICATION_RECORD} exceeds {_MAX_RECORD_BYTES} bytes",)
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as malformed:
        return (f"{VERIFICATION_RECORD} is not valid JSON: {malformed}",)
    if not isinstance(record, dict):
        return (f"{VERIFICATION_RECORD} must contain a JSON object",)
    problems = _shape_problems(record)
    if not problems:
        claimed_digest = record["candidate_digest"]
        actual_digest = workspace.digest(exclude=frozenset({VERIFICATION_RECORD}))
        if claimed_digest != actual_digest:
            problems.append(
                f"{VERIFICATION_RECORD}.candidate_digest does not match the current candidate"
            )
    return tuple(problems)


def _shape_problems(record: dict[object, object]) -> list[str]:
    problems: list[str] = []
    for key in _REQUIRED_TOP_LEVEL:
        if key not in record:
            problems.append(f"{VERIFICATION_RECORD} is missing top-level {key!r}")
    candidate_digest = record.get("candidate_digest")
    if not (
        isinstance(candidate_digest, str)
        and len(candidate_digest) == 64
        and all(character in "0123456789abcdef" for character in candidate_digest)
    ):
        problems.append(
            f"{VERIFICATION_RECORD}.candidate_digest must be a lowercase SHA-256 hex digest"
        )
    audit = record.get("audit")
    if not isinstance(audit, dict):
        problems.append(f"{VERIFICATION_RECORD}.audit must be an object")
    else:
        for key in _REQUIRED_AUDIT:
            if not _nonempty_text(audit.get(key)):
                problems.append(f"{VERIFICATION_RECORD}.audit.{key} must be nonempty text")
    matrix = record.get("matrix")
    if not isinstance(matrix, list) or not matrix:
        problems.append(f"{VERIFICATION_RECORD}.matrix must be a nonempty array")
        return problems
    for number, row in enumerate(matrix, start=1):
        if not isinstance(row, dict):
            problems.append(f"{VERIFICATION_RECORD}.matrix[{number}] must be an object")
            continue
        for key in _REQUIRED_ROW:
            if not _nonempty_text(row.get(key)):
                problems.append(
                    f"{VERIFICATION_RECORD}.matrix[{number}].{key} must be nonempty text"
                )
    return problems


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
