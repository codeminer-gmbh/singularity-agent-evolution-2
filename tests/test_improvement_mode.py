"""The improvement path end to end: a changed tree is published only with its proof."""

import json
from pathlib import Path
from unittest.mock import patch

from evolving_agent.evidence import VERIFICATION_RECEIPT, VERIFICATION_RECORD, candidate_digest
from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import run_improvement
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess
from evolving_agent.workspace import Workspace


def _tool(identifier: str, name: str, arguments: dict) -> ModelReply:
    raw = json.dumps(arguments)
    return ModelReply(
        text="",
        tool_calls=(ToolCall(identifier, name, raw),),
        output=({"type": "function_call", "call_id": identifier, "name": name, "arguments": raw},),
    )


def _text(text: str) -> ModelReply:
    return ModelReply(text=text, tool_calls=(), output=())


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY . /app\n")
    (root / "main.py").write_text("print('ok')\n")
    (root / "behavior.py").write_text("import sys\nprint(sys.argv[1])\n")
    return root


def _settings(tmp_path: Path, source: Path, max_steps: int = 4) -> AgentSettings:
    return AgentSettings(
        mode=AgentMode.IMPROVE,
        task="Last encountered valid value must win.",
        workspace=tmp_path / "workspace",
        source_root=source,
        materials=None,
        output=None,
        model=ModelAccess(None, "fake-model", 60, None),
        max_steps=max_steps,
        time_budget_seconds=60,
    )


def _record(workspace_root: Path, observed: str) -> str:
    return json.dumps(
        {
            "candidate_digest": candidate_digest(Workspace(workspace_root)),
            "audit": {
                "costly_failure": "Changes reached evaluation without verification.",
                "evidence": "ledger rows 3 and 5",
            },
            "matrix": [
                {
                    "requirement": "Last encountered valid value wins a conflict.",
                    "input": "behavior.py first last",
                    "expected": "last",
                    "interaction": "python behavior.py first last",
                    "observed": observed,
                }
            ],
        }
    )


class Scripted:
    """A model double driven by the state of the workspace, not a fixed script."""

    workspace: Path
    conversations: list[tuple[dict, ...]] = []
    calls = 0

    def __init__(self, access: ModelAccess) -> None:
        pass

    def reply(self, *, conversation, tools=()):
        cls = self.__class__
        cls.conversations.append(tuple(conversation))
        cls.calls += 1
        return self.step(cls.calls, conversation)

    def step(self, call: int, conversation) -> ModelReply:
        raise NotImplementedError


def _run(model_class, tmp_path: Path, **kwargs):
    source = _source(tmp_path)
    settings = _settings(tmp_path, source, **kwargs)
    model_class.workspace = settings.workspace
    model_class.conversations = []
    model_class.calls = 0
    with patch("evolving_agent.modes.ModelClient", model_class):
        return run_improvement(settings), settings


def test_a_reviewed_repaired_and_bound_successor_is_published(tmp_path: Path) -> None:
    class Reviewing(Scripted):
        def step(self, call, conversation):
            root = self.workspace
            if call == 1:
                return _tool(
                    "edit",
                    "write_file",
                    {"path": "behavior.py", "content": "import sys\nprint(sys.argv[1])  # wrong\n"},
                )
            if call == 2:
                return _tool(
                    "draft-proof",
                    "write_file",
                    {
                        "path": VERIFICATION_RECORD,
                        "content": _record(root, "printed first (not rerun)"),
                    },
                )
            if call == 3:
                return _text("Implemented precedence and recorded the proof.")
            if call == 4:
                return _tool(
                    "critic", "run_command", {"command": ["python", "behavior.py", "first", "last"]}
                )
            if call == 5:
                return _text("DEFECT: the conflict printed first; the rule requires last.")
            if call == 6:
                return _tool(
                    "repair",
                    "write_file",
                    {"path": "behavior.py", "content": "import sys\nprint(sys.argv[-1])\n"},
                )
            if call == 7:
                return _tool(
                    "retest", "run_command", {"command": ["python", "behavior.py", "first", "last"]}
                )
            if call == 8:
                return _tool(
                    "proof",
                    "write_file",
                    {"path": VERIFICATION_RECORD, "content": _record(root, "exit 0, printed last")},
                )
            return _text("Repaired precedence; python behavior.py first last printed last.")

    report, settings = _run(Reviewing, tmp_path)

    assert report.succeeded, report.detail
    assert report.answer.startswith("Runtime receipt: 2 commands run, 2 exited 0, 0 did not.")
    assert report.answer.endswith("printed last.")
    workspace = settings.workspace
    assert (workspace / "behavior.py").read_text().endswith("argv[-1])\n")
    receipt = json.loads((workspace / VERIFICATION_RECEIPT).read_text())
    assert [item["command"][1] for item in receipt["commands"]] == ["behavior.py", "behavior.py"]
    assert Reviewing.calls == 9
    assert any("independent" in str(item.get("content", "")) for item in Reviewing.conversations[3])


def test_a_changed_tree_without_a_proof_record_is_put_back(tmp_path: Path) -> None:
    class Unproven(Scripted):
        def step(self, call, conversation):
            if call == 1:
                return _tool(
                    "edit",
                    "write_file",
                    {"path": "behavior.py", "content": "import sys\nprint(sys.argv[-1])\n"},
                )
            return _text("Changed behaviour; no proof recorded.")

    report, settings = _run(Unproven, tmp_path)

    assert not report.succeeded
    assert report.answer == "No successor was produced: the source was put back unchanged."
    assert f"{VERIFICATION_RECORD} is missing" in report.detail
    assert (settings.workspace / "behavior.py").read_text() == "import sys\nprint(sys.argv[1])\n"
    repair_requests = [
        turn[-1]["content"]
        for turn in Unproven.conversations
        if str(turn[-1].get("content", "")).startswith(
            "The workspace is not a publishable successor yet"
        )
    ]
    assert len(repair_requests) == 2 and VERIFICATION_RECORD in repair_requests[0]


def test_a_record_written_before_the_last_edit_no_longer_binds(tmp_path: Path) -> None:
    class Stale(Scripted):
        def step(self, call, conversation):
            root = self.workspace
            if call == 1:
                return _tool(
                    "proof",
                    "write_file",
                    {"path": VERIFICATION_RECORD, "content": _record(root, "printed last")},
                )
            if call == 2:
                return _tool(
                    "edit",
                    "write_file",
                    {"path": "behavior.py", "content": "import sys\nprint(sys.argv[-1])\n"},
                )
            return _text("Done.")

    report, _ = _run(Stale, tmp_path)

    assert not report.succeeded
    assert "candidate_digest does not match" in report.detail


def test_a_note_claiming_an_unshipped_test_blocks_publication(tmp_path: Path) -> None:
    class Overclaiming(Scripted):
        def step(self, call, conversation):
            root = self.workspace
            if call == 1:
                return _tool(
                    "edit",
                    "write_file",
                    {"path": "behavior.py", "content": "import sys\nprint(sys.argv[-1])\n"},
                )
            if call == 2:
                return _tool(
                    "note",
                    "write_file",
                    {
                        "path": "memories/precedence.md",
                        "content": "Last value wins; the integration tests passed.",
                    },
                )
            if call == 3:
                return _tool(
                    "proof",
                    "write_file",
                    {"path": VERIFICATION_RECORD, "content": _record(root, "printed last")},
                )
            return _text("Done.")

    report, _ = _run(Overclaiming, tmp_path)

    assert not report.succeeded
    assert "memories/precedence.md claims" in report.detail


def test_an_unchanged_tree_owes_no_proof(tmp_path: Path) -> None:
    class Idle(Scripted):
        def step(self, call, conversation):
            return _text("Nothing needed changing.")

    report, _ = _run(Idle, tmp_path)

    assert not report.succeeded
    assert report.answer == "No successor was produced: the source is unchanged."
    assert Idle.calls == 3
