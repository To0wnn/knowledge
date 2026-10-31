#!/bin/sh
# The project check: self-checks of the plugin hooks, graph and lint, then the server tests. Must pass before "done" and before deploy.
set -eu
# The server tests use a fixed port: two checks at once (parallel agents, a commit hook) break each other.
if [ -z "${KNOWLEDGE_CHECK_LOCKED:-}" ] && command -v flock >/dev/null; then
  KNOWLEDGE_CHECK_LOCKED=1 exec flock /tmp/knowledge-check.lock sh "$0" "$@"  # sh: "sh check.sh" has no exec bit
fi
cd "$(dirname "$0")/server"
PATH="$HOME/.local/bin:$PATH"  # uv lives there; shells started at boot may lack it
python3 ../plugin/hooks/limits.py --self-test && python3 ../plugin/hooks/secret-guard.py --self-test && python3 ../plugin/hooks/companion.py --self-test
uv run --quiet graph.py && uv run --quiet lint.py && uv run --quiet external_skills.py && uv run --quiet test_server.py
