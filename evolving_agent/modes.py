"""The three things this agent is ever started to do, end to end.

An improvement run is asked to leave a successor in the workspace; a probe run
is asked a question and answers it on standard output. Both are the same
machinery — a tool server, a model and a session between them — differing in
what they are told and in what is done with the result. A describe run asks no
model anything: it prints the tools the other two would offer one, so the
orchestrator can aim its exam at what this agent can actually do.

Every run that was given an output directory leaves a record of the tools it
called there, under ``.meta/tool_calls.json``, so which capabilities an exam
exercised is a fact on the record rather than something reconstructed from
standard error afterwards.

Two rules run through both. Whatever happens, the run ends with a report rather
than an exception: a model that could not be reached, a tool server that broke,
a session that ran out of steps and a successor that will not parse are all
*results*, and the evidence the orchestrator keeps is only as good as the run's
willingness to state them. And an improvement run never leaves a broken tree
behind: it asks for a repair, and failing that puts back the source it started
from, so the cycle carries an honest "no improvement" rather than a candidate
that cannot be built.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from evolving_agent.evidence import verification_problems
from evolving_agent.mcp_client import McpClient, McpError, connect
from evolving_agent.model import ModelClient, ModelUnavailableError
from evolving_agent.prompts import (
    final_answer_request,
    improvement_instructions,
    improvement_opening,
    probe_instructions,
    probe_opening,
    repair_opening,
)
from evolving_agent.session import Deadline, SessionOutcome, ToolAgentSession
from evolving_agent.settings import AgentSettings
from evolving_agent.successor import successor_problems
from evolving_agent.workspace import Workspace, WorkspaceError

_LOG = logging.getLogger(__name__)

_MAX_REPAIR_ROUNDS = 2
"""How many times a broken successor is handed back to be fixed.

Each round costs a share of the budget, and a model that has not fixed a syntax
error in two passes is not about to on the third.
"""

TOOL_CALLS_RECORD = ".meta/tool_calls.json"
"""Where a run leaves the count of every tool it called, under its output."""

MANIFEST_NOTES = (
    "Every tool is reachable from the workspace, from the task's input files "
    "under materials/ and from the deliverables under output/; commands run "
    "with no shell, bounded by the run's remaining budget."
)
"""What a describe run says about its tools beyond listing them."""


@dataclass(frozen=True)
class RunReport:
    """What one run achieved, as the process reports it.

    ``answer`` is what belongs on standard output — the whole of it, since a
    probe's answer is read on its own as evidence. ``detail`` is for standard
    error and the operator.
    """

    succeeded: bool
    answer: str
    detail: str


def run_improvement(settings: AgentSettings) -> RunReport:
    """Leave a successor to this agent in the workspace.

    Args:
        settings: The run as the environment described it.

    Returns:
        Whether a usable successor was left behind, and what happened.

    """
    workspace = Workspace(settings.workspace)
    workspace.prepare()
    restorable = workspace.is_empty()
    if restorable:
        copied = workspace.copy_tree_from(settings.source_root)
        _LOG.info(
            "Initialized the workspace with %s files of this agent's source.", copied
        )
    started_from = workspace.digest()
    outcome, refusal = _improve(settings, workspace)
    problems = _publication_problems(workspace)
    if problems:
        return _restored(
            settings, workspace, problems, restorable=restorable, refusal=refusal
        )
    if workspace.digest() == started_from:
        return RunReport(
            succeeded=False,
            answer="No successor was produced: the source is unchanged.",
            detail=_detail(
                "the run left its own source exactly as it found it", outcome, refusal
            ),
        )
    return RunReport(
        succeeded=refusal is None,
        answer=_improvement_answer(outcome),
        detail=_detail("a successor was left in the workspace", outcome, refusal),
    )


def run_probe(settings: AgentSettings) -> RunReport:
    """Answer the question this run was started with.

    Args:
        settings: The run as the environment described it.

    Returns:
        The answer, and whether the run produced one at all.

    """
    workspace = Workspace(settings.workspace)
    workspace.prepare()
    deadline = Deadline(settings.time_budget_seconds)
    model = ModelClient(settings.model)
    try:
        session = _session(
            model, _tools(workspace, deadline, settings), deadline, settings
        )
        outcome = session.run(
            instructions=probe_instructions(),
            opening=probe_opening(
                settings.task,
                materials_listing=_materials_listing(settings),
                output_available=settings.output is not None,
            ),
        )
    except (ModelUnavailableError, McpError) as unreachable:
        return RunReport(succeeded=False, answer="", detail=str(unreachable))
    _record_tool_calls(settings, session)
    if outcome.finished and outcome.summary.strip():
        return RunReport(
            succeeded=True,
            answer=outcome.summary.strip(),
            detail=f"answered in {outcome.steps} steps",
        )
    return _final_answer(model, settings, outcome)


def run_describe(settings: AgentSettings) -> RunReport:
    """Print what this agent can do: the tools a model would be offered.

    No model is asked. The list is read from the same server the other two
    modes offer their model, so what the orchestrator is told this agent can
    do and what it actually can do are one list.

    Args:
        settings: The run as the environment described it.

    Returns:
        The manifest as one JSON document, or why none could be read.

    """
    workspace = Workspace(settings.workspace)
    workspace.prepare()
    try:
        tools = connect(workspace.root, Deadline(settings.time_budget_seconds))
        published = tools.list_tools()
    except (McpError, WorkspaceError) as unreachable:
        return RunReport(succeeded=False, answer="", detail=str(unreachable))
    manifest = {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.input_schema),
            }
            for tool in published
        ],
        "notes": MANIFEST_NOTES,
    }
    return RunReport(
        succeeded=True,
        answer=json.dumps(manifest, sort_keys=True),
        detail=f"described {len(published)} tools",
    )


def _improve(
    settings: AgentSettings, workspace: Workspace
) -> tuple[SessionOutcome | None, str | None]:
    """Run the improvement session, reporting a dependency that failed instead.

    A model that cannot be reached and a tool server that will not publish its
    tools are both reported here rather than raised, so that whatever the
    session did manage to leave in the workspace is still judged, repaired or
    put back by the caller.

    Returns:
        What the session came to, and — when a dependency rather than the work
        itself failed — why it could not run.

    """
    deadline = Deadline(settings.time_budget_seconds)
    try:
        session = _session(
            ModelClient(settings.model),
            _tools(workspace, deadline, settings),
            deadline,
            settings,
        )
        outcome = session.run(
            instructions=improvement_instructions(),
            opening=improvement_opening(
                settings.task,
                _listing(workspace),
                settings.time_budget_seconds,
                settings.max_steps,
                materials_listing=_materials_listing(settings),
            ),
        )
        repaired = _repaired(workspace, session, deadline, outcome)
    except (ModelUnavailableError, McpError) as unavailable:
        _LOG.error("The improvement run could not proceed: %s", unavailable)
        return None, str(unavailable)
    _record_tool_calls(settings, session)
    return repaired, None


def _record_tool_calls(settings: AgentSettings, session: ToolAgentSession) -> None:
    """Leave the count of every tool this run called under its output.

    A run given no output directory has nowhere to leave it; a record that
    cannot be written is logged and costs the run nothing, because the answer
    is what the run owes and the record is what it offers.
    """
    if settings.output is None:
        return
    target = Path(settings.output) / TOOL_CALLS_RECORD
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(dict(sorted(session.tool_calls.items())), sort_keys=True),
            encoding="utf-8",
        )
    except OSError as unwritable:
        _LOG.warning("The tool-call record could not be written: %s", unwritable)


def _tools(
    workspace: Workspace, deadline: Deadline, settings: AgentSettings
) -> McpClient:
    """Return the tool server one run works through, with its side trees.

    Raises:
        WorkspaceError: If a tree the run was given cannot be used.

    """
    return connect(
        workspace.root,
        deadline,
        materials=settings.materials,
        output=settings.output,
    )


def _materials_listing(settings: AgentSettings) -> str | None:
    """Return what the task's input files are, or none when there are none."""
    if settings.materials is None:
        return None
    entries = Workspace(settings.materials).entries()
    if not entries:
        return None
    return "\n".join(
        f"  materials/{entry.relative_path} ({entry.byte_size} bytes)"
        for entry in entries
    )


def _session(
    model: ModelClient,
    tools: McpClient,
    deadline: Deadline,
    settings: AgentSettings,
) -> ToolAgentSession:
    """Return the session one run takes its steps through.

    The capabilities are read from the server rather than declared here, and
    they are what becomes the function definitions the model is offered — so
    what the model is told it can do and what the agent can actually do are the
    same list.
    """
    return ToolAgentSession(
        model,
        tools,
        tools.list_tools(),
        deadline=deadline,
        max_steps=settings.max_steps,
    )


def _publication_problems(workspace: Workspace) -> tuple[str, ...]:
    """Return syntax/build blockers plus the durable proof required to publish.

    A changed candidate is repaired or restored when its claimed improvement
    has no inspectable requirement-to-proof record.  This keeps the evidence
    protocol on the same public path as the existing successor gate.
    """
    return successor_problems(workspace) + verification_problems(workspace)


def _repaired(
    workspace: Workspace,
    session: ToolAgentSession,
    deadline: Deadline,
    outcome: SessionOutcome,
) -> SessionOutcome:
    """Hand a broken successor back to be fixed, while there is budget for it.

    Returns:
        How the last session ended — the original one when nothing was wrong.

    """
    for round_number in range(1, _MAX_REPAIR_ROUNDS + 1):
        problems = _publication_problems(workspace)
        if not problems or deadline.expired():
            return outcome
        _LOG.warning(
            "Repair round %s: the successor has %s problem(s).",
            round_number,
            len(problems),
        )
        outcome = session.run(
            instructions=improvement_instructions(),
            opening=repair_opening(problems),
        )
    return outcome


def _restored(
    settings: AgentSettings,
    workspace: Workspace,
    problems: tuple[str, ...],
    *,
    restorable: bool,
    refusal: str | None,
) -> RunReport:
    """Put the source back when the successor cannot be salvaged.

    Restoring is only possible when this run is what filled the workspace: a
    tree that arrived already populated is somebody else's, and overwriting it
    would destroy work this agent never did.
    """
    listed = "; ".join(problems)
    _LOG.error("The successor is not usable: %s", listed)
    if not restorable:
        return RunReport(
            succeeded=False,
            answer="No usable successor was produced.",
            detail=_with_refusal(f"the successor is not usable: {listed}", refusal),
        )
    try:
        workspace.clear()
        restored = workspace.copy_tree_from(settings.source_root)
    except WorkspaceError as unrestorable:
        return RunReport(
            succeeded=False,
            answer="No usable successor was produced.",
            detail=(
                f"the successor is not usable ({listed}) and the source could "
                f"not be put back: {unrestorable}"
            ),
        )
    _LOG.info("Put %s files of the original source back in the workspace.", restored)
    return RunReport(
        succeeded=False,
        answer="No successor was produced: the source was put back unchanged.",
        detail=_with_refusal(
            f"the successor was discarded and the source restored: {listed}", refusal
        ),
    )


def _final_answer(
    model: ModelClient, settings: AgentSettings, outcome: SessionOutcome
) -> RunReport:
    """Ask once more for an answer when the session ran out before giving one.

    The agent's own budget stops it short of the limit the orchestrator holds
    the container to, so there is room for exactly this: one last exchange that
    turns a run which was still working into a run that answered.
    """
    _LOG.warning(
        "The session ended because %s; asking for a final answer.", outcome.reason
    )
    try:
        # Continue the actual run: its opening records material/output paths and
        # its tool results contain the evidence the final answer must report.
        # Reconstruct only for synthetic/legacy outcomes with no transcript.
        conversation = list(outcome.conversation) or [
            {"role": "system", "content": probe_instructions()},
            {"role": "user", "content": probe_opening(settings.task)},
        ]
        conversation.append({"role": "user", "content": final_answer_request()})
        reply = model.reply(conversation=conversation)
    except ModelUnavailableError as unreachable:
        return RunReport(
            succeeded=False, answer="", detail=f"{outcome.reason}; {unreachable}"
        )
    if not reply.text:
        # An empty artifact where a judgment was expected is worse than a run
        # that says it produced nothing, because only one of the two is legible.
        return RunReport(
            succeeded=False,
            answer="",
            detail=f"{outcome.reason}, and the last exchange answered nothing",
        )
    return RunReport(
        succeeded=True,
        answer=reply.text,
        detail=f"answered after {outcome.steps} steps, because {outcome.reason}",
    )


def _listing(workspace: Workspace) -> str:
    """Return the workspace's files as the opening message shows them."""
    entries = workspace.entries()
    if not entries:
        return "(the workspace is empty)"
    return "\n".join(
        f"  {entry.relative_path} ({entry.byte_size} bytes)" for entry in entries
    )


def _improvement_answer(outcome: SessionOutcome | None) -> str:
    """Return what an improvement run prints about what it changed."""
    if outcome is None or not outcome.summary.strip():
        return "A successor was left in the workspace."
    return outcome.summary.strip()


def _detail(headline: str, outcome: SessionOutcome | None, refusal: str | None) -> str:
    """Return the sentence an operator reads about how a run went."""
    if outcome is not None:
        headline = f"{headline} after {outcome.steps} steps, because {outcome.reason}"
    return _with_refusal(headline, refusal)


def _with_refusal(headline: str, refusal: str | None) -> str:
    """Return one detail line, naming the dependency that failed if one did."""
    return headline if refusal is None else f"{headline}; {refusal}"
