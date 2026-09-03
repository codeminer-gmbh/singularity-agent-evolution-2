"""Regression cases for publication evidence provenance."""

from pathlib import Path

from evolving_agent.evidence import verification_record_bytes
from evolving_agent.modes import _publication_problems
from evolving_agent.workspace import Workspace


def _freshness_messages(problems: tuple[str, ...]) -> list[str]:
    return [problem for problem in problems if "was not updated" in problem]


def test_unchanged_tree_does_not_require_a_new_verification_record() -> None:
    """Inherited evidence is sufficient when no successor was made."""
    workspace = Workspace(Path("."))
    inherited = verification_record_bytes(workspace)

    assert _freshness_messages(_publication_problems(workspace, inherited, False)) == []


def test_changed_candidate_requires_record_different_from_inherited_one() -> None:
    """The same inherited bytes cannot prove a newly changed candidate."""
    workspace = Workspace(Path("."))
    inherited = verification_record_bytes(workspace)

    assert _freshness_messages(_publication_problems(workspace, inherited, True)) == [
        "memories/verification.json was not updated for this changed successor"
    ]
