# the owner's knowledge (fallback when the session-start hook did not run)

The knowledge server (MCP server `knowledge`) holds the owner's docs, memory and working rules for homelab, anpitek and
trade. Before you answer or change something, look it up: `knowledge_search` (exact) or `knowledge_semantic_search`
(by meaning), then `knowledge_read`. The working rules are in `claude/CLAUDE.md` there; read them at the start.
Every memo and doc has `## Stand` (what is true now) and `## Keuzes` (decisions with date and reason): check
`## Keuzes` before you add, remove or undo something. Memory is a hint: check a fact that can change on the live
system before acting on it. The full house rules are in the server's MCP instructions. This token can only read;
never write a secret anywhere.
