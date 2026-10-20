#!/usr/bin/env python3
"""PostToolUse hook (Edit|Write|MultiEdit) for sessions on the VM, which write memos directly instead of through the
knowledge server: after a memo or MEMORY.md changes, run the knowledge lint on that memory root and tell Claude what
concerns this file; for a new memo, also list existing memos with a similar meaning (the server's duplicate hint).

Advisory only: always exits 0, prints {"hookSpecificOutput": {"additionalContext": ...}} when there is something to
say, and stays silent (and fast) for every other file. Standard library only; never breaks a session.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PROJECTS = Path.home() / ".claude/projects"
LINT_DIR = Path(__file__).resolve().parents[2] / "server"  # ~/knowledge/server/lint.py
SERVER = "https://knowledge.to0wnn.com"
TOKEN = Path.home() / ".config/knowledge-server/report.token"  # read-only device token
# Claude Code memory dir -> knowledge server root and project (the semantic search filters on project)
ROOTS = {"-home-to0wnn-homelab": ("memory", "homelab"), "-home-to0wnn-anpitek": ("anpitek-memory", "anpitek"),
         "-home-to0wnn-trade-bot": ("trade-memory", "trade")}
SIMILAR = 0.72  # same threshold as the server (calibrated 18 Oct 2026)


def memo_of(path: str):
    """(memory dir, file name) when path is a memo or index directly in a Claude Code memory dir, else None."""
    p = Path(path)
    if p.suffix != ".md" or p.parent.name != "memory" or p.parent.parent.parent != PROJECTS:
        return None
    return p.parent, p.name


def lint_notes(mem_dir: Path, name: str) -> list[str]:
    sys.path.insert(0, str(LINT_DIR))
    import lint  # the same rules as the server and the weekly report
    files = {f"memory/{f.name}": f.read_text(errors="replace") for f in mem_dir.glob("*.md")}
    for i, other in enumerate(d for d in PROJECTS.glob("*/memory") if d != mem_dir):  # [[links]] across projects are valid
        files.update({f"other{i}-memory/{f.name}": f.read_text(errors="replace") for f in other.glob("*.md")})
    report = lint.lint(files, mem="memory")
    index_checks = {"index_line_without_heading", "index_near_load_limit", "index_points_to_missing_file"}
    notes = []
    for check, items in report.items():
        for item in items:
            mine = item.startswith(name + ":") or item.startswith(name + " ") or item == name \
                or f"→ {name}" in item or (name == "MEMORY.md" and check in index_checks)
            if mine and check not in ("unprocessed_source", "orphan_memo"):
                notes.append(f"{check}: {item}")
    return notes


def similar_notes(mem_dir: Path, name: str, text: str) -> list[str]:
    root, project = ROOTS.get(mem_dir.parent.name, (None, None))
    if not root or name == "MEMORY.md" or not TOKEN.exists():
        return []
    body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)[:1500]
    url = f"{SERVER}/semantic?" + urllib.parse.urlencode({"q": body, "project": project})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN.read_text().strip()}"})
    with urllib.request.urlopen(req, timeout=6) as r:
        hits = json.load(r).get("hits", [])
    seen, out = set(), []
    for h in hits:
        p = h["path"]
        if p.startswith(root + "/") and p.count("/") == 1 and p != f"{root}/{name}" and not p.endswith("/MEMORY.md") \
                and h["score"] >= SIMILAR and p not in seen:
            seen.add(p)
            out.append(f"{p} ({h['score']:.2f})")
    return out[:3]


def main() -> None:
    hook = json.load(sys.stdin)
    path = (hook.get("tool_input") or {}).get("file_path", "")
    found = memo_of(path)
    if not found:
        return
    mem_dir, name = found
    notes = lint_notes(mem_dir, name)
    created = (hook.get("tool_response") or {}).get("type") == "create"
    if created:
        try:
            similar = similar_notes(mem_dir, name, Path(path).read_text(errors="replace"))
        except (OSError, ValueError) as e:  # server unreachable: still report the lint notes
            similar = []
            notes.append(f"duplicate check unavailable ({type(e).__name__})")
        if similar:
            notes.append("Similar existing memos (merge into one of these if it covers the same subject, then delete "
                         "this one): " + ", ".join(similar))
    if notes:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
              "Knowledge house rules, about the memo you just wrote:\n- " + "\n- ".join(notes)}}))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert memo_of(str(PROJECTS / "-home-to0wnn-homelab/memory/x.md")) == (PROJECTS / "-home-to0wnn-homelab/memory", "x.md")
        assert memo_of(str(PROJECTS / "-home-to0wnn-homelab/memory/archief/x.md")) is None
        assert memo_of("/home/to0wnn/homelab/diensten.md") is None
        print("ok")
        sys.exit(0)
    try:
        main()
    except Exception as e:  # advisory: a failing check must never break or block a session
        print(f"memory-check skipped: {e}", file=sys.stderr)
    sys.exit(0)
