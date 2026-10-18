#!/bin/sh
# SessionStart: print one part of the shared startup context (CLAUDE.md files + memory index) from the knowledge
# server. Claude Code caps each hook's output at 10,000 characters, so the server splits it into parts and
# hooks.json asks for each part separately. The session's directory picks the project (homelab, anpitek, trade).
# Never blocks a session: on failure it prints one short notice.
part=${1:-1}
url=${CLAUDE_PLUGIN_OPTION_URL:-${CLAUDE_PLUGIN_OPTION_url:-https://knowledge.to0wnn.com}}
token=${CLAUDE_PLUGIN_OPTION_TOKEN:-${CLAUDE_PLUGIN_OPTION_token:-}}
code=$(curl -sS --max-time 8 -o /tmp/knowledge-context.$$ -w '%{http_code}' -H "Authorization: Bearer $token" -G --data-urlencode "project=${CLAUDE_PROJECT_DIR:-$PWD}" "$url/context/$part" 2>/dev/null)
if [ "$code" = 200 ]; then
    cat /tmp/knowledge-context.$$
elif [ "$part" = 1 ] && [ "$code" = 401 ]; then
    echo "knowledge server: token invalid or revoked (plugin setting 'token'). Shared memory and docs were not loaded."
elif [ "$part" = 1 ] && [ "$code" = 403 ]; then
    echo "knowledge server: only reachable from the home network or WireGuard. Shared memory and docs were not loaded."
elif [ "$part" = 1 ]; then
    echo "knowledge server ($url) unreachable (HTTP $code): shared memory and docs were not loaded for this session."
fi
rm -f /tmp/knowledge-context.$$
exit 0
