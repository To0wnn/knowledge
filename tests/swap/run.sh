#!/bin/sh
# Proof that server/swap.sh switches with no downtime: fake servers with the real handover (SO_REUSEPORT on the host
# network), a request every 50 ms during two deploys; every request must succeed. Needs Docker with compose; never
# collides with the real stack (own names kswap*, port 18766). Run by hand on the Docker host: sh tests/swap/run.sh
set -eu
cd "$(dirname "$0")"
mkdir -p state; cp ../../server/swap.sh .
export KNOWLEDGE_PROJECT=knowledge-swaptest KNOWLEDGE_STATE_DIR="$PWD/state" KNOWLEDGE_NAME=kswap
c="docker compose -p $KNOWLEDGE_PROJECT"
cleanup() { $c --profile deploy down -t 2 >/dev/null 2>&1 || true; rm -rf state swap.sh; }
trap cleanup EXIT
sh swap.sh                                  # first run: starts blue
( set +e; while :; do curl -s -o /dev/null -w "%{http_code}\n" --max-time 2 http://127.0.0.1:18766/x; sleep 0.05; done ) > codes.txt &  # a failed request is counted (000), never ends the loop
loop=$!
sh swap.sh; sh swap.sh                      # two zero-downtime switches
kill $loop
total=$(wc -l < codes.txt); fails=$(grep -vc '^200$' codes.txt || true); rm -f codes.txt
echo "requests $total, failed $fails"
[ "$fails" -eq 0 ] && [ "$total" -gt 100 ]
