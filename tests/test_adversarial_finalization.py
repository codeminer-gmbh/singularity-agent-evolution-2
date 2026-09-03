"""The public probe path separates defect discovery from repair/publication."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import run_probe
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess


class ArtifactRepairModel:
    """Script a realistic bad draft, critique, repair, and counterexample run."""

    instances: list["ArtifactRepairModel"] = []

    def __init__(self, access: ModelAccess) -> None:
        self.calls = 0
        self.transcripts = []
        self.__class__.instances.append(self)

    @staticmethod
    def tool(call_id: str, name: str, arguments: dict) -> ModelReply:
        raw = json.dumps(arguments)
        return ModelReply(
            text="",
            tool_calls=(ToolCall(call_id, name, raw),),
            output=({
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": raw,
            },),
        )

    def reply(self, *, conversation, tools=()):
        self.calls += 1
        self.transcripts.append(tuple(conversation))
        if self.calls == 1:
            # The first implementation checks only the URL-scheme discriminator;
            # its unrequested helper visibly fails for a missing authority.
            return self.tool("bad", "write_file", {
                "path": "output/validator.py",
                "content": (
                    "import re,sys\n"
                    "sys.exit(0 if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', sys.argv[1]) else 1)\n"
                ),
            })
        if self.calls == 2:
            return self.tool("scratch", "write_file", {
                "path": "output/debug_test.py",
                "content": "# Known failure: http:/broken must be rejected.\n",
            })
        if self.calls == 3:
            return ModelReply(
                text="Delivered validator.py; it accepts any input with a URL scheme.",
                tool_calls=(), output=(),
            )
        if self.calls == 4:
            return self.tool("counterexample", "run_command", {
                "command": ["python", "output/validator.py", "http:/broken"],
            })
        if self.calls == 5:
            return ModelReply(
                text=("DEFECT: http:/broken returned 0 although HTTP requires an authority; "
                      "debug_test.py is an unrequested failing scratch artifact."),
                tool_calls=(), output=(),
            )
        if self.calls == 6:
            return ModelReply(
                text="",
                tool_calls=(
                    ToolCall("fix", "write_file", json.dumps({
                        "path": "output/validator.py",
                        "content": (
                            "import sys,urllib.parse\n"
                            "u=urllib.parse.urlsplit(sys.argv[1])\n"
                            "sys.exit(0 if u.scheme in {'http','https'} and bool(u.netloc) else 1)\n"
                        ),
                    })),
                    ToolCall("clean", "delete_path", '{"path":"output/debug_test.py"}'),
                ),
                output=(),
            )
        if self.calls == 7:
            return self.tool("retest", "run_command", {
                "command": ["python", "output/validator.py", "http:/broken"],
            })
        return ModelReply(
            text="Corrected validator delivered; http:/broken was observed rejected.",
            tool_calls=(), output=(),
        )


class AdversarialFinalizationTests(unittest.TestCase):
    def test_counterexample_is_repaired_and_scratch_is_not_delivered(self) -> None:
        ArtifactRepairModel.instances.clear()
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as output:
            settings = AgentSettings(
                mode=AgentMode.PROBE,
                task=("Deliver output/validator.py. Accept absolute HTTP(S) URLs and reject "
                      "malformed URLs. Do not deliver scratch files."),
                workspace=Path(root), source_root=Path.cwd(), materials=None,
                output=Path(output), model=ModelAccess(None, "fake-model", 60, None),
                max_steps=6, time_budget_seconds=60,
            )
            with patch("evolving_agent.modes.ModelClient", ArtifactRepairModel):
                report = run_probe(settings)

            self.assertTrue(report.succeeded, report.detail)
            self.assertEqual(
                "Corrected validator delivered; http:/broken was observed rejected.",
                report.answer,
            )
            self.assertTrue(Path(output, "validator.py").is_file())
            self.assertFalse(Path(output, "debug_test.py").exists())
            import subprocess
            bad = subprocess.run(
                ["python", str(Path(output, "validator.py")), "http:/broken"], check=False
            )
            good = subprocess.run(
                ["python", str(Path(output, "validator.py")), "https://example.test/a"], check=False
            )
            self.assertNotEqual(0, bad.returncode)
            self.assertEqual(0, good.returncode)

        model = ArtifactRepairModel.instances[0]
        self.assertEqual(8, model.calls)
        final_transcript = model.transcripts[-1]
        self.assertTrue(any(
            "DEFECT: http:/broken" in str(item.get("content", ""))
            for item in final_transcript
        ))


if __name__ == "__main__":
    unittest.main()
