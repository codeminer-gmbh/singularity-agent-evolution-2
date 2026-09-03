"""Task-facing checks for durable-note evidence gating."""

import tempfile
import unittest
from pathlib import Path

from evolving_agent.modes import _repaired
from evolving_agent.successor import durable_note_problems, durable_note_snapshot
from evolving_agent.workspace import Workspace


class _Deadline:
    def expired(self) -> bool:
        return False


class _RepairingSession:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.openings: list[str] = []

    def run(self, *, instructions: str, opening: str) -> str:
        self.openings.append(opening)
        self.workspace.write_text(
            "memories/new-capability.md",
            "The parser handles empty records. Shipped test: `tests/test_parser.py`.",
        )
        return "repaired"


class DurableNoteGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self.temporary.name))
        self.workspace.prepare()
        self.workspace.write_text(
            "memories/legacy.md", "Verified once in an earlier unshipped run."
        )
        self.baseline = durable_note_snapshot(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_unchanged_inherited_claim_does_not_block_successor(self) -> None:
        self.assertEqual(durable_note_problems(self.workspace, self.baseline), ())

    def test_new_execution_result_claim_is_rejected(self) -> None:
        self.workspace.write_text(
            "memories/new-capability.md",
            "Added empty-record handling. The integration checks passed.",
        )
        problems = durable_note_problems(self.workspace, self.baseline)
        self.assertEqual(len(problems), 1)
        self.assertIn("memories/new-capability.md", problems[0])
        self.assertIn("final answer", problems[0])

    def test_shipped_behavior_and_test_path_are_accepted(self) -> None:
        self.workspace.write_text(
            "memories/new-capability.md",
            "The parser handles empty records. Shipped test: `tests/test_parser.py`.",
        )
        self.assertEqual(durable_note_problems(self.workspace, self.baseline), ())

    def test_repair_loop_exposes_claim_and_accepts_clean_rewrite(self) -> None:
        self.workspace.write_text("Dockerfile", "FROM scratch\n")
        self.workspace.write_text("main.py", "print('ok')\n")
        self.workspace.write_text(
            "memories/new-capability.md", "Smoke-tested with an integration fixture."
        )
        session = _RepairingSession(self.workspace)
        outcome = _repaired(
            self.workspace, session, _Deadline(), "original", self.baseline
        )
        self.assertEqual(outcome, "repaired")
        self.assertEqual(len(session.openings), 1)
        self.assertIn("transient test or execution-result claim", session.openings[0])
        self.assertEqual(durable_note_problems(self.workspace, self.baseline), ())


if __name__ == "__main__":
    unittest.main()
