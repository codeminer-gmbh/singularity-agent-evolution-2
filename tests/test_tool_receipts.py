import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evolving_agent.mcp_client import McpError, ToolOutcome
from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import _record_tool_calls
from evolving_agent.session import Deadline, ToolAgentSession


class Replies:
    def __init__(self):
        self.calls = 0

    def reply(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            calls = (
                ToolCall("ok", "inspect", '{"path":"fixture.txt"}'),
                ToolCall("bad", "inspect", "not json"),
                ToolCall("broken", "inspect", "{}"),
                ToolCall("refused", "inspect", '{"path":"restricted.txt"}'),
            )
            return ModelReply("", calls, ())
        return ModelReply("done", (), ())


class Tools:
    def call(self, _name, arguments):
        if not arguments:
            raise McpError("boundary unavailable")
        if arguments.get("path") == "restricted.txt":
            return ToolOutcome("access refused", True)
        return ToolOutcome("fixture contains expected value", False)


class ToolReceiptTests(unittest.TestCase):
    def test_receipts_preserve_observed_outcomes_and_are_written(self):
        session = ToolAgentSession(Replies(), Tools(), deadline=Deadline(10), max_steps=2)
        outcome = session.run(instructions="test", opening="test")
        self.assertTrue(outcome.finished)
        self.assertEqual([r.is_error for r in session.tool_receipts], [False, True, True, True])
        self.assertEqual(
            [r.status for r in session.tool_receipts],
            ["success", "invalid_arguments", "boundary_error", "tool_error"],
        )
        self.assertEqual(session.tool_receipts[0].observation, "fixture contains expected value")
        self.assertIn("arguments are not valid JSON", session.tool_receipts[1].observation)
        self.assertIn("boundary unavailable", session.tool_receipts[2].observation)
        self.assertEqual(session.tool_receipts[3].observation, "[error] access refused")
        with tempfile.TemporaryDirectory() as directory:
            _record_tool_calls(SimpleNamespace(output=Path(directory)), session)
            receipts = json.loads((Path(directory) / ".meta/tool_receipts.json").read_text())
        self.assertEqual(receipts[0]["name"], "inspect")
        self.assertEqual(receipts[0]["observation"], "fixture contains expected value")
        self.assertTrue(receipts[2]["is_error"])
        self.assertEqual(receipts[1]["status"], "invalid_arguments")
        self.assertEqual(receipts[2]["status"], "boundary_error")
        self.assertEqual(receipts[3]["status"], "tool_error")


if __name__ == "__main__":
    unittest.main()
