"""The probe path end to end: drafts are reviewed, checked, repaired, then published."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import run_probe
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess


def _tool(identifier: str, name: str, arguments: dict) -> ModelReply:
    raw = json.dumps(arguments)
    return ModelReply(
        text="",
        tool_calls=(ToolCall(identifier, name, raw),),
        output=({"type": "function_call", "call_id": identifier, "name": name, "arguments": raw},),
    )


def _text(text: str) -> ModelReply:
    return ModelReply(text=text, tool_calls=(), output=())


def _settings(task: str, workspace: Path, *, materials: Path | None = None, output: Path | None = None, max_steps: int = 2) -> AgentSettings:
    return AgentSettings(
        mode=AgentMode.PROBE,
        task=task,
        workspace=workspace,
        source_root=Path.cwd(),
        materials=materials,
        output=output,
        model=ModelAccess(None, "fake-model", 60, None),
        max_steps=max_steps,
        time_budget_seconds=60,
    )


class Scripted:
    """A model double: a script of replies, and every conversation it saw."""

    script: list[ModelReply] = []
    conversations: list[tuple[dict, ...]] = []

    def __init__(self, access: ModelAccess) -> None:
        self.replies = list(self.script)

    def reply(self, *, conversation, tools=()):
        self.__class__.conversations.append(tuple(conversation))
        return self.replies.pop(0)


def _run(task: str, script: list[ModelReply], tmp_path: Path, **kwargs):
    Scripted.script = script
    Scripted.conversations = []
    with patch("evolving_agent.modes.ModelClient", Scripted):
        return run_probe(_settings(task, tmp_path / "workspace", **kwargs))


def _last_user_message(conversation: tuple[dict, ...]) -> str:
    """Return the last message put to the model, ignoring the runtime's ledger."""
    for item in reversed(conversation):
        content = str(item.get("content", ""))
        if item.get("role") == "user" and not content.startswith("Machine-maintained verification ledger"):
            return content
    raise AssertionError("no user message in the conversation")


def test_a_factual_draft_is_reviewed_and_the_repaired_answer_is_published(tmp_path: Path) -> None:
    report = _run(
        "Which valid source wins, the one declared first or the one encountered last?",
        [
            _text("The first declared valid source always wins."),
            _text("DEFECT: the draft confused declaration order with encounter order."),
            _text("Corrected after review: the last encountered valid source wins."),
        ],
        tmp_path,
        max_steps=1,
    )

    assert report.succeeded, report.detail
    assert report.answer == "Corrected after review: the last encountered valid source wins."
    assert len(Scripted.conversations) == 3
    assert "Do not publish a final answer yet" in _last_user_message(Scripted.conversations[1])
    assert "preceding defect report" in _last_user_message(Scripted.conversations[2])
    assert any("DEFECT:" in str(item.get("content", "")) for item in Scripted.conversations[2])


def test_a_named_deliverable_that_is_missing_sends_the_draft_back(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    report = _run(
        "Put the answer 42 in output/answer.txt.",
        [
            _text("The answer is 42."),
            _tool("w", "write_file", {"path": "output/answer.txt", "content": "42\n"}),
            _text("Wrote output/answer.txt with 42."),
            _text("No defect found: the file holds 42."),
            _text("Delivered output/answer.txt containing 42."),
        ],
        tmp_path,
        output=output,
    )

    assert report.succeeded, report.detail
    assert (output / "answer.txt").read_text() == "42\n"
    assert "not present yet: output/answer.txt" in _last_user_message(Scripted.conversations[1])
    assert "Do not publish a final answer yet" in _last_user_message(Scripted.conversations[3])
    assert json.loads((output / ".meta/tool_calls.json").read_text()) == {"write_file": 1}


def test_code_handed_in_without_ever_running_is_challenged_first(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    report = _run(
        "Deliver output/solution.py with a function that doubles its argument.",
        [
            _tool("w", "write_file", {"path": "output/solution.py", "content": "def double(x):\n    return 2 * x\n"}),
            _text("Delivered solution.py."),
            _tool("r", "run_command", {"command": [sys.executable, "-c", "import sys; sys.path.insert(0, 'output'); from solution import double; assert double(3) == 6; print('ok')"]}),
            _text("Exercised: double(3) == 6."),
            _text("No defect found."),
            _text("Delivered output/solution.py; double(3) returned 6 when run."),
        ],
        tmp_path,
        output=output,
    )

    assert report.succeeded, report.detail
    assert report.answer.startswith("Delivered output/solution.py")
    checkpoint = _last_user_message(Scripted.conversations[2])
    assert "no command has exercised it yet" in checkpoint
    assert "syntax check alone" in checkpoint
    assert "Do not publish a final answer yet" in _last_user_message(Scripted.conversations[4])


def test_review_keeps_the_tools_and_replaces_a_guess_with_evidence(tmp_path: Path) -> None:
    materials = tmp_path / "materials"
    materials.mkdir()
    (materials / "fact.txt").write_text("The actual code is OMEGA-913.\n")
    report = _run(
        "Read fact.txt and report its code.",
        [
            _text("The material says ALPHA (unverified)."),
            _tool("read", "read_file", {"path": "materials/fact.txt"}),
            _text("DEFECT: the material says OMEGA-913, not ALPHA."),
            _text("Verified during review: OMEGA-913."),
        ],
        tmp_path,
        materials=materials,
    )

    assert report.succeeded, report.detail
    assert report.answer == "Verified during review: OMEGA-913."
    tool_results = [item for turn in Scripted.conversations for item in turn if item.get("type") == "function_call_output"]
    assert any("OMEGA-913" in str(item.get("output")) for item in tool_results)


def test_a_limit_stop_is_finished_from_the_transcript(tmp_path: Path) -> None:
    materials = tmp_path / "materials"
    materials.mkdir()
    (materials / "fact.txt").write_text("The code is ORCHID-742.\n")

    class NeverStops(Scripted):
        """Reads the file on every exchange until told it is out of steps."""

        def reply(self, *, conversation, tools=()):
            self.__class__.conversations.append(tuple(conversation))
            if "You are out of steps" not in str(conversation[-1].get("content", "")):
                # A real turn opens with a reasoning item; the history trimming
                # cuts back to one, so the double must produce it too.
                identifier = f"read-{len(self.__class__.conversations)}"
                arguments = '{"path": "materials/fact.txt"}'
                return ModelReply(
                    text="",
                    tool_calls=(ToolCall(identifier, "read_file", arguments),),
                    output=(
                        {"type": "reasoning", "id": f"rs-{identifier}", "summary": []},
                        {"type": "function_call", "call_id": identifier, "name": "read_file", "arguments": arguments},
                    ),
                )
            seen = any(
                item.get("type") == "function_call_output" and "ORCHID-742" in str(item.get("output"))
                for item in conversation
            )
            return _text("The file's code is ORCHID-742." if seen else "I cannot answer without the tool result.")

    NeverStops.conversations = []
    with patch("evolving_agent.modes.ModelClient", NeverStops):
        report = run_probe(_settings("Read fact.txt and report its code.", tmp_path / "w", materials=materials, max_steps=1))

    assert report.succeeded, report.detail
    assert report.answer == "The file's code is ORCHID-742."
    assert "because the step limit" in report.detail


def test_a_counterexample_found_in_review_is_repaired_and_scratch_is_not_delivered(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    loose = "import re,sys\nsys.exit(0 if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', sys.argv[1]) else 1)\n"
    strict = (
        "import sys,urllib.parse\nu=urllib.parse.urlsplit(sys.argv[1])\n"
        "sys.exit(0 if u.scheme in {'http','https'} and bool(u.netloc) else 1)\n"
    )
    report = _run(
        "Deliver output/validator.py that accepts absolute HTTP(S) URLs and rejects malformed ones. Do not deliver scratch files.",
        [
            _tool("bad", "write_file", {"path": "output/validator.py", "content": loose}),
            _tool("scratch", "write_file", {"path": "output/debug_test.py", "content": "# known failure\n"}),
            _tool("happy", "run_command", {"command": [sys.executable, "output/validator.py", "https://example.test/a"]}),
            _text("Delivered validator.py; it accepts URLs with a scheme."),
            _tool("counterexample", "run_command", {"command": [sys.executable, "output/validator.py", "http:/broken"]}),
            _text("DEFECT: http:/broken exits 0 although HTTP requires an authority; debug_test.py is an unrequested scratch file."),
            ModelReply(
                text="",
                tool_calls=(
                    ToolCall("fix", "write_file", json.dumps({"path": "output/validator.py", "content": strict})),
                    ToolCall("clean", "delete_path", '{"path": "output/debug_test.py"}'),
                ),
                output=(),
            ),
            _tool("retest", "run_command", {"command": [sys.executable, "output/validator.py", "http:/broken"]}),
            _text("Corrected validator delivered; http:/broken was observed rejected."),
        ],
        tmp_path,
        output=output,
        max_steps=6,
    )

    assert report.succeeded, report.detail
    assert report.answer.startswith("Corrected validator delivered")
    assert (output / "validator.py").is_file()
    assert not (output / "debug_test.py").exists()
    assert subprocess.run([sys.executable, str(output / "validator.py"), "http:/broken"], check=False).returncode != 0
    assert subprocess.run([sys.executable, str(output / "validator.py"), "https://example.test/a"], check=False).returncode == 0
    transcript = "\n".join(str(item) for turn in Scripted.conversations for item in turn)
    assert "[exit code 1]" in transcript and "[exit code 0]" in transcript
