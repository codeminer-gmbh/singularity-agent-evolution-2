"""The capabilities this agent has: reading, writing and running things.

The tools are declared here, once, as data — a name, a description and a JSON
Schema for the arguments — and are implemented against the workspace and the
command runner. The MCP server publishes exactly this list, so what a model is
told it can do and what the agent can actually do cannot drift apart.

Every failure is a :class:`ToolFailureError` carrying a sentence the model can act
on. That is the difference between a tool that teaches and one that merely
refuses: "that path leaves the workspace" gets a corrected second attempt, an
unhandled exception ends the run.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evolving_agent.commands import CommandRunner
from evolving_agent.workspace import Workspace, WorkspaceError

_MAX_LISTING_CHARACTERS = 8_000

MATERIALS_PREFIX = "materials/"
"""How a path names the read-only input files a task was given."""

OUTPUT_PREFIX = "output/"
"""How a path names the directory a task's deliverables are left in."""


class ToolFailureError(Exception):
    """A tool could not do what it was asked, for a reason worth reporting."""


@dataclass(frozen=True)
class ToolDefinition:
    """One capability, as it is published to a model."""

    name: str
    description: str
    input_schema: dict[str, Any]


class WorkspaceTools:
    """Reads, writes and runs things, all inside one workspace.

    Two further trees may sit beside it. ``materials/`` is what a task handed
    in: readable, listed, never written. ``output/`` is where a task's
    deliverables go: written, listed, collected afterwards. Both are named by a
    path prefix, so the model has one set of tools and three places to point
    them at, and the containment story is the same for each — a path is
    resolved against the tree its prefix names and refused if it leaves it.
    """

    def __init__(
        self,
        workspace: Workspace,
        commands: CommandRunner,
        *,
        materials: Workspace | None = None,
        output: Workspace | None = None,
    ) -> None:
        """Hold the trees the tools work in and the runner they execute through.

        Args:
            workspace: The contained directory every path is resolved against.
            commands: How a command is run, bounded and cleaned.
            materials: The read-only inputs a task was given, if any.
            output: Where a task's deliverables are left, if anywhere.

        """
        self._workspace = workspace
        self._commands = commands
        self._materials = materials
        self._output = output

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return every tool this agent publishes, in a stable order."""
        return (
            ToolDefinition(
                name="list_files",
                description=(
                    "List every file in the workspace with its size in bytes, "
                    "followed by the task's input files under materials/ and "
                    "the deliverables written so far under output/. Takes no "
                    "arguments."
                ),
                input_schema={"type": "object", "properties": {}, "required": []},
            ),
            ToolDefinition(
                name="read_file",
                description=(
                    "Read one file as text. Paths are relative to the workspace; "
                    "a path starting with materials/ reads one of the task's "
                    "input files, and one starting with output/ reads a "
                    "deliverable. Large files are truncated."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace root.",
                        }
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="write_file",
                description=(
                    "Write one file, replacing it if it exists and creating "
                    "the directories it needs. The content is the whole new "
                    "file, not a patch. A path starting with output/ writes a "
                    "deliverable the task asked for; materials/ is read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace root.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The complete new content of the file.",
                        },
                    },
                    "required": ["path", "content"],
                },
            ),
            ToolDefinition(
                name="delete_path",
                description="Remove one file or directory from the workspace.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace root.",
                        }
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="run_command",
                description=(
                    "Run one command in the workspace and return its exit "
                    "code and output. There is no shell: give the program and "
                    'its arguments as a list, e.g. ["python", "-m", '
                    '"unittest", "discover"]. The container has no network '
                    "unless the deployment granted one."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The program and its arguments.",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": (
                                "How long the command may run before it is stopped."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            ),
        )

    def call(self, name: str, arguments: Mapping[str, Any]) -> str:
        """Perform one published tool call and return what it produced.

        Args:
            name: Name of the tool, as published.
            arguments: The arguments the caller supplied.

        Returns:
            What the tool produced, as the text a model reads.

        Raises:
            ToolFailureError: If the tool is unknown, an argument is missing
                or of the wrong shape, or the operation itself was refused.

        """
        handlers = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "delete_path": self._delete_path,
            "run_command": self._run_command,
        }
        handler = handlers.get(name)
        if handler is None:
            published = ", ".join(sorted(handlers))
            raise ToolFailureError(
                f"There is no tool called {name!r}. Tools: {published}."
            )
        try:
            return handler(arguments)
        except WorkspaceError as refused:
            raise ToolFailureError(str(refused)) from refused

    def _list_files(self, arguments: Mapping[str, Any]) -> str:
        """Return every tree's files, one per line, with their sizes."""
        del arguments
        lines = [
            f"{entry.relative_path} ({entry.byte_size} bytes)"
            for entry in self._workspace.entries()
        ]
        for prefix, tree in (
            (MATERIALS_PREFIX, self._materials),
            (OUTPUT_PREFIX, self._output),
        ):
            if tree is None:
                continue
            lines.extend(
                f"{prefix}{entry.relative_path} ({entry.byte_size} bytes)"
                for entry in tree.entries()
            )
        if not lines:
            return "The workspace is empty."
        listing = "\n".join(lines)
        if len(listing) <= _MAX_LISTING_CHARACTERS:
            return listing
        return (
            f"{listing[:_MAX_LISTING_CHARACTERS]}\n"
            f"... [truncated at {_MAX_LISTING_CHARACTERS} characters]"
        )

    def _read_file(self, arguments: Mapping[str, Any]) -> str:
        """Return one file's text, from whichever tree the path names."""
        tree, relative = self._located(_text_argument(arguments, "path"))
        return tree.read_text(relative)

    def _write_file(self, arguments: Mapping[str, Any]) -> str:
        """Write one whole file and report what was written."""
        path = _text_argument(arguments, "path")
        tree, relative = self._located(path, writing=True)
        written = tree.write_text(
            relative, _text_argument(arguments, "content", allow_empty=True)
        )
        return f"Wrote {written} bytes to {path}."

    def _delete_path(self, arguments: Mapping[str, Any]) -> str:
        """Remove one path and report that it is gone."""
        path = _text_argument(arguments, "path")
        tree, relative = self._located(path, writing=True)
        tree.delete(relative)
        return f"Removed {path}."

    def _located(self, path: str, *, writing: bool = False) -> tuple[Workspace, str]:
        """Return the tree one path names and the path relative to it.

        Raises:
            WorkspaceError: If the path names a tree this run was not given,
                or asks to write into the read-only materials.

        """
        stripped = path.strip()
        if stripped.startswith(MATERIALS_PREFIX):
            if writing:
                raise WorkspaceError(
                    f"{stripped!r} is under materials/, which is read-only: the "
                    "task's input files cannot be changed."
                )
            if self._materials is None:
                raise WorkspaceError(
                    f"{stripped!r} names materials/, but this run was given no "
                    "input files."
                )
            return self._materials, stripped[len(MATERIALS_PREFIX) :]
        if stripped.startswith(OUTPUT_PREFIX):
            if self._output is None:
                raise WorkspaceError(
                    f"{stripped!r} names output/, but this run has nowhere to "
                    "leave deliverables."
                )
            return self._output, stripped[len(OUTPUT_PREFIX) :]
        return self._workspace, stripped

    def _run_command(self, arguments: Mapping[str, Any]) -> str:
        """Run one command and report how it ended and what it printed.

        Raises:
            ToolFailureError: If the command is not a list of strings, or the
                timeout is not a whole number.

        """
        command = _command_argument(arguments)
        try:
            result = self._commands.run(
                command, timeout_seconds=_timeout_argument(arguments)
            )
        except (TypeError, ValueError) as unusable:
            raise ToolFailureError(str(unusable)) from unusable
        status = (
            "timed out"
            if result.timed_out
            else f"exit code {result.exit_code}"
            if result.exit_code is not None
            else "did not start"
        )
        return "\n".join(
            (
                f"$ {' '.join(result.command)}",
                f"[{status}]",
                f"--- stdout ---\n{result.stdout}",
                f"--- stderr ---\n{result.stderr}",
            )
        )


def _text_argument(
    arguments: Mapping[str, Any], name: str, *, allow_empty: bool = False
) -> str:
    """Return one string argument.

    Raises:
        ToolFailureError: If the argument is absent, not a string, or blank when a
            value is required.

    """
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolFailureError(f"{name!r} is required and must be a string.")
    if not allow_empty and not value.strip():
        raise ToolFailureError(f"{name!r} must not be blank.")
    return value


def _command_argument(arguments: Mapping[str, Any]) -> Sequence[str]:
    """Return the argument vector a command call named.

    Raises:
        ToolFailureError: If it is not a non-empty list of strings.

    """
    value = arguments.get("command")
    if isinstance(value, str) or not isinstance(value, list):
        raise ToolFailureError(
            "'command' must be a list of strings, e.g. ['python', '--version']."
        )
    if not value:
        raise ToolFailureError("'command' needs at least the program to run.")
    if not all(isinstance(argument, str) for argument in value):
        raise ToolFailureError("Every element of 'command' must be a string.")
    return [str(argument) for argument in value]


def _timeout_argument(arguments: Mapping[str, Any]) -> int | None:
    """Return the bound a command call asked for, if it asked for one.

    Raises:
        ToolFailureError: If the bound is not a positive whole number.

    """
    value = arguments.get("timeout_seconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolFailureError("'timeout_seconds' must be a whole number of seconds.")
    if value <= 0:
        raise ToolFailureError("'timeout_seconds' must be greater than zero.")
    return int(value)
