"""Conservative completion checkpoint for executable probe tasks.

A prose instruction to test code is easy to acknowledge and skip.  This module
identifies only tasks that plainly ask for executable source and supplies one
specific reminder when the session has not used the command runner at all.
It does not pretend that invoking a command proves correctness; the reminder
asks for a discriminating behavioral check rather than a syntax-only smoke test.
"""

import re
from collections.abc import Mapping

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


def asks_for_executable_source(task: str) -> bool:
    """Return whether *task* unambiguously requests executable source code.

    The check intentionally excludes general discussion of code and algorithm
    explanations.  A false negative merely leaves the model's ordinary testing
    instruction in force; a false positive spends a model exchange.
    """
    return bool(
        _SOURCE_SUFFIX.search(task)
        or _SOURCE_REQUEST.search(task)
        or _CODE_REQUEST.search(task)
    )


def execution_verification_message(
    task: str, tool_calls: Mapping[str, int]
) -> str | None:
    """Request a behavioral run when executable work has not used the runner."""
    if not asks_for_executable_source(task) or tool_calls.get("run_command", 0) > 0:
        return None
    return (
        "Verification checkpoint: this task asks for executable source, but no "
        "command has exercised it yet. Before finalizing, use run_command for the "
        "strongest available tests. If none are supplied, run a small temporary "
        "fixture covering the ordinary case and a contract boundary; where practical, "
        "compare several small cases with a simple independent oracle. A parse or "
        "syntax check alone is not behavioral evidence. Then inspect the decisive "
        "output and give the complete answer."
    )
