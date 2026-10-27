#!/bin/sh
# Connect Claude Code, Codex and agy on this computer to a knowledge server, in one line (Linux, macOS):
#   curl -fsSL https://raw.githubusercontent.com/To0wnn/knowledge/main/install.sh | sh
# Update every agent that has the plugin, no questions:
#   curl -fsSL https://raw.githubusercontent.com/To0wnn/knowledge/main/install.sh | sh -s update
# One agent: ... | sh -s install codex
# Gets or updates the scripts in ~/knowledge (git), then runs clients/knowledge-agent.py. Same steps as install.ps1.
set -eu
REPO=${KNOWLEDGE_REPO:-https://github.com/To0wnn/knowledge}
DIR=${KNOWLEDGE_DIR:-$HOME/knowledge}
command -v git >/dev/null 2>&1 || { echo "git is missing: install it (e.g. sudo apt install git, or xcode-select --install), then run this again" >&2; exit 1; }
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "python3 is missing: install Python 3, then run this again" >&2; exit 1; }

if [ -d "$DIR/.git" ]; then
  # The same steps as sync() in plugin/hooks/self_update.py; an old checkout may not have that function yet.
  [ ! -f "$DIR/.git/MERGE_HEAD" ] || git -C "$DIR" merge --abort
  if ! git -C "$DIR" pull --ff-only -q 2>/dev/null; then
    if [ -n "$(git -C "$DIR" status --porcelain --untracked-files=no)" ]; then
      echo "$DIR has local changes: keep them elsewhere, then: git -C $DIR reset --hard origin/main" >&2; exit 1
    fi
    git -C "$DIR" fetch -q
    git -C "$DIR" branch -f knowledge-before-update HEAD
    git -C "$DIR" reset -q --hard '@{u}'
  fi
elif [ -e "$DIR" ]; then
  echo "$DIR exists but is not a git checkout: move it away, or set KNOWLEDGE_DIR" >&2; exit 1
else
  git clone -q "$REPO" "$DIR"
fi
echo "$DIR: up to date with GitHub"

[ $# -gt 0 ] || set -- install
# Through a pipe stdin is this script, so the setup's questions read the terminal.
if [ "$1" = install ] && ( : </dev/tty ) 2>/dev/null; then
  exec "$PY" "$DIR/clients/knowledge-agent.py" "$@" </dev/tty
fi
exec "$PY" "$DIR/clients/knowledge-agent.py" "$@"
