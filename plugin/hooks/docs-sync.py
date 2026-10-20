#!/usr/bin/env python3
"""Mirror the Markdown of a project folder to the knowledge server, for a guest whose own folder is the source.

Opt-in per folder: only below a folder that holds a file `.knowledge-project` with the project name (e.g. "a350").
SessionStart: every .md of that folder (full mirror: the server removes mirrored files that are gone here, keeping a
copy). PostToolUse Edit|Write|MultiEdit: the one .md that changed. The token must be a write token limited to that
project. Standard library only, works on Windows; never blocks a session.
Also takes the hook input of Codex (apply_patch: the paths are in the patch text) and agy (camelCase, toolCall.args);
outside the Claude plugin the URL and token come from KNOWLEDGE_URL and KNOWLEDGE_TOKEN.
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

MARKER = ".knowledge-project"
SKIP = {"venv", "node_modules", "__pycache__", "dist", "build", "site-packages"}  # plus every dot-folder
MAX_FILE, MAX_FILES = 300_000, 2000
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


def edited_paths(hook: dict) -> list:
    """The files an edit tool changed: Claude's file_path, the paths in a Codex patch, agy's TargetFile."""
    if "toolCall" in hook:
        args = (hook.get("toolCall") or {}).get("args") or {}
        target = args.get("Path") or args.get("TargetFile") or ""  # agy CLI: write_file/edit_file Path; app/IDE: TargetFile
        return [json.loads(target) if target.startswith('"') else target] if target else []
    inp = hook.get("tool_input") or {}
    if isinstance(inp, str):  # a raw patch instead of {"command": patch}
        inp = {"command": inp}
    if inp.get("file_path"):
        return [inp["file_path"]]
    return [a or b for a, b in PATCH_FILE.findall(str(inp.get("command") or inp.get("patch") or ""))]


def option(name: str, default: str = "") -> str:
    return os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name.upper()}", os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name}")) \
        or os.environ.get(f"KNOWLEDGE_{name.upper()}", default)


def project_root(start: Path):
    """(folder, project name) of the nearest folder at or above `start` that holds the marker, else (None, None)."""
    for d in [start, *start.parents]:
        marker = d / MARKER
        if marker.is_file():
            raw = marker.read_bytes()  # PowerShell's "echo a350 > file" writes UTF-16, Set-Content -Encoding utf8 a BOM
            text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", errors="replace")
            name = text.strip().splitlines()[:1]
            return d, (name[0].strip() if name else "")
    return None, None


def markdown_files(root: Path) -> dict:
    out = {}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP)
        for n in sorted(names):
            p = Path(dirpath) / n
            if n.endswith(".md") and p.stat().st_size <= MAX_FILE:
                out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
                if len(out) >= MAX_FILES:
                    return out
    return out


def post(payload: dict) -> dict:
    url = option("url", "https://knowledge.to0wnn.com").rstrip("/") + "/sync"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {option('token')}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main():
    hook = json.load(sys.stdin)
    event = hook.get("hook_event_name") or ("PostToolUse" if "toolCall" in hook else "SessionStart")  # agy names no event
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or (hook.get("workspacePaths") or ["."])[0]
    root, project = project_root(Path(cwd).resolve())
    if not root or not project:
        return
    if event == "SessionStart":
        payload = {"project": project, "files": markdown_files(root), "full": True}
    else:
        changed = [p for p in (Path(cwd, f).resolve() for f in edited_paths(hook))  # Codex patch paths can be relative
                   if p.suffix == ".md" and root in p.parents and p.is_file()]
        if not changed:
            return
        payload = {"project": project, "files": {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                                                 for p in changed}}
    try:
        res = post(payload)
    except Exception as e:  # offline, server down, token wrong: say so once, never fail the session
        print(f"knowledge docs sync for {project} failed: {e}", file=sys.stderr if event != "SessionStart" else sys.stdout)
        return
    if res.get("refused"):
        print(f"knowledge docs sync for {project}: refused {', '.join(res['refused'][:5])}",
              file=sys.stderr if event != "SessionStart" else sys.stdout)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            (r / MARKER).write_text("a350\n")
            (r / "docs").mkdir(); (r / "docs" / "x.md").write_text("x")
            (r / ".venv").mkdir(); (r / ".venv" / "y.md").write_text("y")
            (r / "main.py").write_text("code")
            assert project_root(r / "docs") == (r, "a350")
            (r / MARKER).write_bytes("a350\r\n".encode("utf-16"))  # PowerShell 5 echo
            assert project_root(r / "docs") == (r, "a350")
            (r / MARKER).write_bytes(b"\xef\xbb\xbfa350\r\n")  # Set-Content -Encoding utf8
            assert project_root(r / "docs") == (r, "a350")
            assert markdown_files(r) == {"docs/x.md": "x"}, markdown_files(r)
            assert project_root(Path(tempfile.gettempdir()).parent) == (None, None)
        patch = "*** Begin Patch\n*** Update File: docs/a.md\n@@\n-x\n+y\n*** Add File: /p/b.md\n+z\n*** End Patch"
        assert edited_paths({"tool_input": {"command": patch}}) == ["docs/a.md", "/p/b.md"]
        assert edited_paths({"toolCall": {"args": {"TargetFile": '"/p/c.md"'}}}) == ["/p/c.md"]
        assert edited_paths({"toolCall": {"name": "edit_file", "args": {"Path": '"/p/e.md"'}}}) == ["/p/e.md"]
        assert edited_paths({"tool_input": {"file_path": "/p/d.md"}}) == ["/p/d.md"]
        print("ok")
    else:
        main()
