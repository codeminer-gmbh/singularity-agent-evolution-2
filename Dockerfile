# syntax=docker/dockerfile:1
#
# The agent's image, which each iteration may evolve while preserving RULES.md.
#
# It is an ordinary container: a Python base, one pinned dependency, the source,
# and an entrypoint that reads its environment. Nothing in it knows about the
# orchestrator that usually starts it, so `docker run` with the same variables
# does the same thing on any machine.
#
# The process runs as root, which is not the usual advice and is deliberate: the
# orchestrator bind-mounts the working copy from a path the daemon creates
# root-owned, so a non-root agent could not write the successor it exists to
# produce. The isolation here is the container — a network the deployment
# decides on, a processor and memory ceiling, a lifetime the caller ends —
# rather than the user inside it.
#
# For reproducible deploys, pin the base image by digest, e.g.
#   FROM python:3.12-slim@sha256:<digest>
FROM python:3.12-slim

# The dependency layer is separate from the source layer, so editing the agent
# does not reinstall anything. Note that this step needs a package index: a
# daemon building a candidate offline has to have the wheels cached or mirrored.
WORKDIR /opt/evolving-agent
COPY requirements.txt requirements.txt
RUN apt-get update \
    && apt-get install --no-install-recommends -y tesseract-ocr \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

# Everything below is this image's own configuration. The run itself arrives in
# the environment — AGENT_MODE, AGENT_TASK, AGENT_WORKSPACE — and so does the
# model: OPENAI_API_KEY, plus OPENAI_BASE_URL when the endpoint is not OpenAI's
# own and OPENAI_MODEL to ask for a different model. Those three are the names
# every OpenAI client already reads, so nothing here is specific to one
# deployment. Anything not stated falls back to the lines below, which is
# exactly the unit the loop versions: a successor that wants a different model
# changes its own image.
#
# Timing is not configurable from outside, because it differs per mode and each
# mode's budget is chosen against the deadline that mode is killed at — an
# improvement run has the best part of 7200 seconds, a probe has to answer
# inside 3600. The numbers live in evolving_agent/settings.py, which is where a
# successor that wants different ones changes them.
ENV AGENT_SOURCE_ROOT=/opt/evolving-agent \
    AGENT_WORKSPACE=/workspace \
    OPENAI_MODEL=gpt-5.6-terra \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY . /opt/evolving-agent

# No command and no arguments: the run is described entirely by the
# environment, and this is what the orchestrator starts.
ENTRYPOINT ["python", "/opt/evolving-agent/main.py"]
