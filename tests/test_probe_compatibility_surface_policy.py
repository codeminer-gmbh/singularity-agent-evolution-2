"""Regression checks for preserving unspecified and inherited public behavior."""

import unittest

from evolving_agent.prompts import improvement_instructions, probe_instructions


class ProbeCompatibilitySurfacePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instructions = " ".join(probe_instructions().split())

    def test_inventories_required_inherited_and_unspecified_behavior(self) -> None:
        self.assertIn("behavior the task requires", self.instructions)
        self.assertIn("behavior inherited from an existing API, parser default, or standard type", self.instructions)
        self.assertIn("behavior the task leaves unspecified", self.instructions)
        self.assertIn("Preserve groups (2) and (3)", self.instructions)

    def test_names_common_compatibility_regressions(self) -> None:
        self.assertIn("do not enable stricter parsing", self.instructions)
        self.assertIn("freeze public attributes", self.instructions)
        self.assertIn("replace a standard semantic type with a lookalike", self.instructions)
        self.assertIn("equality, hashing, arithmetic, mutation, and edge inputs", self.instructions)

    def test_requires_a_counterexample_to_overrestriction(self) -> None:
        self.assertIn("run one permissive or legacy case", self.instructions)
        self.assertIn("tempting stricter implementation would reject", self.instructions)
        self.assertIn("as well as the required new case", self.instructions)

    def test_probe_policy_does_not_leak_into_improvement_role(self) -> None:
        self.assertNotIn("standard semantic type with a lookalike", improvement_instructions())


if __name__ == "__main__":
    unittest.main()
