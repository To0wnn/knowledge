#!/usr/bin/env python3
"""Run tests/ui_smoke.py against a throwaway server: the demo notes from demo/, plus seeded projects, a used token, a
pc with a companion report, a chat with events and a task line. What CI runs on every push; locally the same:

  uv run --project server --with playwright python tests/ui_ci.py
(needs Playwright's Chromium once: `uv run --with playwright python -m playwright install chromium`)

Exit code is the smoke test's. KNOWLEDGE_SMOKE_SHOTS=<dir> also keeps screenshots; the server log is printed on a failure.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SECRET, USER, MID = "smoke-secret", "smoke", "c" * 16
with socket.socket() as _s:  # shortcut: the port could be taken before the server binds it; rerun if that ever bites
    _s.bind(("127.0.0.1", 0))
    PORT = _s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"
ADMIN = {"X-Knowledge-Proxy": SECRET, "X-authentik-username": USER, "X-Knowledge-UI": "1"}


def http(method: str, path: str, body=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def ok(res, what: str) -> str:
    assert res[0] == 200, f"seeding {what}: {res}"
    return res[1]


def start(tmp: Path) -> subprocess.Popen:
    data = tmp / "data"
    shutil.copytree(REPO / "demo" / "memory", data / "memory")
    shutil.copytree(REPO / "demo" / "homelab", data / "homelab")
    (data / "claude").mkdir()
    (data / "claude" / "CLAUDE.md").write_text("shared rules\n")
    (tmp / "tokens.json").write_text("{}")
    (tmp / "secrets.json").write_text("[]")
    env = {**os.environ, "KNOWLEDGE_MEMORY": str(data / "memory"), "KNOWLEDGE_HOMELAB": str(data / "homelab"),
           "KNOWLEDGE_CLAUDE": str(data / "claude"), "KNOWLEDGE_TOKENS": str(tmp / "tokens.json"), "KNOWLEDGE_ADMIN_DIR": str(tmp),
           "KNOWLEDGE_PROJECTS_DIR": str(tmp / "projects"), "KNOWLEDGE_PROXY_SECRET": SECRET, "KNOWLEDGE_ADMINS": USER,
           "KNOWLEDGE_AUDIT": str(tmp / "audit.log"), "KNOWLEDGE_PORT": str(PORT), "KNOWLEDGE_BIND": "127.0.0.1",
           "KNOWLEDGE_ALLOWED_HOSTS": f"127.0.0.1:{PORT}", "KNOWLEDGE_EMBED_MODEL": "hash", "KNOWLEDGE_SECRETS": str(tmp / "secrets.json"),
           "KNOWLEDGE_VECTORS": str(tmp / "vectors.npz"), "KNOWLEDGE_RESCAN": "3600",
           "KNOWLEDGE_GITHUB_RAW": "http://127.0.0.1:9"}  # no newest-version lookup on GitHub from a test
    proc = subprocess.Popen([sys.executable, str(REPO / "server" / "server.py")], env=env,
                            stdout=open(tmp / "server.log", "wb"), stderr=subprocess.STDOUT)
    for _ in range(300):
        try:
            if http("GET", "/health")[0] == 200:
                return proc
        except OSError:
            time.sleep(0.2)
    proc.kill()
    sys.exit("server did not start:\n" + (tmp / "server.log").read_text(errors="replace")[-3000:])


def seed() -> None:
    for name in ("garden", "shed"):  # the admin page wants three projects, the project switch a second one
        ok(http("POST", "/ui/api/admin/project", {"name": name}, ADMIN), f"project {name}")
    tok = json.loads(ok(http("POST", "/ui/api/admin/token", {"name": "smoke-pc", "write": True, "self": True,
                                                            "roots": ["memory", "garden", "garden-memory"]}, ADMIN), "token"))["token"]
    bearer = {"Authorization": f"Bearer {tok}"}
    memo = ("---\nname: tasks\ndescription: garden tasks\nmetadata:\n  type: project\n---\n\n## Status\n\n"
            "- open until 1 jan 2099 [Codex, GD-1] from Claude, start now: water the raised beds\n\n## Decisions\n")
    body = ok(http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write",
               "arguments": {"path": "garden-memory/project_tasks.md", "content": memo}}}, bearer), "task line")
    assert '"isError":false' in body, body  # the write also marks the token as used ("ago" on the admin page)
    body = ok(http("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "knowledge_edit", "arguments": {
               "path": "memory/project_wireguard.md", "old_string": "split tunnel.", "new_string": "split tunnel.\nKeys rotate yearly."}}}, bearer), "edit")
    assert '"isError":false' in body, body  # an edit of a homelab memo: Changes shows its lines added/removed
    pc = {**bearer, "X-Knowledge-Machine": MID, "X-Knowledge-Machine-Label": "smoke-pc"}
    report = {"version": "0.21.1", "maxAgents": 4, "agents": {"codex": {}, "claude": {"models": [
        {"id": "opus", "name": "Opus", "efforts": ["low", "high"], "default": "high"}, {"id": "sonnet", "name": "Sonnet", "efforts": ["low"]}],
        "commands": [{"name": "compact", "description": "Clear history, keep a summary", "hint": ""}, {"name": "verify-done", "description": "Prove a task is done", "hint": ""}]}},
        "folders": {"linked": [{"path": "/home/x/lab", "project": "homelab"}], "candidates": []}, "projects": ["garden"],
        "tools": {"rea": {"name": "REA", "kind": "program", "installed": True, "ok": True, "version": "6.8.0"},
                  "no-ai-slop": {"name": "No AI Slop", "kind": "skill", "installed": False}}}
    ok(http("POST", "/machine/report", report, pc), "machine report")
    def keep_online():  # as a companion does: else the pc goes offline once the tests take longer than the server's limit
        while True:
            time.sleep(30)
            try:
                http("POST", "/machine/report", report, pc)
            except OSError:
                return
    threading.Thread(target=keep_online, daemon=True).start()
    cid = json.loads(ok(http("POST", "/ui/api/chats", {"machine": MID, "agent": "claude", "folder": "/home/x/lab"}, ADMIN), "chat"))["chat"]["id"]
    ok(http("POST", "/ui/api/chat/send", {"machine": MID, "id": cid, "text": "Which beds need water?"}, ADMIN), "chat message")
    ok(http("GET", "/machine/chats", headers=pc), "chat pickup")
    ok(http("POST", "/machine/chats/events", {"chat": cid, "session": "smoke-1", "done": True, "events": [
        {"kind": "tool", "text": "Bash: ls beds"}, {"kind": "text", "text": "The two south beds need water."}]}, pc), "chat events")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t:
        server = start(Path(t))
        try:
            seed()
            env = {**os.environ, "KNOWLEDGE_URL": BASE, "KNOWLEDGE_PROXY_SECRET": SECRET, "KNOWLEDGE_TEST_USER": USER,
                   "KNOWLEDGE_SMOKE_SEARCH": "wireguard", "KNOWLEDGE_SMOKE_BROWSER": "chromium", "KNOWLEDGE_SMOKE_WRITES": "1"}
            code = subprocess.run([sys.executable, str(REPO / "tests" / "ui_smoke.py")], env=env).returncode
        finally:
            server.terminate()
            server.wait()
        if code:
            print("---- server log (tail) ----\n" + (Path(t) / "server.log").read_text(errors="replace")[-5000:])
    sys.exit(code)
