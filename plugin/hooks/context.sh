#!/bin/sh
# SessionStart: print one part of the shared startup context (CLAUDE.md files + memory index) from the kennis
# server. Claude Code caps each hook's output at 10,000 characters, so the server splits it into parts and
# hooks.json asks for each part separately. Never blocks a session: on failure it prints one short notice.
part=${1:-1}
url=${CLAUDE_PLUGIN_OPTION_URL:-${CLAUDE_PLUGIN_OPTION_url:-https://kennis.to0wnn.com}}
token=${CLAUDE_PLUGIN_OPTION_TOKEN:-${CLAUDE_PLUGIN_OPTION_token:-}}
if out=$(curl -fsS --max-time 8 -H "Authorization: Bearer $token" "$url/context/$part" 2>/dev/null); then
    printf '%s' "$out"
elif [ "$part" = 1 ]; then
    echo "kennis-server ($url) niet bereikbaar: gedeelde memory en docs zijn deze sessie niet geladen."
fi
exit 0
