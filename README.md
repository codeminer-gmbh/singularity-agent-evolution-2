# The codex judge — a referee tree

An operator-authored referee that shares **no code, no scaffolding and no
model family** with the lineage it judges: the agent is the OpenAI Codex CLI
(`@openai/codex`), and this tree is a ~60-line adapter mapping the
orchestrator's contract (`AGENT_MODE`, `AGENT_TASK`, stdout-is-the-answer)
onto `codex exec`. Its temperament (`preamble.md`) is the skeptic's: verify
before believing, weigh only what is demonstrated, never invent a difference
to avoid a tie.

Run it with `REFEREE_OPENAI_MODEL` set to a codex-family model so the judge's
errors are not the lineage's own on any axis.

It is not part of the lineage: nothing improves on it, it sits on no side of
any comparison, and it answers `improve` mode with a refusal.
