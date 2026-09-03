"""The public probe path must not publish its first unreviewed draft."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import run_probe
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess


class SelfCorrectingModel:
    """First answers the happy path, then corrects only when forced to audit."""

    instances: list["SelfCorrectingModel"] = []

    def __init__(self, access: ModelAccess) -> None:
        self.conversations = []
        self.__class__.instances.append(self)

    def reply(self, *, conversation, tools=()):
        self.conversations.append(tuple(conversation))
        if len(self.conversations) == 1:
            return ModelReply(
                text="The first declared valid source always wins.",
                tool_calls=(),
                output=(),
            )
        request = str(conversation[-1].get("content", ""))
        if "ordering, precedence, fallback, concurrency, or lifecycle" in request:
            return ModelReply(
                text="Corrected after conflict review: the last encountered valid source wins.",
                tool_calls=(),
                output=(),
            )
        return ModelReply(text="UNREVIEWED", tool_calls=(), output=())


class ReviewToolModel:
    """Uses a tool only after the mandatory completion audit requests proof."""

    instances: list["ReviewToolModel"] = []

    def __init__(self, access: ModelAccess) -> None:
        self.calls = 0
        self.__class__.instances.append(self)

    def reply(self, *, conversation, tools=()):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(text="The material says ALPHA (unverified).", tool_calls=(), output=())
        observations = [
            item.get("output", "")
            for item in conversation
            if item.get("type") == "function_call_output"
        ]
        if observations:
            answer = "Verified during review: " + (
                "OMEGA-913" if any("OMEGA-913" in value for value in observations) else "missing"
            )
            return ModelReply(text=answer, tool_calls=(), output=())
        return ModelReply(
            text="",
            tool_calls=(ToolCall("review-read", "read_file", '{"path":"materials/fact.txt"}'),),
            output=(
                {
                    "type": "function_call",
                    "call_id": "review-read",
                    "name": "read_file",
                    "arguments": '{"path":"materials/fact.txt"}',
                },
            ),
        )


class CompletionReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        SelfCorrectingModel.instances.clear()

    def test_probe_returns_reviewed_answer_not_first_draft(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = AgentSettings(
                mode=AgentMode.PROBE,
                task=(
                    "State the winner when a valid source declared first is "
                    "encountered before a second valid source."
                ),
                workspace=Path(root),
                source_root=Path.cwd(),
                materials=None,
                output=None,
                model=ModelAccess(None, "fake-model", 60, None),
                max_steps=1,
                time_budget_seconds=60,
            )
            with patch("evolving_agent.modes.ModelClient", SelfCorrectingModel):
                report = run_probe(settings)

        self.assertTrue(report.succeeded, report.detail)
        self.assertEqual(
            "Corrected after conflict review: the last encountered valid source wins.",
            report.answer,
        )
        model = SelfCorrectingModel.instances[0]
        self.assertEqual(2, len(model.conversations))
        reviewed = model.conversations[1]
        self.assertTrue(any(item.get("role") == "assistant" for item in reviewed))
        self.assertIn("Do not merely endorse", reviewed[-1]["content"])

    def test_review_keeps_tools_and_replaces_unsupported_draft_with_evidence(self) -> None:
        ReviewToolModel.instances.clear()
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as materials:
            Path(materials, "fact.txt").write_text("The actual code is OMEGA-913.\n")
            settings = AgentSettings(
                mode=AgentMode.PROBE,
                task="Read fact.txt and report its code.",
                workspace=Path(root),
                source_root=Path.cwd(),
                materials=Path(materials),
                output=None,
                model=ModelAccess(None, "fake-model", 60, None),
                max_steps=2,
                time_budget_seconds=60,
            )
            with patch("evolving_agent.modes.ModelClient", ReviewToolModel):
                report = run_probe(settings)

        self.assertTrue(report.succeeded, report.detail)
        self.assertEqual("Verified during review: OMEGA-913", report.answer)
        self.assertEqual(3, ReviewToolModel.instances[0].calls)


if __name__ == "__main__":
    unittest.main()
