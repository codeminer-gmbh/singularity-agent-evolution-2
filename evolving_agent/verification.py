"""Conservative completion checkpoint for executable probe tasks.

A prose instruction to test code is easy to acknowledge and skip. This module
identifies only tasks that plainly ask for executable source. It first requests an
actual run when none occurred, then separately challenges an ordinary or supplied
test run with adversarial boundaries and, where practical, an independent oracle.
Invoking a command is not treated as proof of correctness.
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


def adversarial_verification_message(
    task: str, tool_calls: Mapping[str, int]
) -> str | None:
    """Request a hidden-case challenge after an executable has been run once.

    Passing supplied or ordinary examples is useful but repeatedly failed to expose
    representation and boundary mistakes.  This checkpoint is deliberately a
    separate completion turn: it occurs after the model has seen its first run, when
    it can use that result to design a test which could falsify the implementation.
    """
    if not asks_for_executable_source(task) or tool_calls.get("run_command", 0) == 0:
        return None
    return (
        "Adversarial verification checkpoint: an executable run has occurred, but "
        "ordinary or supplied tests alone often miss the decisive hidden case. Before "
        "finalizing, try to falsify the implementation: construct small edge cases for "
        "the contract's exact boundaries and representation hazards (for example exact "
        "arithmetic, overflow, empty/degenerate state, or equality), and where practical "
        "compare many small cases with a simple independent oracle or alternate "
        "implementation. Run that check and inspect the decisive output. Do not merely "
        "repeat the same examples. If an independent check is genuinely impractical, "
        "state why and report that limitation rather than claiming it was verified; "
        "then give the complete answer."
    )
