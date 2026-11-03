#!/bin/sh
# The project check: self-checks of the plugin hooks, graph and lint, then the server tests. Must pass before "done" and before deploy.
set -eu
# A git hook exports GIT_DIR and friends; tests that clone or init in temp dirs would then write into this repo
# (a commit hook once set core.bare=true here).
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
unset KNOWLEDGE_TOKEN KNOWLEDGE_URL KNOWLEDGE_CHAT
cd "$(dirname "$0")/server"
PATH="$HOME/.local/bin:$PATH"  # uv lives there; shells started at boot may lack it
python3 ../plugin/hooks/limits.py --self-test && python3 ../plugin/hooks/secret-guard.py --self-test && python3 ../plugin/hooks/companion.py --self-test && python3 ../plugin/hooks/workers.py --self-test && python3 ../plugin/hooks/chat.py --self-test && python3 ../plugin/hooks/watch.py --self-test
# client code also runs on macOS's /usr/bin/python3 (3.9): an f-string with a backslash in {} once broke the setup there,
# and an `X | None` annotation only fails on import, so each file is imported, not just compiled
uv run --quiet --no-project --python 3.9 python -I -c "
import importlib.util as u, os, sys
for f in sys.argv[1:]:
    sys.path[:0] = [os.path.dirname(f)]
    s = u.spec_from_file_location(os.path.basename(f)[:-3].replace('-', '_'), f)
    s.loader.exec_module(u.module_from_spec(s))" ../clients/*.py ../plugin/hooks/*.py
uv run --quiet graph.py && uv run --quiet lint.py && uv run --quiet tasks.py && uv run --quiet external_skills.py && uv run --quiet test_server.py
