"""What one agent run was asked to do, read from the environment it starts in.

Two kinds of setting arrive here. The run — the mode, the task, the workspace,
and where a task's input files and deliverables live — comes from whoever
started the container. The model comes from the ordinary
OpenAI environment: ``OPENAI_API_KEY``, ``OPENAI_BASE_URL`` when the endpoint is
not OpenAI's own, and ``OPENAI_MODEL``. Those are the names every OpenAI client
already reads, which is what lets this image run under an orchestrator, under
``docker run``, or on a laptop against a local server.

Timing is a property of the image rather than of a run, and differs per mode: an
improvement run has an hour and a half, a probe fifty minutes. A run that is
killed at its deadline produces nothing at all, so each budget leaves room for
the exchange still in flight when it runs out.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_DEFAULT_MODEL_NAME = "gpt-5.6-sol"
"""The model asked when nothing names one.

The orchestrator names one on every container it starts, through
``OPENAI_MODEL``, and that name wins: which model the lineage runs under is the
deployment's decision and is recorded on every run, so a successor changing
this default changes nothing under the loop. It matters only for ``docker run``
by hand and for a laptop.
"""
_DEFAULT_WORKSPACE = "/workspace"
_DEFAULT_SOURCE_ROOT = "/opt/evolving-agent"


class ConfigurationError(Exception):
    """The environment does not describe a run this agent can take."""


class AgentMode(StrEnum):
    """The three things the orchestrator ever asks an agent to do.

    ``improve`` leaves a successor, ``probe`` answers a question, and
    ``describe`` prints what this agent can do — the tools it would offer a
    model, read off the same registry — so the exam can be aimed at them.
    """

    IMPROVE = "improve"
    PROBE = "probe"
    DESCRIBE = "describe"


@dataclass(frozen=True)
class ModelAccess:
    """Which OpenAI-compatible endpoint to ask, and how patiently.

    ``base_url`` is ``None`` for OpenAI itself, which is what the client falls
    back to; anything else — a gateway, a local server, a proxy — states it.
    """

    base_url: str | None
    model_name: str
    timeout_seconds: int
    credential: str | None


@dataclass(frozen=True)
class ModeTiming:
    """How long one mode's run may take, and how patiently it asks the model."""

    time_budget_seconds: int
    model_timeout_seconds: int
    max_steps: int


TIMING: Mapping[AgentMode, ModeTiming] = {
    AgentMode.IMPROVE: ModeTiming(
        time_budget_seconds=5400, model_timeout_seconds=360, max_steps=60
    ),
    AgentMode.PROBE: ModeTiming(time_budget_seconds=3000, model_timeout_seconds=240, max_steps=40),
    AgentMode.DESCRIBE: ModeTiming(time_budget_seconds=60, model_timeout_seconds=30, max_steps=0),
}
"""What each mode runs under.

The deployment stops an improvement container after 7200 seconds and a probe
after 3600. Each budget above is set well inside its own deadline, because a
session stops taking new steps when the budget is spent but the exchange under
way still runs to its timeout, and one more exchange may follow it. What a run
cannot also overrun by is a command: the tools are given this budget and bound
every command to what is left of it, so the only thing that outlives the budget
is the exchange in flight and the one after it.
``tests/test_settings.py`` holds the numbers to that.

The model timeout is what a *reasoning* model has to answer one exchange in,
not what a round trip costs, and it is the number this image has been wrong
about twice. A probe was first given fifteen seconds against a deadline read
off the wrong setting entirely; every exchange of every probe timed out, both
attempts of it, so probe runs produced nothing and the cycles that depend on
them — test generation first — had no evidence to go on. Forty-five seconds
against a hundred-second budget was the same mistake made smaller: still
minutes where a reasoning model with tools in front of it wants tens of
minutes. So the deadlines themselves moved. A probe container is started under
``MAX_TEST_RUN_SECONDS`` and an improvement container under
``MAX_IMPROVEMENT_RUN_SECONDS``, which the deployment now defaults to an hour
and two hours; the budgets here are what fits inside those with room for the
exchanges that outlive them. Being generous costs a stalled run held longer.
Being mean cost every run there was.
"""


@dataclass(frozen=True)
class AgentSettings:
    """One run of this agent, exactly as the environment described it."""

    mode: AgentMode
    task: str
    workspace: Path
    source_root: Path
    model: ModelAccess
    time_budget_seconds: int
    max_steps: int
    materials: Path | None = None
    """Read-only input files the task refers to, when the run was given any.

    Named by ``AGENT_MATERIALS``. The orchestrator delivers them before the
    container starts; the tools read them under ``materials/`` and never
    write there.
    """
    output: Path | None = None
    """Where files the task asks for are left, when the run was given a place.

    Named by ``AGENT_OUTPUT``. The orchestrator collects it after the run; the
    tools write there under ``output/``. Standard output stays the answer.
    """


def from_environment(environ: Mapping[str, str] | None = None) -> AgentSettings:
    """Read one run's settings out of the environment the agent started in.

    Args:
        environ: The environment to read; this process's own when none is
            given.

    Returns:
        The settings of the run that was asked for.

    Raises:
        ConfigurationError: If the mode is missing or unknown, or the task is
            blank. Both are the caller's side of the contract, so a run that
            cannot honour them refuses at once rather than answering a question
            nobody asked.

    """
    source = os.environ if environ is None else environ
    mode = _mode(source.get("AGENT_MODE", "").strip())
    task = source.get("AGENT_TASK", "").strip()
    if not task and mode is not AgentMode.DESCRIBE:
        raise ConfigurationError("AGENT_TASK is empty; there is nothing for this run to work on.")
    timing = TIMING[mode]
    return AgentSettings(
        mode=mode,
        task=task,
        workspace=Path(source.get("AGENT_WORKSPACE", "").strip() or _DEFAULT_WORKSPACE),
        source_root=Path(source.get("AGENT_SOURCE_ROOT", "").strip() or _DEFAULT_SOURCE_ROOT),
        model=ModelAccess(
            base_url=source.get("OPENAI_BASE_URL", "").strip() or None,
            model_name=source.get("OPENAI_MODEL", "").strip() or _DEFAULT_MODEL_NAME,
            timeout_seconds=timing.model_timeout_seconds,
            credential=source.get("OPENAI_API_KEY", "").strip() or None,
        ),
        time_budget_seconds=timing.time_budget_seconds,
        max_steps=timing.max_steps,
        materials=_optional_path(source.get("AGENT_MATERIALS", "")),
        output=_optional_path(source.get("AGENT_OUTPUT", "")),
    )


def _optional_path(value: str) -> Path | None:
    """Return a path the environment named, or none when it named none."""
    stated = value.strip()
    return Path(stated) if stated else None


def _mode(value: str) -> AgentMode:
    """Return the mode the caller asked for.

    Raises:
        ConfigurationError: If the mode is absent or is one this agent does not
            implement.

    """
    try:
        return AgentMode(value)
    except ValueError as unknown:
        raise ConfigurationError(
            f"AGENT_MODE must be one of "
            f"{', '.join(member.value for member in AgentMode)}; got {value!r}."
        ) from unknown
