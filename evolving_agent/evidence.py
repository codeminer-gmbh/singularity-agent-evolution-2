"""Validate and preserve the durable proof record an improvement leaves.

The experiment cannot rerun a model's one-off commands.  The record therefore
contains both the model's requirement matrix and a machine-captured transcript
of the commands that session actually asked the public tool surface to run.
The latter does not establish that an oracle was good, but prevents a prose
claim from impersonating an executed demonstration.
"""

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from evolving_agent.workspace import Workspace, WorkspaceError

VERIFICATION_RECORD = "memories/verification.json"
"""Workspace-relative location of the evidence required for a changed tree."""

_REQUIRED_TOP_LEVEL = ("audit", "matrix", "tool_transcript")
_REQUIRED_AUDIT = ("costly_failure", "evidence")
_REQUIRED_ROW = ("requirement", "input", "expected", "interaction", "observed")
_MAX_RECORD_BYTES = 100_000
_MAX_TRANSCRIPT_ENTRIES = 80
_MAX_TRANSCRIPT_TEXT = 8_000


def verification_problems(workspace: Workspace) -> tuple[str, ...]:
    """Return stable reasons a changed successor lacks reviewable proof."""
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
    return tuple(_shape_problems(record))


def attach_tool_transcript(
    workspace: Workspace, calls: Iterable[Mapping[str, object]]
) -> None:
    """Attach machine-observed command calls to a valid proof record.

    Only ``run_command`` calls are evidence of a runnable demonstration.  The
    session owns their argument/result capture, so text in the record cannot
    forge this field.  A malformed or absent record is deliberately left for
    the normal publication gate to explain and repair.
    """
    command_calls = [
        _safe_call(call)
        for call in calls
        if call.get("name") == "run_command"
    ][-_MAX_TRANSCRIPT_ENTRIES:]
    if not command_calls:
        return
    try:
        path = workspace.resolve(VERIFICATION_RECORD)
        record = json.loads(path.read_text(encoding="utf-8"))
    except (WorkspaceError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(record, dict):
        return
    record["tool_transcript"] = command_calls
    encoded = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if len(encoded.encode("utf-8")) > _MAX_RECORD_BYTES:
        return
    try:
        path.write_text(encoded, encoding="utf-8")
    except OSError:
        return


def _safe_call(call: Mapping[str, object]) -> dict[str, object]:
    """Bound one captured public command for durable JSON evidence."""
    return {
        "step": call.get("step"),
        "arguments": call.get("arguments"),
        "is_error": bool(call.get("is_error")),
        "result": _limited_text(call.get("result")),
    }


def _limited_text(value: object) -> str:
    text = str(value)
    return text if len(text) <= _MAX_TRANSCRIPT_TEXT else text[:_MAX_TRANSCRIPT_TEXT] + "\n... [evidence truncated]"


def _shape_problems(record: dict[object, object]) -> list[str]:
    problems: list[str] = []
    for key in _REQUIRED_TOP_LEVEL:
        if key not in record:
            problems.append(f"{VERIFICATION_RECORD} is missing top-level {key!r}")
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
    else:
        for number, row in enumerate(matrix, start=1):
            if not isinstance(row, dict):
                problems.append(f"{VERIFICATION_RECORD}.matrix[{number}] must be an object")
                continue
            for key in _REQUIRED_ROW:
                if not _nonempty_text(row.get(key)):
                    problems.append(f"{VERIFICATION_RECORD}.matrix[{number}].{key} must be nonempty text")
    transcript = record.get("tool_transcript")
    if not isinstance(transcript, list) or not transcript:
        problems.append(f"{VERIFICATION_RECORD}.tool_transcript must contain a captured run_command result")
    return problems


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
