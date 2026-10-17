#!/bin/sh
# Deploy the knowledge server to forge as Dockhand stack "knowledge" and rebuild. Tokens are copied separately.
set -eu
STACK=/root/docker/dockhand/dockhand_data/stacks/forge/knowledge
cd "$(dirname "$0")/server"
uv run --quiet test_server.py
ssh root@<lan-ip> "mkdir -p $STACK /root/docker/knowledge/config /root/docker/knowledge/state && chown 1000:1000 /root/docker/knowledge/state"
rsync -a --delete --exclude .venv --exclude __pycache__ ./ root@<lan-ip>:$STACK/
ssh root@<lan-ip> "cd $STACK && docker compose -p knowledge build -q && docker compose -p knowledge up -d && sleep 5 && docker ps --filter name=^knowledge$ --format '{{.Names}} {{.Status}}' && curl -fsS http://127.0.0.1:8766/health"
