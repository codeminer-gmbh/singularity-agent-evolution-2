"""A task-derived oracle catches a cross-rule stream failure on the public path."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolving_agent.model import ModelReply, ToolCall
from evolving_agent.modes import run_probe
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess


class OracleDrivenModel:
    """Draft against one example, then obey the oracle review and repair it."""

    instances: list["OracleDrivenModel"] = []

    def __init__(self, access: ModelAccess) -> None:
        self.calls = 0
        self.transcripts: list[tuple[dict, ...]] = []
        self.__class__.instances.append(self)

    @staticmethod
    def tools(*calls: tuple[str, str, dict]) -> ModelReply:
        tool_calls = tuple(
            ToolCall(call_id, name, json.dumps(arguments))
            for call_id, name, arguments in calls
        )
        return ModelReply(text="", tool_calls=tool_calls, output=())

    def reply(self, *, conversation, tools=()):
        self.calls += 1
        self.transcripts.append(tuple(conversation))
        if self.calls == 1:
            return self.tools(
                ("decoder", "write_file", {
                    "path": "output/decoder.py",
                    "content": (
                        "def decode(chunks):\n"
                        "    out = []\n"
                        "    for line in b''.join(chunks).splitlines():\n"
                        "        if not line.startswith(b'F:') or len(line) == 2:\n"
                        "            break  # BUG: invalid records must not poison recovery\n"
                        "        out.append(line[2:])\n"
                        "    return out\n"
                    ),
                }),
                ("example", "write_file", {
                    "path": "output/test_example.py",
                    "content": "from decoder import decode\nassert decode([b'F:A\\n']) == [b'A']\n",
                }),
            )
        if self.calls == 2:
            return self.tools(("happy", "run_command", {
                "command": ["python", "output/test_example.py"],
            }))
        if self.calls == 3:
            return ModelReply(
                text="decoder.py delivered; the supplied single-frame example passes.",
                tool_calls=(), output=(),
            )
        if self.calls == 4:
            # Expected output is a task literal, not computed by another decoder.
            return self.tools(("oracle", "write_file", {
                "path": "output/.oracle_check.py",
                "content": (
                    "from decoder import decode\n"
                    "wire = b'BROKEN\\nF:OK\\n'\n"
                    "for cut in range(len(wire) + 1):\n"
                    "    got = decode([wire[:cut], wire[cut:]])\n"
                    "    assert got == [b'OK'], (cut, got)\n"
                    "print('oracle: all split points recover OK')\n"
                ),
            }))
        if self.calls == 5:
            return self.tools(("expose", "run_command", {
                "command": ["python", "output/.oracle_check.py"],
            }))
        if self.calls == 6:
            return ModelReply(
                text=("DEFECT: the independent literal oracle fails at split 0: "
                      "a malformed record prevents the following valid frame."),
                tool_calls=(), output=(),
            )
        if self.calls == 7:
            return self.tools(("repair", "write_file", {
                "path": "output/decoder.py",
                "content": (
                    "def decode(chunks):\n"
                    "    out = []\n"
                    "    for line in b''.join(chunks).splitlines():\n"
                    "        if not line.startswith(b'F:') or len(line) == 2:\n"
                    "            continue\n"
                    "        out.append(line[2:])\n"
                    "    return out\n"
                ),
            }))
        if self.calls == 8:
            return self.tools(("recheck", "run_command", {
                "command": ["python", "output/.oracle_check.py"],
            }))
        if self.calls == 9:
            return self.tools(("clean", "delete_path", {
                "path": "output/.oracle_check.py",
            }))
        return ModelReply(
            text=("Delivered decoder.py; the independent malformed-then-valid "
                  "oracle passed at every split point."),
            tool_calls=(), output=(),
        )


class IndependentOracleReviewTests(unittest.TestCase):
    def test_oracle_exposes_cross_rule_failure_then_exact_check_passes(self) -> None:
        OracleDrivenModel.instances.clear()
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as output:
            settings = AgentSettings(
                mode=AgentMode.PROBE,
                task=("Deliver output/decoder.py with decode(chunks). Lines F:<payload> "
                      "produce payloads, malformed lines are skipped, parsing resumes, "
                      "and arbitrary byte chunk boundaries do not change results. Also "
                      "deliver the supplied example test."),
                workspace=Path(root), source_root=Path.cwd(), materials=None,
                output=Path(output), model=ModelAccess(None, "fake-model", 60, None),
                max_steps=4, time_budget_seconds=60,
            )
            with patch("evolving_agent.modes.ModelClient", OracleDrivenModel):
                report = run_probe(settings)

            self.assertTrue(report.succeeded, report.detail)
            self.assertIn("passed at every split point", report.answer)
            self.assertFalse(Path(output, ".oracle_check.py").exists())
            check = subprocess.run(
                ["python", "-c",
                 ("import sys;sys.path.insert(0,sys.argv[1]);from decoder import decode;"
                  "w=b'BAD\\nF:OK\\n';"
                  "assert all(decode([w[:i],w[i:]])==[b\"OK\"] "
                  "for i in range(len(w)+1))"), str(output)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, check.returncode, check.stderr)

        model = OracleDrivenModel.instances[0]
        self.assertEqual(10, model.calls)
        review_prompt = str(model.transcripts[3][-1].get("content", ""))
        final_prompt = str(model.transcripts[6][-1].get("content", ""))
        self.assertIn("independent oracle", review_prompt)
        self.assertIn("malformed-then-valid", review_prompt)
        self.assertIn("exact independent-oracle command", final_prompt)
        transcript = "\n".join(str(item) for turn in model.transcripts for item in turn)
        self.assertIn("[exit code 1]", transcript)
        self.assertIn("oracle: all split points recover OK", transcript)


if __name__ == "__main__":
    unittest.main()
