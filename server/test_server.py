"""End-to-end self-check: starts server.py on temp dirs and talks raw MCP JSON-RPC over HTTP to it.
Run: uv run test_server.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 18766
BASE = f"http://127.0.0.1:{PORT}"


def http(method, path, body=None, token="rw-token"):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def call(tool, args, token="rw-token"):
    st, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": tool, "arguments": args}}, token)
    assert st == 200, (st, body)
    res = json.loads(body)["result"]
    text = "".join(c.get("text", "") for c in res["content"])
    return res.get("isError", False), text


def main():
    tmp = Path(tempfile.mkdtemp())
    mem, lab, cla = tmp / "memory", tmp / "homelab", tmp / "claude"
    for d in (mem, lab, cla / "skills/x"):
        d.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# Index\n" + "".join(f"- [Item {i}](f{i}.md) — {'x' * 120}\n" for i in range(120)))
    (mem / "feit.md").write_text("regel een\nregel twee\n")
    (lab / "00_OVERZICHT.md").write_text("overzicht\n")
    (lab / "geheim.env").write_text("nope")
    (cla / "CLAUDE.md").write_text("globale regels\n")
    (cla / "skills/x/SKILL.md").write_text("skill\n")
    tokens = tmp / "tokens.json"
    tokens.write_text(json.dumps({"laptop": {"token": "rw-token", "write": True}, "werkpc": {"token": "ro-token", "write": False}}))
    env = {**os.environ, "KENNIS_MEMORY": str(mem), "KENNIS_HOMELAB": str(lab), "KENNIS_CLAUDE": str(cla),
           "KENNIS_TOKENS": str(tokens), "KENNIS_AUDIT": str(tmp / "audit.log"), "KENNIS_PORT": str(PORT),
           "KENNIS_BIND": "127.0.0.1", "KENNIS_ALLOWED_HOSTS": f"127.0.0.1:{PORT}"}
    proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("server.py"))], env=env)
    try:
        for _ in range(50):
            try:
                if http("GET", "/health", token=None)[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        assert http("GET", "/health", token=None) == (200, "ok")
        assert http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)[0] == 401
        assert http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="wrong")[0] == 401

        st, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in json.loads(body)["result"]["tools"]}
        assert {"kennis_list", "kennis_read", "kennis_search", "kennis_edit", "kennis_write"} <= names, names

        err, text = call("kennis_read", {"path": "memory/feit.md"})
        assert not err and "regel twee" in text, text
        ver = text.split("version ")[1].split(",")[0]
        assert not call("kennis_edit", {"path": "memory/feit.md", "old_string": "regel een", "new_string": "regel 1"})[0]
        err, text = call("kennis_write", {"path": "memory/feit.md", "content": "overschreven\n", "base_version": ver})
        assert err and "changed since you read it" in text, text  # stale version refused
        assert "regel 1" in (mem / "feit.md").read_text()
        assert not call("kennis_write", {"path": "memory/nieuw.md", "content": "nieuw\n"})[0]

        for bad in ("memory/../../etc/passwd", "homelab/geheim.env", "claude/settings.json", "etc/x"):
            assert call("kennis_read", {"path": bad})[0], bad
        assert call("kennis_write", {"path": "homelab/sub/x.md", "content": "x"})[0]  # not a writable pattern
        err, text = call("kennis_edit", {"path": "memory/feit.md", "old_string": "regel", "new_string": "r"}, token="ro-token")
        assert err and "read-only" in text, text
        assert not call("kennis_read", {"path": "claude/skills/x/SKILL.md"}, token="ro-token")[0]
        assert "memory/feit.md" in call("kennis_search", {"pattern": "regel 1"})[1]
        assert not call("kennis_edit", {"path": "memory/feit.md", "old_string": "regel 1", "new_string": "regel cache"})[0]
        assert "memory/feit.md" in call("kennis_search", {"pattern": "regel cache"})[1]  # text cache must not go stale

        parts, n = [], 1
        while True:
            st, body = http("GET", f"/context/{n}")
            if not body:
                break
            assert len(body) <= 10_000, (n, len(body))
            parts.append(body)
            n += 1
        assert len(parts) >= 2 and "Item 119" in parts[-1] and "globale regels" in parts[0], len(parts)
        assert "laptop edit memory/feit.md" in (tmp / "audit.log").read_text()
        print(f"ok ({len(parts)} context parts)")
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
