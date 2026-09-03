"""Executable contract for strict acceptance-boundary reasoning in probes."""

from evolving_agent.prompts import probe_instructions


def test_probe_policy_distinguishes_exact_language_from_convenient_helpers() -> None:
    prompt = probe_instructions()

    # Representative rule: after permitted trimming, accept ASCII decimal
    # digits only.  ``+1`` is a nearest invalid neighbour that int(), a broad
    # regex, or an incautious character helper can accidentally admit.
    assert "exact acceptance language" in prompt
    assert "list every transformation the specification permits, in order" in prompt
    assert "sign, alphabet, case, whitespace position" in prompt
    assert "numeric conversion, character-class helpers" in prompt
    assert "actual public" in prompt
    assert "entry point or produced deliverable" in prompt
    assert "produced deliverable" in prompt


def test_probe_policy_accounts_for_existing_decode_layer_and_syntax_variants() -> None:
    prompt = probe_instructions()

    # An XML parser turns ``&amp;amp;`` into literal ``&amp;``.  A second entity
    # decode would incorrectly turn it into ``&``.  Paired and self-closing
    # elements are likewise equivalent only when the specified grammar says so.
    assert "second entity/URL/escape decode" in prompt
    assert "decoded text is not permission to decode it again" in prompt
    assert "paired and self-closing elements" in prompt
    assert "silently accepting unrelated syntax" in prompt


def test_probe_policy_prevents_defaults_from_masking_valid_explicit_values() -> None:
    prompt = probe_instructions()

    assert "first test an explicit valid value" in prompt
    assert "default cannot hide an over-broad rejection" in prompt
    assert "invalid neighbours" in prompt
    assert "Supplied tests are a floor" in prompt
