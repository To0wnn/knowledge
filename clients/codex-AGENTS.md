# Anthony's knowledge (fallback when the session-start hook did not run)

The knowledge server (MCP server `knowledge`) holds Anthony's docs, memory and working rules for homelab, anpitek and
trade. Before you answer or change something, look it up: `knowledge_search` (exact) or `knowledge_semantic_search`
(by meaning), then `knowledge_read`. The working rules are in `claude/CLAUDE.md` there; read them at the start.
Every memo and doc has `## Stand` (what is true now) and `## Keuzes` (decisions with the reason): check `## Keuzes`
before you add, remove or undo something. This token can only read; never write a secret anywhere.
