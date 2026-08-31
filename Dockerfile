# The codex judge: a referee that shares no code, no scaffolding and no model
# family with the lineage it judges. The whole agent is the OpenAI Codex CLI;
# this image only adapts the orchestrator's contract (AGENT_MODE, AGENT_TASK,
# stdout-is-the-answer) onto `codex exec`.
FROM node:22-slim

# The codex binary is Rust and validates TLS against the system CA store,
# which the slim image does not ship; without this every connection fails as
# an unknown issuer while Node's own fetch (bundled CAs) works fine.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

# Unpinned deliberately: this is an operator tree, rebuilt only when the
# operator rebuilds it, and the installed version is printed at build time so
# every image records what it got.
RUN npm install -g @openai/codex && codex --version

ENV PYTHONUNBUFFERED=1
WORKDIR /agent
COPY . /agent
CMD ["python3", "/agent/main.py"]
