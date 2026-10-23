#!/bin/sh
# MCP headersHelper: the Authorization header from the keyring (token_store.py), else {} so the static
# "Bearer ${user_config.token}" header of older installs still applies. Always prints valid JSON.
out=$(sh "$(dirname "$0")/py.sh" "$(dirname "$0")/token_store.py" header 2>/dev/null)
if [ -n "$out" ]; then echo "$out"; else echo "{}"; fi
