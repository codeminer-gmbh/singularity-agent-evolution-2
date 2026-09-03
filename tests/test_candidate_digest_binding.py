"""Adversarial proof that an evidence receipt is bound to its candidate."""

import json
from pathlib import Path

from evolving_agent.evidence import VERIFICATION_RECORD, verification_problems
from evolving_agent.workspace import Workspace


def _record(workspace: Workspace) -> str:
    return json.dumps(
        {
            "candidate_digest": workspace.digest(
                exclude=frozenset({VERIFICATION_RECORD})
            ),
            "audit": {"costly_failure": "stale proof", "evidence": "ledger"},
            "matrix": [
                {
                    "requirement": "candidate binding",
                    "input": "source content",
                    "expected": "matching receipt accepted",
                    "interaction": "verification_problems",
                    "observed": "matching",
                }
            ],
        }
    )


def test_receipt_accepts_only_the_exact_candidate_and_ignores_itself(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.prepare()
    workspace.write_text("agent.py", "version = 1\n")
    workspace.write_text(VERIFICATION_RECORD, _record(workspace))

    assert verification_problems(workspace) == ()

    workspace.write_text("agent.py", "version = 2\n")
    assert verification_problems(workspace) == (
        "memories/verification.json.candidate_digest does not match the current candidate",
    )
