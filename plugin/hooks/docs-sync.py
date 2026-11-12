#!/usr/bin/env python3
"""Mirror the Markdown of a project folder to the knowledge server, for a guest whose own folder is the source.

Opt-in per folder: only below a folder that holds a file `.knowledge-project` with the project name (e.g. "a350").
SessionStart and SessionEnd: every .md of that folder (full mirror: the server removes mirrored files that are gone here,
keeping a copy; at the end it also catches files changed through the shell, which PostToolUse never sees). PostToolUse Edit|Write|MultiEdit: the one .md that changed. The token must be a write token limited to that
project. Standard library only, works on Windows; never blocks a session.
Also takes the hook input of Codex (apply_patch: the paths are in the patch text) and agy (camelCase, toolCall.args);
outside the Claude plugin the URL and token come from KNOWLEDGE_URL and KNOWLEDGE_TOKEN.
A doc the server cannot place (no topic: that is a heading of the project's MEMORY.md) comes back as a hint. Claude
and Codex get it at once as PostToolUse additionalContext (plain stdout reaches neither model there); agy's
PostToolUse may only answer {}, so its hint waits in PENDING until agent_hooks.py hints (PostInvocation) injects it.
At session start the hint is a line of the startup context.
"""
import json
import token_store
from project_marker import MARKER, project_root  # the folder-to-project link, shared with the session start
import os
import re
import sys
import urllib.request
from pathlib import Path

SKIP = {"venv", "node_modules", "__pycache__", "dist", "build", "site-packages"}  # plus every dot-folder
MAX_FILE, MAX_FILES = 300_000, 2000
KN_HOME = Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge")
PENDING = KN_HOME / "pending-hints.txt"  # agy: shown by agent_hooks.py hints at the next turn
PENDING_SYNC = KN_HOME / "pending-sync.json"  # files that failed to sync, retried on next edit
NOSYNC_FILE = KN_HOME / "nosync-projects.json"  # projects/devices that don't mirror docs (admin or builtin)
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)


def is_nosync(project: str) -> bool:
    try:
        return project in json.loads(NOSYNC_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False


def mark_nosync(project: str) -> None:
    try:
        NOSYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
        known = set(json.loads(NOSYNC_FILE.read_text(encoding="utf-8"))) if NOSYNC_FILE.exists() else set()
        known.add(project)
        NOSYNC_FILE.write_text(json.dumps(sorted(known)), encoding="utf-8")
    except Exception:
        pass


def load_pending(project: str) -> dict:
    try:
        if PENDING_SYNC.exists():
            return json.loads(PENDING_SYNC.read_text(encoding="utf-8")).get(project) or {}
    except Exception:
        pass
    return {}


def save_pending(project: str, files: dict) -> None:
    if not files:
        return
    try:
        PENDING_SYNC.parent.mkdir(parents=True, exist_ok=True)
        cur = json.loads(PENDING_SYNC.read_text(encoding="utf-8")) if PENDING_SYNC.exists() else {}
        cur.setdefault(project, {}).update(files)
        PENDING_SYNC.write_text(json.dumps(cur), encoding="utf-8")
    except Exception:
        pass


def clear_pending(project: str, synced_keys: list = None) -> None:
    try:
        if not PENDING_SYNC.exists():
            return
        cur = json.loads(PENDING_SYNC.read_text(encoding="utf-8"))
        if project in cur:
            if synced_keys is None:
                del cur[project]
            else:
                for k in synced_keys:
                    cur[project].pop(k, None)
                if not cur[project]:
                    del cur[project]
            if cur:
                PENDING_SYNC.write_text(json.dumps(cur), encoding="utf-8")
            else:
                PENDING_SYNC.unlink(missing_ok=True)
    except Exception:
        pass


def report(event: str, hook: dict, text: str) -> None:
    """Report a message or error loudly to the agent/user: startup context, agy pending hint, or PostToolUse additionalContext."""
    if event == "SessionEnd":
        return
    if event == "SessionStart":
        print(text)
    elif "toolCall" in hook:  # agy: its PostToolUse output must be {}; the next PostInvocation shows it
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        with PENDING.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
        print("{}")
    else:  # Claude Code and Codex
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}))


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


def nested_repo(d: Path) -> bool:
    """A git clone or submodule inside the project (e.g. vendor/VoxCPM): someone else's docs, never mirrored."""
    return (d / ".git").exists()  # a folder in a clone, a file in a submodule or worktree


def markdown_files(root: Path) -> dict:
    out = {}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP and not nested_repo(Path(dirpath, d)))
        for n in sorted(names):
            p = Path(dirpath) / n
            if n.endswith(".md") and p.stat().st_size <= MAX_FILE:
                out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
                if len(out) >= MAX_FILES:
                    return out
    return out


def post(payload: dict) -> dict:
    url = token_store.url() + "/sync"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {token_store.token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def hint_text(res: dict) -> str:
    hints = res.get("hints") or []
    if not hints:
        return ""
    more = f" (and {res['more_hints']} more)" if res.get("more_hints") else ""
    return ("Knowledge house rules: these docs need a topic: in their frontmatter that is a heading of the project's "
            f"MEMORY.md, or the knowledge graph cannot place them: {'; '.join(hints)}{more}. Choose one of: "
            f"{' | '.join(res.get('headings') or [])}. Add at the very top '---' / 'topic: <heading>' / '---' "
            "(or the topic line in frontmatter the doc already has).")


def main():
    hook = json.load(sys.stdin)
    event = hook.get("hook_event_name") or ("PostToolUse" if "toolCall" in hook else "SessionStart")  # agy names no event
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or (hook.get("workspacePaths") or ["."])[0]
    root, project = project_root(Path(cwd).resolve())
    if not root or not project or is_nosync(project):
        if "toolCall" in hook:
            print("{}")
        return
    if event in ("SessionStart", "SessionEnd"):
        payload = {"project": project, "files": markdown_files(root), "full": True}
    else:
        changed = [p for p in (Path(cwd, f).resolve() for f in edited_paths(hook))  # Codex patch paths can be relative
                   if p.suffix == ".md" and root in p.parents and p.is_file()
                   and not any(nested_repo(q) for q in p.parents if root in q.parents)]
        file_map = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                    for p in changed}
        # Pick up any previously failed files for this project ("beter oppakken")
        for rel in list(load_pending(project)):
            p = root / rel
            if p.is_file() and rel not in file_map and p.stat().st_size <= MAX_FILE:
                file_map[rel] = p.read_text(encoding="utf-8", errors="replace")
        if not file_map:
            if "toolCall" in hook:
                print("{}")
            return
        payload = {"project": project, "files": file_map}
    try:
        res = post(payload)
    except urllib.error.HTTPError as e:
        err_data = {}
        try:
            err_data = json.loads(e.read().decode(encoding="utf-8", errors="replace"))
        except Exception:
            pass
        if e.code == 403 and (err_data.get("admin") or err_data.get("builtin") or (project in ("knowledge", "homelab", "anpitek", "trade") and "sync needs a write token" in err_data.get("error", ""))):
            mark_nosync(project)
            if "toolCall" in hook:
                print("{}")
            return
        err_detail = f": {err_data['error']}" if err_data.get("error") else ""
        err_msg = f"knowledge docs sync for {project} failed: {e}{err_detail}"
        if event != "SessionStart":
            print(err_msg, file=sys.stderr)
        if event != "SessionStart" and "files" in payload:
            save_pending(project, payload["files"])
        report(event, hook, err_msg)
        return
    except Exception as e:  # offline, server down: report loudly, queue for retry
        err_msg = f"knowledge docs sync for {project} failed: {e}"
        if event != "SessionStart":
            print(err_msg, file=sys.stderr)
        if event != "SessionStart" and "files" in payload:
            save_pending(project, payload["files"])
        report(event, hook, err_msg)
        return
    clear_pending(project, list(payload.get("files", {}).keys()) if not payload.get("full") else None)
    if res.get("refused"):
        ref_msg = f"knowledge docs sync for {project}: refused {', '.join(res['refused'][:5])}"
        if event != "SessionStart":
            print(ref_msg, file=sys.stderr)
        report(event, hook, ref_msg)
        return
    if not (text := hint_text(res)):
        if "toolCall" in hook:
            print("{}")
        return
    report(event, hook, text)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import tempfile
        assert hint_text({}) == "" and hint_text({"hints": []}) == ""
        assert hint_text({"archived": ["old/a.md"], "more_archived": 1}) == ""  # the server tells archive moves (/tasks notices)
        t = hint_text({"hints": ["a.md: no topic"], "more_hints": 2, "headings": ["Bot", "Reviews"]})
        assert "a.md: no topic" in t and "(and 2 more)" in t and "Bot | Reviews" in t, t
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
            (r / "vendor" / "lib" / ".git").mkdir(parents=True); (r / "vendor" / "lib" / "README.md").write_text("theirs")
            assert markdown_files(r) == {"docs/x.md": "x"}, markdown_files(r)  # a nested clone stays out
            assert project_root(Path(tempfile.gettempdir()).parent) == (None, None)
            # a worktree, even with its own stale marker, syncs from its main checkout: it never overwrites main's docs
            import subprocess
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(r)]
            for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "i"], ["worktree", "add", "-q", "--detach", str(r / ".wt")]):
                subprocess.run(git + args, check=True, capture_output=True)
            (r / ".wt" / MARKER).write_text("other\n")
            assert project_root(r / ".wt") == (r.resolve(), "a350"), project_root(r / ".wt")
        with tempfile.TemporaryDirectory() as d:  # no main checkout to name: unlinked, its own stale marker never syncs
            import shutil
            d = Path(d)
            main, orphan, bare = d / "main", d / "orphan", d / "bare.git"
            main.mkdir()
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
            for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "i"], ["worktree", "add", "-q", "--detach", str(orphan)]):
                subprocess.run(git + ["-C", str(main)] + args, check=True, capture_output=True)
            (orphan / MARKER).write_text("a350\n")
            subprocess.run(git + ["clone", "-q", "--bare", str(main), str(bare)], check=True, capture_output=True)
            shutil.rmtree(main)
            assert project_root(orphan) == (None, None), project_root(orphan)
            (d / MARKER).write_text("unrelated\n")  # a marker above a bare repo is not the worktree's
            subprocess.run(git + ["-C", str(bare), "worktree", "add", "-q", "--detach", str(d / "bw")], check=True, capture_output=True)
            assert project_root(d / "bw") == (None, None), project_root(d / "bw")
        patch = "*** Begin Patch\n*** Update File: docs/a.md\n@@\n-x\n+y\n*** Add File: /p/b.md\n+z\n*** End Patch"
        assert edited_paths({"tool_input": {"command": patch}}) == ["docs/a.md", "/p/b.md"]
        assert edited_paths({"toolCall": {"args": {"TargetFile": '"/p/c.md"'}}}) == ["/p/c.md"]
        assert edited_paths({"toolCall": {"name": "edit_file", "args": {"Path": '"/p/e.md"'}}}) == ["/p/e.md"]
        assert edited_paths({"tool_input": {"file_path": "/p/d.md"}}) == ["/p/d.md"]
        # Pending sync and nosync tracking:
        with tempfile.TemporaryDirectory() as td:
            old_kh = globals()["KN_HOME"]
            old_p = globals()["PENDING"]
            old_ps = globals()["PENDING_SYNC"]
            old_ns = globals()["NOSYNC_FILE"]
            try:
                globals()["KN_HOME"] = Path(td)
                globals()["PENDING"] = Path(td) / "pending-hints.txt"
                globals()["PENDING_SYNC"] = Path(td) / "pending-sync.json"
                globals()["NOSYNC_FILE"] = Path(td) / "nosync-projects.json"
                assert not is_nosync("prj")
                mark_nosync("prj")
                assert is_nosync("prj")
                assert load_pending("prj") == {}
                save_pending("prj", {"a.md": "content a"})
                assert load_pending("prj") == {"a.md": "content a"}
                save_pending("prj", {"b.md": "content b"})
                assert load_pending("prj") == {"a.md": "content a", "b.md": "content b"}
                clear_pending("prj", ["a.md"])
                assert load_pending("prj") == {"b.md": "content b"}
                clear_pending("prj")
                assert load_pending("prj") == {}
            finally:
                globals()["KN_HOME"] = old_kh
                globals()["PENDING"] = old_p
                globals()["PENDING_SYNC"] = old_ps
                globals()["NOSYNC_FILE"] = old_ns
        print("ok")
    else:
        main()
