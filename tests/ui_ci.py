#!/usr/bin/env python3
"""Run tests/ui_smoke.py against a throwaway server: the demo notes from demo/, plus seeded projects, a used token, a
pc with a companion report, a chat with events and a task line. What CI runs on every push; locally the same:

  uv run --project server --with playwright python tests/ui_ci.py
(needs Playwright's Chromium once: `uv run --with playwright python -m playwright install chromium`)

Runs as 6 shards side by side (KNOWLEDGE_SMOKE_SHARDS=n to change). Exit code is the smoke test's. KNOWLEDGE_SMOKE_SHOTS=<dir> also keeps screenshots; the server log is printed on a failure.
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
           "KNOWLEDGE_GITHUB_RAW": "http://127.0.0.1:9", "KNOWLEDGE_GITHUB_API": "http://127.0.0.1:9"}  # no newest-version lookup on GitHub from a test
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
    ok_agent = {"plugin": "0.21.1", "trusted": True}  # as a healthy pc reports: no Plugin or Hooks row repeats it
    report = {"version": "0.21.1", "maxAgents": 4, "agents": {"codex": {**ok_agent, "accounts": [{"id": "", "kind": "login", "email": "me@example.org", "plan": "plus"},
        {"id": "chatgpt", "login": "https://auth.openai.com/codex/device", "usercode": "ABCD-EFGH2"}, {"id": "openai", "kind": "apikey", "preview": "sk-proj-…kkkk"}]},
        "claude": {**ok_agent, "models": [
        {"id": "opus", "name": "Opus", "efforts": ["low", "high"], "default": "high"}, {"id": "sonnet", "name": "Sonnet", "efforts": ["low"]}],
        "commands": [{"name": "compact", "description": "Clear history, keep a summary", "hint": ""}, {"name": "verify-done", "description": "Prove a task is done", "hint": ""}],
        "accounts": [{"id": "", "email": "me@example.org", "plan": "max_5x"}, {"id": "work", "kind": "token", "windows": [[40, "5h", time.time() + 3600]]},
                     {"id": "home", "kind": "login", "login": "https://claude.com/cai/oauth/authorize?code=true"},
                     {"id": "cloud", "kind": "apikey", "spent": 2.5, "budget": 10}]},
        "agy": {**ok_agent, "accounts": [{"id": "", "kind": "login"}, {"id": "gmail", "login": "https://accounts.google.com/o/oauth2/auth?client_id=x"},
                             {"id": "aikey", "kind": "apikey", "preview": "AIzaSyAB…kkkk"}, {"id": "gwork", "kind": "login", "email": "g@example.org", "windows": [[30, "Gemini 5h", time.time() + 3600]]}]}},
        "stats": {"bucket": 600, "total": {"ram": 32000, "vram": 12000},  # 24 h of load, with the pc off for a while
                  **{k: {"min": lo, "max": hi, "avg": (lo + hi) / 2, "now": lo, "series": [None if 40 < i < 60 else lo + (hi - lo) * (i * 7 % 13) / 13 for i in range(144)],
                         "hour": [lo + (hi - lo) * (i % 10) / 10 for i in range(60)], "windows": {"1h": [lo, hi * 0.9, lo + 1], "6h": [lo, hi, lo + 2]}}
                     for k, lo, hi in (("cpu", 2, 97), ("gpu", 0, 88), ("ram", 8000, 21000), ("vram", 500, 9000))}},
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


def shards(n: int, script: str = __file__) -> int:
    """Run n copies of this script side by side, each with its own server and a share of the checks (the checks mostly
    wait, so this cuts the wall time to about the longest shard); print each one's output, fail when one fails.
    ui_smoke.py uses it too against the live server (script: itself, no server of its own)."""
    with tempfile.TemporaryDirectory() as t:
        runs = []
        for i in range(n):
            log = open(Path(t) / f"shard{i}.log", "w+")
            runs.append((subprocess.Popen([sys.executable, script], env={**os.environ, "KNOWLEDGE_SMOKE_SHARD": f"{i}/{n}"},
                                          stdout=log, stderr=subprocess.STDOUT), log))
        codes = [proc.wait() for proc, _ in runs]
        for i, (_, log) in enumerate(runs):
            log.seek(0)
            print(f"---- shard {i}/{n}: {'ok' if not codes[i] else 'FAILED'} ----\n{log.read()}", flush=True)
    return 1 if any(codes) else 0  # a shard killed by a signal has a negative code


if __name__ == "__main__":
    if not os.environ.get("KNOWLEDGE_SMOKE_SHARD"):
        sys.exit(shards(int(os.environ.get("KNOWLEDGE_SMOKE_SHARDS", "6"))))
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
