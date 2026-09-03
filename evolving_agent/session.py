"""The loop: ask the model what to do, do it, tell it what happened.

One step is one exchange. The model is given the tools the MCP server published,
in the provider's own function-calling format, and answers either by asking for
tool calls or by saying something — and a reply that asks for no tool is the
answer, because there is nothing left for the agent to do with it. So there is
no reply format to teach, nothing to parse out of prose, and no way for the model
to name a tool that does not exist.

The conversation is a list of the API's own input items rather than of messages,
and grows the way that API is answered: the turn the model produced goes back
verbatim — its tool calls, and the reasoning it did to arrive at them — followed
by one result item per call, matched to it by the identifier the model gave. The
provider stores none of it, so this list is the whole of the run's memory.

Three things end a session, and each is reported rather than raised: the model
gave its answer, the step limit was reached, or the time budget ran out. A run
that stops on a budget has still done everything it did up to that point, and
in improvement mode that work is in the workspace and is kept.
"""

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from evolving_agent.mcp_client import McpError, ToolDescription, ToolOutcome
from evolving_agent.model import ModelReply, ToolCall, tool_schemas

_LOG = logging.getLogger(__name__)

_HISTORY_LIMIT = 32
"""How many recent items are carried forward, besides the opening two.

The system instruction and the message saying what the run is *for* are what a
session means, so they are never dropped; the middle of a long session is the
part a model needs least.
"""

_OBSERVATION_LIMIT = 8_000
_LOGGED_ARGUMENT_CHARACTERS = 200

_TOOL_RESULT = "function_call_output"

_CONTINUES_A_TURN = frozenset({"function_call", _TOOL_RESULT})
"""The item kinds that only mean anything alongside the rest of their turn.

A tool call and its result refer to each other by an identifier, and either one
without the other refers to nothing — which every provider rejects. Trimming the
history therefore cuts back to where a turn starts rather than to a count.
"""


class Replies(Protocol):
    """Whoever decides what this agent does next."""

    def reply(
        self,
        *,
        conversation: Sequence[Mapping[str, Any]],
        tools: Sequence[Any] = (),
    ) -> ModelReply:
        """Return the answer to one exchange."""
        ...


class Tools(Protocol):
    """Whatever the steps are actually taken through."""

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Perform one tool call and return what it produced.

        Raises:
            McpError: If the tool boundary itself failed, as opposed to the
                tool refusing the work.

        """
        ...


@dataclass(frozen=True)
class SessionOutcome:
    """How one session ended and what it says it achieved."""

    finished: bool
    summary: str
    steps: int
    reason: str
    # A bounded continuation for a last answer after a limit is reached.
    conversation: tuple[Mapping[str, Any], ...] = ()


class Deadline:
    """The moment after which a run stops asking for another step."""

    def __init__(self, seconds: float) -> None:
        """Start the clock.

        Args:
            seconds: How long from now the deadline is.

        """
        self._ends_at = time.monotonic() + seconds

    def expired(self) -> bool:
        """Report whether the deadline has passed."""
        return self.remaining() <= 0

    def remaining(self) -> float:
        """Return how many seconds are left, never below zero."""
        return max(0.0, self._ends_at - time.monotonic())


class ToolAgentSession:
    """Runs one model-driven session against one set of MCP tools."""

    def __init__(
        self,
        model: Replies,
        tools: Tools,
        published: Sequence[ToolDescription] = (),
        *,
        deadline: Deadline,
        max_steps: int,
    ) -> None:
        """Hold the model, the tools and the bounds one session runs under.

        Args:
            model: Who is asked what to do next.
            tools: The published capabilities the steps are taken through.
            published: Those capabilities as the server described them, which
                is what the model is offered.
            deadline: When the session stops asking for another step.
            max_steps: How many steps it may take at most.

        """
        self._model = model
        self._tools = tools
        self._schemas = tool_schemas(published)
        self._deadline = deadline
        self._max_steps = max_steps
        self.tool_calls: dict[str, int] = {}
        """How often each tool was called, over every run of this session.

        Kept on the session rather than in the log, so a run can leave it as a
        record — the first experiment had to reconstruct which tools an exam
        ever used from standard error.
        """

    def run(self, *, instructions: str, opening: str) -> SessionOutcome:
        """Take steps until the model answers, or a bound is reached.

        Args:
            instructions: The system instruction the session is held under.
            opening: The first message, describing the work.

        Returns:
            How the session ended and what it reported.

        Raises:
            ModelUnavailableError: If the model could not be reached at all.
                Nothing the agent does can make a missing model answer, so this
                is the run's result rather than something to work around.

        """
        conversation: list[dict[str, Any]] = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": opening},
        ]
        steps = 0
        while steps < self._max_steps:
            if self._deadline.expired():
                return SessionOutcome(
                    finished=False,
                    summary="",
                    steps=steps,
                    reason="the time budget ran out",
                    conversation=tuple(_recent(conversation)),
                )
            steps += 1
            reply = self._model.reply(
                conversation=_recent(conversation), tools=self._schemas
            )
            if not reply.tool_calls:
                _LOG.info("Step %s: done after %s steps", steps, steps)
                return SessionOutcome(
                    finished=True,
                    summary=reply.text,
                    steps=steps,
                    reason="the agent reported that the work was done",
                    conversation=tuple(_recent(conversation)),
                )
            # The turn goes back as the model made it — the calls it asked for
            # and the reasoning behind them — and each result follows, carrying
            # the identifier that says which call it belongs to.
            conversation.extend(dict(item) for item in reply.output)
            conversation.extend(
                {
                    "type": _TOOL_RESULT,
                    "call_id": call.identifier,
                    "output": self._observe(steps, call),
                }
                for call in reply.tool_calls
            )
        return SessionOutcome(
            finished=False,
            summary="",
            steps=steps,
            reason=f"the step limit of {self._max_steps} was reached",
            conversation=tuple(_recent(conversation)),
        )

    def _observe(self, step: int, call: ToolCall) -> str:
        """Take one step and return what the model is told about it.

        Every way a step can go wrong is a sentence the model reads and can act
        on, including the tool boundary breaking under an argument the server
        did not expect. A run that raised there would take with it the report it
        owes the orchestrator — and, in improvement mode, the restore that keeps
        a half-written tree out of the next cycle.
        """
        _LOG.info("Step %s: %s %s", step, call.name, _short(call.arguments))
        self.tool_calls[call.name] = self.tool_calls.get(call.name, 0) + 1
        arguments = _arguments(call.arguments)
        if isinstance(arguments, str):
            return f"[error] {arguments}"
        try:
            outcome = self._tools.call(call.name, arguments)
        except McpError as broken:
            _LOG.warning("Step %s: the tool boundary failed: %s", step, broken)
            return f"[error] {broken}"
        text = outcome.text[:_OBSERVATION_LIMIT] or "(no output)"
        return f"[error] {text}" if outcome.is_error else text


def _arguments(raw: str) -> dict[str, Any] | str:
    """Return one call's arguments, or the sentence saying why they are unusable.

    A model that produced arguments which are not a JSON object has made a
    mistake it can correct, so it is told about it in the tool's own result
    rather than having the run end.
    """
    try:
        parsed = json.loads(raw or "{}")
    except ValueError as malformed:
        return f"the arguments are not valid JSON ({malformed}); send them again"
    if not isinstance(parsed, dict):
        return "the arguments must be a JSON object; send them again"
    return parsed


def _recent(conversation: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the items carried into the next exchange.

    The tail is cut back to where a turn begins. A tool call and its result name
    each other, so a tail that opens on either half of a pair opens on something
    that refers to an item no longer there — which every provider rejects — and
    the halves are dropped until what is left starts with a turn of its own.
    """
    if len(conversation) <= _HISTORY_LIMIT + 2:
        return [dict(item) for item in conversation]
    tail = list(conversation[-_HISTORY_LIMIT:])
    while tail and tail[0].get("type") in _CONTINUES_A_TURN:
        tail.pop(0)
    return [dict(item) for item in (*conversation[:2], *tail)]


def _short(text: str) -> str:
    """Return one value cut down to something a log line can carry."""
    if len(text) <= _LOGGED_ARGUMENT_CHARACTERS:
        return text
    return f"{text[:_LOGGED_ARGUMENT_CHARACTERS]}…"
