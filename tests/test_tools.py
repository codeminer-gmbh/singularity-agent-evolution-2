"""The published tool registry, and what a describe run says about it."""

import json
from pathlib import Path

from evolving_agent.modes import run_describe
from evolving_agent.settings import AgentMode, AgentSettings, ModelAccess

EXPECTED_TOOLS = (
    "list_files",
    "read_file",
    "write_file",
    "read_document",
    "query_sqlite",
    "inspect_delimited",
    "extract_archive",
    "http_fetch",
    "delete_path",
    "run_command",
)


def test_describe_publishes_exactly_the_tools_a_run_would_offer(tmp_path: Path) -> None:
    settings = AgentSettings(
        mode=AgentMode.DESCRIBE,
        task="",
        workspace=tmp_path,
        source_root=Path.cwd(),
        model=ModelAccess(None, "unused", 30, None),
        time_budget_seconds=30,
        max_steps=0,
    )

    report = run_describe(settings)

    assert report.succeeded, report.detail
    manifest = json.loads(report.answer)
    assert tuple(tool["name"] for tool in manifest["tools"]) == EXPECTED_TOOLS
    assert all({"name", "description", "input_schema"} <= set(tool) for tool in manifest["tools"])
    assert "materials/" in manifest["notes"]
