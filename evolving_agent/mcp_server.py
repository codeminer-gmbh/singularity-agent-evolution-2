"""The agent's file and command tools, published over MCP.

Publishing the tools over the Model Context Protocol makes the agent's
capabilities a described *surface* rather than an internal detail: the list
comes back as data, and it is that data the model is offered as function
definitions, so what the model is told it can do cannot drift from what the
agent can do.

Messages are MCP's own: JSON-RPC 2.0. A protocol fault is answered with a
JSON-RPC error; a tool that refused its work is answered with an ordinary result
marked as an error, because that is a fact about the work and the client is
meant to read it and try something else.

The agent's own loop hands messages to :meth:`WorkspaceMcpServer.handle` in
process (see ``mcp_client.py``). :meth:`WorkspaceMcpServer.serve` is the stdio
transport — one message per line — for everything else. Run it directly to
attach any MCP client to a directory::

    python -m evolving_agent.mcp_server /path/to/workspace
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from evolving_agent.commands import Budget, CommandRunner
from evolving_agent.tools import ToolDefinition, ToolFailureError, WorkspaceTools
from evolving_agent.workspace import Workspace

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "evolving-agent-workspace"
SERVER_VERSION = "1.0.0"

_JSONRPC_VERSION = "2.0"
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

_NOTIFICATION_PREFIX = "notifications/"


class WorkspaceMcpServer:
    """Answers MCP requests by calling this agent's workspace tools."""

    def __init__(self, tools: WorkspaceTools) -> None:
        """Hold the tools every call is dispatched to.

        Args:
            tools: The capabilities this server publishes.

        """
        self._tools = tools

    def serve(self, reader: TextIO, writer: TextIO) -> None:
        """Answer messages until the client closes the stream.

        Args:
            reader: Where requests arrive, one JSON message per line.
            writer: Where answers are written, one JSON message per line.

        """
        for line in reader:
            if not line.strip():
                continue
            answer = self.handle_line(line)
            if answer is None:
                continue
            writer.write(f"{answer}\n")
            writer.flush()

    def handle_line(self, line: str) -> str | None:
        """Answer one received line.

        Args:
            line: One JSON-RPC message as it arrived.

        Returns:
            The answer to write back, or ``None`` when the message was a
            notification and needs none.

        """
        try:
            message = json.loads(line)
        except json.JSONDecodeError as malformed:
            return json.dumps(
                _error(None, _PARSE_ERROR, f"The message is not JSON: {malformed}")
            )
        if not isinstance(message, dict):
            return json.dumps(
                _error(None, _INVALID_REQUEST, "A message must be a JSON object.")
            )
        answer = self.handle(message)
        return None if answer is None else json.dumps(answer)

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        """Answer one parsed message.

        Args:
            message: The JSON-RPC message.

        Returns:
            The response object, or ``None`` for a notification.

        """
        identifier = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return _error_for(identifier, _INVALID_REQUEST, "A message needs a method.")
        if method.startswith(_NOTIFICATION_PREFIX):
            # Notifications are one-way by definition, including the
            # ``initialized`` one every client sends after the handshake.
            return None
        params = message.get("params")
        body = self._body(method, params if isinstance(params, dict) else {})
        if identifier is None:
            return None
        return {"jsonrpc": _JSONRPC_VERSION, "id": identifier, **body}

    def _body(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return either the result of one call or the error it produced."""
        try:
            return {"result": self._result(method, params)}
        except _MethodNotFoundError:
            return _failure(_METHOD_NOT_FOUND, f"Unknown method {method!r}.")
        except _InvalidParamsError as invalid:
            return _failure(_INVALID_PARAMS, str(invalid))
        except Exception as unexpected:
            # A server that died of one call would take the whole run with it,
            # and the client would see a closed pipe rather than a reason.
            return _failure(
                _INTERNAL_ERROR, f"{method!r} failed unexpectedly: {unexpected}"
            )

    def _result(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return the result of one method call.

        Raises:
            _MethodNotFoundError: If the method is not one this server implements.
            _InvalidParamsError: If the parameters do not describe a call.

        """
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [_published(tool) for tool in self._tools.definitions()]}
        if method == "tools/call":
            return self._call_tool(params)
        raise _MethodNotFoundError(method)

    def _call_tool(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Perform one tool call and return it as an MCP result.

        A tool that refused its work is reported as a result marked in error
        rather than as a protocol fault: the client asked a well-formed
        question and the answer is that it could not be done.

        Raises:
            _InvalidParamsError: If no tool was named.

        """
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _InvalidParamsError("A tool call needs the name of the tool.")
        supplied = params.get("arguments")
        arguments: Mapping[str, Any] = supplied if isinstance(supplied, dict) else {}
        try:
            text = self._tools.call(name, arguments)
        except ToolFailureError as refused:
            return _content(str(refused), is_error=True)
        return _content(text, is_error=False)


class _MethodNotFoundError(Exception):
    """The client asked for a method this server does not implement."""


class _InvalidParamsError(Exception):
    """The client's parameters do not describe a call that can be made."""


def _published(tool: ToolDefinition) -> dict[str, Any]:
    """Return one tool as MCP publishes it."""
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }


def _content(text: str, *, is_error: bool) -> dict[str, Any]:
    """Return one tool's output in the shape MCP results carry."""
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _failure(code: int, message: str) -> dict[str, Any]:
    """Return the error half of a JSON-RPC response."""
    return {"error": {"code": code, "message": message}}


def _error(identifier: object, code: int, message: str) -> dict[str, Any]:
    """Return one whole JSON-RPC error response."""
    return {"jsonrpc": _JSONRPC_VERSION, "id": identifier, **_failure(code, message)}


def _error_for(identifier: object, code: int, message: str) -> dict[str, Any] | None:
    """Return an error response, or nothing when the message was a notification."""
    return None if identifier is None else _error(identifier, code, message)


def build_server(
    root: Path,
    budget: Budget | None = None,
    *,
    materials: Path | None = None,
    output: Path | None = None,
) -> WorkspaceMcpServer:
    """Build a server whose tools are confined to one directory.

    Args:
        root: The workspace the tools may read, write and run things in.
        budget: What is left of the run these tools serve, when they serve one,
            so that no command it starts outlives it.
        materials: The read-only input files the task was given, if any.
        output: Where the task's deliverables are to be left, if anywhere.

    Returns:
        The server, ready to answer on any pair of streams.

    """
    workspace = Workspace(root)
    workspace.prepare()
    output_tree = None
    if output is not None:
        output_tree = Workspace(output)
        output_tree.prepare()
    return WorkspaceMcpServer(
        WorkspaceTools(
            workspace,
            CommandRunner(workspace.root, budget=budget),
            materials=None if materials is None else Workspace(materials),
            output=output_tree,
        )
    )


def main(argv: Sequence[str]) -> int:
    """Serve MCP on standard input and output for one workspace.

    Args:
        argv: Command-line arguments; the first is the workspace directory,
            which otherwise comes from ``AGENT_WORKSPACE``.

    Returns:
        The process exit status.

    """
    root = argv[0] if argv else os.environ.get("AGENT_WORKSPACE", "").strip()
    if not root:
        sys.stderr.write(
            "A workspace directory is required, as an argument or in AGENT_WORKSPACE.\n"
        )
        return 2
    materials = os.environ.get("AGENT_MATERIALS", "").strip()
    output = os.environ.get("AGENT_OUTPUT", "").strip()
    build_server(
        Path(root),
        materials=Path(materials) if materials else None,
        output=Path(output) if output else None,
    ).serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
