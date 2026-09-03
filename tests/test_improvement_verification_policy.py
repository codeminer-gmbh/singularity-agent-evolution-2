"""Regression checks for the improvement run's evidence policy."""

import unittest

from evolving_agent.prompts import improvement_instructions, probe_instructions


class ImprovementVerificationPolicyTests(unittest.TestCase):
    def test_requires_executed_command_and_observed_result(self) -> None:
        instructions = " ".join(improvement_instructions().split())
        self.assertIn("execute the closest practical fixture", instructions)
        self.assertIn("inspect its exit status and decisive output", instructions)
        self.assertIn("quote the command actually run", instructions)
        self.assertIn("behavior remains unverified", instructions)

    def test_separates_transient_run_evidence_from_durable_notes(self) -> None:
        instructions = " ".join(improvement_instructions().split())
        self.assertIn("Keep transient evidence separate from durable notes", instructions)
        self.assertIn("executable test or fixture is also present in the tree", instructions)
        self.assertIn("Never turn code inspection", instructions)

    def test_policy_does_not_leak_into_probe_role(self) -> None:
        self.assertNotIn("durable notes", probe_instructions())


if __name__ == "__main__":
    unittest.main()
