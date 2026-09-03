"""Regression checks for contract-specified diagnostic behavior in probe tasks."""

import unittest

from evolving_agent.prompts import improvement_instructions, probe_instructions


class ProbeErrorContractPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instructions = " ".join(probe_instructions().split())

    def test_treats_specified_errors_as_observable_output(self) -> None:
        self.assertIn("treat its error surface as output", self.instructions)
        self.assertIn("each distinct failure category", self.instructions)
        self.assertIn("required source location or offending token", self.instructions)
        self.assertIn("precedence when one input has multiple defects", self.instructions)

    def test_rejects_catch_all_flattening_and_requires_discriminating_fixture(self) -> None:
        self.assertIn("do not flatten actionable failures into one catch-all message", self.instructions)
        self.assertIn("malformed fixtures from at least two different constructs", self.instructions)
        self.assertIn("externally visible diagnostics remain distinguishable", self.instructions)

    def test_preserves_permissive_unspecified_cases(self) -> None:
        self.assertIn("unless the contract explicitly requires that", self.instructions)
        self.assertIn("Do not invent detailed errors", self.instructions)

    def test_probe_policy_does_not_leak_into_improvement_role(self) -> None:
        self.assertNotIn("error surface as output", improvement_instructions())


if __name__ == "__main__":
    unittest.main()
