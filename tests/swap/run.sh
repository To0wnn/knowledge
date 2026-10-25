#!/bin/sh
# Proof that server/swap.sh switches with no downtime: fake servers behind the real proxy config, a request every
# 50 ms during two deploys; every request must succeed. Needs Docker with compose; never collides with the real stack
# (own names kswap*, port 18766). Run by hand, e.g. on the Docker host: sh tests/swap/run.sh
set -eu
cd "$(dirname "$0")"
mkdir -p state haproxy; cp ../../server/swap.sh .
# the real haproxy.cfg, with the test container names
sed "s/ knowledge:8766/ kswap:8766/; s/ knowledge-green:8766/ kswap-green:8766/" ../../server/haproxy/haproxy.cfg > haproxy/haproxy.cfg
export KNOWLEDGE_PROJECT=knowledge-swaptest KNOWLEDGE_STATE_DIR="$PWD/state" KNOWLEDGE_NAME=kswap
c="docker compose -p $KNOWLEDGE_PROJECT"
cleanup() { $c --profile deploy down -t 1 >/dev/null 2>&1 || true; rm -rf state haproxy swap.sh; }
trap cleanup EXIT
sh swap.sh                                  # first run: starts proxy + blue
fails=0; total=0
( while :; do curl -s -o /dev/null -w "%{http_code}\n" --max-time 2 http://127.0.0.1:18766/x; sleep 0.05; done ) > codes.txt &
loop=$!
sh swap.sh; sh swap.sh                      # two zero-downtime switches
kill $loop
total=$(wc -l < codes.txt); fails=$(grep -vc '^200$' codes.txt || true); rm -f codes.txt
echo "requests $total, failed $fails"
[ "$fails" -eq 0 ] && [ "$total" -gt 100 ]
