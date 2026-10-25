#!/bin/sh
# Zero-downtime switch of the knowledge stack, run on the Docker host by deploy.sh after the image is built.
# blue (container knowledge) normally serves behind the proxy on :8766; green only during this script:
#   1. green starts with the new image and must become ready (index + semantic index loaded)
#   2. blue is drained (the proxy stops sending to it within ~2 s), renewed with the new image, and must become ready
#   3. green is drained and stopped
# Any step that fails leaves a ready server serving and exits non-zero (deploy.sh fails loudly).
set -eu
cd "$(dirname "$0")"
c="docker compose -p ${KNOWLEDGE_PROJECT:-knowledge}"
state=${KNOWLEDGE_STATE_DIR:-/root/docker/knowledge/state}
n=${KNOWLEDGE_NAME:-knowledge}  # container names: $n (blue), $n-green, $n-proxy (tests/swap/run.sh uses its own)
ready() { docker exec "$1" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health?ready=1', timeout=3)" >/dev/null 2>&1; }
proxy_serves() {  # the proxy itself routes to a ready server (it needs two good checks after a start)
    i=0; while [ $i -lt 60 ]; do
        docker exec "$n-proxy" wget -q -O /dev/null http://127.0.0.1:8766/health?ready=1 2>/dev/null && return 0
        sleep 1; i=$((i + 1)); done
    return 1
}
wait_ready() {  # up to 10 minutes: a changed embedding model re-embeds everything
    i=0; while [ $i -lt 600 ]; do ready "$1" && return 0; sleep 1; i=$((i + 1)); done
    docker logs "$1" --tail 20; return 1
}
# Check both configs before anything stops: a typo must never take the running server down.
$c --profile deploy config -q
docker run --rm -v "$PWD/haproxy:/usr/local/etc/haproxy:ro" "$(sed -n 's/^ *image: \(haproxy:.*\)/\1/p' compose.yaml | head -1)" \
    haproxy -c -q -f /usr/local/etc/haproxy/haproxy.cfg
if ! docker ps --format '{{.Names}}' | grep -qx "$n-proxy"; then
    # First run of this layout (blue still owns :8766): one short outage, once.
    $c up -d --remove-orphans proxy knowledge
    wait_ready "$n" && proxy_serves
    echo "proxy layout started"; exit 0
fi
docker kill -s USR2 "$n-proxy" >/dev/null  # reload haproxy/haproxy.cfg (rsynced by deploy.sh), no dropped connections
rm -f "$state/drain-green" "$state/drain-blue"
$c --profile deploy up -d --no-deps --force-recreate knowledge-green
if ! wait_ready "$n-green"; then
    $c --profile deploy stop knowledge-green; echo "green not ready: blue keeps the old version" >&2; exit 1
fi
touch "$state/drain-blue"; sleep 3       # check every 1 s, fall 2: green serves from here
$c up -d --no-deps --force-recreate knowledge
rm -f "$state/drain-blue"
if ! wait_ready "$n"; then echo "new blue not ready: green keeps serving the new version" >&2; exit 1; fi
sleep 3                                  # rise 2: blue serves again
touch "$state/drain-green"; sleep 3
$c --profile deploy stop knowledge-green
rm -f "$state/drain-green"
proxy_serves
echo "switched with no downtime"
