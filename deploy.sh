#!/bin/sh
# Deploy the knowledge server to forge as Dockhand stack "knowledge" and rebuild. Tokens are copied separately.
set -eu
STACK=/root/docker/dockhand/dockhand_data/stacks/forge/knowledge
cd "$(dirname "$0")"
[ -f local.env ] || { echo "local.env missing: copy local.env.example and fill in the addresses" >&2; exit 1; }
set -a; . ./local.env; set +a
cp local.env server/.env  # docker compose reads the addresses from .env next to compose.yaml
./check.sh
cd server
ssh "$KNOWLEDGE_FORGE_SSH" "mkdir -p $STACK /root/docker/knowledge/config /root/docker/knowledge/state && chown 1000:1000 /root/docker/knowledge/state"
rsync -a --delete --exclude .venv --exclude __pycache__ ./ "$KNOWLEDGE_FORGE_SSH:$STACK/"
rsync -a --delete --copy-links ../plugin/skills/ "$KNOWLEDGE_FORGE_SSH:$STACK/plugin_skills/"  # bundled, see Dockerfile
# Build, then switch with no downtime (swap.sh: green takes over while blue is renewed; fails loudly, never leaves
# no server). The proxy keeps :8766, so nothing else has to know.
ssh "$KNOWLEDGE_FORGE_SSH" "cd $STACK && docker compose -p knowledge build -q && sh swap.sh"
# Ready = semantic index loaded too (a first start opens the port before that; a switch only after): wait for it.
ssh "$KNOWLEDGE_FORGE_SSH" 'for i in $(seq 1 600); do curl -fsS -o /dev/null http://127.0.0.1:8766/health?ready=1 2>/dev/null && { docker ps --filter name=^knowledge --format "{{.Names}} {{.Status}}"; echo ok; exit 0; }; sleep 1; done; docker logs knowledge --tail 20; exit 1'
