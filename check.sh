#!/bin/sh
# The project check: self-checks of the plugin hooks, graph and lint, then the server tests. Must pass before "done" and before deploy.
set -eu
# A git hook exports GIT_DIR and friends; tests that clone or init in temp dirs would then write into this repo
# (a commit hook once set core.bare=true here).
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
unset KNOWLEDGE_TOKEN KNOWLEDGE_URL KNOWLEDGE_CHAT
cd "$(dirname "$0")"
# The tree this run tests, when the working copy is exactly that (no unstaged or untracked files): the stamp of a pass.
tree=$(git diff --quiet 2>/dev/null && [ -z "$(git ls-files --others --exclude-standard)" ] && git write-tree 2>/dev/null || true)
stamp=$(git rev-parse --path-format=absolute --git-path check-passed 2>/dev/null || true)
if [ "${1:-}" = --unless-passed ] && [ -n "$tree" ] && [ "$tree" = "$(cat "$stamp" 2>/dev/null || true)" ]; then
  echo "check: tree $tree passed already, not run again"
  exit 0
fi
cd server
PATH="$HOME/.local/bin:$PATH"  # uv lives there; shells started at boot may lack it
# Every step below is independent: they run side by side, each with its own log, shown when all are done (about
# 25 s on 8 cores instead of 80 s one after the other). test_server.py runs as shards, see SHARD_OF there.
logs=$(mktemp -d); trap 'rm -rf "$logs"' EXIT
# A background job of sh ignores Ctrl-C: on INT or TERM stop the whole group (the steps and the servers they started).
trap 'trap - INT TERM; kill -TERM 0' INT TERM
step() { name=$1; shift; : >"$logs/$name.rc"; ( "$@" >"$logs/$name" 2>&1 && echo 0 >"$logs/$name.rc" ) & }  # no 0 in .rc = failed
hooks() { for h in limits secret-guard self_update accounts agy_accounts companion model_settings workers chat watch tools sysstats awake admin; do
  python3 "../plugin/hooks/$h.py" --self-test || return 1; done; python3 ../plugin/hooks/docs-sync.py --selftest; }
step hooks hooks
# client code also runs on macOS's /usr/bin/python3 (3.9): an f-string with a backslash in {} once broke the setup there,
# and an `X | None` annotation only fails on import, so each file is imported, not just compiled
step py39 uv run --quiet --no-project --python 3.9 python -I -c "
import importlib.util as u, os, sys
for f in sys.argv[1:]:
    sys.path[:0] = [os.path.dirname(f)]
    s = u.spec_from_file_location(os.path.basename(f)[:-3].replace('-', '_'), f)
    s.loader.exec_module(u.module_from_spec(s))" ../clients/*.py ../plugin/hooks/*.py
# the web UI's scripts: a syntax error stops a whole view in the browser and no Python test would see it (a comment
# once cut a line of agents.js in half, and live showed "ck is not defined")
step js sh -c 'for f in static/*.js; do node --check "$f" || exit 1; done'
step selfchecks sh -c 'uv run --quiet graph.py && uv run --quiet lint.py && uv run --quiet tasks.py && uv run --quiet external_skills.py'
for i in 0 1 2 3 4 5 6 7; do step "test_server-$i" uv run --quiet test_server.py --shard $i; done
step backup_restore uv run --quiet python ../tests/backup_restore.py
step deploy_landing python3 ../tests/deploy_landing.py
wait
failed=
for rc in "$logs"/*.rc; do
  name=$(basename "$rc" .rc)
  if [ "$(cat "$rc")" = 0 ]; then echo "check: $name ok"; else echo "check: $name FAILED:"; cat "$logs/$name"; failed=1; fi
done
[ -z "$failed" ] || exit 1
if [ -n "$tree" ]; then echo "$tree" > "$stamp"; fi
