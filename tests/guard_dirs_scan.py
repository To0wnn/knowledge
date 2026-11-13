"""Which folders in the agents' homes (~/.claude, ~/.codex, ~/.gemini, ~/.knowledge) agents write with their edit tools,
counted from the Claude and Codex session logs on this pc: the measurement behind secret-guard.py AGENT_DIRS.
Run: python3 -I tests/guard_dirs_scan.py (read only; prints counts and one example per folder)."""
import json, os, re, sys, collections
from pathlib import Path
home = str(Path.home())
homes = [home + "/.claude/", home + "/.codex/", home + "/.gemini/", home + "/.knowledge/"]
hits = collections.Counter(); ex = {}
def note(p, src):
    if not isinstance(p, str): return
    if p.startswith("~/"): p = home + p[1:]
    for h in homes:
        if p.startswith(h):
            parts = p[len(home)+1:].split("/")
            key = "/".join(parts[:3] if parts[0] == ".gemini" else parts[:2])
            hits[key] += 1; ex.setdefault(key, (p, src))
for f in Path(home, ".claude/projects").rglob("*.jsonl"):
    try:
        for line in open(f, encoding="utf-8", errors="replace"):
            if '"tool_use"' not in line: continue
            try: d = json.loads(line)
            except ValueError: continue
            for c in (d.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    i = c.get("input") or {}
                    note(i.get("file_path") or i.get("notebook_path"), "claude")
    except OSError: pass
for f in Path(home, ".codex/sessions").rglob("*.jsonl"):
    try:
        for line in open(f, encoding="utf-8", errors="replace"):
            if "apply_patch" not in line and "Begin Patch" not in line: continue
            for m in re.finditer(r"\*\*\* (?:Add|Update|Delete) File: ([^\\\n\"]+)", line):
                note(m.group(1).strip(), "codex")
    except OSError: pass
for k, n in hits.most_common(40):
    print(f"{n:5d}  {k:45s} {ex[k][1]}: {ex[k][0][:110]}")
