"""The session loop: the ledger that survives truncation, and completion stages."""

from evolving_agent.mcp_client import ToolDescription, ToolOutcome
from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.session import Deadline, ToolAgentSession


def _tool_reply(identifier: str, name: str, arguments: str) -> ModelReply:
    return ModelReply(
        text="",
        tool_calls=(ToolCall(identifier, name, arguments),),
        output=(
            {"type": "function_call", "call_id": identifier, "name": name, "arguments": arguments},
        ),
    )


def _text_reply(text: str) -> ModelReply:
    return ModelReply(text=text, tool_calls=(), output=())


class ScriptedModel:
    """Answers from a fixed script and keeps every conversation it was shown."""

    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = list(replies)
        self.conversations: list[tuple[dict, ...]] = []

    def reply(self, *, conversation, tools=()):
        self.conversations.append(tuple(conversation))
        return self.replies.pop(0)


class CommandTools:
    """A command runner whose exit code is the first argument."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self):
        return [ToolDescription("run_command", "Run a command", {"type": "object"})]

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        code = int(arguments["command"][-1])
        return ToolOutcome(f"$ {' '.join(arguments['command'])}\n[exit code {code}]\n--- stdout ---\n", False)


def _session(model, tools, max_steps=8) -> ToolAgentSession:
    return ToolAgentSession(model, tools, tools.list_tools(), deadline=Deadline(60), max_steps=max_steps)


def test_a_failed_command_stays_in_the_ledger_after_a_passing_rerun() -> None:
    model = ScriptedModel(
        [
            _tool_reply("c1", "run_command", '{"command": ["python", "check.py", "1"]}'),
            _tool_reply("c2", "run_command", '{"command": ["python", "check.py", "0"]}'),
            _text_reply("Repaired and rerun."),
        ]
    )
    session = _session(model, CommandTools())

    outcome = session.run(instructions="work", opening="task")

    assert outcome.finished and outcome.summary == "Repaired and rerun."
    ledger = model.conversations[-1][-1]["content"]
    assert ledger.startswith("Machine-maintained verification ledger")
    assert '"result": "FAILED [exit code 1]"' in ledger
    assert '"result": "PASSED (exit code 0)"' in ledger
    assert ledger.index("FAILED") < ledger.index("PASSED")
    # The first exchange had nothing to reconcile, so no ledger was shown.
    assert all(item.get("role") != "user" or "ledger" not in str(item.get("content"))
               for item in model.conversations[0])


def test_completion_stages_run_in_order_and_callables_may_decline() -> None:
    model = ScriptedModel(
        [_text_reply("draft"), _text_reply("reviewed"), _text_reply("final")]
    )
    session = _session(model, CommandTools())
    seen: list[str] = []

    def silent(active: ToolAgentSession) -> str | None:
        seen.append("silent")
        return None

    def speaking(active: ToolAgentSession) -> str | None:
        seen.append("speaking")
        return "Second look."

    outcome = session.run(
        instructions="work", opening="task", completion=(silent, speaking, "Last word.")
    )

    assert outcome.summary == "final"
    assert seen == ["silent", "speaking"]
    assert model.conversations[1][-1] == {"role": "user", "content": "Second look."}
    assert model.conversations[2][-1] == {"role": "user", "content": "Last word."}
    # The draft is kept in the transcript so a stage can refer to it.
    assert {"role": "assistant", "content": "draft"} in model.conversations[1]


def test_stages_have_their_own_allowance_and_keep_the_tools() -> None:
    model = ScriptedModel(
        [
            _tool_reply("c1", "run_command", '{"command": ["python", "a.py", "0"]}'),
            _text_reply("draft after the only ordinary step"),
            _tool_reply("c2", "run_command", '{"command": ["python", "b.py", "0"]}'),
            _text_reply("checked in review"),
        ]
    )
    tools = CommandTools()
    session = _session(model, tools, max_steps=1)

    outcome = session.run(instructions="work", opening="task", completion=("Review it.",))

    assert outcome.finished and outcome.summary == "checked in review"
    assert [call[1]["command"][1] for call in tools.calls] == ["a.py", "b.py"]
    assert outcome.steps == 4


def test_a_limit_stop_carries_the_transcript_for_a_final_answer() -> None:
    model = ScriptedModel(
        [_tool_reply("c1", "run_command", '{"command": ["python", "a.py", "0"]}')]
    )
    session = _session(model, CommandTools(), max_steps=1)

    outcome = session.run(instructions="work", opening="task")

    assert not outcome.finished
    assert outcome.reason == "the step limit of 1 was reached"
    assert any(item.get("type") == "function_call_output" for item in outcome.conversation)
    assert session.tool_calls == {"run_command": 1}
    assert [observed.name for observed in session.observations] == ["run_command"]
