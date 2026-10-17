#!/bin/sh
# Deploy the kennis server to forge as Dockhand stack "kennis" and rebuild. Tokens are copied separately.
set -eu
STACK=/root/docker/dockhand/dockhand_data/stacks/forge/kennis
cd "$(dirname "$0")/server"
uv run --quiet test_server.py
ssh root@<lan-ip> "mkdir -p $STACK /root/docker/kennis/config /root/docker/kennis/state && chown 1000:1000 /root/docker/kennis/state"
rsync -a --delete --exclude .venv --exclude __pycache__ ./ root@<lan-ip>:$STACK/
ssh root@<lan-ip> "cd $STACK && docker compose -p kennis build -q && docker compose -p kennis up -d && sleep 5 && docker ps --filter name=^kennis$ --format '{{.Names}} {{.Status}}' && curl -fsS http://127.0.0.1:8766/health"
