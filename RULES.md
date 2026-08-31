# Runtime contract

These rules are immutable. They are enforced outside the agent, so changing
this file does not change them. Every iteration must preserve and obey them.

- Start with `README.md`. It describes the current implementation and should
  remain the entry point for the next agent.
- Keep non-empty `Dockerfile` and `main.py` files at the workspace root. The
  resulting tree must build as a container, and every Python file must parse.
- The container is started with no arguments. It must read `AGENT_MODE`,
  `AGENT_TASK`, and `AGENT_WORKSPACE` from the environment. Model access uses
  `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL`.
- A run may also be given `AGENT_MATERIALS`, a directory of read-only input
  files the task refers to, delivered before the container starts, and
  `AGENT_OUTPUT`, a directory in which the files the task asks for are left
  and from which they are collected after the run. Files a task names as
  deliverables must be written there, at the paths the task names.
- Support all three runtime modes. `improve` must leave a complete next
  iteration in the workspace. `probe` must write only its final answer to
  standard output; diagnostics belong on standard error. `describe` must
  write one JSON document to standard output — `{"tools": [{"name",
  "description", "input_schema"}], "notes": "..."}` — listing exactly the
  tools a `probe` or `improve` run would offer its model, without asking any
  model; `AGENT_TASK` may be empty for it.
- A run given `AGENT_OUTPUT` leaves `.meta/tool_calls.json` there — a JSON
  object of tool name to how many times the run called it — so which tools an
  exam exercised is on the record.
- Exit with `0` after a successful run, `1` when a run produces no usable
  result, and `2` when the environment does not describe a valid run.
- Stay within the externally imposed deadlines. Internal timeouts and retries
  must leave enough time to finish and report a result.
- Do not store credentials or secrets in the source tree.
- Only files admitted by `.gitignore` survive into the next iteration. Keep
  durable knowledge in tracked source or documentation; use `memories/` for
  iteration-to-iteration memories.
- Preserve this file unchanged. Rules can only be changed by the system that
  enforces them, never by the agent itself.
