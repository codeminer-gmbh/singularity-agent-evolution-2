"""Public-path proof that probe runs receive the adversarial contract protocol."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolving_agent.model import ModelReply
from evolving_agent.modes import run_probe
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess


class CapturingModel:
    conversations: list[tuple[dict, ...]] = []

    def __init__(self, access: ModelAccess) -> None:
        pass

    def reply(self, *, conversation, tools=()):
        self.__class__.conversations.append(tuple(conversation))
        return ModelReply(text="done", tool_calls=(), output=())


class ProbeContractReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        CapturingModel.conversations.clear()

    def test_public_probe_receives_contract_matrix_and_non_happy_path_rule(self) -> None:
        conversation = self._run_and_capture("Implement the requested parser.")
        system = " ".join(conversation[0]["content"].split())

        self.assertIn("every normative rule", system)
        self.assertIn("representative input or event", system)
        self.assertIn("expected observable result", system)
        self.assertIn("interactions the supplied tests omit", system)
        self.assertIn("declaration order, encounter order, explicit values", system)
        self.assertIn("State the winner before implementing it", system)
        self.assertIn("after all supplied tests pass", system)
        self.assertIn("is not evidence for an untested interaction", system)

    def test_lifecycle_decision_table_covers_detached_and_scheduling_races(self) -> None:
        conversation = self._run_and_capture("Repair the concurrent lifecycle.")
        system = " ".join(conversation[0]["content"].split())

        for state in (
            "owned/visible",
            "detached-but-running",
            "completed-but-not-yet-settled",
            "terminated",
        ):
            self.assertIn(state, system)
        for distinguishing_case in (
            "completion before its scheduled callback",
            "detachment followed by cancellation suppression",
            "shutdown while detached work is still live",
            "Cleanup must reach every live resource",
            "must not accept a new participant",
        ):
            self.assertIn(distinguishing_case, system)

    def test_protocol_is_conditional_for_non_software_questions(self) -> None:
        conversation = self._run_and_capture("Summarize the supplied article.")
        system = " ".join(conversation[0]["content"].split())
        self.assertIn("do not invent software tests for a factual or", system)

    @staticmethod
    def _run_and_capture(task: str) -> tuple[dict, ...]:
        with tempfile.TemporaryDirectory() as root:
            settings = AgentSettings(
                mode=AgentMode.PROBE,
                task=task,
                workspace=Path(root),
                source_root=Path.cwd(),
                materials=None,
                output=None,
                model=ModelAccess(None, "fake-model", 60, None),
                max_steps=1,
                time_budget_seconds=60,
            )
            with patch("evolving_agent.modes.ModelClient", CapturingModel):
                report = run_probe(settings)
        assert report.succeeded and report.answer == "done"
        return CapturingModel.conversations[-1]


if __name__ == "__main__":
    unittest.main()
