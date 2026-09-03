# Probe contract review

Probe-mode software tasks now receive a requirement-to-check contract protocol in their actual system prompt. It requires omitted parser precedence cases and lifecycle state/event races, including detached live work and completion-before-callback, to be exercised after supplied tests pass. `tests/test_probe_contract_review.py` invokes `run_probe` with a capturing model and proves delivery; the protocol is explicitly conditional for non-software questions.
