---
name: fan-out-verify
description: Do a broad job that must be trustworthy (an audit, a review of many files or memos, checking facts against live systems) by splitting it over subagents, having every finding refuted by an independent verifier and reporting only what survives. Use when one pass over many items would not fit one context or when findings must hold up.
effort: high
---

# Fan out, verify, report

1. **Worth it?** Several agents cost many times the tokens of one session. Use this only for many independent items or
   findings that must hold up; otherwise work through them in one session. Tell the user the size first (items,
   agents); at most 10 agents unless the user says otherwise.
2. **Split** into items (files, memos, endpoints, hosts). Each worker gets a fixed brief: the goal, its items, what it
   may read, what it must not do (read-only unless the user asked for changes) and the output: one finding per entry
   with where, what and the evidence.
3. **Verify** every finding with a fresh agent that did not make it, read-only, told to refute it against the source or
   the live system: "confirmed" only with evidence, else "rejected" or "unsure".
4. **Report** only confirmed findings, most severe first, each with its evidence; give the number rejected and say what
   was not covered.
5. **Per agent**, same steps and same report: Claude Code with parallel subagents, or the Workflow tool when the user
   asked for a workflow; Codex by asking for subagents (fixed roles in `~/.codex/agents/`); agy with `invoke_subagent`
   (several at once).
6. **Changes** come after the report, one at a time (skill plan-change), never inside the fan-out.
