"""Regression checks for bounded state at contract-defined degenerate domains."""

import unittest

from evolving_agent.prompts import probe_instructions


class ProbeDegenerateStatePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instructions = " ".join(probe_instructions().split())

    def test_empty_semantic_domain_is_eliminated_before_state_allocation(self) -> None:
        self.assertIn("Before choosing a state representation, simplify any degenerate domain", self.instructions)
        self.assertIn("short-circuit without retaining them", self.instructions)
        self.assertIn("instead of sending them through the general state machine", self.instructions)

    def test_streaming_fixture_checks_storage_not_only_outputs(self) -> None:
        self.assertIn("run a repetitive adversarial fixture", self.instructions)
        self.assertIn("storage tracks semantically relevant items rather than total input", self.instructions)
        self.assertIn("output-only examples do not verify that bound", self.instructions)

    def test_generic_small_fixture_is_no_longer_the_whole_reward(self) -> None:
        self.assertNotIn("Then exercise at least one small fixture", self.instructions)
        self.assertIn("For other contracts, exercise at least one small fixture", self.instructions)


if __name__ == "__main__":
    unittest.main()
