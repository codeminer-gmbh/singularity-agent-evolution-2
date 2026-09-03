"""The probe receives verdict-directed semantic and boundary guidance."""

from collections.abc import Mapping, Sequence
from typing import Any

from evolving_agent.model import ModelReply
from evolving_agent.prompts import probe_instructions
from evolving_agent.session import Deadline, ToolAgentSession


class _CapturingModel:
    def __init__(self) -> None:
        self.conversation: Sequence[Mapping[str, Any]] = ()

    def reply(
        self,
        *,
        conversation: Sequence[Mapping[str, Any]],
        tools: Sequence[Any] = (),
    ) -> ModelReply:
        self.conversation = conversation
        return ModelReply(text="done", tool_calls=())


class _UnusedTools:
    def call(self, name: str, arguments: Mapping[str, Any]) -> Any:
        raise AssertionError("the model should not call a tool")


def test_probe_session_receives_exact_boundary_and_public_path_policy() -> None:
    """The actual session system message guards the ledger's `+7` failure."""
    model = _CapturingModel()
    session = ToolAgentSession(
        model,
        _UnusedTools(),
        deadline=Deadline(10),
        max_steps=1,
    )

    outcome = session.run(instructions=probe_instructions(), opening="write parser")

    assert outcome.finished
    system_message = model.conversation[0]
    assert system_message["role"] == "system"
    guidance = " ".join(system_message["content"].split())
    assert "exact accepted language" in guidance
    assert "do not silently broaden digits to signs, Unicode" in guidance
    assert "ASCII digits only, `+7` is a rejected counterexample" in guidance
    assert "exact rejected or competing counterexample" in guidance
    assert "through the public function, command, or delivered file" in guidance
    assert "only the rules the task actually exposes" in guidance
    assert "Do not build a universal checklist" in guidance
