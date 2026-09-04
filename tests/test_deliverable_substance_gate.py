"""A prose-only source artifact cannot survive probe completion review."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolving_agent.deliverables import source_delivery_problems
from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import run_probe
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess


class StubThenRepairModel:
    instances: list["StubThenRepairModel"] = []

    def __init__(self, access: ModelAccess) -> None:
        self.calls = 0
        self.transcripts = []
        self.__class__.instances.append(self)

    @staticmethod
    def tool(identifier: str, name: str, arguments: dict) -> ModelReply:
        raw = json.dumps(arguments)
        return ModelReply(
            text="", tool_calls=(ToolCall(identifier, name, raw),),
            output=({"type": "function_call", "call_id": identifier,
                     "name": name, "arguments": raw},),
        )

    def reply(self, *, conversation, tools=()):
        self.calls += 1
        self.transcripts.append(tuple(conversation))
        if self.calls == 1:
            return self.tool("stub", "write_file", {
                "path": "output/solution.py",
                "content": '"""A robust streaming parser would be implemented here."""\n',
            })
        if self.calls == 2:
            return ModelReply(text="Delivered the requested parser.", tool_calls=(), output=())
        if self.calls == 3:
            # Deliberately reproduce the ledger failure: semantic review overlooks
            # that the artifact has no code at all.
            return ModelReply(text="No defect found.", tool_calls=(), output=())
        if self.calls == 4:
            return ModelReply(text="Final answer: solution.py is complete.", tool_calls=(), output=())
        if self.calls == 5:
            return self.tool("repair", "write_file", {
                "path": "output/solution.py",
                "content": (
                    "class StreamParser:\n"
                    "    def __init__(self): self.total = 0\n"
                    "    def feed(self, chunk):\n"
                    "        self.total += len(chunk)\n"
                    "        return self.total\n"
                ),
            })
        if self.calls == 6:
            return self.tool("prove", "run_command", {
                "command": ["python", "-c", (
                    "from output.solution import StreamParser; "
                    "p=StreamParser(); assert p.feed(b'ab')==2; assert p.feed(b'c')==3"
                )],
            })
        return ModelReply(
            text="Repaired solution.py; the two-chunk state test passed.",
            tool_calls=(), output=(),
        )


class NeverRepairModel(StubThenRepairModel):
    instances: list["NeverRepairModel"] = []

    def reply(self, *, conversation, tools=()):
        self.calls += 1
        self.transcripts.append(tuple(conversation))
        if self.calls == 1:
            return self.tool("stub", "write_file", {
                "path": "output/solution.py",
                "content": '"""Description only."""\n',
            })
        return ModelReply(text="The implementation is complete.", tool_calls=(), output=())


class DeliverableSubstanceGateTests(unittest.TestCase):
    def test_docstring_only_python_forces_tool_enabled_repair(self) -> None:
        StubThenRepairModel.instances.clear()
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as output:
            settings = AgentSettings(
                mode=AgentMode.PROBE,
                task="Implement a stateful StreamParser in output/solution.py and test it.",
                workspace=Path(root), source_root=Path.cwd(), materials=None,
                output=Path(output), model=ModelAccess(None, "fake-model", 60, None),
                max_steps=2, time_budget_seconds=60,
            )
            with patch("evolving_agent.modes.ModelClient", StubThenRepairModel):
                report = run_probe(settings)

            self.assertTrue(report.succeeded, report.detail)
            self.assertEqual((), source_delivery_problems(Path(output)))
            self.assertIn("class StreamParser", Path(output, "solution.py").read_text())
            self.assertIn("two-chunk state test passed", report.answer)

        model = StubThenRepairModel.instances[0]
        self.assertEqual(7, model.calls)
        repair_transcript = model.transcripts[4]
        self.assertTrue(any(
            "has no executable implementation" in str(item.get("content", ""))
            for item in repair_transcript
        ))

    def test_unrepaired_stub_cannot_be_published_by_text_finalizer(self) -> None:
        NeverRepairModel.instances.clear()
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as output:
            settings = AgentSettings(
                mode=AgentMode.PROBE, task="Implement output/solution.py.",
                workspace=Path(root), source_root=Path.cwd(), materials=None,
                output=Path(output), model=ModelAccess(None, "fake-model", 60, None),
                max_steps=2, time_budget_seconds=60,
            )
            with patch("evolving_agent.modes.ModelClient", NeverRepairModel):
                report = run_probe(settings)

        self.assertFalse(report.succeeded)
        self.assertEqual(
            "I could not complete the requested source-code deliverable.", report.answer
        )
        self.assertIn("has no executable implementation", report.detail)
        self.assertEqual(6, NeverRepairModel.instances[0].calls)

    def test_placeholders_and_syntax_errors_are_distinguished_from_code(self) -> None:
        with tempfile.TemporaryDirectory() as output:
            root = Path(output)
            (root / "placeholder.py").write_text(
                "import json\n\ndef parse(data):\n    raise NotImplementedError\n"
            )
            (root / "broken.py").write_text("def parse(:\n")
            problems = source_delivery_problems(root)
        self.assertTrue(any("placeholder.py has no executable" in item for item in problems))
        self.assertTrue(any("broken.py is not valid" in item for item in problems))


if __name__ == "__main__":
    unittest.main()
