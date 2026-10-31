#!/bin/sh
# Connect Claude Code, Codex and agy on this computer to a knowledge server, in one line (Linux, macOS):
#   curl -fsSL https://raw.githubusercontent.com/To0wnn/knowledge/main/install.sh | sh
# The same line every time: the first time it installs; after that it updates, offers agents found without the
# plugin, and asks for a token only when the server refuses the stored one (clients/knowledge-agent.py setup).
# It also sets up the local companion: autostart at logon, a menu entry for its page, a first hook-trust run.
# Gets or updates the scripts in ~/knowledge (git), then runs clients/knowledge-agent.py. Same steps as install.ps1.
set -eu
# Everything inside main, called on the last line: a download cut off half-way runs nothing.
main() {
REPO=${KNOWLEDGE_REPO:-https://github.com/To0wnn/knowledge}
DIR=${KNOWLEDGE_DIR:-$HOME/knowledge}
command -v git >/dev/null 2>&1 || { echo "git is missing: install it (e.g. sudo apt install git, or xcode-select --install), then run this again" >&2; exit 1; }
PY=$(command -v python3 || true)
[ -n "$PY" ] || { echo "python3 is missing: install Python 3, then run this again" >&2; exit 1; }

version() { sed -n 's/.*"version": *"\([0-9.]*\)".*/\1/p' "$DIR/plugin/.claude-plugin/plugin.json" 2>/dev/null | head -1; }
old_version=
if [ -d "$DIR/.git" ]; then
  old_version=$(version)
  # Someone's own ~/knowledge (notes, another project) is never reset to our repository.
  have=$(git -C "$DIR" remote get-url origin 2>/dev/null || true)
  if [ "$(norm "$have")" != "$(norm "$REPO")" ]; then
    echo "$DIR is another repository (origin: ${have:-none}): move it away, or set KNOWLEDGE_DIR" >&2; exit 1
  fi
  # The same steps as sync() in plugin/hooks/self_update.py; an old checkout may not have that function yet.
  [ ! -f "$DIR/.git/MERGE_HEAD" ] || git -C "$DIR" merge --abort
  if ! git -C "$DIR" pull --ff-only -q 2>/dev/null; then
    if [ -n "$(git -C "$DIR" status --porcelain --untracked-files=no)" ]; then
      echo "$DIR has local changes: keep them elsewhere, then: git -C $DIR reset --hard origin/main" >&2; exit 1
    fi
    git -C "$DIR" fetch -q
    git -C "$DIR" branch -f "knowledge-before-update-$(git -C "$DIR" rev-parse --short=8 HEAD)" HEAD
    git -C "$DIR" reset -q --keep '@{u}'
  fi
elif [ -e "$DIR" ]; then
  echo "$DIR exists but is not a git checkout: move it away, or set KNOWLEDGE_DIR" >&2; exit 1
else
  git clone -q "$REPO" "$DIR"
fi
new_version=$(version)
if [ -z "$old_version" ]; then echo "$DIR: installed version $new_version"
elif [ "$old_version" = "$new_version" ]; then echo "$DIR: up to date (version $new_version)"
else echo "$DIR: updated version $old_version -> $new_version"
fi

[ $# -gt 0 ] || set -- setup
# Through a pipe stdin is this script, so the setup's questions read the terminal.
if ( : </dev/tty ) 2>/dev/null; then
  exec "$PY" "$DIR/clients/knowledge-agent.py" "$@" </dev/tty
fi
exec "$PY" "$DIR/clients/knowledge-agent.py" "$@"
}

# A clone URL without case, trailing slash or .git.
norm() { printf %s "$1" | tr 'A-Z\\' 'a-z/' | sed -e 's#/*$##' -e 's#\.git$##'; }

main "$@"
