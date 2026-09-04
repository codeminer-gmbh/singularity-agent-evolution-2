"""Mechanical postconditions for source-code deliverables.

A semantic reviewer can miss the most basic failure mode: a requested source
file exists but contains only a description or placeholders.  This module does
not try to judge correctness.  It identifies Python artifacts for which there
is literally no implementation to test, so the tool-using session gets one
focused repair pass before its answer can be published.
"""

from __future__ import annotations

import ast
from pathlib import Path


def source_delivery_problems(output: Path | None) -> tuple[str, ...]:
    """Return concrete Python delivery failures under *output*.

    Empty package initializers are conventional and are ignored.  Other Python
    files must parse and contain at least one statement beyond imports,
    docstrings, ``pass``, ellipses, or explicitly unimplemented definitions.
    The check is intentionally narrow: it catches absence of implementation,
    not an opinion about whether an implementation is correct.
    """
    if output is None or not output.is_dir():
        return ()
    problems: list[str] = []
    for path in sorted(output.rglob("*.py")):
        if path.name == "__init__.py" or any(part.startswith(".") for part in path.parts):
            continue
        relative = path.relative_to(output).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, UnicodeError, SyntaxError) as error:
            problems.append(f"output/{relative} is not valid readable Python: {error}")
            continue
        if not _has_implementation(tree.body):
            problems.append(
                f"output/{relative} has no executable implementation; it contains "
                "only documentation, imports, or placeholder statements"
            )
    return tuple(problems)


def _has_implementation(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        if isinstance(statement, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(statement, ast.Expr) and (
            isinstance(statement.value, ast.Constant)
            and (isinstance(statement.value.value, str) or statement.value.value is Ellipsis)
        ):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _has_implementation(statement.body):
                return True
            continue
        if isinstance(statement, ast.Raise) and _is_not_implemented(statement.exc):
            continue
        return True
    return False


def _is_not_implemented(expression: ast.expr | None) -> bool:
    if isinstance(expression, ast.Name):
        return expression.id == "NotImplementedError"
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "NotImplementedError"
    )
