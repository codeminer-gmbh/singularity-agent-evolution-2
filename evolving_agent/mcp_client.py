"""The client half of the tool boundary: MCP, in this process.

The client speaks the same JSON-RPC the stdio server answers, and hands each
message straight to the server object instead of writing it down a pipe. A pipe
would buy process isolation the agent does not want here — both halves are the
same program under the same limits — and would cost every run a child process
and a handshake. Attaching a *different* client over stdio still works and is
what ``python -m evolving_agent.mcp_server`` is for.

A tool that *refused* is not an error here: it comes back as an outcome marked
in error, carrying the sentence the server wrote, which is exactly what the
model needs in order to try something else.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evolving_agent.commands import Budget, CommandResult
from evolving_agent.mcp_server import WorkspaceMcpServer, build_server

_JSONRPC_VERSION = "2.0"


class McpError(Exception):
    """The tool server answered with something that is not a result."""


@dataclass(frozen=True)
class ToolDescription:
    """One tool as the server published it."""

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    """What one tool call produced, and whether the tool refused the work."""

    text: str
    is_error: bool


class McpClient:
    """One connected tool server, for the length of one agent run."""

    def __init__(self, server: WorkspaceMcpServer) -> None:
        """Hold the server every message is handed to.

        Args:
            server: The published capabilities this client reaches.

        """
        self._server = server
        self._next_id = 0

    def list_tools(self) -> tuple[ToolDescription, ...]:
        """Return every tool the server publishes.

        Returns:
            The published tools, in the order the server listed them.

        Raises:
            McpError: If the server answers with something that is not a tool
                list.

        """
        published = self._request("tools/list", {}).get("tools")
        if not isinstance(published, list):
            raise McpError("The tool server published no tool list.")
        return tuple(
            ToolDescription(
                name=str(tool.get("name", "")),
                description=str(tool.get("description", "")),
                input_schema=tool.get("inputSchema", {}),
            )
            for tool in published
            if isinstance(tool, dict)
        )

    def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Call one tool and return what it produced.

        Args:
            name: Name of the tool to call.
            arguments: The arguments to call it with.

        Returns:
            The tool's text output, and whether the tool refused the work.

        Raises:
            McpError: If the server refuses the call itself, as opposed to the
                tool refusing the work.

        """
        result = self._request(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        parts = result.get("content")
        text = (
            "\n".join(
                str(part.get("text", ""))
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if isinstance(parts, list)
            else ""
        )
        return ToolOutcome(text=text, is_error=bool(result.get("isError")))

    def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send one request and return the result it was answered with.

        Raises:
            McpError: If the server answers with an error, or with something
                that is not a result.

        """
        self._next_id += 1
        answer = self._server.handle(
            {
                "jsonrpc": _JSONRPC_VERSION,
                "id": self._next_id,
                "method": method,
                "params": dict(params),
            }
        )
        failure = (answer or {}).get("error")
        if isinstance(failure, dict):
            raise McpError(
                f"The tool server refused {method!r}: {failure.get('message')}"
            )
        result = (answer or {}).get("result")
        if not isinstance(result, dict):
            raise McpError(f"The tool server's answer to {method!r} is not a result.")
        return result


def connect(
    root: Path,
    budget: Budget | None = None,
    *,
    materials: Path | None = None,
    output: Path | None = None,
    on_command_result: Callable[[CommandResult], None] | None = None,
) -> McpClient:
    """Return a client connected to a tool server rooted at one directory.

    Args:
        root: The workspace the tools may read, write and run things in.
        budget: What is left of the run being served, when there is one, so
            that no command these tools start outlives it.
        materials: The read-only input files the task was given, if any.
        output: Where the task's deliverables are to be left, if anywhere.
        on_command_result: Called after each command the published tool runs.

    Returns:
        The connected client.

    Raises:
        WorkspaceError: If the workspace itself cannot be used.

    """
    return McpClient(build_server(
        root, budget, materials=materials, output=output,
        on_command_result=on_command_result,
    ))
