#!/bin/sh
# The project check: self-checks of graph and lint, then the server tests. Must pass before "done" and before deploy.
set -eu
cd "$(dirname "$0")/server"
PATH="$HOME/.local/bin:$PATH"  # uv lives there; shells started at boot may lack it
uv run --quiet graph.py && uv run --quiet lint.py && uv run --quiet test_server.py
