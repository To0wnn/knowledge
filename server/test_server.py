"""End-to-end self-check: starts server.py on temp dirs and talks raw MCP JSON-RPC over HTTP to it.
Run: uv run test_server.py"""
import gzip
import hashlib
import http.client as httpc
import json
import re
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
    (lab / "overzicht.md").write_text("overview\n")
    (lab / "secret.env").write_text("nope")
    (cla / "CLAUDE.md").write_text("global rules\n")
    (cla / "skills/x/SKILL.md").write_text("skill\n")
    trade, tmem = tmp / "trade", tmp / "trade-memory"
    for d in (trade / "venv", tmem, tmp / "anpitek", tmp / "anpitek-memory"):
        d.mkdir(parents=True)
    (trade / "CLAUDE.md").write_text("trade bot rules\n")
    (trade / "bot.py").write_text("API_KEY = 'x'\n")
    (trade / "venv" / "README.md").write_text("vendored\n")
    (tmem / "MEMORY.md").write_text("- [Bot plan](plan.md) — entry timing\n")
    (tmem / "plan.md").write_text("---\nname: plan\n---\nentry timing plan\n")
    tokens = tmp / "tokens.json"
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()
    tokens.write_text(json.dumps({"laptop": {"sha256": h("rw-token"), "write": True},
                                  "workpc": {"sha256": h("ro-token"), "write": False},
                                  "watcher": {"sha256": h("watch-token"), "watch": True},
                                  "agent": {"sha256": h("agent-token"), "roots": ["memory", "claude"], "redact": True}}))
    (tmp / "secrets.json").write_text(json.dumps([h("Sup3rSecret99")]))
    (mem / "creds.md").write_text("login pi/Sup3rSecret99 or KEY=Sup3rSecret99, password: hunter2hunter2\nplain line\n")
    env = {**os.environ, "KNOWLEDGE_MEMORY": str(mem), "KNOWLEDGE_HOMELAB": str(lab), "KNOWLEDGE_CLAUDE": str(cla),
           "KNOWLEDGE_TRADE": str(trade), "KNOWLEDGE_TRADE_MEMORY": str(tmem), "KNOWLEDGE_ANPITEK": str(tmp / "anpitek"),
           "KNOWLEDGE_ANPITEK_MEMORY": str(tmp / "anpitek-memory"),
           "KNOWLEDGE_TOKENS": str(tokens), "KNOWLEDGE_ADMIN_DIR": str(tmp), "KNOWLEDGE_PROJECTS_DIR": str(tmp / "projects"),
           "KNOWLEDGE_PROXY_SECRET": "proxy-secret", "KNOWLEDGE_ADMINS": "anthony", "KNOWLEDGE_AUDIT": str(tmp / "audit.log"), "KNOWLEDGE_PORT": str(PORT),
           "KNOWLEDGE_BIND": "127.0.0.1", "KNOWLEDGE_RESCAN": "3600",
           "KNOWLEDGE_EMBED_MODEL": "hash", "KNOWLEDGE_SECRETS": str(tmp / "secrets.json"), "KNOWLEDGE_VECTORS": str(tmp / "vectors.npz"), "KNOWLEDGE_ALLOWED_HOSTS": f"127.0.0.1:{PORT}"}
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
        assert {"knowledge_list", "knowledge_read", "knowledge_search", "knowledge_edit", "knowledge_write"} <= names, names

        err, text = call("knowledge_read", {"path": "memory/fact.md"})
        assert not err and "line two" in text, text
        ver = text.split("version ")[1].split(",")[0]
        assert not call("knowledge_edit", {"path": "memory/fact.md", "old_string": "line one", "new_string": "line 1"})[0]
        err, text = call("knowledge_write", {"path": "memory/fact.md", "content": "overwritten\n", "base_version": ver})
        assert err and "changed since you read it" in text, text  # stale version refused
        assert "line 1" in (mem / "fact.md").read_text()
        assert not call("knowledge_write", {"path": "memory/new.md", "content": "new\n"})[0]

        for bad in ("memory/../../etc/passwd", "homelab/secret.env", "claude/settings.json", "etc/x"):
            assert call("knowledge_read", {"path": bad})[0], bad
        assert call("knowledge_write", {"path": "homelab/sub/x.md", "content": "x"})[0]  # not a writable pattern
        # Scoped agent token: only its roots, secrets masked everywhere, no web UI, no writes.
        err, text = call("knowledge_read", {"path": "memory/creds.md"}, token="agent-token")
        assert not err and "Sup3rSecret99" not in text and "hunter2" not in text and text.count("‹secret›") == 3, text
        assert "plain line" in text and "pi/‹secret›" in text, text
        assert "Sup3rSecret99" in call("knowledge_read", {"path": "memory/creds.md"})[1]  # full token unchanged
        assert "Sup3rSecret99" not in call("knowledge_search", {"pattern": "login pi"}, token="agent-token")[1]
        assert call("knowledge_read", {"path": "homelab/overzicht.md"}, token="agent-token")[0]
        assert call("knowledge_list", {"root": "homelab"}, token="agent-token")[0]
        assert "homelab/" not in call("knowledge_search", {"pattern": "overview"}, token="agent-token")[1]
        assert call("knowledge_write", {"path": "memory/x.md", "content": "x"}, token="agent-token")[0]
        assert http("GET", "/ui/api/graph", token="rw-token")[0] == 401  # tokens are for the API; the UI signs in via Authentik
        # Write guard: a known secret value in a note is refused with a hint; the placeholder is fine.
        err, text = call("knowledge_write", {"path": "memory/leak.md", "content": "db password Sup3rSecret99\n"})
        assert err and "secret set" in text and not (mem / "leak.md").exists(), text
        assert not call("knowledge_write", {"path": "memory/leak.md", "content": "db password ‹secret:DB_PASSWORD›\n"})[0]
        assert call("knowledge_edit", {"path": "memory/leak.md", "old_string": "‹secret:DB_PASSWORD›", "new_string": "Sup3rSecret99"})[0]
        # Project roots: only Markdown of the code repos, never venv; project memory is writable.
        err, text = call("knowledge_read", {"path": "trade/bot.py"})
        assert err and "only Markdown" in text, text
        assert "trade/venv" not in call("knowledge_list", {"root": "trade"})[1]
        assert "trade-memory/plan.md:4" in call("knowledge_search", {"pattern": "entry timing plan", "root": "trade,trade-memory"})[1]
        assert "trade-memory" not in call("knowledge_search", {"pattern": "entry timing plan", "root": "memory"})[1]
        assert not call("knowledge_write", {"path": "trade-memory/new.md", "content": "x\n"})[0]
        assert call("knowledge_write", {"path": "trade/bot2.py", "content": "x"})[0]
        err, text = call("knowledge_edit", {"path": "memory/fact.md", "old_string": "line", "new_string": "l"}, token="ro-token")
        assert err and "read-only" in text, text
        assert not call("knowledge_read", {"path": "claude/skills/x/SKILL.md"}, token="ro-token")[0]
        assert "memory/fact.md" in call("knowledge_search", {"pattern": "line 1"})[1]
        assert not call("knowledge_edit", {"path": "memory/fact.md", "old_string": "line 1", "new_string": "line cache"})[0]
        assert "memory/fact.md" in call("knowledge_search", {"pattern": "line cache"})[1]  # text cache must not go stale

        big = urllib.request.Request(BASE + "/mcp", method="POST", data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "knowledge_read", "arguments": {"path": "memory/MEMORY.md"}}}).encode())
        for k, v in (("Content-Type", "application/json"), ("Accept", "application/json, text/event-stream"),
                     ("Accept-Encoding", "gzip"), ("Authorization", "Bearer rw-token")):
            big.add_header(k, v)
        with urllib.request.urlopen(big, timeout=10) as r:
            assert r.headers.get("Content-Encoding") == "gzip", r.headers  # compressed by the server itself
            assert b"Item 119" in gzip.decompress(r.read())

        # Delete keeps a copy, refuses a stale version and read-only tokens.
        (mem / "old.md").write_text("outdated\n")
        ver_old = call("knowledge_read", {"path": "memory/old.md"})[1].split("version ")[1].split(",")[0]
        assert call("knowledge_delete", {"path": "memory/old.md", "base_version": "0" * 16})[0]
        assert call("knowledge_delete", {"path": "memory/old.md", "base_version": ver_old}, token="ro-token")[0]
        err, text = call("knowledge_delete", {"path": "memory/old.md", "base_version": ver_old})
        assert not err and not (mem / "old.md").exists(), text
        assert list((tmp / "deleted").glob("*_memory__old.md"))[0].read_text() == "outdated\n"
        # The re-check right before replacing refuses when the file changed meanwhile (a native edit on the VM).
        sys.path.insert(0, str(Path(__file__).parent))
        os.environ.update({k: v for k, v in env.items() if k.startswith("KNOWLEDGE_")})
        import server as srv
        f = mem / "race.md"
        f.write_text("native\n")
        assert not srv.replace_if_unchanged(f, b"stale\n", b"server\n") and f.read_text() == "native\n"
        assert srv.replace_if_unchanged(f, b"native\n", b"server\n") and f.read_text() == "server\n"
        assert not list(mem.glob(".knowledge-*")), "temp files must not be left behind"
        # Lint runs on the in-memory index (the temp MEMORY.md points to 120 files that do not exist).
        lint_out = call("knowledge_lint", {})[1]
        assert "index_points_to_missing_file (120)" in lint_out, lint_out[:300]
        # Case-insensitive fast path, uppercase-escape fallback, and a file whose lowercase form changes length.
        (mem / "case.md").write_text("Mixed CASE Velux\nİstanbul Velux\n")
        http("POST", "/invalidate", {"paths": ["memory/case.md"]}, token="watch-token")
        assert "memory/case.md:1" in call("knowledge_search", {"pattern": "mixed case velux"})[1]
        assert "memory/case.md:1" in call("knowledge_search", {"pattern": r"\S+ CASE"})[1]  # \S must stay non-space
        assert "memory/case.md:2" in call("knowledge_search", {"pattern": "istanbul|İstanbul"})[1]
        # Index: every matching line, not only the first per file.
        hits = call("knowledge_search", {"pattern": "^line", "root": "memory"})[1].splitlines()
        assert len([h for h in hits if h.startswith("memory/fact.md:")]) == 2, hits
        # A native edit on the VM is invisible until the watcher reports it; then it is visible at once.
        (mem / "fact.md").write_text("line one\nnative edit\n")
        assert "native edit" not in call("knowledge_search", {"pattern": "native edit"})[1]
        assert http("POST", "/invalidate", {"paths": ["memory/fact.md"]}, token="rw-token")[0] == 403
        assert http("POST", "/invalidate", {"paths": ["memory/fact.md"]}, token="watch-token")[0] == 200
        assert "memory/fact.md:2" in call("knowledge_search", {"pattern": "native edit"})[1]
        (mem / "fact.md").unlink()
        http("POST", "/invalidate", {"paths": ["memory/fact.md"]}, token="watch-token")
        assert "memory/fact.md" not in call("knowledge_list", {"root": "memory"})[1]

        # ---- web UI: Authentik identity via Traefik (shared secret), CSRF header, graph, note, search, lint, ingest
        def raw(method, path, body=None, headers=None):
            c = httpc.HTTPConnection("127.0.0.1", PORT, timeout=15)
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        user = lambda name, secret="proxy-secret": {"X-Knowledge-Proxy": secret, "X-authentik-username": name}
        ck = user("anthony")
        assert raw("GET", "/ui")[0] == 401
        assert raw("GET", "/ui", headers={"X-authentik-username": "anthony"})[0] == 401  # header without the secret: forged
        assert raw("GET", "/ui", headers=user("anthony", "wrong"))[0] == 401
        assert raw("GET", "/ui/api/graph", headers=user("anthony", ""))[0] == 401
        assert raw("GET", "/ui/static/app.js")[0] == 200  # static is public (no data)
        assert raw("GET", "/", headers=ck)[1].get("location") == "/ui"
        assert raw("GET", "/ui", headers=ck)[0] == 200
        assert json.loads(raw("GET", "/ui/api/me", headers=ck)[2]) == {"user": "anthony", "admin": True, "projects": ["homelab", "anpitek", "trade"], "writable": ["homelab", "anpitek", "trade"]}
        g = json.loads(raw("GET", "/ui/api/graph", headers=ck)[2])
        assert g["nodes"] and not any(n["id"].endswith("/MEMORY.md") for n in g["nodes"]), g["nodes"][:3]  # index not drawn, still readable below
        assert {n["project"] for n in g["nodes"] if n["id"].startswith("trade")} == {"trade"}
        th = json.loads(raw("GET", "/ui/api/search?q=entry%20timing&project=trade", headers=ck)[2])["notes"]
        assert th and all(h["project"] == "trade" for h in th), th
        assert not json.loads(raw("GET", "/ui/api/search?q=entry%20timing&project=homelab", headers=ck)[2])["notes"]
        note = json.loads(raw("GET", "/ui/api/note?path=memory/MEMORY.md", headers=ck)[2])
        assert "Item 1" in note["text"] and note["version"]
        assert raw("GET", "/ui/api/note?path=../../etc/passwd", headers=ck)[0] == 404
        hits = json.loads(raw("GET", "/ui/api/search?q=Item%20119", headers=ck)[2])["notes"]
        assert hits and hits[0]["path"] == "memory/MEMORY.md" and 121 in [h["line"] for h in hits[0]["hits"]], hits
        # Several words: all must occur in the note (not as one phrase); the note named after them ranks first.
        (mem / "project_nas_freeze.md").write_text("---\nname: NAS freeze\n---\nThe NAS hangs at 05:15.\n")
        (lab / "07_BACKUP.md").write_text("backup notes: nas, later the freeze of the pool\n")
        http("POST", "/invalidate", {"paths": ["memory/project_nas_freeze.md", "homelab/07_BACKUP.md"]}, token="watch-token")
        hits = json.loads(raw("GET", "/ui/api/search?q=nas%20freeze&project=homelab", headers=ck)[2])["notes"]
        assert [h["path"] for h in hits][:2] == ["memory/project_nas_freeze.md", "homelab/07_BACKUP.md"], hits
        # A catastrophic regex costs at most a few seconds and never blocks other requests.
        t0 = time.time()
        err, text = call("knowledge_search", {"pattern": "(a+)+$"})
        assert time.time() - t0 < 6, time.time() - t0
        (mem / "redos.md").write_text("a" * 30000 + "!\n")
        http("POST", "/invalidate", {"paths": ["memory/redos.md"]}, token="watch-token")
        import threading
        res = {}
        th_ = threading.Thread(target=lambda: res.update(r=call("knowledge_search", {"pattern": "(a+)+$"})))
        th_.start(); time.sleep(0.5)
        t1 = time.time(); assert http("GET", "/health", token=None)[0] == 200 and time.time() - t1 < 1.5
        th_.join()
        assert res["r"][0] and "longer than" in res["r"][1], res
        (mem / "redos.md").unlink()
        http("POST", "/invalidate", {"paths": ["memory/redos.md"]}, token="watch-token")
        assert isinstance(json.loads(raw("GET", "/ui/api/lint", headers=ck)[2]), dict)
        body = json.dumps({"text": "# Source\nRouter firmware notes", "title": "Router notes"})
        assert raw("POST", "/ui/api/ingest", body, {**ck, "Content-Type": "application/json"})[0] == 403  # no CSRF header
        st, _, res = raw("POST", "/ui/api/ingest", body, {**ck, "Content-Type": "application/json", "X-Knowledge-UI": "1"})
        res = json.loads(res)
        assert st == 200 and res["path"].startswith("homelab/sources/") and res["path"].endswith("router-notes.md"), res
        src = lab / "sources" / res["path"].split("/")[-1]
        assert "Router firmware notes" in src.read_text() and "by: user:anthony" in src.read_text()
        assert "| Router notes |" in (lab / "sources" / "LOG.md").read_text()
        html = b"<html><head><title>Wolf guide</title></head><body><nav>menu</nav><article><h1>Wolf guide</h1>" + b"".join(f"<p>Paragraph {i}: Games on Whales streams game number {i} to player {i % 3}, with its own controller and audio.</p>".encode() for i in range(20)) + b"</article></body></html>"
        st, _, res = raw("POST", "/ui/api/ingest", html, {**ck, "X-Knowledge-UI": "1", "X-Filename": "wolf.html", "Content-Type": "application/octet-stream"})
        res = json.loads(res)
        assert st == 200 and res["title"] == "Wolf guide" and res["chars"] > 100, res
        assert "unprocessed_source" in call("knowledge_lint", {})[1]
        err, text = call("knowledge_ingest", {"text": "pasted note about PBS", "title": "PBS note"})
        assert not err and "homelab/sources/" in text and "<source_document" in text, text
        err, text = call("knowledge_ingest", {"url": "http://127.0.0.1/secret"})
        assert err and "non-public" in text, text  # SSRF guard
        assert call("knowledge_ingest", {"text": "x"}, token="ro-token")[0]
        # UI editor: save with the version check, new memo plus index line, read-only refused; recent; clusters.
        post = lambda body, hdr=ck: raw("POST", "/ui/api/save", json.dumps(body), {**hdr, "Content-Type": "application/json", "X-Knowledge-UI": "1"})
        note = json.loads(raw("GET", "/ui/api/note?path=trade-memory/plan.md", headers=ck)[2])
        assert note["writable"] and not json.loads(raw("GET", "/ui/api/note?path=claude/CLAUDE.md", headers=ck)[2])["writable"]
        st, _, res = post({"path": "trade-memory/plan.md", "text": "edited in the UI\n", "base_version": note["version"]})
        assert st == 200 and (tmem / "plan.md").read_text() == "edited in the UI\n", res
        st, _, res = post({"path": "trade-memory/plan.md", "text": "stale\n", "base_version": note["version"]})
        assert st == 409 and b"changed since you read it" in res, res
        st, _, res = post({"path": "trade-memory/project_new.md", "text": "new\n", "index_line": "- [New](project_new.md) — x\n- injected"})
        assert st == 200 and (tmem / "MEMORY.md").read_text().endswith("- [New](project_new.md) — x - injected\n"), (tmem / "MEMORY.md").read_text()
        assert post({"path": "trade/STATUS2.md", "text": "x", "index_line": "- x"})[0] == 409  # index line only for memos
        # People: unknown users get no access but show up for the admin; access per project, read or write.
        admin = lambda path, body, hdr=ck: raw("POST", path, json.dumps(body), {**hdr, "Content-Type": "application/json", "X-Knowledge-UI": "1"})
        assert raw("GET", "/ui", headers=user("bob"))[0] == 403
        assert "bob" in json.loads(raw("GET", "/ui/api/admin", headers=ck)[2])["users"]
        assert admin("/ui/api/admin/user", {"username": "bob", "projects": {"trade": "read"}}, user("bob"))[0] == 403  # only admins
        assert admin("/ui/api/admin/user", {"username": "bob", "projects": {"nope": "read"}})[0] == 400
        assert admin("/ui/api/admin/user", {"username": "anthony", "projects": {}})[0] == 400  # admins come from the config
        assert admin("/ui/api/admin/user", {"username": "bob", "projects": {"trade": "read", "homelab": "none"}})[0] == 200
        ro = user("bob")
        assert json.loads(raw("GET", "/ui/api/me", headers=ro)[2]) == {"user": "bob", "admin": False, "projects": ["trade"], "writable": []}
        assert not json.loads(raw("GET", "/ui/api/note?path=trade-memory/plan.md", headers=ro)[2])["writable"]
        assert post({"path": "trade-memory/x.md", "text": "x"}, ro)[0] == 409
        assert raw("GET", "/ui/api/note?path=memory/MEMORY.md", headers=ro)[0] == 404
        assert not json.loads(raw("GET", "/ui/api/search?q=Item%20119", headers=ro)[2])["notes"]
        assert all(n["id"].startswith("trade") for n in json.loads(raw("GET", "/ui/api/graph", headers=ro)[2])["nodes"])
        assert json.loads(raw("GET", "/ui/api/lint", headers=ro)[2]) == {} and raw("GET", "/ui/admin", headers=ro)[0] == 403
        assert raw("GET", "/ui/api/admin", headers=ro)[0] == 403
        assert admin("/ui/api/admin/user", {"username": "bob", "projects": {"trade": "write"}})[0] == 200
        assert json.loads(raw("GET", "/ui/api/note?path=trade-memory/plan.md", headers=ro)[2])["writable"]
        assert admin("/ui/api/admin/user", {"username": "bob", "delete": True})[0] == 200
        assert raw("GET", "/ui", headers=ro)[0] == 403
        # Tokens: created in the UI (shown once, hash stored), usable right away, revoked right away.
        st, _, res = admin("/ui/api/admin/token", {"name": "phone", "write": False, "roots": ["trade", "trade-memory"]})
        tok = json.loads(res)["token"]
        assert st == 200 and tok not in tokens.read_text() and json.loads(tokens.read_text())["phone"]["roots"] == ["trade", "trade-memory"]
        assert admin("/ui/api/admin/token", {"name": "phone"})[0] == 400  # name in use
        assert call("knowledge_write", {"path": "memory/tokleak.md", "content": f"phone token {tok}\n"})[0]  # a token is a secret
        assert admin("/ui/api/admin/token", {"name": "x", "roots": ["../etc"]})[0] == 400
        assert not call("knowledge_read", {"path": "trade-memory/plan.md"}, token=tok)[0]
        assert call("knowledge_read", {"path": "memory/fact.md"}, token=tok)[0]  # outside its scope
        assert call("knowledge_write", {"path": "trade-memory/y.md", "content": "y"}, token=tok)[0]  # read-only
        assert admin("/ui/api/admin/token", {"name": "phone", "revoke": True})[0] == 200
        assert http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=tok)[0] == 401
        # Projects: created in the UI with docs + memory roots, writable, known to /ui/api/me and the session context.
        assert admin("/ui/api/admin/project", {"name": "Bad-Name"})[0] == 400
        assert admin("/ui/api/admin/project", {"name": "trade"})[0] == 400
        assert admin("/ui/api/admin/project", {"name": "garden"})[0] == 200
        assert (tmp / "projects/garden/memory/MEMORY.md").exists()
        assert not call("knowledge_write", {"path": "garden-memory/project_beds.md", "content": "raised beds\n"})[0]
        assert "raised beds" in call("knowledge_read", {"path": "garden-memory/project_beds.md"})[1]
        assert "garden" in json.loads(raw("GET", "/ui/api/me", headers=ck)[2])["projects"]
        assert "project garden" in http("GET", "/context/1?project=/home/x/garden")[1]
        # Review fixes 20 Oct: a project named like a path part does not capture other sessions; write patterns are
        # anchored; "Add" never wipes access; graph ETag per user; body cap; scoped ingest/lint; token routes.
        assert admin("/ui/api/admin/project", {"name": "home"})[0] == 200
        assert "project anpitek" in http("GET", "/context/1?project=/home/x/anpitek/boekhouding")[1]
        assert "project homelab" in http("GET", "/context/1?project=/home/x/homelab")[1]
        assert call("knowledge_write", {"path": "homelab/deep/memory/x.md", "content": "x"})[0]
        assert admin("/ui/api/admin/user", {"username": "dora", "projects": {"trade": "write"}})[0] == 200
        assert admin("/ui/api/admin/user", {"username": "dora", "projects": {}, "add": True})[0] == 400
        assert json.loads(raw("GET", "/ui/api/me", headers=user("dora"))[2])["writable"] == ["trade"]
        etag_admin = raw("GET", "/ui/api/graph", headers=ck)[1]["etag"]
        assert raw("GET", "/ui/api/graph", headers={**user("dora"), "If-None-Match": etag_admin})[0] == 200
        st = raw("POST", "/ui/api/save", b"x" * (13 * 1024 * 1024), {**user("dora"), "Content-Type": "application/json", "X-Knowledge-UI": "1"})[0]
        assert st == 413, st
        st, _, res = admin("/ui/api/admin/token", {"name": "tradebot", "write": True, "roots": ["trade", "trade-memory"]})
        tb = json.loads(res)["token"]
        err, text = call("knowledge_ingest", {"text": "a trade-only token may not add homelab sources"}, token=tb)
        assert err and "may not add sources" in text, text
        assert "memory/" not in call("knowledge_lint", {}, token=tb)[1].replace("trade-memory/", "")
        st, body = http("GET", "/lint", token=tb)
        assert st == 200 and all(not i.startswith(("memory", "homelab")) for v in json.loads(body).values() for i in v), body[:300]
        assert http("GET", "/ui/api/lint", token="rw-token")[0] == 401
        listed = {x["name"]: x for x in json.loads(raw("GET", "/ui/api/admin", headers=ck)[2])["tokens"]}
        assert listed["tradebot"]["last_used"] and time.time() - listed["tradebot"]["last_used"] < 60, listed["tradebot"]
        st, body = http("GET", "/semantic?q=entry%20timing&project=trade")
        assert st == 200 and "hits" in json.loads(body)
        st, _, res = admin("/ui/api/admin/token", {"name": "watcher2", "watch": True})
        assert http("POST", "/invalidate", {"paths": []}, token=json.loads(res)["token"])[0] == 200
        # New memo from the UI: the index line goes under the chosen heading.
        (tmem / "MEMORY.md").write_text("# Index\n\n## Plans\n- [Bot plan](plan.md) — entry timing\n\n## Other\n- [X](x.md) — x\n")
        http("POST", "/invalidate", {"paths": ["trade-memory/MEMORY.md"]}, token="watch-token")
        st, _, res = post({"path": "trade-memory/project_risk.md", "text": "risk\n", "index_line": "- [Risk](project_risk.md) — r", "heading": "Plans"})
        assert st == 200 and "- [Bot plan](plan.md) — entry timing\n- [Risk](project_risk.md) — r\n\n## Other" in (tmem / "MEMORY.md").read_text(), (tmem / "MEMORY.md").read_text()
        files = json.loads(raw("GET", "/ui/api/recent?project=trade", headers=ck)[2])["files"]
        assert files[0]["path"] in ("trade-memory/MEMORY.md", "trade-memory/project_new.md") and files[0]["by"] == "user:anthony", files[:3]
        assert all(f["path"].startswith("trade") for f in files)
        g = json.loads(raw("GET", "/ui/api/graph", headers=ck)[2])
        assert all("cluster" in n and "sources" in n for n in g["nodes"]) and {"isolated", "sparse_topics"} <= set(g["insights"])
        # Ingest dedup: the same text twice is refused with the existing path.
        err, text = call("knowledge_ingest", {"text": "pasted note about PBS", "title": "PBS again"})
        assert err and "already ingested as homelab/sources/" in text, text
        # Semantic search (test embedder) once the background index is built; related notes; graph gaps in lint.
        for _ in range(40):
            res = json.loads(raw("GET", "/ui/api/semantic?q=entry%20timing&project=trade", headers=ck)[2])
            if res["hits"]:
                break
            time.sleep(0.5)
        assert res["hits"] and res["hits"][0]["path"].startswith("trade"), res
        assert "trade-memory/" in call("knowledge_semantic_search", {"query": "entry timing", "root": "trade-memory"})[1]
        assert not call("knowledge_related", {"path": "trade-memory/plan.md"})[0]
        # A new memo that says the same as an existing one gets a merge hint (not a refusal); a different one does not.
        err, text = call("knowledge_write", {"path": "trade-memory/plan_copy.md", "content": "---\nname: plan copy\n---\nentry timing plan\n"})
        assert not err and "Similar existing memos" in text and "trade-memory/plan.md" in text, text
        err, text = call("knowledge_write", {"path": "trade-memory/other.md", "content": "---\nname: other\n---\nvolcano weather report\n"})
        assert not err and "Similar existing memos" not in text, text
        rel = json.loads(raw("GET", "/ui/api/related?path=trade-memory/plan.md", headers=ck)[2])["related"]
        assert isinstance(rel, list)
        assert "isolated_note" in json.loads(raw("GET", "/ui/api/lint", headers=ck)[2])
        assert (tmp / "vectors.npz").exists()
        # History: every version with who changed it; native VM edits show as VM; diff + restore via the UI.
        (tmem / "hist.md").write_text("v1\n")
        http("POST", "/invalidate", {"paths": ["trade-memory/hist.md"]}, token="watch-token")
        ver = call("knowledge_read", {"path": "trade-memory/hist.md"})[1].split("version ")[1].split(",")[0]
        assert not call("knowledge_write", {"path": "trade-memory/hist.md", "content": "v2\n", "base_version": ver})[0]
        (tmem / "hist.md").write_text("v3 native\n")
        http("POST", "/invalidate", {"paths": ["trade-memory/hist.md"]}, token="watch-token")
        hist = call("knowledge_history", {"path": "trade-memory/hist.md"})[1].splitlines()
        assert len(hist) == 3 and "by VM" in hist[0] and "by laptop" in hist[1] and "by before" in hist[2], hist
        old_id = hist[1].split()[0]
        text = call("knowledge_history", {"path": "trade-memory/hist.md", "version": old_id})[1]
        assert "-v2" in text and "+v3 native" in text and "<version" in text, text
        hv = json.loads(raw("GET", "/ui/api/history?path=trade-memory/hist.md", headers=ck)[2])["versions"]
        one = json.loads(raw("GET", f"/ui/api/history?path=trade-memory/hist.md&version={hv[0]['id']}", headers=ck)[2])
        assert one["text"] == "v3 native\n" and "+v3 native" in one["diff"], one
        assert raw("GET", "/ui/api/history?path=trade-memory/hist.md&version=../../x", headers=ck)[0] == 404
        # Session log: the hook script posts prompts/files/summary; server redacts and appends per project and month.
        tr = tmp / "transcript.jsonl"
        rows = [{"type": "user", "message": {"content": "fix the trade bot, password Sup3rSecret99"}},
                {"type": "user", "isMeta": True, "message": {"content": "<local-command-caveat>x"}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/home/x/trade_bot/bot.py"}},
                                                              {"type": "text", "text": "Done: fixed bot.py."}]}}]
        tr.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        hook = json.dumps({"session_id": "abcdef1234", "transcript_path": str(tr), "cwd": "/home/x/trade_bot", "reason": "clear"})
        senv = {**os.environ, "KNOWLEDGE_URL": BASE, "KNOWLEDGE_TOKEN": "rw-token", "XDG_CACHE_HOME": str(tmp / "cache"), "HOME": str(tmp)}
        script = str(Path(__file__).parents[1] / "plugin/hooks/session-log.py")
        subprocess.run([sys.executable, script], input=hook, text=True, env=senv, check=True)
        log = (tmem / "sessions" / f"{time.strftime('%Y-%m')}.md").read_text()
        assert "· trade · laptop · clear · session abcdef12" in log and "Asked: fix the trade bot" in log, log
        assert "Sup3rSecret99" not in log and "bot.py" in log and "Outcome: Done" in log and "caveat" not in log, log
        subprocess.run([sys.executable, script], input=hook, text=True, env=senv, check=True)  # nothing new: no entry
        assert (tmem / "sessions" / f"{time.strftime('%Y-%m')}.md").read_text() == log
        assert http("POST", "/sessions", {"prompts": ["x"], "cwd": "/"}, token="ro-token")[0] == 403
        # Changes: lines added/removed by the last change, its version for the diff, and the session that made it.
        assert http("POST", "/sessions", {"prompts": ["tidy the hist memo"], "cwd": "/home/x/trade_bot",
                                          "files": ["/home/x/.claude/trade-memory/hist.md"]})[0] == 200
        files = json.loads(raw("GET", "/ui/api/recent?project=trade", headers=ck)[2])["files"]
        h = next(f for f in files if f["path"] == "trade-memory/hist.md")
        assert h["added"] == 1 and h["removed"] == 1 and h["version"] == hv[0]["id"], h
        assert h["session"]["asked"] == "tidy the hist memo" and h["session"]["device"] == "laptop", h
        assert not any("/sessions/" in f["path"] for f in files)
        # Caching: vendor files immutable, app files revalidated, graph 304 on an unchanged ETag.
        assert "immutable" in raw("GET", "/ui/static/vendor/force-graph-1.52.0.js")[1]["cache-control"]
        assert raw("GET", "/ui/static/app.js")[1]["cache-control"] == "no-cache"
        st, hd, body = raw("GET", "/ui", headers=ck)
        rel = re.search(rb'app\.js\?v=([0-9a-f]{12})"', body).group(1).decode()
        assert hd["cache-control"] == "no-cache" and f'data-release="{rel}"'.encode() in body and b"app.css?v=" + rel.encode() in body
        assert "immutable" in raw("GET", f"/ui/static/app.js?v={rel}")[1]["cache-control"]
        assert raw("GET", "/ui/static/app.js?v=old")[1]["cache-control"] == "no-cache"
        st, hd, _ = raw("GET", "/ui/api/graph", headers=ck)
        assert raw("GET", "/ui/api/graph", headers={**ck, "If-None-Match": hd["etag"]})[0] == 304
        # Live updates: the event stream says "changed" after a write by any session.
        c = httpc.HTTPConnection("127.0.0.1", PORT, timeout=10)
        c.request("GET", "/ui/api/events", headers=ck)
        r = c.getresponse()
        assert r.status == 200 and r.getheader("content-type").startswith("text/event-stream")
        hello = b"".join(r.fp.readline() for _ in range(5))  # chunk-size line + retry + event + data
        assert b"event: hello" in hello and rel.encode() in hello, hello
        call("knowledge_write", {"path": "trade-memory/live.md", "content": "live\n"})
        got = b""
        while b"event: changed" not in got:
            got += r.fp.readline()
        c.close()

        parts, n = [], 1
        while True:
            st, body = http("GET", f"/context/{n}")
            if not body:
                break
            assert len(body) <= 10_000, (n, len(body))
            parts.append(body)
            n += 1
        assert len(parts) >= 2 and "Item 119" in parts[-1] and "global rules" in parts[0], len(parts)
        st, body = http("GET", "/context/1?project=/home/x/trade_bot")
        assert "project trade" in body and "trade bot rules" in body and "Bot plan" in body and "Item 1 " not in body, body[:500]
        assert "project anpitek" in http("GET", "/context/1?project=/home/x/anpitek/boekhouding")[1]
        assert "laptop edit memory/fact.md" in (tmp / "audit.log").read_text()
        print(f"ok ({len(parts)} context parts)")
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
