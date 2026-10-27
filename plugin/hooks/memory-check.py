#!/usr/bin/env python3
"""PostToolUse hook (Edit|Write|MultiEdit) for sessions on the VM, which write memos directly instead of through the
knowledge server: after a memo or MEMORY.md changes, run the knowledge lint on that memory root and tell Claude what
concerns this file; for a new memo, also list existing memos with a similar meaning (the server's duplicate hint).

Advisory only: always exits 0, prints {"hookSpecificOutput": {"additionalContext": ...}} when there is something to
say, and stays silent (and fast) for every other file. Standard library only; never breaks a session.

A project doc (Markdown in ~/homelab top level, ~/anpitek or ~/trade_bot) whose frontmatter has no topic: that is a
heading of its project's MEMORY.md gets a hint with the headings to choose from: the server places docs on that island
and its lint lists docs without one (36 trade docs piled up unseen before 25 Oct 2026). A hint, never a block.

With --pre (PreToolUse Write): a NEW memo that misses its frontmatter or ## Stand / ## Keuzes (house rule 2) is
refused before it is written, like the server refuses it; existing memos and all other files pass.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

os.environ.setdefault("KNOWLEDGE_LANGUAGE", "nl")  # this hook runs only on the original installation's VM (Dutch memos)

PROJECTS = Path.home() / ".claude/projects"
LINT_DIR = Path(__file__).resolve().parents[2] / "server"  # ~/knowledge/server/lint.py
SERVER = "https://knowledge.to0wnn.com"
TOKEN = Path.home() / ".config/knowledge-server/report.token"  # read-only device token
# Claude Code memory dir -> knowledge server root and project (the semantic search filters on project)
CC = lambda folder: re.sub(r"[^A-Za-z0-9]", "-", str(Path.home() / folder))  # Claude Code's memory folder name
ROOTS = {CC("homelab"): ("memory", "homelab"), CC("anpitek"): ("anpitek-memory", "anpitek"), CC("trade_bot"): ("trade-memory", "trade")}
# Project folder -> its Claude Code memory dir; homelab docs are only the top-level .md files (lint.py, NOT_DOCS)
DOCS = {Path.home() / "homelab": CC("homelab"), Path.home() / "anpitek": CC("anpitek"), Path.home() / "trade_bot": CC("trade_bot")}
NOT_PLACED = {"CLAUDE.md", "AGENTS.md", "GEMINI.md"}  # instructions, not docs (server.py lint_all)
SKIP_DIRS = {"archief", "archive", "old", "history", "node_modules", "venv", "site-packages"}  # server ARCHIVE_DIRS + code
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
    body = re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", text, flags=re.S)[:1500]
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


def pre_check(hook: dict) -> str:
    """Why this Write is refused ("" = allowed): a new memo that misses what house rule 2 asks."""
    inp = hook.get("tool_input") or {}
    found = memo_of(inp.get("file_path", ""))
    if not found or found[1] == "MEMORY.md" or Path(inp["file_path"]).exists():
        return ""
    sys.path.insert(0, str(LINT_DIR))
    import lint
    lacks = lint.new_memo_lacks(inp.get("content", ""))
    return (f"A new memo needs {', '.join(lacks)} (house rule 2): frontmatter name, description, metadata.type, then "
            "## Stand and ## Keuzes (feedback/user memos: frontmatter only). Write it with those, or create it with the "
            "MCP tool knowledge_new_memo, which also adds the index line.") if lacks else ""


def doc_of(path: str) -> Path | None:
    """The memory dir of the project whose doc this is, or None (not a doc the server places on a topic)."""
    p = Path(path)
    if p.suffix != ".md" or p.name in NOT_PLACED:
        return None
    for root, cc in DOCS.items():
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            continue
        if root.name == "homelab" and (len(parts) != 1 or p.name == "README.md"):
            return None
        if any(x in SKIP_DIRS or x.startswith((".", "archief-", "archive-")) for x in parts[:-1]):
            return None
        return PROJECTS / cc / "memory"
    return None


def topic_note(path: str, mem_dir: Path) -> str:
    """A hint when the doc's topic: is missing or no heading of its project's MEMORY.md; "" when it is fine."""
    text = Path(path).read_text(errors="replace")
    m = re.match(r"\A---\r?\n(.*?)\r?\n---", text, re.S)
    topic = (re.search(r"^topic:\s*(.+?)\s*$", m.group(1), re.M).group(1).strip('"') if m and re.search(r"^topic:", m.group(1), re.M) else "")
    headings = re.findall(r"^## (.+?)\s*$", (mem_dir / "MEMORY.md").read_text(errors="replace"), re.M) if (mem_dir / "MEMORY.md").exists() else []
    if not headings or topic in headings:
        return ""
    what = f"Its topic {topic!r} is no heading" if topic else "It has no topic: line, which must be a heading"
    return (f"{what} of the project's MEMORY.md, so the knowledge graph cannot place this doc. Add at the very top: "
            f"'---' / 'topic: <one of: {' | '.join(headings)}>' / '---' (or add the topic line to frontmatter it already has).")


def replaced_note(hook: dict, path: str, created: bool) -> str:
    """The server's hint for a newly added replacing decision. The old text is rebuilt from the Edit's own strings.
    shortcut: an overwrite with Write has no old text here and stays quiet; upgrade if Write gets used for edits."""
    sys.path.insert(0, str(LINT_DIR))
    import lint
    ti = hook.get("tool_input") or {}
    try:
        new = Path(path).read_text(errors="replace")
    except OSError:
        return ""
    if "new_string" in ti:
        old = new.replace(ti["new_string"], ti.get("old_string", ""), -1 if ti.get("replace_all") else 1)
    elif created:
        old = ""
    else:
        return ""
    return lint.replaced_decision(old, new).strip()


def main() -> None:
    hook = json.load(sys.stdin)
    path = (hook.get("tool_input") or {}).get("file_path", "")
    if (mem_dir := doc_of(path)) is not None:
        if (note := topic_note(path, mem_dir)):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
                  "Knowledge house rules, about the doc you just wrote:\n- " + note}}))
        return
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
    if (note := replaced_note(hook, path, created)):
        notes.append(note)
    if notes:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
              "Knowledge house rules, about the memo you just wrote:\n- " + "\n- ".join(notes)}}))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert memo_of(str(PROJECTS / CC("homelab") / "memory/x.md")) == (PROJECTS / CC("homelab") / "memory", "x.md")
        assert memo_of(str(PROJECTS / CC("homelab") / "memory/archief/x.md")) is None
        assert memo_of(str(Path.home() / "homelab/diensten.md")) is None
        new = str(PROJECTS / CC("homelab") / "memory/zz_selftest_new.md")
        assert "## Stand" in pre_check({"tool_input": {"file_path": new, "content": "plain"}})
        good = "---\nname: z\ndescription: d\nmetadata:\n  type: project\n---\n## Stand\nx\n## Keuzes\n"
        assert pre_check({"tool_input": {"file_path": new, "content": good}}) == ""
        assert pre_check({"tool_input": {"file_path": str(PROJECTS / CC("homelab") / "memory/MEMORY.md"), "content": "x"}}) == ""
        assert pre_check({"tool_input": {"file_path": str(Path.home() / "homelab/diensten.md"), "content": "x"}}) == ""
        tb = Path.home() / "trade_bot"
        assert doc_of(str(tb / "OPUS_REVIEW_X.md")) == PROJECTS / CC("trade_bot") / "memory"
        for not_doc in (tb / "old/README.md", tb / "venv/lib/x.md", tb / "CLAUDE.md", tb / "bot.py", tb / ".github/x.md",
                        Path.home() / "homelab/README.md", Path.home() / "homelab/ansible/x.md", Path.home() / "other/x.md"):
            assert doc_of(str(not_doc)) is None, not_doc
        assert doc_of(str(Path.home() / "homelab/diensten.md")) == PROJECTS / CC("homelab") / "memory"
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "MEMORY.md").write_text("# I\n## Bot en strategie\n## Reviews en adviezen\n")
            d = Path(t) / "d.md"
            for text, ok in (("# no front\n", False), ("---\ntopic: Nope\n---\n", False), ("---\ntopic: Bot en strategie\n---\nx", True),
                             ('---\ntitle: x\ntopic: "Reviews en adviezen"\n---\n', True)):
                d.write_text(text)
                assert (topic_note(str(d), Path(t)) == "") == ok, (text, topic_note(str(d), Path(t)))
            d.write_text("# x\n")
            assert "Bot en strategie | Reviews en adviezen" in topic_note(str(d), Path(t))
            f = Path(t) / "r.md"
            f.write_text("---\nname: r\n---\n## Stand\n## Keuzes\n- **B** (2 okt): y. Vervangt A.\n")
            edit = {"tool_input": {"old_string": "## Keuzes\n", "new_string": "## Keuzes\n- **B** (2 okt): y. Vervangt A.\n"}}
            assert "replaces an earlier one" in replaced_note(edit, str(f), False)
            assert replaced_note({"tool_input": {"old_string": "x", "new_string": "y."}}, str(f), False) == ""
            assert replaced_note({"tool_input": {"content": "z"}}, str(f), False) == ""
        print("ok")
        sys.exit(0)
    try:
        if "--pre" in sys.argv:
            if why := pre_check(json.load(sys.stdin)):
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                         "permissionDecisionReason": why}}))
        else:
            main()
    except Exception as e:  # a failing check must never break or block a session
        print(f"memory-check skipped: {e}", file=sys.stderr)
    sys.exit(0)
