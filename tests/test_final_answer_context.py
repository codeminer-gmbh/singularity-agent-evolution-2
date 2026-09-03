"""Executable contract for the reserve final-answer exchange."""

import unittest
from typing import Any, Mapping, Sequence

from evolving_agent.mcp_client import ToolOutcome
from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import _final_answer
from evolving_agent.session import Deadline, ToolAgentSession


class RecordingModel:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Mapping[str, Any]], Sequence[Any]]] = []
        self._replies = [
            ModelReply(
                text="",
                tool_calls=(ToolCall("call-1", "read", "{}"),),
                output=(
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "read",
                        "arguments": "{}",
                    },
                ),
            ),
            ModelReply(text="answer based on inspected result", tool_calls=()),
        ]

    def reply(
        self, *, conversation: Sequence[Mapping[str, Any]], tools: Sequence[Any] = ()
    ) -> ModelReply:
        self.calls.append((list(conversation), tools))
        return self._replies.pop(0)


class FixedTools:
    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        self.last_call = (name, dict(arguments))
        return ToolOutcome("DISTINGUISHING INSPECTED FACT", False)


class FinalAnswerContextTests(unittest.TestCase):
    def test_step_limit_final_answer_keeps_tool_observation_and_disables_tools(self) -> None:
        model = RecordingModel()
        session = ToolAgentSession(
            model, FixedTools(), deadline=Deadline(30), max_steps=1
        )
        outcome = session.run(instructions="system", opening="task")
        report = _final_answer(session, outcome)

        self.assertTrue(report.succeeded)
        self.assertEqual(report.answer, "answer based on inspected result")
        final_conversation, final_tools = model.calls[-1]
        self.assertEqual(final_tools, ())
        self.assertIn(
            "DISTINGUISHING INSPECTED FACT",
            [item.get("output") for item in final_conversation],
        )
        self.assertEqual(final_conversation[-1]["role"], "user")

    def test_unrun_session_final_answer_has_no_stale_context(self) -> None:
        model = RecordingModel()
        session = ToolAgentSession(
            model, FixedTools(), deadline=Deadline(30), max_steps=1
        )
        reply = session.final_reply("finish")
        self.assertEqual(reply.text, "")
        self.assertEqual(model.calls[0][0], [{"role": "user", "content": "finish"}])


if __name__ == "__main__":
    unittest.main()
