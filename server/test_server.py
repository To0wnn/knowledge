"""End-to-end self-check: starts server.py on temp dirs and talks raw MCP JSON-RPC over HTTP to it.
Run: uv run test_server.py"""
import gzip
import hashlib
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
    (mem / "fact.md").write_text("line one\nline two\n")
    (lab / "00_OVERZICHT.md").write_text("overview\n")
    (lab / "secret.env").write_text("nope")
    (cla / "CLAUDE.md").write_text("global rules\n")
    (cla / "skills/x/SKILL.md").write_text("skill\n")
    tokens = tmp / "tokens.json"
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()
    tokens.write_text(json.dumps({"laptop": {"sha256": h("rw-token"), "write": True},
                                  "workpc": {"sha256": h("ro-token"), "write": False},
                                  "watcher": {"sha256": h("watch-token"), "watch": True}}))
    env = {**os.environ, "KENNIS_MEMORY": str(mem), "KENNIS_HOMELAB": str(lab), "KENNIS_CLAUDE": str(cla),
           "KENNIS_TOKENS": str(tokens), "KENNIS_AUDIT": str(tmp / "audit.log"), "KENNIS_PORT": str(PORT),
           "KENNIS_BIND": "127.0.0.1", "KENNIS_RESCAN": "3600", "KENNIS_ALLOWED_HOSTS": f"127.0.0.1:{PORT}"}
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

        err, text = call("kennis_read", {"path": "memory/fact.md"})
        assert not err and "line two" in text, text
        ver = text.split("version ")[1].split(",")[0]
        assert not call("kennis_edit", {"path": "memory/fact.md", "old_string": "line one", "new_string": "line 1"})[0]
        err, text = call("kennis_write", {"path": "memory/fact.md", "content": "overwritten\n", "base_version": ver})
        assert err and "changed since you read it" in text, text  # stale version refused
        assert "line 1" in (mem / "fact.md").read_text()
        assert not call("kennis_write", {"path": "memory/new.md", "content": "new\n"})[0]

        for bad in ("memory/../../etc/passwd", "homelab/secret.env", "claude/settings.json", "etc/x"):
            assert call("kennis_read", {"path": bad})[0], bad
        assert call("kennis_write", {"path": "homelab/sub/x.md", "content": "x"})[0]  # not a writable pattern
        err, text = call("kennis_edit", {"path": "memory/fact.md", "old_string": "line", "new_string": "l"}, token="ro-token")
        assert err and "read-only" in text, text
        assert not call("kennis_read", {"path": "claude/skills/x/SKILL.md"}, token="ro-token")[0]
        assert "memory/fact.md" in call("kennis_search", {"pattern": "line 1"})[1]
        assert not call("kennis_edit", {"path": "memory/fact.md", "old_string": "line 1", "new_string": "line cache"})[0]
        assert "memory/fact.md" in call("kennis_search", {"pattern": "line cache"})[1]  # text cache must not go stale

        big = urllib.request.Request(BASE + "/mcp", method="POST", data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "kennis_read", "arguments": {"path": "memory/MEMORY.md"}}}).encode())
        for k, v in (("Content-Type", "application/json"), ("Accept", "application/json, text/event-stream"),
                     ("Accept-Encoding", "gzip"), ("Authorization", "Bearer rw-token")):
            big.add_header(k, v)
        with urllib.request.urlopen(big, timeout=10) as r:
            assert r.headers.get("Content-Encoding") == "gzip", r.headers  # compressed by the server itself
            assert b"Item 119" in gzip.decompress(r.read())

        # Case-insensitive fast path, uppercase-escape fallback, and a file whose lowercase form changes length.
        (mem / "case.md").write_text("Mixed CASE Velux\nİstanbul Velux\n")
        http("POST", "/invalidate", {"paths": ["memory/case.md"]}, token="watch-token")
        assert "memory/case.md:1" in call("kennis_search", {"pattern": "mixed case velux"})[1]
        assert "memory/case.md:1" in call("kennis_search", {"pattern": r"\S+ CASE"})[1]  # \S must stay non-space
        assert "memory/case.md:2" in call("kennis_search", {"pattern": "istanbul|İstanbul"})[1]
        # Index: every matching line, not only the first per file.
        hits = call("kennis_search", {"pattern": "^line", "root": "memory"})[1].splitlines()
        assert len([h for h in hits if h.startswith("memory/fact.md:")]) == 2, hits
        # A native edit on the VM is invisible until the watcher reports it; then it is visible at once.
        (mem / "fact.md").write_text("line one\nnative edit\n")
        assert "native edit" not in call("kennis_search", {"pattern": "native edit"})[1]
        assert http("POST", "/invalidate", {"paths": ["memory/fact.md"]}, token="rw-token")[0] == 403
        assert http("POST", "/invalidate", {"paths": ["memory/fact.md"]}, token="watch-token")[0] == 200
        assert "memory/fact.md:2" in call("kennis_search", {"pattern": "native edit"})[1]
        (mem / "fact.md").unlink()
        http("POST", "/invalidate", {"paths": ["memory/fact.md"]}, token="watch-token")
        assert "memory/fact.md" not in call("kennis_list", {"root": "memory"})[1]

        parts, n = [], 1
        while True:
            st, body = http("GET", f"/context/{n}")
            if not body:
                break
            assert len(body) <= 10_000, (n, len(body))
            parts.append(body)
            n += 1
        assert len(parts) >= 2 and "Item 119" in parts[-1] and "global rules" in parts[0], len(parts)
        assert "laptop edit memory/fact.md" in (tmp / "audit.log").read_text()
        print(f"ok ({len(parts)} context parts)")
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
