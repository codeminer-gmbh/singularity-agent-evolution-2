"""Regression tests for the evidence policy given to improvement runs."""

from evolving_agent.prompts import improvement_instructions


def test_improvement_role_requires_observed_behavioral_evidence() -> None:
    """Claims must be tied to a command/call and its observed result."""
    instructions = improvement_instructions()

    assert "Verification is evidence, not a ritual" in instructions
    assert "exercises the changed\nbehaviour" in instructions
    assert "exact command and its observed outcome" in instructions
    assert "unverified" in instructions


def test_improvement_role_distinguishes_smoke_checks_from_capability_tests() -> None:
    """A parse/build check alone must not be presented as a feature result."""
    instructions = improvement_instructions()

    assert "focused, reproducible test or fixture" in instructions
    assert "not evidence that a new capability works" in instructions
