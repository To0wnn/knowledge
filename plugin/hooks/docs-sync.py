#!/usr/bin/env python3
"""Mirror the Markdown of a project folder to the knowledge server, for a guest whose own folder is the source.

Opt-in per folder: only below a folder that holds a file `.knowledge-project` with the project name (e.g. "a350").
SessionStart: every .md of that folder (full mirror: the server removes mirrored files that are gone here, keeping a
copy). PostToolUse Edit|Write|MultiEdit: the one .md that changed. The token must be a write token limited to that
project. Standard library only, works on Windows; never blocks a session.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

MARKER = ".knowledge-project"
SKIP = {"venv", "node_modules", "__pycache__", "dist", "build", "site-packages"}  # plus every dot-folder
MAX_FILE, MAX_FILES = 300_000, 2000


def option(name: str, default: str = "") -> str:
    return os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name.upper()}", os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name}", default))


def project_root(start: Path):
    """(folder, project name) of the nearest folder at or above `start` that holds the marker, else (None, None)."""
    for d in [start, *start.parents]:
        marker = d / MARKER
        if marker.is_file():
            name = marker.read_text(encoding="utf-8", errors="replace").strip().splitlines()[:1]
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
    event = hook.get("hook_event_name", "")
    root, project = project_root(Path(os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or ".").resolve())
    if not root or not project:
        return
    if event == "SessionStart":
        payload = {"project": project, "files": markdown_files(root), "full": True}
    else:
        path = (hook.get("tool_input") or {}).get("file_path", "")
        p = Path(path).resolve() if path else None
        if not p or p.suffix != ".md" or root not in p.parents or not p.is_file():
            return
        payload = {"project": project, "files": {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")}}
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
            assert markdown_files(r) == {"docs/x.md": "x"}, markdown_files(r)
            assert project_root(Path(tempfile.gettempdir()).parent) == (None, None)
        print("ok")
    else:
        main()
