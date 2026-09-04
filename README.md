# Self-improving agent

This is an intelligent agent with MCP tools and a writable workspace. In
`improve` mode, the workspace contains its own source: the agent studies that
source and leaves behind a successor that is better at its exams and better at
improving itself. In `probe` mode, it uses the same reasoning and tools to
answer a question. In `describe` mode it asks no model at all and prints, as
one JSON document, the tools the other two modes would offer one, read off the
same registry, so the orchestrator can aim its exam at what this agent can
actually do.

Start here whenever you work on this agent, then read [RULES.md](RULES.md).
`RULES.md` is the immutable external contract every iteration must obey.

This tree is the seed of a third run, assembled from what the second run's
four lines of descent learned over 150 rounds. Read
[memories/inherited-lessons.md](memories/inherited-lessons.md) before choosing
what to spend a round on: it says what the exams rewarded, what they never
did, and where the lines lost their work.

## How a round works

An improvement run opens with an evidence plan: four lines in
`memories/round-plan.md` naming the failure with the strongest evidence in the
record, the gap behind it, the one change that removes it, and the check that
would falsify the fix. It makes that change, runs that check, and then passes
through two completion stages before it may finish: an adversarial review of
its own successor, with the tools still available, and a repair stage that
publishes only what the review left standing.

A changed tree is published only if it passes the gate in
`evolving_agent/modes.py`:

- it still holds a `Dockerfile` and a `main.py`, and every Python file parses
  (`evolving_agent/successor.py`);
- no note it added or changed claims a test or verification without naming, in
  a code span, a test or fixture that ships in the tree
  (`evolving_agent/successor.py`);
- it carries `memories/verification.json`, a requirement-to-proof record whose
  `candidate_digest` matches the tree as it stands, so a record written before
  a later edit attests to nothing (`evolving_agent/evidence.py`). The digest is
  printed by `python -m evolving_agent.evidence`, run after the last edit.

A tree that fails the gate is handed back for repair twice, and then put back
as it was found: the cycle records an honest "no successor" rather than a
candidate that cannot be built or a claim that cannot be checked.

Two things are written by the runtime, not by the model. On every exchange the
session shows the model an append-only ledger of every command it has run and
how each one ended, so an early failure stays visible after its output has
fallen out of the bounded history. After the run, `.meta/verification.json`
receives the same facts as a receipt, and the run's report on standard output
opens with a count of commands that ran and passed, before the model's own
summary. A model can describe a check it never ran; it cannot change either.

A probe run is held to three things before its first draft becomes the answer:
a deliverable the task names under `output/` must be on disk, a program the
task asked for must have been run at least once, and the draft must survive an
adversarial review followed by a repair stage (`evolving_agent/completion.py`,
`evolving_agent/prompts.py`). The task's `materials/` and `output/` trees are
linked into the probe's scratch workspace, so a command can run what a tool
wrote.

## How it works

The MCP server publishes file and command tools together with their JSON
schemas. Those schemas become OpenAI Responses API function definitions, so
there is only one tool registry. The model can inspect and edit its workspace,
run bounded commands, observe their results, and repeat until it returns a
final response. Two further trees sit beside the workspace when a run is given
them: the task's input files under `materials/` (read-only) and the place its
deliverables go under `output/`; the same tools reach both by path prefix, and
a probe is told which files it was given and where to put what it delivers.

Requests use `/v1/responses` with `store: false`. The session therefore carries
its own model output and trims history when necessary. The implementation is
container-portable and does not depend on the orchestrator that normally runs
it.

The tools are the ten the second run's exams actually called: files, commands,
documents (PDF, Word, Excel, PowerPoint, OpenDocument, EPUB, email), SQLite,
delimited data, archives, and HTTP. Every reader that was built and never
called by an exam was left behind, with its dependencies.

## Passing knowledge forward

Use `memories/` for durable notes that future iterations should inherit: gaps
found, failed approaches and why, design rationale, what the exam rewarded.
Keep each note concise and useful to an agent that has no access to earlier
conversations, and describe only code that is actually in the tree; every
note added or changed is audited against the diff the version shipped, and the
publication gate refuses a new note that claims a result the tree cannot show.
`memories/round-plan.md` is rewritten by every improvement run;
`memories/verification.json` is the round's proof record.

## Running

```bash
docker build -t evolving-agent .
docker run --rm \
  -e OPENAI_API_KEY \
  -e AGENT_MODE=probe \
  -e AGENT_TASK='What is 6 times 7?' \
  evolving-agent
```

For an improvement run, mount the source workspace:

```bash
docker run --rm \
  -e OPENAI_API_KEY \
  -e AGENT_MODE=improve \
  -e AGENT_TASK='Remove the costliest failure in the record.' \
  -e AGENT_WORKSPACE=/workspace \
  -v "$PWD/scratch:/workspace" \
  evolving-agent
```

To see what the agent can do, without a model:

```bash
docker run --rm -e AGENT_MODE=describe evolving-agent
```

Without Docker:

```bash
pip install -r requirements.txt
AGENT_MODE=probe AGENT_TASK='What does this agent do?' \
AGENT_WORKSPACE=/tmp/agent-scratch python main.py
```

## Gates

The tree is held to four checks, configured in `pyproject.toml` and installed
in the image, so an improvement run can run them on its own successor:

```bash
ruff format . && ruff check . && mypy && python -m pytest -q tests
```

Formatting, lint with docstring and annotation rules, strict typing, and the
tests. A round that leaves any of them failing has made the next round's work
harder; a round that adds behaviour adds the test that would notice it going.

## Configuration

| Variable            | Meaning                                                                 |
| ------------------- | ----------------------------------------------------------------------- |
| `AGENT_MODE`        | `improve`, `probe`, or `describe`                                       |
| `AGENT_TASK`        | Improvement request or question                                         |
| `AGENT_WORKSPACE`   | Writable workspace (default `/workspace`)                               |
| `AGENT_SOURCE_ROOT` | Source copied into an empty improvement workspace                       |
| `AGENT_MATERIALS`   | Read-only input files a task refers to, readable under `materials/`     |
| `AGENT_OUTPUT`      | Where a task's deliverables are left (`output/`) and collected from     |
| `OPENAI_API_KEY`    | Endpoint credential, when required                                      |
| `OPENAI_BASE_URL`   | Responses-compatible endpoint                                           |
| `OPENAI_MODEL`      | Model name; the orchestrator sets it, and the image's default applies only when nothing does |

The endpoint must implement `/v1/responses` and function tools. Dependencies
are pinned in `requirements.txt`; runtime budgets live in
`evolving_agent/settings.py`.

## Layout

| Path                            | Purpose                                                     |
| ------------------------------- | ----------------------------------------------------------- |
| `main.py`                       | Environment-driven entry point                              |
| `RULES.md`                      | Immutable runtime and iteration contract                    |
| `evolving_agent/prompts.py`     | Role, improvement, and completion-stage instructions        |
| `evolving_agent/modes.py`       | Improvement, probe, and describe workflows; the publication gate |
| `evolving_agent/session.py`     | Model/tool loop, history, completion stages, the ledger     |
| `evolving_agent/evidence.py`    | The proof record, its digest binding, the runtime receipt   |
| `evolving_agent/successor.py`   | Build blockers and the notes rule                           |
| `evolving_agent/completion.py`  | What a probe's draft is held to                             |
| `evolving_agent/tools.py`       | The tool registry and its implementations                   |
| `evolving_agent/mcp_server.py`  | MCP workspace tools                                         |
| `evolving_agent/model.py`       | Responses API client                                        |
| `evolving_agent/workspace.py`   | Contained filesystem operations                             |
| `tests/`                        | The agent's own tests, run with `python -m pytest -q tests` |

The MCP server can also run over stdio:

```bash
python -m evolving_agent.mcp_server /path/to/workspace
```
