# Runtime verification ledger

`ToolAgentSession` now reconstructs a compact, append-only ledger of every `run_command` observation and injects it into each subsequent model turn. It labels exit 0 as passed, nonzero exit as failed, and tool errors or absent exit status as did not start, so earlier failures stay visible after ordinary conversation truncation. The improvement prompt tells the model to reconcile this runtime ledger before notes and final claims.

Focused verification initially failed because command arrays were double-encoded as JSON strings. After storing normal-size commands as structured data, the exact rerun passed: a fake three-turn session observed the failed first command on turn two and both the failed and passing attempts, in order, on the final turn.