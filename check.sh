#!/bin/sh
# The project check: self-checks of the plugin hooks, graph and lint, then the server tests. Must pass before "done" and before deploy.
set -eu
# A git hook exports GIT_DIR and friends; tests that clone or init in temp dirs would then write into this repo
# (a commit hook once set core.bare=true here).
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
cd "$(dirname "$0")/server"
PATH="$HOME/.local/bin:$PATH"  # uv lives there; shells started at boot may lack it
python3 ../plugin/hooks/limits.py --self-test && python3 ../plugin/hooks/secret-guard.py --self-test && python3 ../plugin/hooks/companion.py --self-test
uv run --quiet graph.py && uv run --quiet lint.py && uv run --quiet external_skills.py && uv run --quiet test_server.py
