---
title: House rules
topic: General
---
# House rules

The default rules of a knowledge server. Your own version goes in house_rules.md of the shared folder (KNOWLEDGE_CLAUDE,
/data/shared in the standalone setup): the server reads that file instead of this one, on every use. Section names follow KNOWLEDGE_LANGUAGE (Dutch:
"## Stand", "## Keuzes", "open tot", archief/); both languages are always accepted, so memos in any
language can keep the English names.

1. Search first (knowledge_search / knowledge_semantic_search) and update the memo that already covers the subject; no near-duplicates. When knowledge_write answers "Similar existing memos", merge. Found nothing, then learned the answer elsewhere: write it down, in the memo that fits or a new one.
2. One subject per memo; make a new one with knowledge_new_memo (it writes the frontmatter, both sections and the index line; a new memo without them is refused). Frontmatter: name, description (one line), metadata: type (project, feedback, reference or user). Body: "## Status" (what is true now: how it works, where things are, pitfalls, open items; an open item with a deadline is written "open until <date>: <what>", and once overdue it shows at session start; a task for an agent or person is written "open until <date> [<Owner>, <ID>] from <Requester>, start <now | after <ID> | <date> | after approval>: <what>", <Owner>@<device> pins it to one machine) and "## Decisions". Feedback and user memos keep their rule with Why: and How to apply:. History is not kept in the memo but in <memory root>/archive/<name>_archive.md; searches skip archive folders unless you pass archive=true.
3. A decision is one bullet under "## Decisions": - **<decision>** (<date>, <who>): <reason>; rejected: <alternative>. Before you add, remove or undo something, read the decisions of its memo. A reversed decision moves to the archive with "replaced on <date> by ..."; never delete it silently.
4. Memory is a hint, not the truth: before you act on or report a fact that can change (address, version, state), check it on the live system, and fix the memo at once when it is wrong. Mark a checked fact "(checked <date>)", a fact you carry over unchecked "(as of <date>, not re-checked)".
5. Every memo gets exactly one index line under the fitting "## " heading of <memory root>/MEMORY.md: - [Title](file.md) - hook, at most ~150 characters. The headings are the topics in the knowledge graph: add one only for a genuinely new area.
6. Absolute dates only (20 Oct 2026), never "yesterday" or "next week". A fact from outside (docs, a forum) carries its source link.
7. Do not store what the code, the git history or a doc already records: link to it. A fact that concerns several projects lives in one memo; the others link to it with [[name]].
8. Never write a password, token or key: write ‹secret:NAME› (writes containing a known secret are refused).
9. Memo text in the language the user speaks with you; code and commands stay as they are.
10. After a larger change run knowledge_lint and fix what it reports.
