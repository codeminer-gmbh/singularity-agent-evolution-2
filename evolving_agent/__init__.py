"""An intelligent agent that evolves its own implementation.

Everything under this package is the agent's own source, and the agent's own
source is the thing it is asked to improve. An improvement run starts this
program with its current tree in a mounted workspace and expects a complete,
buildable next iteration to be left behind; a probe run starts it with a
question and expects an answer on standard output.

There is one dependency, pinned in ``requirements.txt``: the official OpenAI
client, which is how the agent reaches any OpenAI-compatible endpoint and how it
offers the model the tools its MCP server publishes. Everything else is the
standard library, and keeping it that way is worth something — each dependency
is another thing the daemon building a successor has to be able to fetch.
"""
