"""What stops a tree being a successor: build blockers and notes that overclaim."""

from pathlib import Path

from evolving_agent.successor import (
    note_problems,
    note_snapshot,
    successor_problems,
    suite_problems,
)
from evolving_agent.workspace import Workspace


def _tree(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path)
    workspace.prepare()
    workspace.write_text("Dockerfile", "FROM scratch\n")
    workspace.write_text("main.py", "print('ok')\n")
    return workspace


def test_a_tree_needs_its_two_files_and_parsable_python(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.prepare()
    assert successor_problems(workspace) == (
        "the workspace is empty; a successor needs a complete source tree",
    )

    workspace.write_text("main.py", "def broken(:\n")
    workspace.write_text("Dockerfile", "")
    problems = successor_problems(workspace)

    assert "Dockerfile is empty" in problems
    assert any(problem.startswith("main.py does not parse") for problem in problems)

    workspace.write_text("Dockerfile", "FROM scratch\n")
    workspace.write_text("main.py", "print('ok')\n")
    assert successor_problems(workspace) == ()


def test_an_inherited_claim_is_not_the_new_round_s_problem(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    workspace.write_text("memories/legacy.md", "Verified once in an earlier run.")
    baseline = note_snapshot(workspace)

    assert note_problems(workspace, baseline) == ()


def test_a_new_claim_needs_a_shipped_test_named_in_a_code_span(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    baseline = note_snapshot(workspace)

    workspace.write_text(
        "memories/parser.md", "Added empty-record handling. The integration tests passed."
    )
    problems = note_problems(workspace, baseline)
    assert len(problems) == 1 and problems[0].startswith("memories/parser.md claims")

    workspace.write_text(
        "memories/parser.md", "Handles empty records; verified by `tests/test_parser.py`."
    )
    assert note_problems(workspace, baseline) == (problems[0],)

    workspace.write_text("tests/test_parser.py", "def test_empty(): pass\n")
    assert note_problems(workspace, baseline) == ()


def test_notes_without_claims_and_the_round_plan_are_never_gated(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    baseline = note_snapshot(workspace)

    workspace.write_text("memories/round-plan.md", "Proof: the tests passed, verified.")
    workspace.write_text("memories/README.md", "Everything here was verified.")
    workspace.write_text(
        "memories/design.md", "The parser streams records so a large file cannot fill memory."
    )

    assert note_problems(workspace, baseline) == ()


def test_the_tree_s_own_tests_must_pass(tmp_path: Path) -> None:
    workspace = _tree(tmp_path)
    assert suite_problems(workspace) == ()

    workspace.write_text("tests/test_it.py", "def test_it():\n    assert 1 + 1 == 2\n")
    assert suite_problems(workspace) == ()

    workspace.write_text("tests/test_it.py", "def test_it():\n    assert 1 + 1 == 3\n")
    problems = suite_problems(workspace)
    assert len(problems) == 1
    assert problems[0].startswith("the tree's own tests fail")
    assert "test_it" in problems[0]
