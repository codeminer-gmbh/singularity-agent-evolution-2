"""What a probe is held to before its first draft is accepted as the answer.

A prose instruction to deliver a file or to test code is easy to acknowledge
and skip, and the second experiment's record is full of drafts that did exactly
that: a deliverable pasted into the answer instead of written to ``output/``, a
program handed in on the strength of a syntax check. These checks are small,
mechanical, and conservative. Each turns one observable fact into one message
the session puts to the model before it may finish, and none of them pretends
that the fact it checks is the whole of correctness.
"""

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

_OUTPUT_PATH = re.compile(r"(?<![\w./-])output/([^\s`'\"<>|\\]+)")
"""An explicit ``output/…`` path in a task's text.

Kept narrow on purpose: it does not infer a filename from prose such as
"write a report", and a bare ``output/`` names nothing.
"""

_SOURCE_SUFFIX = re.compile(
    r"(?<![\w.])[^\s`'\"/]+\.(?:py|js|mjs|cjs|ts|tsx|java|c|cc|cpp|go|rs|rb|php|sh)\b",
    re.IGNORECASE,
)
_CODE_REQUEST = re.compile(
    r"\b(?:implement|complete|repair|fix|debug)\b(?:\s+\w+){0,4}\s+"
    r"(?:function|program|script|parser|compiler|interpreter|module|class|cli|command-line tool)\b"
    r"|\b(?:write|create|provide)\b"
    r"(?:\s+(?:a|an|the|this|following|working|complete|single|small|new|python|javascript|typescript|java|c\+\+|go|rust|ruby|php|shell)){0,4}\s+"
    r"(?:function|program|script|parser|compiler|interpreter|module|class|cli|command-line tool)\b",
    re.IGNORECASE,
)
_SOURCE_REQUEST = re.compile(r"\b(?:source|executable)\s+code\b", re.IGNORECASE)

_COMMAND_TOOL = "run_command"


def named_output_paths(task: str) -> tuple[Path, ...]:
    """Return the deliverable paths a task names explicitly, in order.

    Args:
        task: The task's text.

    Returns:
        Each distinct relative path written as ``output/<path>`` in the task,
        with trailing punctuation dropped and traversal components refused.

    """
    found: list[Path] = []
    for match in _OUTPUT_PATH.finditer(task):
        candidate = Path(match.group(1).rstrip(".,;:!?)]}"))
        if not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
            continue
        if candidate not in found:
            found.append(candidate)
    return tuple(found)


def missing_output_paths(task: str, output_root: Path | None) -> tuple[Path, ...]:
    """Return the named deliverables that are not on disk yet.

    Args:
        task: The task's text.
        output_root: Where deliverables are collected from, or none when the
            run was given nowhere to leave them.

    """
    if output_root is None:
        return ()
    return tuple(path for path in named_output_paths(task) if not (output_root / path).is_file())


def missing_deliverables_message(missing: Iterable[Path]) -> str:
    """Return the message that sends a draft back for its missing files."""
    listed = ", ".join(f"output/{path.as_posix()}" for path in missing)
    return (
        "Completion check: the task names these deliverable files, and they are "
        f"not present yet: {listed}. Write each one at exactly that output/ path "
        "with the tools, confirm it exists, and then give the final answer."
    )


def asks_for_executable_source(task: str) -> bool:
    """Report whether a task unambiguously asks for runnable source code.

    A false negative leaves the ordinary instruction to test in force; a false
    positive costs one model exchange. So the check is deliberately narrow and
    ignores discussion of code, algorithm explanations, and reports.
    """
    return bool(
        _SOURCE_SUFFIX.search(task) or _SOURCE_REQUEST.search(task) or _CODE_REQUEST.search(task)
    )


def unexercised_source_message(task: str, tool_calls: Mapping[str, int]) -> str | None:
    """Return the checkpoint for code that was handed in without ever being run.

    Args:
        task: The task's text.
        tool_calls: How often each tool has been called so far.

    Returns:
        The message, or none when the task does not ask for executable source
        or the run has already used the command runner.

    """
    if not asks_for_executable_source(task) or tool_calls.get(_COMMAND_TOOL, 0) > 0:
        return None
    return (
        "Verification checkpoint: this task asks for executable source, and no "
        "command has exercised it yet. Before finalizing, use run_command for the "
        "strongest available tests. If none are supplied, run a small temporary "
        "fixture covering the ordinary case and a contract boundary, and where "
        "practical compare a few cases against a simple independent oracle. A parse "
        "or syntax check alone is not behavioral evidence. Inspect the decisive "
        "output, then give the complete answer."
    )
