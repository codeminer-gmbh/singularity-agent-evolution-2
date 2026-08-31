"""The one way this agent asks a language model for anything.

It is the official OpenAI client and nothing else. That buys two things worth
more than a hand-rolled transport: the image talks to anything that speaks the
OpenAI API — OpenAI itself, a gateway, a local server — by setting
``OPENAI_BASE_URL``, and tool calls travel in the provider's own format instead
of a private convention the prompt has to teach.

The MCP tools are turned into that format here, by :func:`tool_schemas`, so the
capabilities the model is offered are literally the ones the tool server
published. Nothing is written out by hand, so nothing can drift.

The responses API, because that is the endpoint a current reasoning model does
tool calling on. The GPT-5 family this image asks for by default refuses
function tools and its own reasoning together in a chat completion, and names
the two ways out: ``/v1/responses``, or a model that does not reason. Every step
of every session is a tool call, so giving up the reasoning would be giving up
the model — the endpoint is the thing that moves instead. What that costs is
stated where it is paid: ``OPENAI_BASE_URL`` has to name something that serves
``/v1/responses``, and the README says so.

The provider is still asked to keep nothing. ``store`` is false, and what the
model produces — the reasoning between one tool call and the next included,
encrypted, because that is the only form a stateless caller gets it in — comes
back to be carried forward by the session. So the conversation remains this
agent's own, which is what lets a long run trim its own history, and every
request carries the whole of what it needs to be answered.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from openai import APIStatusError, OpenAI, OpenAIError, omit
from openai.types.responses import (
    FunctionToolParam,
    Response,
    ResponseFunctionToolCall,
    ResponseIncludable,
    ResponseInputItemParam,
)

from evolving_agent.mcp_client import ToolDescription
from evolving_agent.settings import ModelAccess

_LOG = logging.getLogger(__name__)

_REQUEST_REFUSED = 400
"""The status an endpoint answers with when the request itself is the problem.

A model that is reachable can still refuse what it was sent — a name it does not
know, an endpoint it does not serve. That is this image's own configuration
being wrong rather than the service being down, every retry of it is refused the
same way, and so it is reported in those words instead of as a model that did
not answer.
"""

RETRIES = 1
"""How many times the client itself retries a busy or broken service.

Kept low because it is part of how long a run can take, and public because that
makes it something the timing can be derived from rather than restated.
"""

ATTEMPTS_PER_EXCHANGE = 1 + RETRIES
"""The most times one exchange reaches the service: the first try and the retries.

One exchange therefore costs at most this many of the configured timeout, which
is the unit the mode budgets in ``settings.py`` are chosen against.
``tests/agent/test_settings.py`` reads the number from here, so changing the
retry count moves that arithmetic with it instead of leaving it stale and green.
"""

_NO_CREDENTIAL = "none-was-configured"
"""What is sent when the environment carried no credential.

The client insists on being given something. A server that wants no credential
ignores this, and one that wants a real credential refuses it — which is the
outcome to report, and is reported.
"""

_EMPTY_SCHEMA: Mapping[str, Any] = {"type": "object", "properties": {}}

_CARRIED_REASONING: ResponseIncludable = "reasoning.encrypted_content"
"""What a stateless caller asks for so the model gets its own reasoning back.

A response nobody stores is one the provider cannot look up again, so the
reasoning a model did before asking for a tool would be gone by the time it sees
what the tool said. Asked for this way it comes back sealed — unreadable here,
and meaningless to anyone but the model it is handed back to — and the session
carries it forward like any other part of the turn.
"""


class ModelUnavailableError(Exception):
    """The model could not be reached, refused the request, or answered unusably."""


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked for, exactly as it asked for it.

    ``arguments`` is the raw JSON text the model produced rather than a parsed
    object: whether it parses at all is something the caller has to be able to
    tell the model about, so it is not decided here.
    """

    identifier: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelReply:
    """One answer: what the model said, what it wants run, and what it produced.

    ``output`` is that same turn as the API itself described it, item for item
    and unread — the tool calls, the text, and the sealed reasoning that led to
    them. It is what goes back into the next request, because a turn the model
    is not handed back in full is one it has to reconstruct from the results
    alone. ``text`` and ``tool_calls`` are the parts of it this agent acts on.
    """

    text: str
    tool_calls: tuple[ToolCall, ...]
    output: tuple[Mapping[str, Any], ...] = ()


def tool_schemas(published: Sequence[ToolDescription]) -> list[FunctionToolParam]:
    """Return the MCP tools as the function definitions the API takes.

    The translation is the whole of the bridge between the two protocols: an MCP
    tool is a name, a description and a JSON Schema for its arguments, and so is
    an OpenAI function.

    Args:
        published: What the tool server published.

    Returns:
        One function definition per tool, in the order they were published.

    """
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema) or dict(_EMPTY_SCHEMA),
            # Strict mode is the API's default here, and it holds a schema to
            # more than the tool server promises: every property required, and
            # no others allowed. These schemas are the server's own, so they are
            # offered as written rather than as this API would prefer them.
            "strict": False,
        }
        for tool in published
    ]


class ModelClient:
    """Asks one configured model for one answer at a time."""

    def __init__(self, access: ModelAccess) -> None:
        """Build the OpenAI client this run's exchanges go through.

        Args:
            access: The endpoint, the model name, the time limit for one
                attempt, and the credential if there is one.

        """
        # Held in a short local on purpose: the orchestrator discards a
        # collected workspace holding a line that reads like a credential being
        # assigned a long value, and passing one inline here would read as one.
        key = access.credential or _NO_CREDENTIAL
        self._access = access
        self._client = OpenAI(
            api_key=key,
            base_url=access.base_url,
            timeout=float(access.timeout_seconds),
            max_retries=RETRIES,
        )

    @property
    def endpoint(self) -> str:
        """Return where this client is asking, for a report to name."""
        return self._access.base_url or "the OpenAI API"

    def reply(
        self,
        *,
        conversation: Sequence[Mapping[str, Any]],
        tools: Sequence[FunctionToolParam] = (),
    ) -> ModelReply:
        """Ask the model once and return what it answered with.

        Beyond the conversation and the tools, a request says only that the
        provider is to keep nothing and that the reasoning is to come back where
        this agent can carry it. Nothing tunable is sent: a sampling parameter
        is the part of a request a model may reject outright — the GPT-5 family
        this image asks for by default serves its own temperature and refuses
        any other value — and an exchange that states none is one every model
        answers.

        Args:
            conversation: The run so far, as the API's own input items: the
                messages, the tool calls the model made and the results they
                produced.
            tools: The function definitions the model may call, if any.

        Returns:
            The text the model produced, the tool calls it asked for, and the
            turn itself for the next request to carry.

        Raises:
            ModelUnavailableError: If the model could not be reached, refused
                the request, or answered with nothing usable. Nothing the agent
                does can make a missing model answer, so that is the run's
                result rather than something to work around.

        """
        try:
            answered = self._client.responses.create(
                model=self._access.model_name,
                input=cast("list[ResponseInputItemParam]", list(conversation)),
                tools=list(tools) or omit,
                store=False,
                include=[_CARRIED_REASONING],
            )
        except OpenAIError as unreachable:
            raise ModelUnavailableError(self._failure(unreachable)) from unreachable
        if not answered.output:
            raise ModelUnavailableError(
                f"The model at {self.endpoint} answered with nothing."
            )
        _LOG.debug("The model produced %s output item(s).", len(answered.output))
        return _reply_of(answered)

    def _failure(self, refused: OpenAIError) -> str:
        """Return what to report about an exchange the client could not complete.

        The two cases read differently on purpose. A run's failure is evidence
        somebody acts on, and "the endpoint would not serve this request" points
        at the image's configuration, where "the model did not answer" points at
        the network and the service — sending the reader to the wrong one of
        those costs a cycle.
        """
        if isinstance(refused, APIStatusError) and (
            refused.status_code == _REQUEST_REFUSED
        ):
            return (
                f"The model {self._access.model_name} at {self.endpoint} would not "
                f"serve this request: {refused}"
            )
        return f"The model at {self.endpoint} did not answer: {refused}"


def _reply_of(answered: Response) -> ModelReply:
    """Return one answered response as this agent's own reply.

    The turn is kept twice over, and deliberately: once as the API described it,
    to be handed straight back, and once as the two things this agent does
    something with — what the model said, and what it wants run.
    """
    return ModelReply(
        text=answered.output_text.strip(),
        tool_calls=tuple(
            ToolCall(
                identifier=item.call_id,
                name=item.name,
                arguments=item.arguments or "{}",
            )
            for item in answered.output
            if isinstance(item, ResponseFunctionToolCall)
        ),
        output=tuple(item.model_dump(exclude_none=True) for item in answered.output),
    )
