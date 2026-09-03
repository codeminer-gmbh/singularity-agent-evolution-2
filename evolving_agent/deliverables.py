"""Small, conservative checks for probe artifacts explicitly named by a task.

The model remains responsible for interpreting a task.  This module only turns an
unambiguous ``output/...`` path in its text into a last completion checkpoint;
it deliberately does not infer filenames from prose such as "write a report".
"""

import re
from collections.abc import Iterable
from pathlib import Path

# A path component cannot be empty or a traversal component.  Keeping this
# narrow avoids treating examples, URLs, and natural-language punctuation as
# deliverable names.
_OUTPUT_PATH = re.compile(r"(?<![\w./-])output/([^\s`'\"<>|\\]+)")


def named_output_paths(task: str) -> tuple[Path, ...]:
    """Return safe, explicitly written relative paths below ``output/``.

    Order is preserved so an audit message mirrors the task, while duplicates
    are collapsed.  A bare ``output/`` and any path containing ``.`` or ``..``
    components is intentionally not a named artifact.
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
    """Return explicitly named artifacts absent from the collector directory."""
    if output_root is None:
        return ()
    return tuple(path for path in named_output_paths(task) if not (output_root / path).is_file())


def completion_repair_message(missing: Iterable[Path]) -> str:
    """Describe an observable completion failure without inventing new work."""
    paths = tuple(missing)
    listed = ", ".join(f"output/{path.as_posix()}" for path in paths)
    return (
        "Completion check: the task explicitly names these deliverable file(s), "
        f"but they are not present yet: {listed}. Use the available tools to write "
        "each at that exact output/ path, verify it exists, then give the final answer."
    )
