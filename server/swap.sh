#!/bin/sh
# Zero-downtime switch of the knowledge stack, run on the Docker host by deploy.sh after the image is built.
# Both servers use the host network and share :8766 with SO_REUSEPORT. A server opens the port only when it is ready
# (index and semantic index loaded) and then writes state/listening-<instance>; when it stops, it closes the port
# first and its queued connections move to the other server (net.ipv4.tcp_migrate_req=1 on the host).
#   1. green starts with the new image and must be listening
#   2. blue stops (green serves alone), is renewed with the new image and must be listening
#   3. green stops
# Any step that fails leaves a server listening and exits non-zero (deploy.sh fails loudly).
set -eu
cd "$(dirname "$0")"
c="docker compose -p ${KNOWLEDGE_PROJECT:-knowledge}"
state=${KNOWLEDGE_STATE_DIR:-/root/docker/knowledge/state}
n=${KNOWLEDGE_NAME:-knowledge}  # container names: $n (blue), $n-green (tests/swap/run.sh uses its own)
listening() {  # up to 10 minutes: a changed embedding model re-embeds everything before the port opens
    i=0; while [ $i -lt 600 ]; do [ -f "$state/listening-$1" ] && return 0; sleep 1; i=$((i + 1)); done
    docker logs "$2" --tail 20; return 1
}
# The handover needs the kernel to move a stopping server's queued connections (Linux 5.14+); fail when it is off.
[ "$(cat /proc/sys/net/ipv4/tcp_migrate_req 2>/dev/null)" = 1 ] || { echo "net.ipv4.tcp_migrate_req is not 1" >&2; exit 1; }
$c --profile deploy config -q  # a broken compose file must never stop the running server
if docker ps -a --format '{{.Names}}' | grep -qx "$n-proxy"; then
    # Once, from the HAProxy layout (25 Oct 2026): the proxy held :8766; blue moves to the host network.
    docker rm -f "$n-proxy" "$n-green" >/dev/null 2>&1 || true
fi
if ! docker ps --format '{{.Names}}' | grep -qx "$n" || [ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$n")" != host ]; then
    rm -f "$state/listening-blue"
    $c up -d --remove-orphans --force-recreate knowledge  # first start, or the layout before this one
    listening blue "$n"
    echo "blue started"; exit 0
fi
rm -f "$state/listening-green"
$c --profile deploy up -d --no-deps --force-recreate knowledge-green
if ! listening green "$n-green"; then
    $c --profile deploy stop knowledge-green; echo "green not ready: blue keeps the old version" >&2; exit 1
fi
$c stop knowledge                         # green serves alone; blue's queued connections move to green
rm -f "$state/listening-blue"
$c up -d --no-deps --force-recreate knowledge
if ! listening blue "$n"; then echo "new blue not ready: green keeps serving the new version" >&2; exit 1; fi
$c --profile deploy stop knowledge-green
echo "switched with no downtime"
