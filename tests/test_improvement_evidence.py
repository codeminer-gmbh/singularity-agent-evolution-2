import unittest

from evolving_agent.modes import _improvement_answer
from evolving_agent.session import SessionOutcome
from evolving_agent.prompts import improvement_instructions


class ImprovementEvidencePolicyTests(unittest.TestCase):
    def test_instructions_require_observed_behavioral_evidence(self) -> None:
        instructions = improvement_instructions()
        self.assertIn("Evidence before narrative:", instructions)
        self.assertIn("focused command, test, or tool call", instructions)
        self.assertIn("say the behaviour is unverified", instructions)
        self.assertIn(
            "never invent output, test coverage, or a successful build",
            " ".join(instructions.split()),
        )

    def test_record_first_policy_replaces_disproven_prompt_bias(self) -> None:
        instructions = improvement_instructions()
        self.assertIn("record, hypothesis, commitment", instructions)
        self.assertIn("read `materials/ledger.md`", instructions)
        self.assertIn("concrete observable acceptance case", instructions)
        self.assertIn("rejected change without naming", instructions)
        self.assertIn("non-empty final report", instructions)
        self.assertNotIn("A new tool is rarely the answer", instructions)
        self.assertNotIn("A large, well-verified capability gain", instructions)
        self.assertLess(
            instructions.index("read `materials/ledger.md`"),
            instructions.index("write `memories/round-plan.md`"),
        )

    def test_process_claim_is_limited_to_checks_it_performs(self) -> None:
        # A model summary is not proof and must not become the runner's record.
        answer = _improvement_answer(
            SessionOutcome(
                finished=True,
                summary="I verified an imaginary capability.",
                steps=1,
                reason="model assertion",
            )
        )
        self.assertIn("automatic usability validation", answer)
        self.assertIn("successor_problems returned no problems", answer)
        self.assertNotIn("imaginary capability", answer)
        self.assertNotIn("verified capability", answer.lower())


if __name__ == "__main__":
    unittest.main()
