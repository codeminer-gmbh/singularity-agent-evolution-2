"""The agent's entry point: the file its container runs.

The container is started with no command and no arguments — everything about
the run arrives in the environment — so this module's whole job is to read that
environment, run the mode it names, and turn what came back into the two things
the orchestrator actually reads: what was printed on standard output, and the
process's exit status.

Standard output belongs to the answer. In probe mode it *is* the answer, kept
as an artifact and read by evaluators who never see anything else, so every
diagnostic in this program goes to standard error instead.

Exit status:
    0  the run did what it was asked
    1  the run happened but did not produce a usable result
    2  the environment does not describe a run that could be taken
"""

import logging
import sys
from collections.abc import Sequence

from evolving_agent.modes import RunReport, run_describe, run_improvement, run_probe
from evolving_agent.settings import (
    AgentMode,
    AgentSettings,
    ConfigurationError,
    from_environment,
)
from evolving_agent.workspace import WorkspaceError

_SUCCESS = 0
_FAILED = 1
_MISCONFIGURED = 2

_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"

_LOG = logging.getLogger("evolving_agent")


def _configure_logging() -> None:
    """Send this run's diagnostics to standard error."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _run(settings: AgentSettings) -> RunReport:
    """Perform the run the settings describe."""
    if settings.mode is AgentMode.IMPROVE:
        return run_improvement(settings)
    if settings.mode is AgentMode.DESCRIBE:
        return run_describe(settings)
    return run_probe(settings)


def main(argv: Sequence[str]) -> int:
    """Run one agent container from start to finish.

    Args:
        argv: Command-line arguments, which this program does not take. The
            orchestrator starts the image with no command, so anything here is
            a sign of a caller that expects a different contract.

    Returns:
        The process exit status.

    """
    if argv:
        sys.stderr.write(
            "This agent takes no arguments; the run is described by AGENT_MODE, "
            "AGENT_TASK and AGENT_WORKSPACE.\n"
        )
        return _MISCONFIGURED
    try:
        settings = from_environment()
    except ConfigurationError as unusable:
        sys.stderr.write(f"{unusable}\n")
        return _MISCONFIGURED
    _configure_logging()
    _LOG.info("Starting in %s mode, workspace %s.", settings.mode.value, settings.workspace)
    try:
        report = _run(settings)
    except ConfigurationError as unusable:
        _LOG.error("%s", unusable)
        return _MISCONFIGURED
    except WorkspaceError as unusable:
        _LOG.error("The workspace could not be used: %s", unusable)
        return _FAILED
    if report.answer:
        sys.stdout.write(f"{report.answer}\n")
        sys.stdout.flush()
    _LOG.info("Finished: %s", report.detail)
    return _SUCCESS if report.succeeded else _FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
