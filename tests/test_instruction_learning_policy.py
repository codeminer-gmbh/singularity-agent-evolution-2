"""Regression checks for ledger-driven instruction learning."""

import unittest

from evolving_agent.prompts import improvement_instructions, probe_instructions


class InstructionLearningPolicyTests(unittest.TestCase):
    def test_outcomes_are_not_treated_as_causal_endorsements(self) -> None:
        instructions = " ".join(improvement_instructions().split())
        self.assertIn("A promotion says the whole version won", instructions)
        self.assertIn("it does not endorse every change", instructions)
        self.assertIn("Repeated losses sharing a decision error outweigh", instructions)
        self.assertIn("including any already closed policy you will not revisit", instructions)

    def test_instruction_edits_replace_failed_rewards_instead_of_accumulating(self) -> None:
        instructions = " ".join(improvement_instructions().split())
        self.assertIn("do not append advice by default", instructions)
        self.assertIn("identify what the old wording rewarded", instructions)
        self.assertIn("delete obsolete or conflicting heuristics", instructions)
        self.assertNotIn("the smallest change that attacks its mechanism", instructions)

    def test_verification_must_discriminate_the_selected_mechanism(self) -> None:
        instructions = " ".join(improvement_instructions().split())
        self.assertIn("would fail before the change for the named mechanism", instructions)
        self.assertIn("an unrelated green suite cannot validate the bet", instructions)

    def test_probe_prefers_contract_matching_primitives_without_extra_strictness(self) -> None:
        instructions = " ".join(probe_instructions().split())
        self.assertIn("standard library or mature dependency", instructions)
        self.assertIn("Prefer that primitive over handwritten parsing", instructions)
        self.assertIn("adapt it only where the contract differs", instructions)
        self.assertIn("strict mode, rejection rule, or public immutability", instructions)
        self.assertIn("that the task did not ask for", instructions)


if __name__ == "__main__":
    unittest.main()
