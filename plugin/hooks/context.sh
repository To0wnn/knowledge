#!/bin/sh
# SessionStart: print one part of the shared startup context (CLAUDE.md files + memory index) from the knowledge
# server. Claude Code caps each hook's output at 10,000 characters, so the server splits it into parts and
# hooks.json asks for each part separately. The folder's .knowledge-project picks the project, else its directory.
# Never blocks a session: on failure it prints one short notice, or the offline context when this device has a copy.
part=${1:-1}
url=${CLAUDE_PLUGIN_OPTION_URL:-${CLAUDE_PLUGIN_OPTION_url:-https://knowledge.to0wnn.com}}
token=$(sh "$(dirname "$0")/py.sh" "$(dirname "$0")/token_store.py" token)
[ -n "$token" ] || token=${CLAUDE_PLUGIN_OPTION_TOKEN:-${CLAUDE_PLUGIN_OPTION_token:-}}  # no Python: plugin setting
# The header goes in over stdin (-H @-), so the token is not on the command line (ps).
# A folder linked with .knowledge-project (knowledge-init skill) asks for that project; else the directory decides.
linked=$(sh "$(dirname "$0")/py.sh" "$(dirname "$0")/project_marker.py" "${CLAUDE_PROJECT_DIR:-$PWD}")
[ "$part" = 1 ] && sh "$(dirname "$0")/py.sh" "$(dirname "$0")/project_marker.py" notice "${CLAUDE_PROJECT_DIR:-$PWD}"
if [ -n "$linked" ]; then set -- --data-urlencode "project=$linked" --data-urlencode "linked=1"
else set -- --data-urlencode "project=${CLAUDE_PROJECT_DIR:-$PWD}"; fi
code=$(printf 'Authorization: Bearer %s\n' "$token" | curl -sS --max-time 8 -o /tmp/knowledge-context.$$ -w '%{http_code}' -H @- -G "$@" "$url/context/$part" 2>/dev/null)
# Offline: the context of the last online session plus the offline copy of the memory (memory_mirror.py).
cache=${KNOWLEDGE_MIRROR:-$HOME/.knowledge/mirror}
key=$(printf %s "${CLAUDE_PROJECT_DIR:-$PWD}" | cksum | cut -d' ' -f1)
if [ "$code" = 200 ]; then
    cat /tmp/knowledge-context.$$
    mkdir -p "$cache" && cp /tmp/knowledge-context.$$ "$cache/.context-$key-$part" 2>/dev/null
    [ "$part" = 1 ] && [ -s "$cache/last-sync.log" ] && { echo; sh "$(dirname "$0")/py.sh" "$(dirname "$0")/memory_mirror.py" notice; echo; }
elif [ "$part" = 1 ] && [ "$code" = 401 ]; then
    echo "knowledge server: token invalid or revoked (keyring or plugin setting 'token'). Shared memory and docs were not loaded."
elif [ "$part" = 1 ] && [ "$code" = 403 ]; then
    echo "knowledge server: only reachable from the home network or WireGuard. Shared memory and docs were not loaded."
elif [ -f "$cache/.state.json" ] && { [ "$code" = 000 ] || [ "${code#5}" != "$code" ]; }; then  # down, not refused
    sh "$(dirname "$0")/py.sh" "$(dirname "$0")/memory_mirror.py" offline "$key" "$part"
elif [ "$part" = 1 ]; then
    echo "knowledge server ($url) unreachable (HTTP $code): shared memory and docs were not loaded for this session."
fi
rm -f /tmp/knowledge-context.$$
exit 0
