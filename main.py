"""The orchestrator's contract, adapted onto the Codex CLI.

AGENT_MODE=probe      answer AGENT_TASK; stdout is the answer, and only the
                      answer — diagnostics go to stderr.
AGENT_MODE=describe   print the capability manifest and exit.
anything else         refuse: a referee is never improved on.

AGENT_MATERIALS (read-only input files) and AGENT_OUTPUT (deliverables) are
named in the prompt when the run was given them. The model comes from
OPENAI_MODEL, the credential from OPENAI_API_KEY — both injected by the
runtime; the key is piped to the CLI's login, never an argument, so it can
appear in no process listing and no log.
"""

import os
import pathlib
import subprocess
import sys
import tempfile

AGENT_ROOT = pathlib.Path(__file__).resolve().parent


def main() -> int:
    mode = os.environ.get("AGENT_MODE", "")
    if mode == "describe":
        sys.stdout.write((AGENT_ROOT / "manifest.json").read_text())
        return 0
    if mode != "probe":
        print("The codex judge only answers questions; it is not improved on.", file=sys.stderr)
        return 3
    task = os.environ.get("AGENT_TASK", "").strip()
    if not task:
        print("No AGENT_TASK was given.", file=sys.stderr)
        return 2

    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        subprocess.run(
            ["codex", "login", "--with-api-key"],
            input=key,
            text=True,
            stdout=sys.stderr,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    parts = [(AGENT_ROOT / "preamble.md").read_text()]
    materials = os.environ.get("AGENT_MATERIALS", "")
    if materials and pathlib.Path(materials).is_dir():
        parts.append(f"Input files for the task are under: {materials} (read-only).")
    output = os.environ.get("AGENT_OUTPUT", "")
    if output:
        pathlib.Path(output).mkdir(parents=True, exist_ok=True)
        parts.append(
            f"Where the task asks for files to be delivered, write them under: {output}"
        )
    parts.append(f"The task:\n{task}")

    workdir = tempfile.mkdtemp()
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as last:
        last_path = last.name
    command = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        workdir,
        "--output-last-message",
        last_path,
    ]
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if model:
        command += ["--model", model]
    command.append("\n\n".join(parts))

    # Codex's own event stream goes to stderr so stdout stays the answer.
    finished = subprocess.run(command, stdout=sys.stderr, check=False)
    answer = pathlib.Path(last_path).read_text() if pathlib.Path(last_path).exists() else ""
    if finished.returncode != 0 or not answer.strip():
        print(
            f"codex exec failed with status {finished.returncode} "
            "or produced no final message.",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
