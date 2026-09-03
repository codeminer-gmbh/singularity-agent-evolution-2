"""Regression tests for probe finalization after a tool-using limit stop."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import run_probe
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess


class TranscriptAwareModel:
    """A deterministic model that requires its tool observation to finalize."""

    instances: list["TranscriptAwareModel"] = []

    def __init__(self, access: ModelAccess) -> None:
        self.calls = 0
        self.final_conversation = ()
        self.__class__.instances.append(self)

    def reply(self, *, conversation, tools=()):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(
                text="",
                tool_calls=(ToolCall("read-1", "read_file", '{"path":"materials/fact.txt"}'),),
                output=(
                    {
                        "type": "function_call",
                        "call_id": "read-1",
                        "name": "read_file",
                        "arguments": '{"path":"materials/fact.txt"}',
                    },
                ),
            )
        self.final_conversation = tuple(conversation)
        observations = [
            item.get("output", "")
            for item in conversation
            if item.get("type") == "function_call_output"
        ]
        if any("ORCHID-742" in output for output in observations):
            text = "The file's code is ORCHID-742."
        else:
            text = "I cannot answer without the tool result."
        return ModelReply(text=text, tool_calls=(), output=())


class LimitFinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        TranscriptAwareModel.instances.clear()

    def test_run_probe_finalizes_from_the_tool_transcript_at_step_limit(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as materials:
            Path(materials, "fact.txt").write_text("The code is ORCHID-742.\n")
            settings = AgentSettings(
                mode=AgentMode.PROBE,
                task="Read fact.txt and report its code.",
                workspace=Path(root),
                source_root=Path.cwd(),
                materials=Path(materials),
                output=None,
                model=ModelAccess(None, "fake-model", 60, None),
                max_steps=1,
                time_budget_seconds=60,
            )
            with patch("evolving_agent.modes.ModelClient", TranscriptAwareModel):
                report = run_probe(settings)

        self.assertTrue(report.succeeded, report.detail)
        self.assertEqual("The file's code is ORCHID-742.", report.answer)
        model = TranscriptAwareModel.instances[0]
        self.assertEqual(4, model.calls)
        self.assertTrue(
            any(
                item.get("type") == "function_call_output"
                and "ORCHID-742" in item.get("output", "")
                for item in model.final_conversation
            )
        )


if __name__ == "__main__":
    unittest.main()
