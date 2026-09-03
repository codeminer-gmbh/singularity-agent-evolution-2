"""Contract tests for the successor memory-evidence publishing gate."""

import tempfile
import unittest
from pathlib import Path

from evolving_agent.successor import successor_problems
from evolving_agent.workspace import Workspace


class MemoryEvidenceGateTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Workspace:
        root.mkdir(parents=True, exist_ok=True)
        (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
        return Workspace(root)

    def test_new_verification_claim_needs_named_shipped_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self._workspace(root / "baseline")
            candidate = self._workspace(root / "candidate")
            candidate.write_text("memories/change.md", "Verified the feature.\n")

            problems = successor_problems(candidate, baseline_root=baseline.root)

            self.assertEqual(
                problems,
                ("memories/change.md claims execution evidence but names no shipped test or fixture",),
            )

    def test_verification_conjugations_need_named_evidence(self) -> None:
        for claim in (
            "This check verifies the feature.\n",
            "Please verify the feature.\n",
            "Verification confirms the feature.\n",
        ):
            with self.subTest(claim=claim), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                baseline = self._workspace(root / "baseline")
                candidate = self._workspace(root / "candidate")
                candidate.write_text("memories/change.md", claim)

                self.assertEqual(
                    successor_problems(candidate, baseline_root=baseline.root),
                    (
                        "memories/change.md claims verification but names no shipped "
                        "test or fixture",
                    ),
                )


    def test_smoke_or_integration_claim_needs_named_shipped_evidence(self) -> None:
        for claim in (
            "An integration check covers the change.\n",
            "An end-to-end exercise covers the change.\n",
        ):
            with self.subTest(claim=claim), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                baseline = self._workspace(root / "baseline")
                candidate = self._workspace(root / "candidate")
                candidate.write_text("memories/change.md", claim)

                self.assertEqual(
                    successor_problems(candidate, baseline_root=baseline.root),
                    (
                        "memories/change.md claims execution evidence but names no "
                        "shipped test or fixture",
                    ),
                )

    def test_named_existing_test_allows_verification_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self._workspace(root / "baseline")
            candidate = self._workspace(root / "candidate")
            candidate.write_text("tests/test_feature.py", "def test_feature(): pass\n")
            candidate.write_text(
                "memories/change.md", "Verified by `tests/test_feature.py`.\n"
            )

            self.assertEqual(successor_problems(candidate, baseline_root=baseline.root), ())

    def test_unchanged_historical_claim_is_not_reaudited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self._workspace(root / "baseline")
            baseline.write_text("memories/old.md", "Verified long ago.\n")
            candidate = self._workspace(root / "candidate")
            candidate.write_text("memories/old.md", "Verified long ago.\n")

            self.assertEqual(successor_problems(candidate, baseline_root=baseline.root), ())


if __name__ == "__main__":
    unittest.main()
