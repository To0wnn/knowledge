#!/bin/sh
# Deploy the knowledge server to forge as Dockhand stack "knowledge" and rebuild. Tokens are copied separately.
set -eu
STACK=/root/docker/dockhand/dockhand_data/stacks/forge/knowledge
cd "$(dirname "$0")"
# The rsync below copies the working tree, not a commit: another session's half work would go live with it.
if [ -n "$(git status --porcelain)" ]; then git status --short >&2; echo "deploy refused: uncommitted changes in this checkout; commit them, or deploy from your own worktree" >&2; exit 1; fi
for a in "$@"; do case $a in --replace-live|--fix-main) ;; *) echo "unknown option $a (known: --replace-live, --fix-main)" >&2; exit 1;; esac; done
has() { for a in $ARGS; do [ "$a" = "$1" ] && return 0; done; return 1; }
ARGS="$*"
# A fresh worktree has no local.env (it is not in git): use the one of the main checkout.
ENVF=./local.env
[ -f "$ENVF" ] || ENVF="$(git rev-parse --path-format=absolute --git-common-dir)/../local.env"
[ -f "$ENVF" ] || { echo "local.env missing: copy local.env.example and fill in the addresses" >&2; exit 1; }
FORGE=$(set -a; . "$ENVF"; printf %s "$KNOWLEDGE_FORGE_SSH")
# A red main has an owner: whoever deploys next would stack work on a broken base, and on 8 Nov 2026 main stayed red
# for about 13 hours while sessions kept deploying. --fix-main: this deploy is that fix.
if ! has --fix-main; then
  if command -v gh >/dev/null 2>&1; then
    for w in check.yml clients.yml; do
      red=$(gh run list --workflow "$w" --branch main --limit 15 --json status,conclusion,headSha,displayTitle --jq \
        'map(select(.status == "completed" and .conclusion != "cancelled" and .conclusion != "skipped"))[0] | select(.conclusion != "success") | "\(.headSha[0:7]) (\(.conclusion)) \(.displayTitle)"' 2>/dev/null) \
        || { echo "warning: could not ask GitHub whether main is green ($w)" >&2; continue; }
      [ -z "$red" ] || { echo "deploy refused: CI $w is red on main since $red; fix that first (gh run list --workflow $w --branch main), and deploy the fix with --fix-main" >&2; exit 1; }
    done
  else echo "warning: gh missing, not checked whether CI on main is green" >&2; fi
fi
# One deploy at a time, and landing happens under it: bringing in main, the plugin version, the check and the push. Two
# at once can finish in the wrong order and leave the older commit live, and two sessions that each picked the next
# version number took the same one 8 times on 9 Nov 2026. A lock left by a deploy that died is taken over after 30
# minutes (check, build and ready wait stay well under that); each deploy removes only its own lock, so one that
# outlived its lock never frees the next one's.
LOCK=/root/docker/knowledge/deploy.lock
me="$(hostname)-$(git rev-parse --short HEAD)-$$-$(date +%s)"
got=$(ssh "$FORGE" "find $LOCK -maxdepth 0 -mmin +30 -exec rm -rf {} + 2>/dev/null; if mkdir $LOCK 2>/dev/null; then echo $me > $LOCK/by && echo ok; else cat $LOCK/by 2>/dev/null || echo unknown; fi") \
  || { echo "deploy refused: forge did not answer over ssh" >&2; exit 1; }
[ "$got" = ok ] || { echo "deploy refused: another deploy is running ($got); run it again when that one is done" >&2; exit 1; }
# A deploy that stops before its push takes back its own merge and version commits: left behind, the version lines
# would conflict with main at the next try.
pre=$(git rev-parse HEAD)
trap '[ -n "${pushed:-}" ] || git reset -q --hard "$pre"; ssh "$FORGE" "[ \"\$(cat $LOCK/by 2>/dev/null)\" = $me ] && rm -rf $LOCK || true"' EXIT
trap 'exit 130' INT; trap 'exit 143' TERM HUP  # dash runs the EXIT trap on exit, not on a signal
# Sessions work side by side: main is brought in here, when landing, so a session need not merge it on every change.
git fetch -q origin main
if ! git merge-base --is-ancestor FETCH_HEAD HEAD; then
  git merge -q --no-edit FETCH_HEAD >/dev/null 2>&1 || { git merge --abort 2>/dev/null; echo "deploy refused: origin/main does not merge cleanly into this checkout; git merge origin/main, resolve, commit, deploy again" >&2; exit 1; }
  echo "deploy: brought in origin/main ($(git rev-parse --short FETCH_HEAD))"
fi
# A pc updates its plugin only to a new version, so a plugin/ change needs a number main does not have yet. Handed out
# here, under the lock, so two sessions never pick the same one; a higher number set by hand (a minor bump) stays.
pv() { git show "$1:plugin/.claude-plugin/plugin.json" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p'; }
old=$(pv FETCH_HEAD); cur=$(pv HEAD)
if ! git diff --quiet FETCH_HEAD HEAD -- plugin && { [ "$cur" = "$old" ] || [ "$(printf '%s\n%s\n' "$cur" "$old" | sort -V | tail -1)" = "$old" ]; }; then
  new=$(echo "$old" | awk -F. '{print $1"."$2"."$3+1}')
  VF="plugin/.claude-plugin/plugin.json plugin/.codex-plugin/plugin.json server.json"
  sed -i -e "s/\"version\": *\"$cur\"/\"version\": \"$new\"/" -e "s/knowledge:$cur\"/knowledge:$new\"/" $VF
  [ "$(grep -l "\"version\": \"$new\"" $VF | wc -l)" = 3 ] || { echo "deploy refused: could not set the plugin version to $new" >&2; git checkout -q -- $VF; exit 1; }
  git commit -q -m "chore(plugin): bump to $new (handed out by deploy.sh)" -- $VF || { git checkout -q -- $VF; echo "deploy refused: the check failed on the combined tree" >&2; exit 1; }
  echo "deploy: plugin version $new"
fi
# The checks do not parse the web UI's JavaScript: a stopped rebase once went live with conflict markers in it.
if [ -n "$(git ls-files -u)" ] || git grep -qE '^(<<<<<<<|>>>>>>>) ' -- server plugin clients watcher; then echo "deploy refused: unresolved merge conflict in this checkout" >&2; exit 1; fi
# Before local.env is read: exported, this installation's settings (projects, language) leak into the tests'
# in-process server and fail them (8 Nov 2026); --unless-passed had hidden that while the tree was already stamped.
# Runs on the combined tree: two branches that merge cleanly can still break together.
./check.sh --unless-passed  # the pre-commit hook usually just ran it on this very tree
set -a; . "$ENVF"; set +a
# What is live must be part of what goes live, or this deploy takes it off the server again: a fix deployed from a
# branch vanished twice when someone deployed main. --replace-live: on purpose, e.g. a deployed branch was rebased.
LIVE=/root/docker/knowledge/deployed_commit
live=$(ssh "$KNOWLEDGE_FORGE_SSH" "cat $LIVE 2>/dev/null || true")
if [ -n "$live" ] && ! has --replace-live && ! git merge-base --is-ancestor "$live" HEAD 2>/dev/null; then
  echo "deploy refused: live runs $live, which this checkout lacks; merge it into your branch first (or ./deploy.sh --replace-live to take it off the server)" >&2; exit 1
fi
# What goes live is in main first: then no branch holds live code that others must merge from it, and every session
# lands its work the same way. A refused push means main moved on outside a deploy (deploys hold the lock).
git push -q origin HEAD:main && pushed=1 || { echo "deploy refused: origin/main moved on outside a deploy; deploy again (it brings main in)" >&2; exit 1; }
cp "$ENVF" server/.env  # docker compose reads the addresses from .env next to compose.yaml
cd server
ssh "$KNOWLEDGE_FORGE_SSH" "mkdir -p $STACK /root/docker/knowledge/config /root/docker/knowledge/state && chown 1000:1000 /root/docker/knowledge/state"
rsync -a --delete --exclude .venv --exclude __pycache__ ./ "$KNOWLEDGE_FORGE_SSH:$STACK/"
rsync -a --delete --copy-links ../plugin/skills/ "$KNOWLEDGE_FORGE_SSH:$STACK/plugin_skills/"  # bundled, see Dockerfile
# Build, then switch with no downtime (swap.sh: green takes over while blue is renewed; fails loudly, never leaves
# no server). The proxy keeps :8766, so nothing else has to know.
ssh "$KNOWLEDGE_FORGE_SSH" "cd $STACK && docker compose -p knowledge build -q && sh swap.sh && echo $(git rev-parse HEAD) > $LIVE"
# Ready = semantic index loaded too (a first start opens the port before that; a switch only after): wait for it.
ssh "$KNOWLEDGE_FORGE_SSH" 'for i in $(seq 1 600); do curl -fsS -o /dev/null http://127.0.0.1:8766/health?ready=1 2>/dev/null && { docker ps --filter name=^knowledge --format "{{.Names}} {{.Status}}"; echo ok; exit 0; }; sleep 1; done; docker logs knowledge --tail 20; exit 1'
