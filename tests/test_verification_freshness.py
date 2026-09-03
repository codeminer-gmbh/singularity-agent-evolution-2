"""Adversarial checks for evidence freshness at the publication boundary."""

import json
from pathlib import Path

from evolving_agent.evidence import (
    VERIFICATION_RECORD,
    verification_record_bytes,
    verification_update_problem,
)
from evolving_agent.workspace import Workspace


def _valid_record(observed: str) -> str:
    return json.dumps(
        {
            "audit": {"costly_failure": "stale evidence", "evidence": "ledger"},
            "matrix": [
                {
                    "requirement": "fresh proof",
                    "input": "candidate source changes",
                    "expected": "new receipt",
                    "interaction": "pytest",
                    "observed": observed,
                }
            ],
        }
    )


def test_unchanged_inherited_record_is_rejected_but_new_record_is_accepted(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.prepare()
    workspace.write_text(VERIFICATION_RECORD, _valid_record("old run"))
    inherited = verification_record_bytes(workspace)

    assert verification_update_problem(workspace, inherited) == (
        "memories/verification.json was not updated for this changed successor"
    )

    workspace.write_text(VERIFICATION_RECORD, _valid_record("current run"))
    assert verification_update_problem(workspace, inherited) is None


def test_first_record_has_no_inherited_baseline(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.prepare()
    assert verification_record_bytes(workspace) is None
    workspace.write_text(VERIFICATION_RECORD, _valid_record("first run"))
    assert verification_update_problem(workspace, None) is None
