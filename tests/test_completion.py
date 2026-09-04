"""The mechanical checks a probe's draft is held to."""

from pathlib import Path

from evolving_agent.completion import (
    asks_for_executable_source,
    missing_deliverables_message,
    missing_output_paths,
    named_output_paths,
    unexercised_source_message,
)


def test_named_output_paths_are_explicit_and_safe() -> None:
    task = (
        "Deliver output/solution.py and output/docs/README.md. Do not touch "
        "output/../secrets or output/. See output/solution.py again."
    )

    assert named_output_paths(task) == (Path("solution.py"), Path("docs/README.md"))
    assert named_output_paths("write a report") == ()


def test_missing_deliverables_are_reported_only_when_there_is_an_output_tree(
    tmp_path: Path,
) -> None:
    task = "Write output/a.txt and output/b.txt."
    (tmp_path / "a.txt").write_text("done")

    assert missing_output_paths(task, None) == ()
    assert missing_output_paths(task, tmp_path) == (Path("b.txt"),)
    assert "output/b.txt" in missing_deliverables_message([Path("b.txt")])


def test_the_source_classifier_is_conservative() -> None:
    assert asks_for_executable_source("Write a Python program that reads stdin")
    assert asks_for_executable_source("Create output/solution.py")
    assert asks_for_executable_source("Provide the source code for the parser")
    assert not asks_for_executable_source("Explain how this algorithm works")
    assert not asks_for_executable_source("Write a report on parser design")


def test_the_checkpoint_speaks_once_and_only_before_a_command_ran() -> None:
    task = "Implement a function in solution.py"

    assert unexercised_source_message(task, {}) is not None
    assert unexercised_source_message(task, {"run_command": 1}) is None
    assert unexercised_source_message("Summarize the article", {}) is None
