"""Improvement mode must independently review and repair its own claimed change."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import run_improvement
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess


class ReviewingImprovementModel:
    """Claim a precedence fix, then expose and repair it only in mandatory review."""

    instances: list["ReviewingImprovementModel"] = []

    def __init__(self, access: ModelAccess) -> None:
        self.calls = 0
        self.transcripts = []
        self.__class__.instances.append(self)

    @staticmethod
    def tool(identifier: str, name: str, arguments: dict) -> ModelReply:
        raw = json.dumps(arguments)
        return ModelReply(
            text="",
            tool_calls=(ToolCall(identifier, name, raw),),
            output=({"type": "function_call", "call_id": identifier,
                     "name": name, "arguments": raw},),
        )

    def reply(self, *, conversation, tools=()):
        self.calls += 1
        self.transcripts.append(tuple(conversation))
        outputs = {
            str(item.get("call_id")): str(item.get("output", ""))
            for item in conversation if item.get("type") == "function_call_output"
        }
        if self.calls == 1:
            return self.tool("draft-proof", "write_file", {
                "path": "memories/verification.json",
                "content": _record("Claimed conflict output was last (not independently run)."),
            })
        if self.calls == 2:
            return ModelReply(text="Implemented precedence behavior and verified it.",
                              tool_calls=(), output=())
        if self.calls == 3:
            return self.tool("critic-conflict", "run_command", {
                "command": ["python", "behavior.py", "first", "last"],
            })
        if self.calls == 4:
            self.assert_bad_observation(outputs)
            return ModelReply(
                text="DEFECT: conflict returned first, but encounter-order precedence requires last.",
                tool_calls=(), output=(),
            )
        if self.calls == 5:
            return self.tool("repair", "write_file", {
                "path": "behavior.py",
                "content": "import sys\nprint(sys.argv[-1])\n",
            })
        if self.calls == 6:
            return self.tool("retest-conflict", "run_command", {
                "command": ["python", "behavior.py", "first", "last"],
            })
        if self.calls == 7:
            return self.tool("final-proof", "write_file", {
                "path": "memories/verification.json",
                "content": _record("python behavior.py first last exited 0 and printed last."),
            })
        return ModelReply(
            text="Repaired precedence and reran the distinguishing conflict: last.",
            tool_calls=(), output=(),
        )

    @staticmethod
    def assert_bad_observation(outputs: dict[str, str]) -> None:
        if "first" not in outputs.get("critic-conflict", ""):
            raise AssertionError("critic did not observe the deliberately wrong conflict result")


def _record(observed: str) -> str:
    return json.dumps({
        "audit": {
            "costly_failure": "Ledger reports changes reaching evaluation without verification.",
            "evidence": "The selected precedence claim needs an independent conflict oracle.",
        },
        "matrix": [{
            "requirement": "Last encountered valid value wins a conflict.",
            "input": "behavior.py first last",
            "expected": "last",
            "interaction": "python behavior.py first last",
            "observed": observed,
        }],
    })


class ImprovementCompletionReviewTests(unittest.TestCase):
    def test_public_improvement_path_repairs_failed_independent_conflict(self) -> None:
        ReviewingImprovementModel.instances.clear()
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as workspace:
            root = Path(source)
            (root / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY . /app\n")
            (root / "main.py").write_text("print('ok')\n")
            (root / "behavior.py").write_text("import sys\nprint(sys.argv[1])\n")
            settings = AgentSettings(
                mode=AgentMode.IMPROVE,
                task="Improve verification using the ledger; last encountered valid value wins.",
                workspace=Path(workspace), source_root=root, materials=None, output=None,
                model=ModelAccess(None, "fake-model", 60, None),
                max_steps=2, time_budget_seconds=60,
            )
            with patch("evolving_agent.modes.ModelClient", ReviewingImprovementModel):
                report = run_improvement(settings)

            result = __import__("subprocess").run(
                ["python", str(Path(workspace) / "behavior.py"), "first", "last"],
                text=True, capture_output=True, check=False,
            )
            proof = json.loads((Path(workspace) / "memories/verification.json").read_text())

        self.assertTrue(report.succeeded, report.detail)
        self.assertEqual("last", result.stdout.strip())
        self.assertIn("printed last", proof["matrix"][0]["observed"])
        model = ReviewingImprovementModel.instances[0]
        self.assertEqual(8, model.calls)
        final = model.transcripts[-1]
        self.assertTrue(any("DEFECT: conflict returned first" in str(x.get("content", ""))
                            for x in final))
        self.assertTrue(any("independent review" in str(x.get("content", ""))
                            for x in final))
        self.assertTrue(any("smallest relevant ledger verdict" in str(x.get("content", ""))
                            for x in final))


if __name__ == "__main__":
    unittest.main()
