from pathlib import Path

from evolving_agent.mcp_client import ToolDescription, ToolOutcome
from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import _session
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess
from evolving_agent.session import Deadline
from evolving_agent.verification import asks_for_executable_source


class ScriptedModel:
    def __init__(self):
        self.conversations = []
        self.replies = [
            ModelReply(text="Implemented.", tool_calls=()),
            ModelReply(
                text="",
                tool_calls=(ToolCall("call-1", "run_command", '{"command":["python","solution.py"]}'),),
                output=({"type": "function_call", "call_id": "call-1"},),
            ),
            ModelReply(text="Implemented and exercised.", tool_calls=()),
        ]

    def reply(self, *, conversation, tools=()):
        self.conversations.append(tuple(conversation))
        return self.replies.pop(0)


class FakeTools:
    def __init__(self):
        self.called = []

    def list_tools(self):
        return [ToolDescription("run_command", "Run command", {"type": "object"})]

    def call(self, name, arguments):
        self.called.append((name, arguments))
        return ToolOutcome("ordinary=3 boundary=0; all assertions passed", False)


def settings(tmp_path, task):
    return AgentSettings(
        mode=AgentMode.PROBE,
        task=task,
        workspace=tmp_path,
        source_root=Path.cwd(),
        model=ModelAccess(None, "unused", 60, None),
        time_budget_seconds=60,
        max_steps=5,
    )


def test_executable_probe_challenges_unexercised_completion_then_accepts_run(tmp_path):
    model = ScriptedModel()
    tools = FakeTools()

    outcome = _session(model, tools, Deadline(60), settings(tmp_path, "Implement a function in solution.py")).run(
        instructions="work", opening="task"
    )

    assert outcome.finished
    assert outcome.summary == "Implemented and exercised."
    assert outcome.steps == 3
    assert tools.called[0][0] == "run_command"
    second_exchange = model.conversations[1]
    assert "no command has exercised it yet" in second_exchange[-1]["content"]
    assert "syntax check alone" in second_exchange[-1]["content"]


def test_classifier_is_conservative_about_non_executable_writing():
    assert asks_for_executable_source("Write a Python program that reads stdin")
    assert asks_for_executable_source("Create output/solution.py")
    assert not asks_for_executable_source("Explain how this algorithm works")
    assert not asks_for_executable_source("Write a report on parser design")
