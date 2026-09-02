# Self-improving agent

This is an intelligent agent with MCP tools and a writable workspace. In
`improve` mode, the workspace contains its own source: the agent studies that
source and leaves behind a more capable next iteration. In `probe` mode, it
uses the same reasoning and tools to answer a question. In `describe` mode it
asks no model at all and prints, as one JSON document, the tools the other two
modes would offer one — read off the same registry, so the orchestrator can aim
its exam at what this agent can actually do.

Every run given `AGENT_OUTPUT` also leaves `.meta/tool_calls.json` there — how
many times it called each tool — so which capabilities an exam exercised is a
fact on the record.

Start here whenever you work on this agent, then read [RULES.md](RULES.md).
`RULES.md` is the immutable external contract every iteration must obey.

An improvement run opens with an evidence-led audit. When the orchestrator
supplies `materials/ledger.md`, the agent reads its selection outcomes before
planning: it distinguishes recurring task failures from unexercised changes
and does not treat a verdict as proof about an unrelated diff. It then writes
three lines in `memories/round-plan.md`: a recurring or plausible task-solving
limitation, the smallest change that removes it, and the evidence for choosing
it over competing gaps. The objective is a successor more likely to solve hard
tasks end-to-end — interpreting requests, collecting evidence, using tools,
writing deliverables, and verifying results — rather than simply one with more
named tools or supported formats. A new capability remains worthwhile when it
removes a demonstrated or strongly plausible blocker. Keeping the tree
buildable, startable and able to improve itself is a gate the orchestrator
checks; it is not what a round is for.

## How it works

The MCP server publishes file and command tools together with their JSON
schemas. Those schemas become OpenAI Responses API function definitions, so
there is only one tool registry. The model can inspect and edit its workspace,
run bounded commands, observe their results, and repeat until it returns a
final response. Two further trees sit beside the workspace when a run is given
them: the task's input files under `materials/` (read-only) and the place its
deliverables go under `output/`; the same tools reach both by path prefix, and
a probe is told which files it was given and where to put what it delivers.

`inspect_database` reads SQLite evidence attachments in immutable read-only mode, reports table/view columns, and can return bounded results for a single read-only SQL query.

`inspect_parquet` reads a Parquet attachment in-process, reports its typed columns, and can run a bounded SQL query against its single `data` view. It refuses external scans and state-changing SQL, so a query cannot turn an evidence inspection into a filesystem read.

`inspect_tabular` provides the same bounded typed-schema and single-`data`-view SQL workflow for CSV, TSV, JSON, JSONL, and NDJSON attachments.

`inspect_document` handles common binary attachments directly: DOCX and PPTX package text; XLSX worksheet cells (with coordinates, cached formula values, and shared or inline text); ODT, ODS, and ODP package text; EPUB spine chapters; PDF embedded text and page OCR; and image OCR. This lets a
probe inspect evidence supplied under `materials/` without extracting an
attachment into the writable tree. Its previews are bounded like other file
inspection results.

`inspect_audio` transcribes a supported local MP3, WAV, M4A, OGG, FLAC, MP4, MPEG, MPGA, or WEBM attachment via the configured OpenAI-compatible transcription API. It never writes the source file, rejects uploads over 25 MB, and bounds returned transcription evidence.

`inspect_email` reads RFC 822 `.eml` correspondence, Unix mbox exports, and Outlook `.msg` messages without writing them out. It presents decoded headers and body text, inventories attachments, can preview a selected textual attachment, and renders an embedded forwarded Outlook message selected as an attachment.

`fetch_web_page` retrieves a single HTTP(S) page as bounded readable evidence, including its visible text, title, final URL, and HTTP(S) links, without executing page content or saving it to disk.

`download_web_file` acquires one bounded HTTP(S) binary resource (up to 25 MB) into the workspace or `output/` atomically. A probe can use it to bring a web-hosted PDF, office file, archive, or dataset into the existing local inspectors; it sends no credentials, cookies, or request body.

Requests use `/v1/responses` with `store: false`. The session therefore carries
its own model output and trims history when necessary. The implementation is
container-portable and does not depend on the orchestrator that normally runs
it.

## Passing knowledge forward

Use a `memories/` directory for durable notes that future iterations should
inherit: gaps found, failed approaches and why, design rationale, what the exam
rewarded, and promising next steps. Keep each memory concise and useful to an
agent that has no access to earlier conversations, and describe only code that
is actually in the tree — a note about a capability the tree does not hold is
inherited by every successor as fact. The directory is intentionally not
ignored and is copied with the rest of the source; `memories/round-plan.md` is
rewritten by every improvement run.

## Running

```bash
docker build -t evolving-agent agent/
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
  -e AGENT_TASK='Improve recovery from a failed step.' \
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

## Configuration

| Variable            | Meaning                                                 |
| ------------------- | ------------------------------------------------------- |
| `AGENT_MODE`        | `improve` or `probe`                                    |
| `AGENT_TASK`        | Improvement request or question                         |
| `AGENT_WORKSPACE`   | Writable workspace (default `/workspace`)               |
| `AGENT_SOURCE_ROOT` | Source copied into an empty improvement workspace       |
| `AGENT_MATERIALS`   | Read-only input files, readable under `materials/`      |
| `AGENT_OUTPUT`      | Deliverable directory, writable under `output/`         |
| `OPENAI_API_KEY`    | Endpoint credential, when required                      |
| `OPENAI_BASE_URL`   | Responses-compatible endpoint                           |
| `OPENAI_MODEL`      | Model name; defaults to `gpt-5.6-terra` when unspecified |

The endpoint must implement `/v1/responses` and function tools. Dependencies
are pinned in `requirements.txt`; runtime budgets live in
evolving_agent/settings.py.

## Layout

| Path                           | Purpose                                      |
| ------------------------------ | -------------------------------------------- |
| `main.py`                      | Environment-driven entry point               |
| `RULES.md`                     | Immutable runtime and iteration contract     |
| `evolving_agent/prompts.py`    | Role and improvement instructions            |
| `evolving_agent/modes.py`      | Improvement and probe workflows              |
| `evolving_agent/session.py`    | Model/tool loop and history                  |
| `evolving_agent/mcp_server.py` | MCP workspace tools                          |
| `evolving_agent/tools.py`      | Shared tool registry and handlers            |
| `evolving_agent/documents.py`  | Bounded attachment text/OCR extraction       |
| `evolving_agent/databases.py`  | Read-only bounded SQLite evidence inspection |
| `evolving_agent/parquet.py`    | Read-only bounded Parquet evidence inspection |
| `evolving_agent/tabular.py`    | Read-only bounded CSV/TSV/JSON evidence inspection |
| `evolving_agent/workspace.py`  | Contained filesystem operations               |
| `evolving_agent/successor.py`  | Next-iteration validation                    |

The MCP server can also run over stdio:

```bash
python -m evolving_agent.mcp_server /path/to/workspace
```
