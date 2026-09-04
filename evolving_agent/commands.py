"""Running a command inside the container, under a bound this agent controls.

The container is the isolation boundary — it has the deployment's network mode,
its processor and memory ceilings, and a lifetime the caller ends. What this
module adds inside that boundary is the part nothing outside can supervise: a
command is given no shell, always terminates, and never returns more output than
a model can read.

"Always terminates" means *before the run does*. A runner given the run's budget
never lets a command outlive it, however long the caller asked for: a command
still running when the orchestrator stops the container takes the whole run's
answer with it, and the agent is the only thing in a position to see that coming.
"""

import os
import signal
import subprocess  # noqa: S404 - running commands is this module's purpose
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_DEFAULT_TIMEOUT_SECONDS = 60
_MAX_TIMEOUT_SECONDS = 180
_MAX_OUTPUT_CHARACTERS = 8_000
_SHUTDOWN_SECONDS = 5
"""How long a stopped command is given to let go of its output pipes."""

_LEAST_USEFUL_SECONDS = 1
"""The shortest bound worth starting a command under.

Below it there is no run left to report the result into, so the command is not
started at all — which is a fact the model can act on, rather than a process
killed a moment after it began.
"""

_NO_TIME_LEFT = (
    "This run's time budget is spent, so the command was not started. Report "
    "what you already know instead of running anything further."
)
"""What is reported about a command there was no run left to hold."""


class Budget(Protocol):
    """Whatever knows how much of the run's own time is left."""

    def remaining(self) -> float:
        """Return the seconds the run has before it must stop."""
        ...


@dataclass(frozen=True)
class CommandResult:
    """How one command ended, and what it printed while it ran."""

    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


class CommandRunner:
    """Runs commands in the workspace, bounded and without a shell."""

    def __init__(
        self,
        cwd: Path,
        *,
        default_timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        max_timeout_seconds: int = _MAX_TIMEOUT_SECONDS,
        output_limit: int = _MAX_OUTPUT_CHARACTERS,
        budget: Budget | None = None,
    ) -> None:
        """Hold where commands run and the bounds they run under.

        Args:
            cwd: Directory every command is started in.
            default_timeout_seconds: The bound applied when a caller names
                none.
            max_timeout_seconds: The bound no caller may exceed, whatever time
                the run still has.
            output_limit: How many characters of each stream are kept.
            budget: What is left of the run this runner serves, when it serves
                one. No command outlasts it. A runner without one — the stdio
                server attached to by somebody else — is bounded by the maximum
                alone, because the run it belongs to is not this program's.

        """
        self._cwd = cwd
        self._default_timeout_seconds = default_timeout_seconds
        self._max_timeout_seconds = max_timeout_seconds
        self._output_limit = output_limit
        self._budget = budget

    def run(self, command: Sequence[str], *, timeout_seconds: int | None = None) -> CommandResult:
        """Run one command to completion or to its bound.

        Args:
            command: The program and its arguments. It is passed to the
                operating system as it stands: there is no shell, so no part of
                it is expanded, split or interpreted.
            timeout_seconds: How long it may run; the default bound when none
                is given, and never more than the maximum or what is left of
                the run.

        Returns:
            The exit status and the captured streams, truncated. A command the
            run has no time left for is reported as one that did not start.

        Raises:
            TypeError: If the command is a string rather than a vector, or an
                argument is not a string.
            ValueError: If no program was named.

        """
        argv = _require_argv(command)
        limit = self._bounded_timeout(timeout_seconds)
        if limit is None:
            return CommandResult(
                command=argv,
                exit_code=None,
                stdout="",
                stderr=_NO_TIME_LEFT,
                timed_out=False,
            )
        try:
            # No shell and a fixed argument vector: running commands is this
            # tool's purpose, and the container is what bounds what they can
            # reach. The command gets no standard input — this process's own may
            # be the tool protocol — and a session of its own, so that stopping
            # it stops everything it started.
            started = subprocess.Popen(  # noqa: S603
                argv,
                cwd=self._cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except OSError as unrunnable:
            return CommandResult(
                command=argv,
                exit_code=None,
                stdout="",
                stderr=self._bounded(f"The command could not be run: {unrunnable}"),
                timed_out=False,
            )
        # However this ends, the pipes are closed and the child is reaped: a run
        # makes many commands, and one leaked handle per hung command would run
        # the agent itself out of them.
        with started:
            try:
                stdout, stderr = started.communicate(timeout=limit)
            except subprocess.TimeoutExpired:
                stdout, stderr = _stopped(started)
                return CommandResult(
                    command=argv,
                    exit_code=None,
                    stdout=self._bounded(stdout),
                    stderr=self._bounded(_overran_text(limit, stderr)),
                    timed_out=True,
                )
            return CommandResult(
                command=argv,
                exit_code=started.returncode,
                stdout=self._bounded(stdout),
                stderr=self._bounded(stderr),
                timed_out=False,
            )

    def _bounded_timeout(self, timeout_seconds: int | None) -> int | None:
        """Return the bound one command runs under.

        Three things bound it and the smallest wins: what the caller asked for,
        the maximum this runner was built with, and what is left of the run.

        Returns:
            The bound, or ``None`` when what is left is too little to start
            anything under.

        """
        wanted = self._default_timeout_seconds if timeout_seconds is None else timeout_seconds
        allowed = min(wanted, self._max_timeout_seconds)
        if self._budget is not None:
            allowed = min(allowed, int(self._budget.remaining()))
        return allowed if allowed >= _LEAST_USEFUL_SECONDS else None

    def _bounded(self, stream: str) -> str:
        """Return one captured stream, cut to the length a model is given."""
        if len(stream) <= self._output_limit:
            return stream
        return f"{stream[: self._output_limit]}\n... [truncated at {self._output_limit} characters]"


def _require_argv(command: Sequence[str]) -> tuple[str, ...]:
    """Return the argument vector one command is to be run as.

    Raises:
        TypeError: If the command is a string, or holds anything but strings.
        ValueError: If the vector is empty.

    """
    if isinstance(command, str):
        raise TypeError(
            "A command is a list of arguments, not a string: there is no "
            'shell to split one. Use ["python", "-m", "pytest"].'
        )
    argv = tuple(command)
    if not argv:
        raise ValueError("A command needs at least the program to run.")
    for argument in argv:
        if not isinstance(argument, str):
            raise TypeError(f"Command arguments must be strings; got {argument!r}.")
    return argv


def _stopped(started: "subprocess.Popen[str]") -> tuple[str, str]:
    """Stop one overrunning command and return what it had printed.

    What is stopped is the whole process group rather than the one process this
    agent can see. A command that left a child running would otherwise keep the
    output pipes open, and reading them to the end — which is how the output is
    collected — would wait on a process nobody is bounding.
    """
    try:
        os.killpg(os.getpgid(started.pid), signal.SIGKILL)
    except (OSError, ValueError):
        started.kill()
    try:
        return started.communicate(timeout=_SHUTDOWN_SECONDS)
    except subprocess.TimeoutExpired:
        return "", ""


def _overran_text(limit: int, stderr: str) -> str:
    """Return what is reported about a command that ran past its bound.

    Whatever it managed to print comes with the explanation: a command that
    hangs is usually diagnosable from the last thing it said.
    """
    explanation = (
        f"The command did not finish within {limit} seconds and was stopped, "
        f"along with anything it had started."
    )
    printed = stderr.strip()
    if not printed:
        return explanation
    return f"{explanation}\nWhat it had printed before it was stopped:\n{printed}"
