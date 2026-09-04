"""The three things this agent is ever started to do, end to end.

An improvement run is asked to leave a successor in the workspace; a probe run
is asked a question and answers it on standard output. Both are the same
machinery, a tool server, a model and a session between them, differing in
what they are told, in what stands between their first draft and their answer,
and in what is done with the result. A describe run asks no model anything: it
prints the tools the other two would offer one, so the orchestrator can aim its
exam at what this agent can actually do.

Two rules run through everything. Whatever happens, the run ends with a report
rather than an exception: a model that could not be reached, a tool server that
broke, a session that ran out of steps and a successor that will not parse are
all *results*, and the evidence the orchestrator keeps is only as good as the
run's willingness to state them. And an improvement run never leaves a broken
or unproven tree behind: it asks for a repair, and failing that puts back the
source it started from, so the cycle carries an honest "no improvement" rather
than a candidate that cannot be built or a claim that cannot be checked.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from evolving_agent.completion import (
    missing_deliverables_message,
    missing_output_paths,
    unexercised_source_message,
)
from evolving_agent.evidence import (
    candidate_digest,
    verification_problems,
    write_receipt,
)
from evolving_agent.mcp_client import McpClient, McpError, connect
from evolving_agent.model import ModelClient, ModelUnavailableError
from evolving_agent.prompts import (
    final_answer_request,
    improvement_finalization,
    improvement_instructions,
    improvement_opening,
    improvement_review,
    probe_finalization,
    probe_instructions,
    probe_opening,
    probe_review,
    repair_opening,
)
from evolving_agent.session import (
    CompletionStage,
    Deadline,
    SessionOutcome,
    ToolAgentSession,
)
from evolving_agent.settings import AgentSettings
from evolving_agent.successor import (
    note_problems,
    note_snapshot,
    successor_problems,
    suite_problems,
)
from evolving_agent.workspace import Workspace, WorkspaceError

_LOG = logging.getLogger(__name__)

_MAX_REPAIR_ROUNDS = 2
"""How many times a broken successor is handed back to be fixed.

Each round costs a share of the budget, and a model that has not fixed a
problem in two passes is not about to on the third.
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

    ``answer`` is what belongs on standard output, the whole of it, since a
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
        Whether a usable, proven successor was left behind, and what happened.

    """
    workspace = Workspace(settings.workspace)
    workspace.prepare()
    restorable = workspace.is_empty()
    if restorable:
        copied = workspace.copy_tree_from(settings.source_root)
        _LOG.info("Initialized the workspace with %s files of this agent's source.", copied)
    started_from = candidate_digest(workspace)
    notes_before = note_snapshot(workspace)
    gate = _PublicationGate(started_from, notes_before)
    outcome, refusal, session = _improve(settings, workspace, gate)
    problems = gate.problems(workspace)
    if problems:
        return _restored(settings, workspace, problems, restorable=restorable, refusal=refusal)
    if candidate_digest(workspace) == started_from:
        return RunReport(
            succeeded=False,
            answer="No successor was produced: the source is unchanged.",
            detail=_detail("the run left its own source exactly as it found it", outcome, refusal),
        )
    if session is not None:
        write_receipt(workspace, session.observations)
    return RunReport(
        succeeded=refusal is None,
        answer=_improvement_answer(outcome, session),
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
    _link_side_trees(workspace, settings)
    deadline = Deadline(settings.time_budget_seconds)
    model = ModelClient(settings.model)
    try:
        session = _session(model, _tools(workspace, deadline, settings), deadline, settings)
        outcome = session.run(
            instructions=probe_instructions(),
            opening=probe_opening(
                settings.task,
                materials_listing=_materials_listing(settings),
                output_available=settings.output is not None,
            ),
            completion=_probe_completion(settings),
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


class _PublicationGate:
    """Everything a changed tree must satisfy before it is left as a successor.

    Build problems are always disqualifying. The notes rule, the proof record
    and the tree's own tests apply only once the tree differs from what the run
    started with: a run that changed nothing owes no proof, and is reported as
    unchanged instead. The tests are run last, and only when everything cheaper
    has passed.
    """

    def __init__(self, started_from: str, notes_before: Mapping[str, str]) -> None:
        self._started_from = started_from
        self._notes_before = notes_before

    def problems(self, workspace: Workspace) -> tuple[str, ...]:
        """Return every reason the tree cannot be published, in a stable order."""
        found = successor_problems(workspace)
        if candidate_digest(workspace) == self._started_from:
            return found
        found += note_problems(workspace, self._notes_before) + verification_problems(workspace)
        return found if found else suite_problems(workspace)


def _improve(
    settings: AgentSettings, workspace: Workspace, gate: _PublicationGate
) -> tuple[SessionOutcome | None, str | None, ToolAgentSession | None]:
    """Run the improvement session, reporting a dependency that failed instead.

    A model that cannot be reached and a tool server that will not publish its
    tools are both reported here rather than raised, so that whatever the
    session did manage to leave in the workspace is still judged, repaired or
    put back by the caller.

    Returns:
        What the session came to, why it could not run when a dependency rather
        than the work itself failed, and the session for its evidence.

    """
    deadline = Deadline(settings.time_budget_seconds)
    try:
        session = _session(
            ModelClient(settings.model), _tools(workspace, deadline, settings), deadline, settings
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
            completion=(improvement_review(), improvement_finalization()),
        )
        repaired = _repaired(workspace, session, deadline, outcome, gate)
    except (ModelUnavailableError, McpError) as unavailable:
        _LOG.error("The improvement run could not proceed: %s", unavailable)
        return None, str(unavailable), None
    _record_tool_calls(settings, session)
    return repaired, None, session


def _probe_completion(settings: AgentSettings) -> tuple[CompletionStage, ...]:
    """Return what stands between a probe's first draft and its answer.

    Two mechanical checks come first and speak only when they have something to
    say: a deliverable the task names that is not on disk, and a program the
    task asked for that no command has run. Then the adversarial review of the
    draft, and the repair that publishes only what the review left standing.
    """

    def deliverables(session: ToolAgentSession) -> str | None:
        del session
        missing = missing_output_paths(settings.task, settings.output)
        return missing_deliverables_message(missing) if missing else None

    def exercised(session: ToolAgentSession) -> str | None:
        return unexercised_source_message(settings.task, session.tool_calls)

    return (deliverables, exercised, probe_review(), probe_finalization())


def _link_side_trees(workspace: Workspace, settings: AgentSettings) -> None:
    """Make a probe's side trees reachable from the commands it runs.

    The tools reach ``materials/`` and ``output/`` by path prefix, but a command
    runs in the workspace root and sees only what is there. The second
    experiment's record has a whole line losing exams because it wrote a
    deliverable under ``output/`` and then could not run it, and a tool that can
    be written to but not executed is exactly the gap the verification
    checkpoint exists to close. So a probe links each side tree it was given
    into its scratch workspace. Links are never listed, digested or copied by
    the workspace, and a probe's workspace is never packaged, so nothing else
    sees them. An improvement run gets no links: its workspace becomes the
    successor.
    """
    for name, target in (("materials", settings.materials), ("output", settings.output)):
        if target is None:
            continue
        link = workspace.root / name
        if link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(Path(target).resolve(), target_is_directory=True)
        except OSError as unlinkable:
            _LOG.warning("%s/ could not be linked into the workspace: %s", name, unlinkable)


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


def _tools(workspace: Workspace, deadline: Deadline, settings: AgentSettings) -> McpClient:
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
        f"  materials/{entry.relative_path} ({entry.byte_size} bytes)" for entry in entries
    )


def _session(
    model: ModelClient, tools: McpClient, deadline: Deadline, settings: AgentSettings
) -> ToolAgentSession:
    """Return the session one run takes its steps through.

    The capabilities are read from the server rather than declared here, and
    they are what becomes the function definitions the model is offered, so
    what the model is told it can do and what the agent can actually do are the
    same list.
    """
    return ToolAgentSession(
        model, tools, tools.list_tools(), deadline=deadline, max_steps=settings.max_steps
    )


def _repaired(
    workspace: Workspace,
    session: ToolAgentSession,
    deadline: Deadline,
    outcome: SessionOutcome,
    gate: _PublicationGate,
) -> SessionOutcome:
    """Hand a tree that cannot be published back to be fixed, while there is budget.

    Returns:
        How the last session ended; the original one when nothing was wrong.

    """
    for round_number in range(1, _MAX_REPAIR_ROUNDS + 1):
        problems = gate.problems(workspace)
        if not problems or deadline.expired():
            return outcome
        _LOG.warning(
            "Repair round %s: the successor has %s problem(s).", round_number, len(problems)
        )
        outcome = session.run(
            instructions=improvement_instructions(), opening=repair_opening(problems)
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
    turns a run which was still working into a run that answered. The exchange
    continues the run's own transcript, because the tool results in it are the
    evidence the answer has to report.
    """
    _LOG.warning("The session ended because %s; asking for a final answer.", outcome.reason)
    conversation = list(outcome.conversation) or [
        {"role": "system", "content": probe_instructions()},
        {"role": "user", "content": probe_opening(settings.task)},
    ]
    conversation.append({"role": "user", "content": final_answer_request()})
    try:
        reply = model.reply(conversation=conversation)
    except ModelUnavailableError as unreachable:
        return RunReport(succeeded=False, answer="", detail=f"{outcome.reason}; {unreachable}")
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
    return "\n".join(f"  {entry.relative_path} ({entry.byte_size} bytes)" for entry in entries)


def _improvement_answer(outcome: SessionOutcome | None, session: ToolAgentSession | None) -> str:
    """Return what an improvement run prints about what it did.

    The first line is the runtime's own fact, counted from the commands the
    session observed: a model can describe a check it never ran, but it cannot
    change this line. The model's closing report follows, for the reader who
    wants the claim, with the fact above it for the reader who wants to check.
    """
    facts = "Runtime receipt: no commands were run."
    if session is not None:
        commands = [item for item in session.observations if item.name == "run_command"]
        if commands:
            passed = sum("[exit code 0]" in item.text and not item.is_error for item in commands)
            facts = (
                f"Runtime receipt: {len(commands)} commands run, {passed} exited 0, "
                f"{len(commands) - passed} did not."
            )
    summary = outcome.summary.strip() if outcome is not None else ""
    return f"{facts}\n\n{summary}" if summary else facts


def _detail(headline: str, outcome: SessionOutcome | None, refusal: str | None) -> str:
    """Return the sentence an operator reads about how a run went."""
    if outcome is not None:
        headline = f"{headline} after {outcome.steps} steps, because {outcome.reason}"
    return _with_refusal(headline, refusal)


def _with_refusal(headline: str, refusal: str | None) -> str:
    """Return one detail line, naming the dependency that failed if one did."""
    return headline if refusal is None else f"{headline}; {refusal}"
