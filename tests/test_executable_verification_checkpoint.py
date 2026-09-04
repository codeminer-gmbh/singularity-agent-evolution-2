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
            # Passing an ordinary run used to be enough to finalize here.
            ModelReply(text="Implemented and ordinary example passed.", tool_calls=()),
            ModelReply(
                text="",
                tool_calls=(ToolCall("call-2", "run_command", '{"command":["python","oracle_check.py"]}'),),
                output=({"type": "function_call", "call_id": "call-2"},),
            ),
            ModelReply(text="Implemented; ordinary and differential checks passed.", tool_calls=()),
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
        if len(self.called) == 1:
            return ToolOutcome("ordinary=3 boundary=0; assertions passed", False)
        return ToolOutcome("exhaustive small cases agree with independent oracle", False)


def settings(tmp_path, task):
    return AgentSettings(
        mode=AgentMode.PROBE,
        task=task,
        workspace=tmp_path,
        source_root=Path.cwd(),
        model=ModelAccess(None, "unused", 60, None),
        time_budget_seconds=60,
        max_steps=7,
    )


def test_executable_probe_requires_run_then_separate_adversarial_challenge(tmp_path):
    model = ScriptedModel()
    tools = FakeTools()

    outcome = _session(model, tools, Deadline(60), settings(tmp_path, "Implement a function in solution.py")).run(
        instructions="work", opening="task"
    )

    assert outcome.finished
    assert outcome.summary == "Implemented; ordinary and differential checks passed."
    assert outcome.steps == 5
    assert [call[0] for call in tools.called] == ["run_command", "run_command"]
    assert "no command has exercised it yet" in model.conversations[1][-1]["content"]
    adversarial = model.conversations[3][-1]["content"]
    assert "Adversarial verification checkpoint" in adversarial
    assert "independent oracle" in adversarial
    assert "Do not merely repeat" in adversarial


def test_classifier_is_conservative_about_non_executable_writing():
    assert asks_for_executable_source("Write a Python program that reads stdin")
    assert asks_for_executable_source("Create output/solution.py")
    assert not asks_for_executable_source("Explain how this algorithm works")
    assert not asks_for_executable_source("Write a report on parser design")
