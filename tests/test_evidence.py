"""The proof record, its binding to the tree, and the runtime receipt."""

import json
import subprocess
import sys
from pathlib import Path

from evolving_agent.evidence import (
    VERIFICATION_RECEIPT,
    VERIFICATION_RECORD,
    candidate_digest,
    command_status,
    verification_problems,
    write_receipt,
)
from evolving_agent.session import ToolObservation
from evolving_agent.workspace import Workspace


def _record(workspace: Workspace, **overrides) -> str:
    record = {
        "candidate_digest": candidate_digest(workspace),
        "audit": {"costly_failure": "stale proof", "evidence": "ledger row 3"},
        "matrix": [
            {
                "requirement": "binding",
                "input": "source content",
                "expected": "matching receipt accepted",
                "interaction": "python -m evolving_agent.evidence",
                "observed": "matching",
            }
        ],
    }
    record.update(overrides)
    return json.dumps(record)


def _workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path)
    workspace.prepare()
    workspace.write_text("agent.py", "version = 1\n")
    return workspace


def test_a_bound_record_is_accepted_and_an_edit_after_it_is_not(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_text(VERIFICATION_RECORD, _record(workspace))

    assert verification_problems(workspace) == ()

    workspace.write_text("agent.py", "version = 2\n")
    problems = verification_problems(workspace)
    assert len(problems) == 1
    assert "candidate_digest does not match" in problems[0]


def test_the_receipt_does_not_disturb_the_binding(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_text(VERIFICATION_RECORD, _record(workspace))
    write_receipt(workspace, [])

    assert (tmp_path / VERIFICATION_RECEIPT).is_file()
    assert verification_problems(workspace) == ()


def test_shape_problems_are_named_one_per_defect(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    workspace.write_text(
        VERIFICATION_RECORD,
        json.dumps({"audit": {"costly_failure": " "}, "matrix": [{"requirement": "x"}], "candidate_digest": "nope"}),
    )

    problems = verification_problems(workspace)

    assert f"{VERIFICATION_RECORD}.audit.costly_failure must be nonempty text" in problems
    assert f"{VERIFICATION_RECORD}.audit.evidence must be nonempty text" in problems
    assert f"{VERIFICATION_RECORD}.matrix[1].observed must be nonempty text" in problems
    assert any("lowercase SHA-256" in problem for problem in problems)
    assert verification_problems(Workspace(tmp_path / "elsewhere")) == (
        f"{VERIFICATION_RECORD} is missing",
    )


def test_the_digest_command_prints_what_the_gate_expects(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    printed = subprocess.run(
        [sys.executable, "-m", "evolving_agent.evidence"],
        cwd=tmp_path,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert printed == candidate_digest(workspace)


def test_the_receipt_counts_from_status_lines_not_prose(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    observations = [
        ToolObservation(1, "run_command", {"command": ["python", "t.py"]}, False, "$ python t.py\n[exit code 1]\n"),
        ToolObservation(2, "run_command", {"command": ["python", "t.py"]}, False, "$ python t.py\n[exit code 0]\nall passed"),
        ToolObservation(3, "run_command", {"command": ["sleep", "9"]}, False, "$ sleep 9\n[timed out]\n"),
        ToolObservation(4, "run_command", {}, True, "the arguments are not valid JSON"),
        ToolObservation(5, "read_file", {"path": "x"}, False, "content"),
    ]

    write_receipt(workspace, observations)

    receipt = json.loads((tmp_path / VERIFICATION_RECEIPT).read_text())
    assert receipt["passed"] == 1 and receipt["failed"] == 2
    assert [item["status"] for item in receipt["commands"]] == [
        "FAILED [exit code 1]",
        "PASSED (exit code 0)",
        "FAILED (timed out)",
        "DID NOT START (tool error)",
    ]
    assert command_status(observations[4]) == "DID NOT START (no exit status returned)"
