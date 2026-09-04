"""The budgets each mode runs under, and how a run is read from the environment."""

import pytest

from evolving_agent.settings import TIMING, AgentMode, ConfigurationError, from_environment

_PROBE_DEADLINE = 3600
_IMPROVEMENT_DEADLINE = 7200


@pytest.mark.parametrize(
    ("mode", "deadline"),
    [(AgentMode.PROBE, _PROBE_DEADLINE), (AgentMode.IMPROVE, _IMPROVEMENT_DEADLINE)],
)
def test_each_budget_leaves_room_for_the_exchanges_that_outlive_it(
    mode: AgentMode, deadline: int
) -> None:
    timing = TIMING[mode]
    # The exchange in flight and one more after it may run to their timeout.
    assert timing.time_budget_seconds + 2 * timing.model_timeout_seconds <= deadline


def test_the_probe_step_allowance_is_no_longer_the_seed_s_twelve() -> None:
    # The second experiment's exam generators ran under a twelve-step cap that
    # no line ever raised, because its cost landed on the cycle rather than on
    # the agent. A generator needs room to write a task, its check, and its
    # materials, and to run the check against its own reference.
    assert TIMING[AgentMode.PROBE].max_steps >= 30
    assert TIMING[AgentMode.IMPROVE].max_steps >= TIMING[AgentMode.PROBE].max_steps


def test_a_run_is_read_from_the_environment() -> None:
    settings = from_environment(
        {
            "AGENT_MODE": "probe",
            "AGENT_TASK": " what? ",
            "OPENAI_MODEL": "m",
            "AGENT_OUTPUT": "/out",
        }
    )

    assert settings.mode is AgentMode.PROBE
    assert settings.task == "what?"
    assert settings.model.model_name == "m"
    assert str(settings.output) == "/out"
    assert settings.materials is None


def test_describe_needs_no_task_but_the_others_do() -> None:
    assert from_environment({"AGENT_MODE": "describe"}).mode is AgentMode.DESCRIBE
    with pytest.raises(ConfigurationError):
        from_environment({"AGENT_MODE": "improve"})
    with pytest.raises(ConfigurationError):
        from_environment({"AGENT_MODE": "dance", "AGENT_TASK": "x"})
