"""Regression checks that probe planning preserves contract-local validation scope."""

import unittest

from evolving_agent.prompts import probe_instructions


class ProbeValidationScopePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instructions = " ".join(probe_instructions().split())

    def test_maps_each_rule_to_the_branch_where_it_applies(self) -> None:
        self.assertIn(
            "for each boundary, validation rule, and malformed-input behavior, record "
            "the operation, branch, or state where the contract says it applies",
            self.instructions,
        )

    def test_forbids_hoisting_branch_local_limits_into_shared_validation(self) -> None:
        self.assertIn(
            "Enforce a rule only in that scope rather than hoisting it into shared "
            "parsing or a common constructor",
            self.instructions,
        )
        self.assertIn(
            "do not silently turn an unspecified case or an unlisted operation into an error",
            self.instructions,
        )

    def test_replaces_the_scope_blind_boundary_checklist(self) -> None:
        self.assertNotIn("every stated boundary", self.instructions)
        self.assertIn("reconcile each conclusion and example with that contract map", self.instructions)


if __name__ == "__main__":
    unittest.main()
