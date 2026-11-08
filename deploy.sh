#!/bin/sh
# Deploy the knowledge server to forge as Dockhand stack "knowledge" and rebuild. Tokens are copied separately.
set -eu
STACK=/root/docker/dockhand/dockhand_data/stacks/forge/knowledge
cd "$(dirname "$0")"
# The rsync below copies the working tree, not a commit: another session's half work would go live with it.
if [ -n "$(git status --porcelain)" ]; then git status --short >&2; echo "deploy refused: uncommitted changes in this checkout; commit them, or deploy from your own worktree" >&2; exit 1; fi
# Sessions work side by side: a checkout without the newest main would silently take other sessions' fixes off the server.
git fetch -q origin main
git merge-base --is-ancestor FETCH_HEAD HEAD || { echo "deploy refused: this checkout lacks commits of origin/main; rebase first" >&2; exit 1; }
# The checks do not parse the web UI's JavaScript: a stopped rebase once went live with conflict markers in it.
if [ -n "$(git ls-files -u)" ] || git grep -qE '^(<<<<<<<|>>>>>>>) ' -- server plugin clients watcher; then echo "deploy refused: unresolved merge conflict in this checkout" >&2; exit 1; fi
[ -f local.env ] || { echo "local.env missing: copy local.env.example and fill in the addresses" >&2; exit 1; }
# Before local.env is read: exported, this installation's settings (projects, language) leak into the tests'
# in-process server and fail them (8 Nov 2026); --unless-passed had hidden that while the tree was already stamped.
./check.sh --unless-passed  # the pre-commit hook usually just ran it on this very tree
set -a; . ./local.env; set +a
# What is live must be part of what goes live, or this deploy takes it off the server again: a fix deployed from a
# branch vanished twice when someone deployed main. --replace-live: on purpose, e.g. a deployed branch was rebased.
LIVE=/root/docker/knowledge/deployed_commit
live=$(ssh "$KNOWLEDGE_FORGE_SSH" "cat $LIVE 2>/dev/null || true")
if [ -n "$live" ] && [ "${1:-}" != --replace-live ] && ! git merge-base --is-ancestor "$live" HEAD 2>/dev/null; then
  echo "deploy refused: live runs $live, which this checkout lacks; merge it into your branch first (or ./deploy.sh --replace-live to take it off the server)" >&2; exit 1
fi
cp local.env server/.env  # docker compose reads the addresses from .env next to compose.yaml
cd server
ssh "$KNOWLEDGE_FORGE_SSH" "mkdir -p $STACK /root/docker/knowledge/config /root/docker/knowledge/state && chown 1000:1000 /root/docker/knowledge/state"
rsync -a --delete --exclude .venv --exclude __pycache__ ./ "$KNOWLEDGE_FORGE_SSH:$STACK/"
rsync -a --delete --copy-links ../plugin/skills/ "$KNOWLEDGE_FORGE_SSH:$STACK/plugin_skills/"  # bundled, see Dockerfile
# Build, then switch with no downtime (swap.sh: green takes over while blue is renewed; fails loudly, never leaves
# no server). The proxy keeps :8766, so nothing else has to know.
ssh "$KNOWLEDGE_FORGE_SSH" "cd $STACK && docker compose -p knowledge build -q && sh swap.sh && echo $(git rev-parse HEAD) > $LIVE"
# Ready = semantic index loaded too (a first start opens the port before that; a switch only after): wait for it.
ssh "$KNOWLEDGE_FORGE_SSH" 'for i in $(seq 1 600); do curl -fsS -o /dev/null http://127.0.0.1:8766/health?ready=1 2>/dev/null && { docker ps --filter name=^knowledge --format "{{.Names}} {{.Status}}"; echo ok; exit 0; }; sleep 1; done; docker logs knowledge --tail 20; exit 1'
