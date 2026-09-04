"""The proof an improvement run leaves behind, and the receipt the runtime adds.

Two files carry the evidence for a changed tree, and they are deliberately not
the same kind of thing.

``memories/verification.json`` is written by the model: an audit naming the
costly failure the round set out to remove and the evidence for it, and a
matrix of requirement, input, expected result, interaction, and observed result.
It is bound to the tree it describes by a digest, so a record written before a
later edit no longer attests to anything. The publication gate refuses a changed
successor without a well-formed, bound record.

``.meta/verification.json`` is written by the runtime after the session ends: a
receipt of every command the run actually executed and how each one ended,
compiled from the session's own observations rather than from anything the
model said. A model can describe a check it never ran; the receipt cannot.

The second experiment found that both are needed. A record alone was filled in
from memory of what should have happened; a receipt alone says what ran but not
what it was supposed to prove. Together, and bound to the tree, they make a
claim reviewable by the next round.
"""

import json
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from evolving_agent.workspace import Workspace, WorkspaceError

VERIFICATION_RECORD = "memories/verification.json"
"""Where an improvement run writes its requirement-to-proof record."""

VERIFICATION_RECEIPT = ".meta/verification.json"
"""Where the runtime leaves machine-captured command outcomes."""

DIGEST_EXCLUSIONS = frozenset({VERIFICATION_RECORD, VERIFICATION_RECEIPT})
"""What the candidate digest leaves out: the evidence files themselves.

The record carries the digest, so it cannot be part of it; the receipt is
written after the model has computed the digest, so it must not change it.
"""

DIGEST_COMMAND = "python -m evolving_agent.evidence"
"""How a model obtains the digest to bind its record to."""

_REQUIRED_TOP_LEVEL = ("audit", "matrix", "candidate_digest")
_REQUIRED_AUDIT = ("costly_failure", "evidence")
_REQUIRED_ROW = ("requirement", "input", "expected", "interaction", "observed")
_MAX_RECORD_BYTES = 100_000
_DIGEST_LENGTH = 64
_DIGEST_ALPHABET = frozenset("0123456789abcdef")


class CommandObservation(Protocol):
    """What one tool call produced, as the session observed it.

    A structural type rather than an import from the session module, so the
    receipt can be written from any object that carries these attributes.
    """

    @property
    def step(self) -> int: ...  # noqa: D102 - the attributes are described above

    @property
    def name(self) -> str: ...  # noqa: D102

    @property
    def arguments(self) -> Mapping[str, Any]: ...  # noqa: D102

    @property
    def is_error(self) -> bool: ...  # noqa: D102

    @property
    def text(self) -> str: ...  # noqa: D102


def candidate_digest(workspace: Workspace) -> str:
    """Return the digest a verification record must carry to be bound.

    Args:
        workspace: The tree the record describes.

    Returns:
        The digest over every source file except the evidence files.

    """
    return workspace.digest(exclude=DIGEST_EXCLUSIONS)


def verification_problems(workspace: Workspace) -> tuple[str, ...]:
    """Return every reason the tree's proof record cannot be accepted.

    The record reports observations rather than a ``passed`` flag, so a reviewer
    can tell a check that ran and failed from one that never ran. This is a
    publication check on the record's shape and binding; it does not, and
    cannot, judge whether the matrix proves the capability it claims.

    Args:
        workspace: The tree an improvement run left behind.

    Returns:
        One sentence per problem, in a stable order; empty when the record is
        well-formed and bound to the tree as it stands.

    """
    loaded = _load_record(workspace)
    if isinstance(loaded, str):
        return (loaded,)
    problems = _shape_problems(loaded)
    if not problems and loaded["candidate_digest"] != candidate_digest(workspace):
        problems.append(
            f"{VERIFICATION_RECORD}.candidate_digest does not match the tree as it "
            f"stands; run `{DIGEST_COMMAND}` after the last edit and record its output"
        )
    return tuple(problems)


def _load_record(workspace: Workspace) -> dict[object, object] | str:
    """Return the record as parsed JSON, or the sentence saying why it could not be."""
    try:
        record_path = workspace.resolve(VERIFICATION_RECORD)
    except WorkspaceError as unusable:
        return f"{VERIFICATION_RECORD} cannot be located: {unusable}"
    if not record_path.is_file():
        return f"{VERIFICATION_RECORD} is missing"
    try:
        raw = record_path.read_bytes()
    except OSError as unreadable:
        return f"{VERIFICATION_RECORD} could not be read: {unreadable}"
    if len(raw) > _MAX_RECORD_BYTES:
        return f"{VERIFICATION_RECORD} exceeds {_MAX_RECORD_BYTES} bytes"
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as malformed:
        return f"{VERIFICATION_RECORD} is not valid JSON: {malformed}"
    if not isinstance(record, dict):
        return f"{VERIFICATION_RECORD} must contain a JSON object"
    return record


def write_receipt(workspace: Workspace, observations: Iterable[CommandObservation]) -> None:
    """Leave a receipt of every command the run executed and how it ended.

    The receipt holds command arguments and status lines, not output: it stays
    small and copies neither task data nor an accidental secret into the
    successor. A failed command and a command that never started are evidence
    too, which is the point of writing this from the observations rather than
    from the model's summary.

    Args:
        workspace: The tree the receipt is left in.
        observations: What the session observed, in order.

    """
    commands: list[dict[str, object]] = []
    statuses: list[str] = []
    for observed in observations:
        if observed.name != "run_command":
            continue
        command = observed.arguments.get("command")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            command = None
        status = command_status(observed)
        statuses.append(status)
        commands.append({"step": observed.step, "command": command, "status": status})
    receipt = {
        "format": 2,
        "meaning": (
            "Machine-captured command outcomes, in order. A status here is a fact "
            "about what ran; it does not establish relevance to any claimed change."
        ),
        "commands": commands,
        "passed": sum(status.startswith("PASSED") for status in statuses),
        "failed": sum(status.startswith("FAILED") for status in statuses),
    }
    target = workspace.root / VERIFICATION_RECEIPT
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as unwritable:
        # The receipt is what the run offers, not what it owes; a successor
        # without one is still a successor.
        sys.stderr.write(f"The verification receipt could not be written: {unwritable}\n")


def command_status(observed: CommandObservation) -> str:
    """Classify one command observation from its status line, never from prose."""
    if observed.is_error:
        return "DID NOT START (tool error)"
    for line in observed.text.splitlines():
        if line == "[exit code 0]":
            return "PASSED (exit code 0)"
        if line.startswith("[exit code "):
            return "FAILED " + line
        if line == "[timed out]":
            return "FAILED (timed out)"
    return "DID NOT START (no exit status returned)"


def _shape_problems(record: dict[object, object]) -> list[str]:
    """Return what is missing or malformed in a record, before binding is judged."""
    problems = [
        f"{VERIFICATION_RECORD} is missing top-level {key!r}"
        for key in _REQUIRED_TOP_LEVEL
        if key not in record
    ]
    audit = record.get("audit")
    if not isinstance(audit, dict):
        problems.append(f"{VERIFICATION_RECORD}.audit must be an object")
    else:
        problems.extend(
            f"{VERIFICATION_RECORD}.audit.{key} must be nonempty text"
            for key in _REQUIRED_AUDIT
            if not _nonempty_text(audit.get(key))
        )
    matrix = record.get("matrix")
    if not isinstance(matrix, list) or not matrix:
        problems.append(f"{VERIFICATION_RECORD}.matrix must be a nonempty array")
    else:
        for number, row in enumerate(matrix, start=1):
            if not isinstance(row, dict):
                problems.append(f"{VERIFICATION_RECORD}.matrix[{number}] must be an object")
                continue
            problems.extend(
                f"{VERIFICATION_RECORD}.matrix[{number}].{key} must be nonempty text"
                for key in _REQUIRED_ROW
                if not _nonempty_text(row.get(key))
            )
    digest = record.get("candidate_digest")
    if "candidate_digest" in record and not _is_digest(digest):
        problems.append(
            f"{VERIFICATION_RECORD}.candidate_digest must be the lowercase SHA-256 "
            f"hex digest printed by `{DIGEST_COMMAND}`"
        )
    return problems


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == _DIGEST_LENGTH and set(value) <= _DIGEST_ALPHABET
    )


def main(argv: list[str]) -> int:
    """Print the candidate digest of a tree, for a model to bind its record to.

    Args:
        argv: An optional single argument, the tree to digest; the current
            directory when none is given, which is where an improvement run's
            commands execute.

    Returns:
        The process exit status.

    """
    if len(argv) > 1:
        sys.stderr.write(f"usage: {DIGEST_COMMAND} [DIRECTORY]\n")
        return 2
    root = Path(argv[0]) if argv else Path(os.getcwd())
    try:
        sys.stdout.write(candidate_digest(Workspace(root)) + "\n")
    except (OSError, WorkspaceError) as unusable:
        sys.stderr.write(f"The tree could not be digested: {unusable}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
