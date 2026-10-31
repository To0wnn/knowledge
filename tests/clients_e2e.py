#!/usr/bin/env python3
"""End-to-end test of everything a client machine runs, on the OS it runs on (CI: Windows, macOS, Linux).

Starts the real server (server/server.py, fake embeddings) on temp dirs with a guest project, then drives the client
side exactly the way the tools do it, under awkward conditions on purpose: a home folder with a space and an "é",
a .knowledge-project written the PowerShell 5 way (UTF-16), text with → and ✅.

  1. Claude Code plugin: every command in plugin/hooks/hooks.json, run through the shell Claude Code uses (Git Bash on
     Windows, sh elsewhere), with ${CLAUDE_PLUGIN_ROOT} both as an environment variable and substituted literally.
  2. agy and Codex: knowledge-agent.py install (twice: safe to rerun), then every generated hook command, on Windows
     through PowerShell and cmd.
  3. The offline copy: background sync, an edit made offline, the offline context, the upload when the server is back.
  4. Codex plugin (plugin/.codex-plugin): its hook commands as Codex runs them, its stdio MCP server, and with the CLI
     the install from this repo's marketplace.
  5. agy plugin: plugin/hooks.json run the way agy runs it (in the plugin folder; cmd /c on Windows), and with agy
     installed `agy plugin validate` and `agy plugin install` from this repo.
  6. Optional, when the CLIs are installed (CI installs them): `claude plugin install` from this repo and
     `codex mcp list` see the knowledge server.

  uv run --project server python tests/clients_e2e.py        (exit 0 = all passed; every check prints a line)
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLIENT = REPO  # replaced in main() by a copy under a path with a space, like C:\Users\Test Usér\...
PORT = 18799
BASE = f"http://127.0.0.1:{PORT}"
WIN = os.name == "nt"
failures = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    print(("ok   " if ok else "FAIL ") + what + ("" if ok or not detail else f"\n     {detail[:800]}"), flush=True)
    if not ok:
        failures.append(what)
    return ok


def http(method: str, path: str, body=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def bash() -> str:
    """The shell Claude Code runs hooks in: Git Bash on Windows (never WSL's bash.exe), sh elsewhere."""
    if not WIN:
        return shutil.which("sh")
    git = Path(shutil.which("git")).resolve()
    for cand in (git.parents[1] / "bin" / "bash.exe", git.parents[1] / "usr" / "bin" / "bash.exe"):
        if cand.exists():
            return str(cand)
    raise SystemExit("Git Bash not found (Claude Code on Windows needs Git for Windows)")


def run(argv, stdin: str = "", env=None, cwd=None, timeout=60):
    r = subprocess.run(argv, input=stdin.encode("utf-8"), env=env, cwd=cwd, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def wait_for(cond, seconds=30.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return cond()


def usage_hook(home: Path, agent: str, sid: str, prompt: str = "usage test") -> dict:
    """A session-log hook payload whose transcript holds one prompt and one model call of 7 output tokens, in the
    format of that agent (Claude repeats a call per content block; Codex logs running totals)."""
    if agent == "codex":
        path = home / ".codex" / "sessions" / f"rollout-{sid}.jsonl"
        total = lambda n: {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 100 * n, "cached_input_tokens": 60 * n, "output_tokens": 7 * n}}}}
        rows = [total(1), {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}}, total(2)]
        hook, done = {"hook_event_name": "SessionEnd", "session_id": sid, "transcript_path": str(path)}, 1  # 1: logged before
    elif agent == "agy":
        path = home / f"agy-{sid}.jsonl"
        rows = [{"type": "USER_INPUT", "content": f"<USER_REQUEST>{prompt}</USER_REQUEST>"},
                {"type": "PLANNER_RESPONSE", "content": "ok", "input_tokens": 40, "cache_read_tokens": 60, "output_tokens": 7}]
        hook, done = {"conversationId": sid, "transcriptPath": str(path)}, 0
    else:
        path = home / f"claude-{sid}.jsonl"
        call = {"type": "assistant", "requestId": "r1", "message": {"id": "m1", "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 40, "cache_read_input_tokens": 60, "output_tokens": 7}}}
        rows = [{"type": "user", "message": {"content": prompt}}, call, call]
        hook, done = {"hook_event_name": "SessionEnd", "session_id": sid, "transcript_path": str(path)}, 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    if done:  # the earlier log of this session read up to here (session-log's own state file)
        state = home / ".cache" / "knowledge-session-log" / f"{sid}.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"lines": done}), encoding="utf-8")
    return hook


def usage_logged(home: Path, agent: str, sid: str) -> bool:
    """The server stored that session's tokens once: 40 in, 60 cache read, 7 out."""
    rows = [json.loads(x) for x in (home.parent / "usage.jsonl").read_text(encoding="utf-8").splitlines()] \
        if (home.parent / "usage.jsonl").exists() else []
    return [(r["agent"], r.get("in"), r.get("cache_read"), r.get("out")) for r in rows if r["session"] == sid] == [(agent, 40, 60, 7)]


def limits_e2e(tok: str, tmp: Path):
    """The usage-limit ladder in every setup (Claude plugin, Codex and agy as plugin and as script setup): each agent's
    own source at 96% (Claude's status line through the tap, a Codex rollout, agy's language server), then the real
    per-turn hook says it once and the subagent hook refuses."""
    import http.server
    home, project = tmp / "Limit Usér", tmp / "Limit Usér" / "proj"
    project.mkdir(parents=True)
    now, root = time.time(), CLIENT / "plugin"

    class FakeLS(http.server.BaseHTTPRequestHandler):  # agy's language server: GetUserStatus with its CSRF header
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            ok = self.headers.get("x-codeium-csrf-token") == "csrf-e2e"  # the wake check asks GetAllCascadeTrajectories first
            body = "{}" if not self.path.endswith("/GetUserStatus") else json.dumps({"userStatus": {"cascadeModelConfigData": {"clientModelConfigs": [{"label": "Pro", "quotaInfo": {
                "remainingFraction": 0.04, "resetTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600))}}]}}})
            self.send_response(200 if ok else 403)
            self.end_headers()
            self.wfile.write(body.encode() if ok else b"{}")

        def log_message(self, *a):
            pass
    ls = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLS)
    threading.Thread(target=ls.serve_forever, daemon=True).start()
    (home / ".knowledge" / "agents").mkdir(parents=True)
    (home / ".knowledge" / "agents" / "agy-ls-e2e.json").write_text(json.dumps(
        {"address": f"127.0.0.1:{ls.server_port}", "csrf": "csrf-e2e", "cwd": str(project), "at": now}), encoding="utf-8")
    rollout = home / ".codex" / "sessions" / "rollout-limits.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 96.0, "window_minutes": 300, "resets_at": int(now + 3600)}}}}) + "\n", encoding="utf-8")
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "echo my-own-line"}}), encoding="utf-8")
    try:
        # Claude: SessionStart puts the tap in front, Claude Code runs the status line, then the hooks read it.
        hooks = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        env = user_env(home, CLAUDE_PLUGIN_ROOT=str(root), CLAUDE_PLUGIN_OPTION_URL=BASE, CLAUDE_PLUGIN_OPTION_TOKEN=tok)
        sh = bash()
        install = next(h["command"] for h in hooks["SessionStart"][0]["hooks"] if "limits.py" in h["command"])
        code, out, err = run([sh, "-c", install], "{}", env)
        line = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))["statusLine"]["command"]
        check(code == 0 and "status line now also hands" in out and "statusline" in line, "claude: SessionStart puts the status line tap in front", out + err)
        code, out, err = run([sh, "-c", line], json.dumps({"rate_limits": {"five_hour": {"used_percentage": 96, "resets_at": int(now + 3600)}}}), env)
        check(code == 0 and out.strip() == "my-own-line", "claude: the tapped status line still shows the user's own line", out + err)
        sets = {"claude plugin": (env, [sh, "-c"], {"session_id": "lim-claude", "cwd": str(project)},
                                  next(h["command"] for h in hooks["UserPromptSubmit"][0]["hooks"] if " tasks " in h["command"]),
                                  next(g["hooks"][0]["command"] for g in hooks["PreToolUse"] if "Agent" in g["matcher"]), {"tool_name": "Agent"})}
        cx = json.loads((root / ".codex-plugin" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        cx_env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok)
        argv = lambda hk: codex_hook_argvs(hk, root)[0][1]
        sets["codex plugin"] = (cx_env, None, {"session_id": "lim-codex", "cwd": str(project), "transcript_path": str(rollout)},
                                cx["UserPromptSubmit"][0]["hooks"][0], cx["PreToolUse"][0]["hooks"][0], {"tool_name": "spawn_agent"})
        ag = json.loads((root / "hooks.json").read_text(encoding="utf-8"))["knowledge"]
        sets["agy plugin"] = (cx_env, "agy", {"conversationId": "lim-agy", "workspacePaths": [str(project)]},
                              ag["PostInvocation"][0]["command"], ag["PreToolUse"][0]["hooks"][0]["command"], {"toolCall": {"name": "invoke_subagent"}})
        agent = CLIENT / "clients" / "knowledge-agent.py"
        for name in ("codex", "agy"):  # the script setup writes its own hook commands
            code, out, err = run([sys.executable, str(agent), "install", name], "", {**cx_env, "KNOWLEDGE_SETUP": "script"})
            check(code == 0, f"{name}: script setup installs in the limits home", out + err)
        sc = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        sets["codex script"] = (cx_env, "plain", {"session_id": "lim-codex-s", "cwd": str(project), "transcript_path": str(rollout)},
                                sc["UserPromptSubmit"][0]["hooks"][0]["command"], sc["PreToolUse"][0]["hooks"][0]["command"], {"tool_name": "spawn_agent"})
        sa = json.loads((home / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8"))["knowledge"]
        sets["agy script"] = (cx_env, "plain", {"conversationId": "lim-agy-s", "workspacePaths": [str(project)]},
                              sa["PostInvocation"][0]["command"], sa["PreToolUse"][0]["hooks"][0]["command"], {"toolCall": {"name": "invoke_subagent"}})
        # secret-guard: the same refusal of a known secret value in each agent's own edit tool
        hashes = home / ".config" / "knowledge-server" / "secret-hashes.json"
        hashes.parent.mkdir(parents=True, exist_ok=True)
        hashes.write_text(json.dumps([hashlib.sha256(b"gategategate").hexdigest()]), encoding="utf-8")
        pick = lambda groups, word: next(g["hooks"][0] for g in groups if word in g["matcher"])
        patch = lambda text: {"tool_name": "apply_patch", "tool_input": {"command": f"*** Begin Patch\n*** Add File: x.md\n+{text}\n*** End Patch"}}
        agy_write = lambda text: {"toolCall": {"name": "write_file", "args": {"Path": "x.md", "Content": text}}}
        guards = {"claude plugin": (next(h["command"] for g in hooks["PreToolUse"] for h in g["hooks"] if "secret-guard" in h["command"]),
                                    lambda text: {"tool_name": "Write", "tool_input": {"file_path": "x.md", "content": text}}),
                  "codex plugin": (pick(cx["PreToolUse"], "apply_patch"), patch),
                  "agy plugin": (pick(ag["PreToolUse"], "write_file")["command"], agy_write),
                  "codex script": (pick(sc["PreToolUse"], "apply_patch")["command"], patch),
                  "agy script": (pick(sa["PreToolUse"], "write_file")["command"], agy_write)}
        for label, (e, how, hook, turn, gate, call) in sets.items():
            def go(cmd, payload):
                if how is None:  # Codex plugin: as Codex starts a plugin hook
                    return run(argv(cmd), json.dumps(payload), e, cwd=str(project))
                if isinstance(how, list):
                    return run(how + [cmd], json.dumps(payload), e, cwd=str(project))
                where = str(root) if how == "agy" else str(project)  # agy runs plugin hooks in the plugin folder
                return run(f'cmd /d /s /c "{cmd}"' if WIN else [shutil.which("sh"), "-c", cmd], json.dumps(payload), e, cwd=where)
            agent_name = label.split()[0]
            code, out, err = go(turn, hook)
            check(code == 0 and f"96% of your {agent_name} usage limit" in out and "refused" in out, f"{label}: the per-turn hook says the 96% rung", out[:400] + err[-400:])
            code, out, err = go(turn, hook)
            check(code == 0 and "usage limit" not in out, f"{label}: the rung is said once per session", out[:400])
            code, out, err = go(gate, {**hook, **call})
            check(code == 0 and '"deny"' in out, f"{label}: a new subagent is refused at 96%", out[:400] + err[-400:])
            guard, edit = guards[label]
            code, out, err = go(guard, {**hook, **edit("pw=gategategate")})
            check(code == 0 and '"deny"' in out and "secret set NAME" in out, f"{label}: an edit with a known secret is refused", out[:400] + err[-400:])
            code, out, err = go(guard, {**hook, **edit("pw=‹secret:X›")})
            check(code == 0 and out.strip() == "", f"{label}: a clean edit gets no output (agy reads any as deny)", out[:400] + err[-400:])
    finally:
        ls.shutdown()


def start_server(tmp: Path):
    data = {n: tmp / "data" / n for n in ("memory", "homelab", "claude", "trade", "trade-memory", "anpitek", "anpitek-memory")}
    for d in data.values():
        d.mkdir(parents=True)
    (data["memory"] / "MEMORY.md").write_text("# Index\n\n## General\n", encoding="utf-8")
    (data["claude"] / "CLAUDE.md").write_text("global rules\n", encoding="utf-8")
    (data["claude"] / "dev_rules.md").write_text((Path.home() / ".claude/gedeeld/dev_rules.md").read_text(encoding="utf-8")
                                             if (Path.home() / ".claude/gedeeld/dev_rules.md").exists() else "# Development rules\n\n1. Read first.\n", encoding="utf-8")
    (data["claude"] / "house_rules.md").write_text("# House rules\n\n1. Search first.\n2. A memo has \"## Stand\" and \"## Keuzes\".\n", encoding="utf-8")
    h = lambda t: hashlib.sha256(t.encode()).hexdigest()
    (tmp / "tokens.json").write_text(json.dumps({"owner": {"sha256": h("owner-token"), "write": True, "skills": True},
                                                 "watcher": {"sha256": h("watch-token"), "watch": True}}), encoding="utf-8")
    (data["claude"] / "skills" / "s1").mkdir(parents=True)
    (data["claude"] / "skills" / "s1" / "SKILL.md").write_text("---\nname: s1\n---\nshared skill → ✅\n", encoding="utf-8")
    (data["claude"] / "skills" / "linked").mkdir()
    (data["claude"] / "skills" / "linked" / "SKILL.md").write_text("server linked\n", encoding="utf-8")
    (tmp / "secrets.json").write_text(json.dumps([h("Sup3rSecret99")]), encoding="utf-8")
    env = {**os.environ, "KNOWLEDGE_MEMORY": str(data["memory"]), "KNOWLEDGE_HOMELAB": str(data["homelab"]),
           "KNOWLEDGE_CLAUDE": str(data["claude"]), "KNOWLEDGE_TRADE": str(data["trade"]),
           "KNOWLEDGE_TRADE_MEMORY": str(data["trade-memory"]), "KNOWLEDGE_ANPITEK": str(data["anpitek"]),
           "KNOWLEDGE_ANPITEK_MEMORY": str(data["anpitek-memory"]), "KNOWLEDGE_TOKENS": str(tmp / "tokens.json"),
           "KNOWLEDGE_ADMIN_DIR": str(tmp), "KNOWLEDGE_PROJECTS_DIR": str(tmp / "projects"),
           "KNOWLEDGE_PROXY_SECRET": "proxy-secret", "KNOWLEDGE_ADMINS": "alice", "KNOWLEDGE_AUDIT": str(tmp / "audit.log"),
           "KNOWLEDGE_PORT": str(PORT), "KNOWLEDGE_BIND": "127.0.0.1", "KNOWLEDGE_RESCAN": "3600", "KNOWLEDGE_EMBED_MODEL": "hash",
           "KNOWLEDGE_SECRETS": str(tmp / "secrets.json"), "KNOWLEDGE_VECTORS": str(tmp / "vectors.npz"),
           "KNOWLEDGE_ALLOWED_HOSTS": f"127.0.0.1:{PORT}", "PYTHONUTF8": "1",
           "KNOWLEDGE_GITHUB_RAW": "http://127.0.0.1:9"}  # no newest-version lookup on GitHub from a test
    log = open(tmp / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, str(REPO / "server" / "server.py")], env=env, stdout=log, stderr=subprocess.STDOUT)
    if not wait_for(lambda: _health(), 60):
        proc.kill()
        raise SystemExit("server did not start:\n" + (tmp / "server.log").read_text(errors="replace")[-3000:])
    admin = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice", "X-Knowledge-UI": "1"}
    assert http("POST", "/ui/api/admin/project", {"name": "garden"}, admin)[0] == 200
    st, body = http("POST", "/ui/api/admin/token", {"name": "friend-pc", "write": True, "redact": True,
                                                    "roots": ["garden", "garden-memory"]}, admin)
    assert st == 200, body
    tok = json.loads(body)["token"]
    # One hand-over per agent: each plugin must say which agent it is, so its own task opens the session context.
    tasks = "".join(f"- open tot 1 jan 2099 [{o}, {o.upper()}-1] from Claude, start now: task for {o}\n" for o in ("Claude", "Codex", "Gemini"))
    st, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write", "arguments":
                    {"path": "garden-memory/project_tasks.md", "content": "---\nname: tasks\ndescription: d\nmetadata:\n  type: project\n---\n"
                     f"## Stand\n{tasks}\n## Keuzes\n"}}}, {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
    assert "\"isError\":false" in body, body
    return proc, tok, tmp / "projects" / "garden"


def _health() -> bool:
    try:
        return http("GET", "/health")[0] == 200
    except OSError:
        return False


def user_env(home: Path, **extra) -> dict:
    """A clean user environment rooted at `home` (Path.home() reads USERPROFILE on Windows, HOME elsewhere)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KNOWLEDGE_", "CLAUDE_"))}
    # Codex on Windows finds its folder through Windows itself, not USERPROFILE: CODEX_HOME is its own override.
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / ".codex"), PYTHONUTF8="0",
               XDG_CACHE_HOME=str(home / ".cache"),
               KNOWLEDGE_COMPANION_DRY_RUN="1", **extra)  # never this machine's own systemd, launchd or Task Scheduler  # PYTHONUTF8 off: like a fresh Windows
    if WIN:
        env.pop("PYTHONIOENCODING", None)
    return env


def claude_plugin(tok: str, home: Path, project: Path, garden: Path):
    hooks = json.loads((CLIENT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    root = str(CLIENT / "plugin")
    sh = bash()
    transcript = home / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "fix the taxi speed → ✅"}}) + "\n", encoding="utf-8")
    payload = {"SessionStart": {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(project)},
               "PreToolUse": {"hook_event_name": "PreToolUse", "tool_name": "Write",
                              "tool_input": {"file_path": str(project / "notes.md"), "content": "db Sup3rSecret99"}},
               "PostToolUse": {"hook_event_name": "PostToolUse", "tool_name": "Write",
                               "tool_input": {"file_path": str(project / "docs" / "taxi.md")}},
               "SessionEnd": {**usage_hook(home, "claude", "claude-usage", "fix the taxi speed → ✅"), "cwd": str(project), "reason": "exit"},
               "PreCompact": {"hook_event_name": "PreCompact", "session_id": "s2", "transcript_path": str(transcript), "cwd": str(project)},
               "UserPromptSubmit": {"hook_event_name": "UserPromptSubmit", "session_id": "s3", "cwd": str(project), "prompt": "hi"}}
    hashes = home / ".config" / "knowledge-server" / "secret-hashes.json"  # an owner device has them (token.py push-secrets)
    hashes.parent.mkdir(parents=True, exist_ok=True)
    hashes.write_text(json.dumps([hashlib.sha256(b"Sup3rSecret99").hexdigest()]), encoding="utf-8")
    known = home / ".claude" / "plugins" / "known_marketplaces.json"  # installed by hand: marketplace known, no autoUpdate
    known.parent.mkdir(parents=True, exist_ok=True)
    known.write_text(json.dumps({"knowledge": {"source": {"source": "directory", "path": str(REPO)}, "installLocation": str(REPO),
                                               "lastUpdated": "2026-10-24T00:00:00.000Z"}}), encoding="utf-8")  # as Claude Code writes it
    for mode in ("env", "literal"):  # Claude Code may expand ${CLAUDE_PLUGIN_ROOT} itself or leave it to the shell
        env = user_env(home, CLAUDE_PLUGIN_ROOT=root, CLAUDE_PLUGIN_OPTION_URL=BASE, CLAUDE_PLUGIN_OPTION_TOKEN=tok,
                       CLAUDE_PROJECT_DIR=str(project))
        outs = {}
        for event in ("SessionStart", "PostToolUse", "PreToolUse", "SessionEnd", "UserPromptSubmit"):
            for group in hooks.get(event, []):
                for hk in group["hooks"]:
                    if hk.get("asyncRewake"):  # waits in the background for a wake; wake_e2e runs it
                        continue
                    cmd = hk["command"] if mode == "env" else hk["command"].replace("${CLAUDE_PLUGIN_ROOT}", root)
                    code, out, err = run([sh, "-c", cmd], json.dumps(payload[event]), env, cwd=str(project), timeout=hk.get("timeout", 30) + 30)
                    check(code == 0, f"[{mode}] {event} hook exits 0: {cmd[-60:]}", err)
                    outs.setdefault(event, []).append(out)
        start = "".join(outs["SessionStart"])
        check("project garden" in start and "Search first" in start, f"[{mode}] SessionStart: guest context with the house rules", start[:500])
        check("Development rules" in start, f"[{mode}] SessionStart: guest context with the development rules", start[:500])
        check("your open tasks (Claude" in start and "CLAUDE-1" in start.split("===== knowledge")[0],
              f"[{mode}] SessionStart: Claude's own task comes first", start[:500])
        check("unreachable" not in start and "skipped" not in start, f"[{mode}] SessionStart: no error notice", start[:500])
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        check(settings["extraKnownMarketplaces"]["knowledge"].get("autoUpdate") is True
              and ("auto-update is now on" in start) == (mode == "env"),
              f"[{mode}] SessionStart: auto-update turned on once, said once", json.dumps(settings) + start[-300:])
        check((garden / "docs" / "README.md").exists() and "→ ✅" in (garden / "docs" / "README.md").read_text(encoding="utf-8"),
              f"[{mode}] SessionStart mirrors the folder's .md (UTF-16 marker, → ✅ intact)")
        check("".join(outs["UserPromptSubmit"]) == "", f"[{mode}] UserPromptSubmit: the first prompt only remembers the open tasks",
              "".join(outs["UserPromptSubmit"]))
        check("deny" in "".join(outs["PreToolUse"]), f"[{mode}] PreToolUse: a known secret is refused", "".join(outs["PreToolUse"]))
        check((garden / "docs" / "docs" / "taxi.md").exists(), f"[{mode}] PostToolUse mirrors the edited file")
        check(usage_logged(home, "claude", "claude-usage"), f"[{mode}] SessionEnd: the session's tokens reach the server once")
        logs = list((garden / "memory" / "sessions").glob("*.md"))
        check(any("fix the taxi speed" in p.read_text(encoding="utf-8") for p in logs), f"[{mode}] SessionEnd posts the session log")
        mirror = home / ".knowledge" / "mirror"
        check(wait_for(lambda: (mirror / ".state.json").exists()), f"[{mode}] the background sync leaves an offline copy",
              (mirror / "last-sync.log").read_text(encoding="utf-8") if (mirror / "last-sync.log").exists() else "no state, no log")
        transcript.write_text(transcript.read_text(encoding="utf-8") + json.dumps({"type": "user", "message": {"content": f"second {mode}"}}) + "\n", encoding="utf-8")
    return mirror


def folder_link(home: Path):
    """The folder-to-project link: a folder with any name and a .knowledge-project (written the PowerShell 5 way) gets that
    project at session start, in the Claude plugin and in the Codex/agy hook; without one the directory decides."""
    linked, plain = home / "Some Folder é", home / "elsewhere-unlinked"
    for d in (linked, plain):
        d.mkdir(parents=True, exist_ok=True)
    (linked / ".knowledge-project").write_bytes("garden\r\n".encode("utf-16"))
    hooks = CLIENT / "plugin" / "hooks"
    env = user_env(home, CLAUDE_PLUGIN_OPTION_URL=BASE, CLAUDE_PLUGIN_OPTION_TOKEN="owner-token", KNOWLEDGE_URL=BASE,
                   KNOWLEDGE_TOKEN="owner-token", KNOWLEDGE_PLUGIN_LATEST=str(latest_file(home)))
    (home / ".config" / "knowledge").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "knowledge" / "url").write_text(BASE + "\n", encoding="utf-8")
    bare = {k: v for k, v in env.items() if k not in ("CLAUDE_PLUGIN_OPTION_URL", "KNOWLEDGE_URL")}
    code, out, err = run([bash(), "-c", f'sh "{(hooks / "context.sh").as_posix()}" 1'], "", {**bare, "CLAUDE_PROJECT_DIR": str(plain)})
    check("(project homelab)" in out, "claude: without the URL option the hook uses the stored server URL", out[:200] + err[-300:])
    for folder, want in ((linked, "garden"), (plain, "homelab")):
        code, out, err = run([bash(), "-c", f'sh "{(hooks / "context.sh").as_posix()}" 1'], "", {**env, "CLAUDE_PROJECT_DIR": str(folder)})
        check(f"(project {want})" in out, f"claude: a folder {'linked' if folder == linked else 'without a link'} gets project {want}",
              out[:200] + err[-300:])
        code, out, err = run([sys.executable, "-X", "utf8", str(hooks / "agent_hooks.py"), "context", "codex"],
                             json.dumps({"cwd": str(folder)}), env)
        check(f"(project {want})" in out, f"codex/agy: a folder {'linked' if folder == linked else 'without a link'} gets project {want}",
              out[:200] + err[-300:])
    # A git worktree elsewhere (Codex/agy make one per chat under ~/.codex/worktrees/): the main checkout's link counts,
    # though .knowledge-project is not committed, and the session is told it runs in a separate checkout.
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(linked)]
    wt = home / ".codex" / "worktrees" / "ab12" / "Some Folder é"
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"], ["worktree", "add", "-q", "--detach", str(wt)]):
        subprocess.run(git + args, check=True, capture_output=True)
    code, out, err = run([bash(), "-c", f'sh "{(hooks / "context.sh").as_posix()}" 1'], "", {**env, "CLAUDE_PROJECT_DIR": str(wt)})
    check("(project garden)" in out and "separate git worktree" in out, "claude: a worktree gets the main checkout's project and a notice",
          out[:300] + err[-300:])
    check("Some Folder é" in out, "claude: the worktree notice keeps a non-ASCII path intact (UTF-8, also on Windows)", out[:300])
    code, out, err = run([sys.executable, "-X", "utf8", str(hooks / "agent_hooks.py"), "context", "codex"], json.dumps({"cwd": str(wt)}), env)
    check("(project garden)" in out and "separate git worktree" in out, "codex/agy: a worktree gets the main checkout's project and a notice",
          out[:300] + err[-300:])
    code, out, err = run([bash(), "-c", f'sh "{(hooks / "context.sh").as_posix()}" 1'], "", {**env, "CLAUDE_PROJECT_DIR": str(linked)})
    check("separate git worktree" not in out, "claude: no worktree notice in the main checkout", out[:300])


def offline(tok: str, home: Path, project: Path, garden: Path, mirror: Path):
    memo = mirror / "garden-memory" / "project_offline.md"
    check(memo.parent.is_dir(), "the offline copy holds the guest's memory folder")
    memo.write_text("---\nname: offline\ndescription: written offline\nmetadata:\n  type: project\n---\n\n## Stand\n\nwritten offline → ✅\n\n## Keuzes\n", encoding="utf-8")
    sh, root = bash(), str(CLIENT / "plugin")
    env = user_env(home, CLAUDE_PLUGIN_ROOT=root, CLAUDE_PLUGIN_OPTION_URL="http://127.0.0.1:9", CLAUDE_PLUGIN_OPTION_TOKEN=tok,
                   CLAUDE_PROJECT_DIR=str(project))
    code, out, err = run([sh, "-c", 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/context.sh" 1'], "{}", env, cwd=str(project))
    check(code == 0 and "offline mode" in out and "garden-memory" in out, "server down: part 1 is the offline context", out[:400] + err)
    check(len(out) <= 10000, f"offline part 1 fits Claude Code's 10,000 characters ({len(out)})")
    check("→" in out or "project garden" in out, "offline part 1 carries the cached context")
    env["CLAUDE_PLUGIN_OPTION_URL"] = BASE
    code, out, err = run([sys.executable, str(CLIENT / "plugin" / "hooks" / "memory_mirror.py"), "--now"], "", {**env, "KNOWLEDGE_URL": BASE, "KNOWLEDGE_TOKEN": tok})
    check(code == 0 and (garden / "memory" / "project_offline.md").exists(), "back online: the offline memo goes up", err)
    check("written offline → ✅" in (garden / "memory" / "project_offline.md").read_text(encoding="utf-8"), "  with its text intact")


def link_dir(link: Path, target: Path):
    """A symlink, or on Windows a junction: what a user gets from mklink /J without admin rights."""
    if WIN:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def skills(tmp: Path, home: Path):
    """Shared skills both ways in ~/.claude/skills: arrive as real skills, a local one goes up, a linked folder is never
    written or emptied through, a skill removed on the server goes here too (folder included)."""
    server_skills, local = tmp / "data" / "claude" / "skills", home / ".claude" / "skills"
    (local / "mine").mkdir(parents=True)
    (local / "mine" / "SKILL.md").write_text("---\nname: mine\ndescription: test skill\n---\nmade on this device, é\n", encoding="utf-8")
    repo = home / "elsewhere" / "linked"
    repo.mkdir(parents=True)
    (repo / "SKILL.md").write_text("repo version\n", encoding="utf-8")
    link_dir(local / "linked", repo)
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN="owner-token", KNOWLEDGE_MIRROR=str(home / "mirror-owner"))
    mm = [sys.executable, str(CLIENT / "plugin" / "hooks" / "memory_mirror.py"), "--now"]
    log = home / "mirror-owner" / "last-sync.log"
    logged = lambda: log.read_text(encoding="utf-8") if log.exists() else ""
    for _ in range(2):
        code, out, err = run(mm, "", env)
    got = local / "s1" / "SKILL.md"
    check(code == 0 and got.exists() and "shared skill → ✅" in got.read_text(encoding="utf-8"), "skills: a shared skill arrives in ~/.claude/skills, text intact", err + logged())
    check((server_skills / "mine" / "SKILL.md").exists() and "é" in (server_skills / "mine" / "SKILL.md").read_text(encoding="utf-8"),
          "skills: a skill made on the device goes up", logged())
    check((repo / "SKILL.md").read_text(encoding="utf-8") == "repo version\n", "skills: never writes through a linked folder (junction on Windows)")
    for name in ("s1", "linked"):
        (server_skills / name / "SKILL.md").unlink()
        (server_skills / name).rmdir()
        http("POST", "/invalidate", {"paths": [f"claude/skills/{name}/SKILL.md"]}, {"Authorization": "Bearer watch-token"})
    code, out, err = run(mm, "", env)
    check(not (local / "s1").exists(), "skills: removed on the server, gone here with its folder", logged())
    check((repo / "SKILL.md").exists() and "failed" not in logged(), "skills: a linked folder is never emptied, the sync does not break", logged())
    # The PostToolUse hook (Git Bash on Windows) starts a sync when the edited file is a skill.
    (local / "mine" / "SKILL.md").write_text("---\nname: mine\ndescription: test skill\n---\nedited → ✅\n", encoding="utf-8")
    hook = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(local / "mine" / "SKILL.md")}})
    env2 = {**env, "CLAUDE_PLUGIN_ROOT": str(CLIENT / "plugin")}
    code, out, err = run([bash(), "-c", 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" "${CLAUDE_PLUGIN_ROOT}/hooks/memory_mirror.py" edited'], hook, env2)
    check(code == 0 and wait_for(lambda: "edited → ✅" in (server_skills / "mine" / "SKILL.md").read_text(encoding="utf-8")),
          "skills: an edit through the PostToolUse hook goes up", err + logged())
    # Many skills gone from the server at once (an emptied folder) are held back until confirmed.
    wait_for(lambda: not (home / "mirror-owner" / ".lock").exists())  # the hook's background sync is done
    bulk = [f"bulk{i}" for i in range(6)]
    for name in bulk:
        (server_skills / name).mkdir()
        (server_skills / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n", encoding="utf-8")
        http("POST", "/invalidate", {"paths": [f"claude/skills/{name}/SKILL.md"]}, {"Authorization": "Bearer watch-token"})
    run(mm, "", env)
    for name in bulk:
        (server_skills / name / "SKILL.md").unlink()
        (server_skills / name).rmdir()
        http("POST", "/invalidate", {"paths": [f"claude/skills/{name}/SKILL.md"]}, {"Authorization": "Bearer watch-token"})
    run(mm, "", env)
    check(all((local / n / "SKILL.md").exists() for n in bulk) and "held back" in logged(), "skills: 6 removed at once are held back", logged())
    run(mm, "", {**env, "KNOWLEDGE_ALLOW_REMOVALS": "1"})
    check(not any((local / n).exists() for n in bulk), "skills: removed after confirming (KNOWLEDGE_ALLOW_REMOVALS=1)")


def agents(tok: str, home: Path, project: Path, garden: Path):
    agent = CLIENT / "clients" / "knowledge-agent.py"
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, KNOWLEDGE_SETUP="script",  # the plugin path: installer()
                   KNOWLEDGE_PLUGIN_LATEST=str(latest_file(home)))
    (home / ".gemini" / "config").mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "config" / "mcp_config.json").write_text("", encoding="utf-8")  # agy leaves it empty on Windows
    for skill in sorted((REPO / "plugin" / "skills").glob("*/SKILL.md")):
        head = skill.read_text(encoding="utf-8").split("---")[1]
        meta = dict(l.split(": ", 1) for l in head.strip().splitlines() if ": " in l)
        # agentskills.io: name = folder, lowercase and hyphens, a description of at most 1024 characters; Codex refuses a skill without one
        check(meta.get("name") == skill.parent.name and re.fullmatch(r"[a-z0-9-]{1,64}", skill.parent.name) and 0 < len(meta.get("description", "")) <= 1024,
              f"skill {skill.parent.name}: valid name and description")
    for name, conf, rules in (("codex", home / ".codex" / "hooks.json", home / ".codex" / "AGENTS.md"),
                              ("agy", home / ".gemini" / "config" / "hooks.json", home / ".gemini" / "GEMINI.md")):
        for attempt in (1, 2):
            code, out, err = run([sys.executable, str(agent), "install", name], "", env)
            check(code == 0 and "token ok" in out, f"{name}: install run {attempt}", out + err)
        check(conf.exists() and rules.read_text(encoding="utf-8").count("knowledge:house-rules (written") == 1,
              f"{name}: hooks written, house rules block once after a rerun")
        check("Development rules" in rules.read_text(encoding="utf-8"), f"{name}: the rules file carries the development rules")
        shipped = sorted(d.name for d in (REPO / "plugin" / "skills").iterdir() if (d / "SKILL.md").exists())
        check(all((home / ".agents" / "skills" / s / "SKILL.md").exists() for s in shipped), f"{name}: all plugin skills in ~/.agents/skills", shipped)
        cfg = json.loads(conf.read_text(encoding="utf-8"))
        url_file = home / ".config" / "knowledge" / "url"
        check(url_file.exists() and url_file.read_text(encoding="utf-8").strip() == BASE, f"{name}: install stores the server URL")
        hook_env = {k: v for k, v in env.items() if k != "KNOWLEDGE_URL"}  # a self-hosted server: the hooks find the stored URL
        cmds = [h["command"] for g in (cfg.get("hooks") or cfg["knowledge"]).values() for e in g for h in (e.get("hooks") or [e])]
        payloads = {"context": {"hook_event_name": "SessionStart", "cwd": str(project), "workspacePaths": [str(project)]},
                    "docs-sync": {"hook_event_name": "PostToolUse", "cwd": str(project), "tool_input": {"command": "*** Begin Patch\n*** Update File: docs/taxi.md\n*** End Patch"}},
                    "session-log": {**usage_hook(home, name, f"{name}-usage"), "cwd": str(project), "workspacePaths": [str(project)]},
                    "hints": {"invocationNum": 1, "workspacePaths": [str(project)]},
                    "tasks": {"hook_event_name": "UserPromptSubmit", "session_id": f"{name}-1", "cwd": str(project), "prompt": "hi"},
                    "gate": {"hook_event_name": "PreToolUse", "session_id": f"{name}-1", "cwd": str(project), "tool_name": "spawn_agent",
                             "toolCall": {"name": "invoke_subagent"}},
                    "guard": {"hook_event_name": "PreToolUse", "cwd": str(project), "tool_name": "apply_patch",
                              "tool_input": {"command": "*** Begin Patch\n*** Add File: a.md\n+clean text\n*** End Patch"},
                              "toolCall": {"name": "write_file", "args": {"Path": "a.md", "Content": "clean text"}}},
                    "wake": {"hook_event_name": "PostToolUse", "session_id": f"{name}-1", "cwd": str(project),
                             "tool_name": "mcp__knowledge__knowledge_search"}}
        # How a program starts a hook command on Windows: PowerShell -Command, or cmd /d /s /c "<command>" as one
        # string (a list would make Python add its own quoting, which cmd reads differently).
        shells = [("powershell", lambda c: ["powershell", "-NoProfile", "-Command", c]), ("cmd", lambda c: f'cmd /d /s /c "{c}"')] \
            if WIN else [("sh", lambda c: [shutil.which("sh"), "-c", c])]
        for label, wrap in shells:
            for cmd in cmds:
                kind = next(k for k in payloads if f" {k} " in f" {cmd} " or cmd.split(" ")[-2:-1] == [k])
                code, out, err = run(wrap(cmd), json.dumps(payloads[kind]), hook_env, cwd=str(project))
                check(code == 0, f"{name} via {label}: {kind} hook runs", err[-600:])
                if kind == "gate":
                    check(out.strip() == "", f"{name} via {label}: below the limit the subagent hook prints nothing", out[:300])
                if kind == "guard":
                    check(out.strip() == "", f"{name} via {label}: a clean edit gets no word from secret-guard", out[:300])
                if kind == "context":
                    try:
                        msg = json.dumps(json.loads(out))
                        check("project garden" in msg, f"{name} via {label}: context JSON carries the guest context", out[:300])
                        own = {"codex": "Codex", "agy": "Gemini"}[name]
                        check(f"your open tasks ({own}" in msg, f"{name} via {label}: its own task comes first", msg[:300])
                        if label == shells[0][0]:  # the first start says it, later ones not
                            check("auto-update is on for " + name in msg, f"{name}: the script setup says auto-update is on", msg[-300:])
                    except ValueError:
                        check(False, f"{name} via {label}: context prints valid JSON", out[:300] + err[-300:])
                if kind == "hints":  # agy PostInvocation: {} or injectSteps, never anything else
                    try:
                        check(json.loads(out) == {} or "injectSteps" in json.loads(out), f"{name} via {label}: hints prints agy JSON", out[:300])
                    except ValueError:
                        check(False, f"{name} via {label}: hints prints valid JSON", out[:300] + err[-300:])
        check(usage_logged(home, name, f"{name}-usage"), f"{name}: session-log sends the session's tokens")
    new_task(agent, env, project, tok, home)
    resumed(home, project, garden)
    wake_e2e(tok, home.parent / "Wake Usér", project)
    st, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write", "arguments":
                    {"path": "garden-memory/leak.md", "content": "---\nname: l\ndescription: d\nmetadata:\n  type: project\n---\n## Stand\nSup3rSecret99\n## Keuzes\n"}}},
                    {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
    check("known secret" in body, "a guest (no local hashes): the server refuses a known secret", body[:300])
    st, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                    "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}}, {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
    check("## Status" in body and "anpitek" not in body and "to0wnn" not in body.lower() and "Dutch" not in body,
          "a new server: generic instructions (English sections, no other installation's projects or address)", body[:600])
    bare = {k: v for k, v in user_env(home.parent / "No Url").items() if k != "KNOWLEDGE_URL"}
    code, out, err = run([sys.executable, str(CLIENT / "clients" / "knowledge-agent.py"), "install", "codex"], "", {**bare, "KNOWLEDGE_TOKEN": "x"})
    check(code != 0 and "Which knowledge server" in out + err, "install without a known server asks for it, never a default", out + err)
    code, out, err = run([sys.executable, str(agent), "install", "codex"], "", user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN="wrong"))
    check(code != 0 and "401" in (out + err), "install with a wrong token stops with a clear 401 message", out + err)


def resumed(home: Path, project: Path, garden: Path):
    """A resumed session (agy --continue) runs no start hook: the per-turn hook alone must bring the server's orders.
    Each agent on its own device token, never synced: its first turn starts the full docs sync in the background, the
    server sees the move into an archive folder, and the next turn tells the archive notice to one session only."""
    hooks = CLIENT / "plugin" / "hooks" / "agent_hooks.py"
    admin = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice", "X-Knowledge-UI": "1"}
    for agent, cmd in (("claude", ["tasks", "claude"]), ("codex", ["tasks", "codex"]), ("agy", ["hints"])):
        st, body = http("POST", "/ui/api/admin/token", {"name": f"resume-{agent}", "write": True, "roots": ["garden", "garden-memory"]}, admin)
        env = user_env(home / f"resume {agent}", KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=json.loads(body)["token"])
        doc = project / "docs" / f"resume-{agent}.md"
        doc.write_text("still true: pushback needs a clearance\n", encoding="utf-8")
        http("POST", "/sync", {"files": {f"docs/{doc.name}": doc.read_text(encoding="utf-8")}},
             {"Authorization": f"Bearer {env['KNOWLEDGE_TOKEN']}"})
        (project / "docs" / "old").mkdir(exist_ok=True)
        doc.rename(project / "docs" / "old" / doc.name)
        turn = lambda sid: run([sys.executable, str(hooks), *cmd], json.dumps(
            {"hook_event_name": "UserPromptSubmit", "session_id": sid, "conversationId": sid, "cwd": str(project),
             "workspacePaths": [str(project)], "prompt": "go on"}), env, cwd=str(project))
        code, out, err = turn(f"{agent}-r1")
        check(code == 0 and wait_for(lambda: (garden / "docs" / "docs" / "old" / doc.name).exists(), 30),
              f"{agent} resumed: a turn runs the full docs sync the server asked for", out + err)
        code, out, err = turn(f"{agent}-r2")
        check(code == 0 and "went into an archive folder" in out and doc.name in out,
              f"{agent} resumed: the next turn tells the archive notice", out[-400:] + err[-300:])
        check("need a topic" in out, f"{agent} resumed: what the background sync printed is said too", out[-400:])
        code, out, err = turn(f"{agent}-r3")
        check("went into an archive folder" not in out, f"{agent} resumed: another session of the device is not told too", out[-300:])


def hook_errors(tmp: Path, project: Path):
    """A failing hook of ours is kept locally and reaches the Tasks tab through the next turn's POST /status, at most
    every two minutes; each agent on its own device token and home."""
    hooks = CLIENT / "plugin" / "hooks"
    admin = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice", "X-Knowledge-UI": "1"}
    sh = bash()
    for agent in ("claude", "codex", "agy"):
        st, body = http("POST", "/ui/api/admin/token", {"name": f"status-{agent}", "write": True, "roots": ["garden", "garden-memory"]}, admin)
        home = tmp / f"Status {agent} Usér"
        env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=json.loads(body)["token"])
        if agent == "claude":  # Claude Code starts every hook through py.sh: a crash of any script is recorded there
            code, out, err = run([sh, str(hooks / "py.sh"), str(hooks / "token_store.py"), "bogus"], "{}", env, cwd=str(project))
            fail, turn_cmd = "token_store.py bogus", [sh, str(hooks / "py.sh"), str(hooks / "agent_hooks.py"), "tasks", "claude"]
        else:  # Codex and agy start agent_hooks.py themselves: a hook that raises is recorded there
            code, out, err = run([sys.executable, str(hooks / "agent_hooks.py"), "context", agent], "not json", env, cwd=str(project))
            fail = f"agent_hooks.py context {agent}"
            turn_cmd = [sys.executable, str(hooks / "agent_hooks.py"), *(["tasks", "codex"] if agent == "codex" else ["hints"])]
        log = home / ".knowledge" / "hook-errors.jsonl"
        kept = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        check(any(e["hook"] == fail and e["code"] != "0" for e in kept), f"{agent}: a failing hook is kept in hook-errors.jsonl", json.dumps(kept)[:400] + err[-300:])
        if not WIN:
            check(log.exists() and log.stat().st_mode & 0o077 == 0, f"{agent}: hook-errors.jsonl is private (0600)")
        turn = lambda sid: run(turn_cmd, json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid, "conversationId": sid,
                                                     "cwd": str(project), "workspacePaths": [str(project)], "prompt": "go on"}), env, cwd=str(project))
        code, out, err = turn(f"{agent}-st1")
        sessions = json.loads(http("GET", "/ui/api/tasks", headers=admin)[1])["sessions"]
        mine = [s for s in sessions if s["device"] == f"status-{agent}" and s["agent"] == agent]
        check(code == 0 and mine and any(e["hook"] == fail and e["source"] == "knowledge" for e in mine[0]["errors"])
              and mine[0]["plugin"] in ("", json.loads((CLIENT / "plugin" / ".claude-plugin" / "plugin.json").read_text())["version"]),
              f"{agent}: the next turn reports it, the Tasks tab sessions carry it", json.dumps(mine)[:500] + err[-300:])
        sent = (home / ".knowledge" / "hook-status.json").read_text(encoding="utf-8")
        run([sys.executable, str(hooks / "agent_hooks.py"), "context", agent if agent != "claude" else "codex"], "not json", env, cwd=str(project))
        turn(f"{agent}-st2")
        check((home / ".knowledge" / "hook-status.json").read_text(encoding="utf-8") == sent,
              f"{agent}: within two minutes the next failure waits for a later turn", sent)
    # The server away: the send fails silently and the error is kept for the next try.
    home = tmp / "Status away Usér"
    env = user_env(home, KNOWLEDGE_URL="http://127.0.0.1:9", KNOWLEDGE_TOKEN="x")
    run([sys.executable, str(hooks / "agent_hooks.py"), "context", "codex"], "not json", env, cwd=str(project))
    code, out, err = run([sys.executable, str(hooks / "agent_hooks.py"), "tasks", "codex"],
                         json.dumps({"session_id": "away-1", "cwd": str(project), "prompt": "hi"}), env, cwd=str(project))
    state = json.loads((home / ".knowledge" / "hook-status.json").read_text(encoding="utf-8"))
    check(code == 0 and state.get("sent", 0) == 0 and state.get("at"), "server away: the turn goes on, the error stays unsent", json.dumps(state) + err[-300:])


FAKE_AGENT = """import json, os, sys
calls, name, args = os.environ["KN_FAKE_CALLS"], sys.argv[1], sys.argv[2:]
rec = lambda x: open(calls, "a", encoding="utf-8").write(json.dumps(x) + "\\n")
if name == "codex" and args[:1] == ["app-server"]:  # codex-rs app-server, as far as the companion uses it
    for line in sys.stdin:
        m = json.loads(line)
        rec({"rpc": m})
        if "id" not in m:
            continue
        res = {}
        if m["method"] == "hooks/list":
            res = {"data": [{"cwd": m["params"]["cwds"][0], "warnings": [], "errors": [], "hooks": [
                {"key": "knowledge@knowledge:.codex-plugin/hooks.json:session_start:0:0", "pluginId": "knowledge@knowledge",
                 "currentHash": "sha256:aa", "trustStatus": "untrusted", "handlerType": "command", "command": "x"},
                {"key": "knowledge@knowledge:.codex-plugin/hooks.json:stop:0:0", "pluginId": "knowledge@knowledge",
                 "currentHash": "sha256:bb", "trustStatus": "trusted", "handlerType": "command", "command": "x"},
                {"key": "/repo/.codex/hooks.json:stop:0:0", "pluginId": None, "currentHash": "sha256:cc",
                 "trustStatus": "untrusted", "handlerType": "command", "command": "./evil.sh"}]}]}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
else:
    rec({"argv": [name] + args, "cwd": os.getcwd()})
"""


def companion_e2e(tok: str, tmp: Path, project: Path):
    """The local companion (plugin/hooks/companion.py, started as `agent_hooks.py serve`) in a home of its own with fake
    agents: its local API answers only this pc's pages, it trusts the knowledge hooks the agents' own way (Codex through
    app-server hooks/list + config/batchWrite, again after a plugin update), YOLO writes each agent's native settings and
    unchecking restores them byte for byte (agy only while agy is closed), a ready task wakes the open Codex session
    once and starts an agy session where allowed, and hook failures the agents log themselves reach the Tasks tab."""
    hooks = CLIENT / "plugin" / "hooks"
    home = tmp / "Companion Usér"
    bin_dir, calls = home / "bin", home / "calls.jsonl"
    bin_dir.mkdir(parents=True)
    (bin_dir / "fakeagent.py").write_text(FAKE_AGENT, encoding="utf-8")
    for name in ("codex", "claude", "agy", "tmux"):
        if WIN:
            if name != "tmux":
                (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{bin_dir / "fakeagent.py"}" {name} %*\r\n', encoding="utf-8")
        else:
            (bin_dir / name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fakeagent.py"}" {name} "$@"\n', encoding="utf-8")
            (bin_dir / name).chmod(0o755)
    claude_json = home / ".claude.json"
    claude_json.write_text(json.dumps({"numStartups": 3, "projects": {}}), encoding="utf-8")
    (home / ".claude").mkdir()
    claude_settings = {"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"}
    (home / ".claude" / "settings.json").write_text(json.dumps(claude_settings), encoding="utf-8")
    codex_cfg = home / ".codex" / "config.toml"
    codex_cfg.parent.mkdir()
    toml = (f'# my codex settings\nmodel = "gpt-5"\napproval_policy = "on-request" # asked\n\n[notice]\nhide_rate_limit_model_nudge = true\n\n'
            f'[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n')
    codex_cfg.write_text(toml, encoding="utf-8")
    agy_dir = home / ".gemini" / "antigravity-cli"
    (agy_dir / "log").mkdir(parents=True)
    agy_settings = {"model": "Gemini", "trustedWorkspaces": [str(home)]}
    (agy_dir / "settings.json").write_text(json.dumps(agy_settings), encoding="utf-8")
    (agy_dir / "log" / "cli-1.log").write_text("I1031 10:00:00.100000       1 hooks_manager.go:53] loaded 2 named hooks\n"
                                               "E1031 10:00:01.200000       1 hooks.go:90] post-tool hook failed: exit status 1: agy-boom\n", encoding="utf-8")
    import sqlite3
    con = sqlite3.connect(str(home / ".codex" / "logs_2.sqlite"))
    con.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, level TEXT, feedback_log_body TEXT)")
    con.execute("INSERT INTO logs (ts, level, feedback_log_body) VALUES (?, 'INFO', 'turn started')", (int(time.time()),))
    con.execute("INSERT INTO logs (ts, level, feedback_log_body) VALUES (?, 'WARN', ?)",
                (int(time.time()), "after_agent hook failed; continuing turn_id=t1 hook_name=knowledge-stop error=exit code 1: codex-kaput"))
    con.commit()
    con.close()
    fake_agy = None
    if not WIN:  # a process named like agy, ended later: YOLO for agy waits for it
        shutil.copy(shutil.which("sleep"), bin_dir / "knfakeagy")
        fake_agy = subprocess.Popen([str(bin_dir / "knfakeagy"), "300"])
    port = PORT + 7
    self_url = f"http://127.0.0.1:{port}"
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                   KNOWLEDGE_COMPANION_PORT=str(port), KNOWLEDGE_COMPANION_EVERY="3600", KN_FAKE_CALLS=str(calls),
                   KNOWLEDGE_AGY_PROCESS="knfakeagy" if fake_agy else Path(sys.executable).stem)

    def call(method, path, body=None, origin=self_url, host=None, ctype="application/json"):
        headers = {**({"Origin": origin} if origin else {}), **({"Host": host} if host else {}), **({"Content-Type": ctype} if ctype else {})}
        req = urllib.request.Request(self_url + path, method=method, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8")
    recorded = lambda: [json.loads(ln) for ln in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []
    log = open(home / "companion.log", "wb")
    proc = subprocess.Popen([sys.executable, str(hooks / "agent_hooks.py"), "serve"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    try:
        up = wait_for(lambda: _quiet(lambda: call("GET", "/local/api/state")[0] == 200 and json.loads(call("GET", "/local/api/state")[2])["status"]["synced"]), 30)
        check(up, "companion: serve answers on 127.0.0.1 and its first round reached the server", (home / "companion.log").read_text(errors="replace")[-600:])
        if not up:
            return
        # Only this pc's pages: a foreign origin, a rebound host name, a POST without Origin or not as JSON get nothing.
        check(call("GET", "/local/api/hello", origin="https://evil.example")[0] == 403, "companion: a foreign origin is refused")
        check(call("GET", "/local/api/hello", origin=None, host="evil.example:%d" % port)[0] == 403, "companion: a foreign Host (DNS rebinding) is refused")
        st, hdr, body = call("GET", "/local/api/hello", origin=BASE)
        check(st == 200 and hdr.get("Access-Control-Allow-Origin") == BASE and "companion" in json.loads(body),
              "companion: hello answers the knowledge web UI's origin with CORS", f"{st} {hdr} {body}")
        st, hdr, _ = call("OPTIONS", "/local/api/hello", origin=BASE, ctype=None)
        check(st == 204 and hdr.get("Access-Control-Allow-Private-Network") == "true" and hdr.get("Access-Control-Allow-Origin") == BASE,
              "companion: the preflight allows Private Network Access", f"{st} {hdr}")
        check(call("POST", "/local/api/trust", {}, origin=None)[0] == 403 and call("POST", "/local/api/trust", {}, ctype="text/plain")[0] == 403,
              "companion: a POST without Origin or not as JSON is refused")
        check(call("POST", "/local/api/yolo", {"agent": "codex", "on": True}, origin=BASE)[0] == 403 and codex_cfg.read_text(encoding="utf-8") == toml,
              "companion: YOLO only from the companion's own page, not from the web UI")
        st, hdr, body = call("GET", "/", origin=None)
        check(st == 200 and "Knowledge companion" in body and "default-src 'self'" in hdr.get("Content-Security-Policy", ""),
              "companion: serves its page with a CSP", f"{st} {hdr}")
        # Hook trust, each agent its own native way; never the home folder, never a foreign hook.
        st, _, body = call("POST", "/local/api/trust", {})
        key = project.as_posix() if WIN else str(project)
        cj = json.loads(claude_json.read_text(encoding="utf-8"))
        check(st == 200 and cj["projects"].get(key, {}).get("hasTrustDialogAccepted") is True and cj["numStartups"] == 3
              and str(home) not in cj["projects"], "companion: Claude trusts the knowledge folder in ~/.claude.json, the rest kept", body + json.dumps(cj)[:300])
        ag = json.loads((agy_dir / "settings.json").read_text(encoding="utf-8"))
        check(ag["trustedWorkspaces"] == [str(home), str(project)] and ag["model"] == "Gemini", "companion: agy trusts the knowledge folder (trustedWorkspaces)", json.dumps(ag))
        writes = [c["rpc"] for c in recorded() if c.get("rpc", {}).get("method") == "config/batchWrite"]
        methods = [c["rpc"].get("method") for c in recorded() if "rpc" in c]
        check(methods[:3] == ["initialize", "initialized", "hooks/list"] and len(writes) == 1
              and writes[0]["params"]["edits"] == [{"keyPath": "hooks.state", "mergeStrategy": "upsert",
                                                     "value": {"knowledge@knowledge:.codex-plugin/hooks.json:session_start:0:0": {"trusted_hash": "sha256:aa"}}}]
              and writes[0]["params"]["reloadUserConfig"] is True,
              "companion: Codex trusts only the untrusted knowledge hooks via app-server, as codex-rs hooks_rpc.rs does", json.dumps(recorded())[:900])
        backups = list((home / "backups" / "knowledge-companion").glob("*/*"))
        check({".claude.json", ".gemini__antigravity-cli__settings.json"} <= {b.name.replace("\\", "__") for b in backups},
              "companion: a backup of each changed file first", str(backups))
        state = json.loads(call("GET", "/local/api/state")[2])
        check(all(a["hooksTrusted"] for a in state["agents"]), "companion: the page shows every agent trusted", json.dumps(state["agents"])[:600])
        # YOLO: the native settings, and unchecking puts back exactly what was there.
        call("POST", "/local/api/yolo", {"agent": "codex", "on": True})
        text = codex_cfg.read_text(encoding="utf-8")
        try:
            import tomllib
            parsed = tomllib.loads(text)
        except ImportError:
            parsed = {"approval_policy": "never", "sandbox_mode": "danger-full-access", "notice": {"hide_full_access_warning": True}, "model": "gpt-5"}
        check(parsed["approval_policy"] == "never" and parsed["sandbox_mode"] == "danger-full-access" and parsed["notice"]["hide_full_access_warning"] is True
              and parsed["model"] == "gpt-5" and text.startswith("# my codex settings\n"), "companion: YOLO on for Codex edits config.toml minimally", text)
        call("POST", "/local/api/yolo", {"agent": "codex", "on": False})
        check(codex_cfg.read_text(encoding="utf-8") == toml, "companion: YOLO off restores config.toml byte for byte", codex_cfg.read_text(encoding="utf-8"))
        call("POST", "/local/api/yolo", {"agent": "claude", "on": True})
        cs = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        check(cs["permissions"] == {"allow": ["Bash(ls)"], "defaultMode": "bypassPermissions"} and cs["skipDangerousModePermissionPrompt"] is True,
              "companion: YOLO on for Claude Code (bypassPermissions)", json.dumps(cs))
        call("POST", "/local/api/yolo", {"agent": "claude", "on": False})
        check(json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8")) == claude_settings, "companion: YOLO off restores Claude's settings")
        st, _, body = call("POST", "/local/api/yolo", {"agent": "agy", "on": True})
        check(json.loads(body).get("yolo") == "pending" and json.loads((agy_dir / "settings.json").read_text(encoding="utf-8")) == ag
              and next(a for a in json.loads(call("GET", "/local/api/state")[2])["agents"] if a["id"] == "agy")["yoloPending"] is True,
              "companion: YOLO for agy waits while agy runs, and the page says so", body)
        if fake_agy:
            fake_agy.kill()
            fake_agy.wait()
            call("POST", "/local/api/tick", {})
            on = json.loads((agy_dir / "settings.json").read_text(encoding="utf-8"))
            check(on.get("toolPermission") == "always-proceed" and on.get("agentMode") == "accept-edits" and on.get("enableTerminalSandbox") is False,
                  "companion: the queued YOLO for agy applies once agy is closed", json.dumps(on))
            call("POST", "/local/api/yolo", {"agent": "agy", "on": False})
            check(json.loads((agy_dir / "settings.json").read_text(encoding="utf-8")) == ag, "companion: YOLO off restores agy's settings")
        else:
            print("skip agy YOLO after closing agy (no stand-in process on Windows)")
        # Tasks: the open Codex session is woken once; agy has none and may start one; Claude may not.
        run([sys.executable, str(hooks / "agent_hooks.py"), "tasks", "codex"], json.dumps({"session_id": "cmp-cx", "cwd": str(project)}), env, cwd=str(project))
        tasks = "".join(f"- open tot 1 jan 2099 [{o}, CMP-{o.upper()}] from Claude, start now: companion test for {o}\n" for o in ("Codex", "Gemini", "Claude"))
        http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write", "arguments":
             {"path": "garden-memory/project_companion.md", "content": "---\nname: c\ndescription: d\nmetadata:\n  type: project\n---\n"
              f"## Stand\n{tasks}\n## Keuzes\n"}}}, {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
        call("POST", "/local/api/settings", {"startWhenNone": {"agy": True}})
        st, _, body = call("POST", "/local/api/tick", {})
        out = json.loads(body) if st == 200 else {}
        queued = [c["argv"] for c in recorded() if c.get("argv", [""])[:2] == ["codex", "queue"]]
        check(len(queued) == 1 and queued[0][2:4] == ["--thread", "cmp-cx"] and "CMP-CODEX" in queued[0][-1] and "CMP-GEMINI" not in queued[0][-1],
              "companion: a ready Codex task wakes the open Codex session through codex queue", body[:400] + str(queued)[:400])
        started = lambda: [c for c in recorded() if c.get("argv", [""])[0] == ("agy" if WIN else "tmux")]
        wait_for(lambda: started(), 15)
        st_agy = started()
        argv = st_agy[0]["argv"] if st_agy else []
        ready = json.loads(http("GET", "/tasks/ready", headers={"Authorization": f"Bearer {tok}"})[1])["tasks"]
        agy_ids = [t["id"] for t in ready if t["agent"] == "agy" and t["project"] == "garden"]
        check(len(st_agy) == 1 and "-i" in argv and "CMP-GEMINI" in agy_ids and all(i in argv[-1] for i in agy_ids)
              and (WIN and st_agy[0]["cwd"] == str(project) or not WIN and argv[1:3] == ["new-session", "-d"] and str(project) in argv),
              "companion: no agy session, start allowed: one new agy session in the project folder gets its ready tasks", json.dumps(st_agy)[:600])
        check(out.get("waiting", {}).get("garden/CMP-CLAUDE", "").startswith("No open session; starting one is off"),
              "companion: no Claude session and starting is off: the task waits and says why", body[:600])
        wakes = json.loads((tmp / "task_wakes.json").read_text(encoding="utf-8")).get("garden", {})
        check(wakes.get("CMP-CODEX", {}).get("session") == "cmp-cx" and wakes.get("CMP-CODEX", {}).get("agent") == "codex" and "CMP-GEMINI" in wakes,
              "companion: each wake is reported with POST /tasks/woken", json.dumps(wakes)[:400])
        n = len(recorded())
        call("POST", "/local/api/tick", {})
        check(len([c for c in recorded()[n:] if "argv" in c]) == 0, "companion: the same task is not woken again within 30 minutes", json.dumps(recorded()[n:])[:400])
        state = json.loads(call("GET", "/local/api/state")[2])
        check(any(t["id"] == "CMP-CLAUDE" for t in state["tasks"]["waiting"]) and any(t["id"] == "CMP-CODEX" for t in state["tasks"]["ready"])
              and state["agents"][1]["sessions"], "companion: the page lists ready and waiting tasks and the open session", json.dumps(state["tasks"])[:600])
        # Hook failures the agents log themselves: reported once, marked as agent-log.
        admin = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice"}
        sessions = json.loads(http("GET", "/ui/api/tasks", headers=admin)[1])["sessions"]
        errs = {s["agent"]: s.get("errors", []) for s in sessions if s["device"] == "friend-pc"}
        check(any(e["source"] == "agent-log" and e["hook"] == "knowledge-stop" and "codex-kaput" in e["stderr"] for e in errs.get("codex", []))
              and any(e["source"] == "agent-log" and "agy-boom" in e["stderr"] and e["event"] == "PostToolUse" for e in errs.get("agy", [])),
              "companion: hook failures from Codex's and agy's own logs reach the Tasks tab as agent-log", json.dumps(errs)[:800])
        before = json.dumps(errs)
        call("POST", "/local/api/tick", {})
        sessions = json.loads(http("GET", "/ui/api/tasks", headers=admin)[1])["sessions"]
        check(json.dumps({s["agent"]: s.get("errors", []) for s in sessions if s["device"] == "friend-pc"}) == before, "companion: a logged failure is reported once")
        # A plugin update (a new cache folder) makes the companion trust Codex's hooks again by itself.
        manifest = home / ".codex" / "plugins" / "cache" / "knowledge" / "knowledge" / "9.9.9" / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name": "knowledge", "version": "9.9.9"}', encoding="utf-8")
        call("POST", "/local/api/tick", {})
        writes = [c for c in recorded() if c.get("rpc", {}).get("method") == "config/batchWrite"]
        codex_state = next(a for a in json.loads(call("GET", "/local/api/state")[2])["agents"] if a["id"] == "codex")
        check(len(writes) == 2 and codex_state["version"] == "9.9.9" and codex_state["hooksTrusted"],
              "companion: after a Codex plugin update the hooks are trusted again automatically", json.dumps(codex_state)[:300])
    finally:
        proc.kill()
        if fake_agy:
            fake_agy.kill()


def _quiet(f):
    try:
        return f()
    except OSError:
        return False


def new_task(agent: Path, env: dict, project: Path, tok: str, home: Path):
    """A task handed to Codex while its session runs reaches the next prompt once; the first prompt only remembers."""
    ask = lambda: run([sys.executable, str(agent), "tasks", "codex", "-"],
                      json.dumps({"session_id": "long-1", "cwd": str(project), "prompt": "go on"}), env, cwd=str(project))[1]
    rewind = lambda: [f.write_text(json.dumps({**json.loads(f.read_text()), "at": 0})) for f in (home / ".knowledge" / "tasks-seen").glob("*.json")]
    check(ask() == "", "tasks: the first prompt of a session says nothing (the start context listed the open tasks)")
    http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write", "arguments":
         {"path": "garden-memory/project_handover.md", "content": "---\nname: h\ndescription: d\nmetadata:\n  type: project\n---\n"
          "## Stand\n- open tot 1 jan 2099 [Codex, NEW-1-int] from Claude, start now: install the clips\n## Keuzes\n"}}},
         {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
    check(ask() == "", "tasks: within two minutes of the last ask it does not ask again")
    rewind()
    out = ask()
    check("NEW-1-int" in out and json.loads(out)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit",
          "tasks: a task handed over meanwhile reaches the next prompt", out[:300])
    rewind()
    check(ask() == "", "tasks: said once, not at every prompt")


def wake_e2e(tok: str, home: Path, project: Path):
    """Agents on one machine wake each other: a knowledge write by one agent puts a new task in front of the newest
    session of each other agent (Codex through `codex queue`, agy through its language server, Claude through its
    asyncRewake waiter), once, and the admin page lists those sessions as wakeable."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    hooks = CLIENT / "plugin" / "hooks" / "agent_hooks.py"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    calls = home / "codex-calls.jsonl"
    (bin_dir / "fakecodex.py").write_text(f"import json, sys\nimport os\nopen({str(calls)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:] + [os.environ.get('CODEX_HOME', '')]) + '\\n')\n", encoding="utf-8")
    if WIN:
        (bin_dir / "codex.cmd").write_text(f'@"{sys.executable}" "{bin_dir / "fakecodex.py"}" %*\r\n', encoding="utf-8")
    else:
        (bin_dir / "codex").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fakecodex.py"}" "$@"\n', encoding="utf-8")
        (bin_dir / "codex").chmod(0o755)
    sent, csrf, busy = [], "gategategate", threading.Event()

    class LS(BaseHTTPRequestHandler):  # agy's language server, as far as the plugin uses it
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if busy.is_set():
                reply, code = {}, 503
            elif self.headers.get("x-codeium-csrf-token") != csrf:
                reply, code = {"code": "unauthenticated"}, 401
            elif self.path.endswith("/GetAllCascadeTrajectories"):
                reply, code = {"trajectorySummaries": {"conv-1": {"lastModifiedTime": "2026-10-27T08:00:00Z",
                               "workspaces": [{"workspaceFolderAbsoluteUri": project.as_uri()}]}}}, 200
            elif self.path.endswith("/SendUserCascadeMessage"):
                sent.append(body)
                reply, code = {}, 200
            else:
                reply, code = {}, 404
            data = json.dumps(reply).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    ls = ThreadingHTTPServer(("127.0.0.1", 0), LS)
    threading.Thread(target=ls.serve_forever, daemon=True).start()
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    hook = lambda args, payload, extra=None: run([sys.executable, str(hooks), *args], json.dumps(payload), {**env, **(extra or {})}, cwd=str(project))
    try:
        # each agent's session: its first prompt (Claude, Codex) or turn (agy) only remembers the open tasks
        hook(["tasks", "codex", "-"], {"session_id": "cx-1", "cwd": str(project)}, {"CODEX_HOME": str(home / "codex-b")})
        hook(["tasks", "claude", "-"], {"session_id": "cl-1", "cwd": str(project)})
        code, out, err = hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        check("agent_hooks.py" in out and " register" in out, "wake: an unregistered agy conversation is asked once to register", out[:300] + err[-300:])
        code, out, err = hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        check("register" not in out, "wake: the register request is not repeated", out[:300])
        code, out, err = hook(["register"], {}, {"ANTIGRAVITY_LS_ADDRESS": f"127.0.0.1:{ls.server_address[1]}", "ANTIGRAVITY_CSRF_TOKEN": csrf,
                                                 "ANTIGRAVITY_CONVERSATION_ID": "conv-1"})
        check(code == 0 and "registered" in out, "wake: agy registers from its terminal", out + err)
        admin = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice"}
        st, body = http("GET", "/ui/api/admin", None, admin)
        early = {c["session"]: c for t in json.loads(body)["tokens"] if t["name"] == "friend-pc" for c in t.get("clients", [])}
        check(early.get("conv-1", {}).get("wake") is True, "wake: the admin page shows agy wakeable right after register, not a turn later", str(early)[:300])
        code, out, err = hook(["register"], {})
        check(code != 0 and "not run by agy" in (out + err), "wake: register outside agy says so", out + err)
        entry = next((home / ".knowledge" / "agents").glob("agy-ls-*.json"))
        check(WIN or (entry.stat().st_mode & 0o077 == 0 and entry.parent.stat().st_mode & 0o077 == 0),
              "wake: agy's entry (its key) and the registry folder are private to the user")
        busy.set()
        hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        busy.clear()
        check(entry.exists(), "wake: a busy agy (HTTP 503) keeps its registration")
        started = time.time()
        code, out, err = hook(["wait", "claude", "-"], {"session_id": "cl-p", "cwd": str(project)}, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
        check(code == 0 and time.time() - started < 10, "wake: under claude -p the waiter exits at once (claude -p waits for it)", out + err)
        waiter = subprocess.Popen([sys.executable, str(hooks), "wait", "claude", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, cwd=str(project), text=True, encoding="utf-8")
        waiter.stdin.write(json.dumps({"session_id": "cl-1", "cwd": str(project)}))
        waiter.stdin.close()
        tasks = "".join(f"- open tot 1 jan 2099 [{o}, WAKE-{o.upper()}] from Claude, start now: wake test for {o} \"a & b > c %PATH%\n" for o in ("Codex", "Claude", "Gemini"))
        http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write", "arguments":
             {"path": "garden-memory/project_wake.md", "content": "---\nname: w\ndescription: d\nmetadata:\n  type: project\n---\n"
              f"## Stand\n{tasks}\n## Keuzes\n"}}}, {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
        code, out, err = hook(["wake", "codex", "-"], {"session_id": "sender", "cwd": str(project), "tool_name": "mcp__knowledge__knowledge_search"})
        check(out.strip() == "{}" and not calls.exists() and not sent, "wake: a read-only knowledge tool wakes nobody", out + err)
        code, out, err = hook(["wake", "codex", "-"], {"session_id": "sender", "cwd": str(project), "tool_name": "mcp__knowledge__knowledge_write"},
                              {"CODEX_HOME": ""})  # the waker's shell lacks the session's CODEX_HOME
        check(code == 0 and "told" in out and "codex" in out and "agy" in out, "wake: the sender hears whom it told", out + err)
        queued = [json.loads(l) for l in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []
        check(len(queued) == 1 and queued[0][:3] == ["queue", "--thread", "cx-1"] and "WAKE-CODEX" in queued[0][-2]
              and "WAKE-GEMINI" not in queued[0][-2] and queued[0][-1] == str(home / "codex-b"),
              "wake: Codex gets its own new task through codex queue, in the session's CODEX_HOME", str(queued)[:400])
        check(bool(queued) and {"c", "PATH"} <= set(re.findall(r"\w+", queued[0][-2])) and not (project / "c").exists(),
              "wake: a quote, & or > in a task reaches Codex as text, never as a command (Windows codex.cmd)", str(queued)[:400])
        check(len(sent) == 1 and sent[0]["cascadeId"] == "conv-1" and "WAKE-GEMINI" in sent[0]["items"][0]["text"],
              "wake: agy gets its own new task in its open conversation", str(sent)[:400])
        try:
            werr = waiter.communicate(timeout=20)[1]
        except subprocess.TimeoutExpired:
            waiter.kill()
            werr = "still waiting after 20 s"
        check(waiter.returncode == 2 and "WAKE-CLAUDE" in werr and "not from the user" in werr and "-done]" in werr,
              "wake: Claude's waiter wakes the session (exit 2) with its own new task", werr[-400:])
        code, out, err = hook(["wake", "agy", "-"], {"conversationId": "other", "workspacePaths": [str(project)],
                                                    "toolCall": {"name": "call_mcp_tool", "args": {"ToolName": "knowledge_edit"}}})
        check(out.strip() == "{}" and len(sent) == 1 and len(calls.read_text(encoding="utf-8").splitlines()) == 1,
              "wake: said once (a second write wakes nobody again); agy's PostToolUse answers {}", out + err)
        st, body = http("GET", "/ui/api/admin", None, admin)
        clients = {c["session"]: c for t in json.loads(body)["tokens"] if t["name"] == "friend-pc" for c in t.get("clients", [])}
        check(all(clients.get(s, {}).get("wake") for s in ("cx-1", "cl-1", "conv-1")) and clients["cx-1"]["agent"] == "codex"
              and clients["conv-1"]["project"] == "garden", "wake: the admin page lists the sessions, their agent, project and wakeable",
              json.dumps(clients)[:600])
        hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})  # registered: the ask is reset
        ls.shutdown(); ls.server_close()  # agy resumed: its old port is gone
        code, out, err = hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        check(" register" in out and "restarted" in out, "wake: a resumed agy (its registration gone) is told so and asked again",
              out[:300] + err[-300:])
        code, out, err = hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        check("register" not in out, "wake: the renewed request is said once", out[:300])
    finally:
        ls.shutdown()


def script_mcp(tok: str, home: Path):
    """The setup script writes no token into a config: agy runs the stdio relay, Codex the header helper (Linux/macOS) or
    the relay (Windows), and each configured relay answers a knowledge_search started exactly as configured. On Windows
    the token comes from the Credential Manager (cmdkey) with KNOWLEDGE_TOKEN unset, also for Claude's headersHelper."""
    import tomllib
    agy_file, codex_file = home / ".gemini" / "config" / "mcp_config.json", home / ".codex" / "config.toml"
    agy = json.loads(agy_file.read_text(encoding="utf-8"))["mcpServers"]["knowledge"]
    codex = tomllib.loads(codex_file.read_text(encoding="utf-8"))["mcp_servers"]["knowledge"]
    check(tok not in agy_file.read_text(encoding="utf-8") + codex_file.read_text(encoding="utf-8"),
          "setup: no token in agy's mcp_config.json or in Codex's config.toml")
    check(("command" in codex) == WIN and ("http_headers_helper" in codex) != WIN,
          "codex setup: the relay on Windows, the header helper elsewhere", json.dumps(codex))
    env = user_env(home, KNOWLEDGE_URL=BASE)  # no KNOWLEDGE_TOKEN: the relay finds the token itself
    if WIN:
        code, out, err = run(["cmdkey", "/generic:knowledge", "/user:knowledge", f"/pass:{tok}"], "", env)
        check(code == 0, "windows: cmdkey stores the token in the Credential Manager", out + err)
    else:
        env["KNOWLEDGE_TOKEN"] = tok  # CI runners have no keyring
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                          "clientInfo": {"name": "e2e", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "knowledge_search", "arguments": {"pattern": "taxi"}}}]
    try:
        for label, srv in (("agy", agy), ("codex", codex)) if WIN else (("agy", agy),):
            code, out, err = run([shutil.which(srv["command"]) or srv["command"], *srv["args"]],
                                 "".join(json.dumps(m) + "\n" for m in msgs), env, timeout=90)
            replies = {m.get("id"): m for m in map(json.loads, out.splitlines())} if code == 0 and out.strip() else {}
            check(set(replies) == {1, 2} and "taxi" in json.dumps(replies[2].get("result")),
                  f"{label} setup: the configured relay answers a knowledge_search", out[:400] + err[-400:])
        if WIN:  # Claude Code runs the plugin's headersHelper through Git Bash
            code, out, err = run([shutil.which("sh"), str(CLIENT / "plugin" / "hooks" / "token-header.sh")], "", env)
            check(f"Bearer {tok}" in out, "claude on windows: the headersHelper reads the token from the Credential Manager",
                  out[:100].replace(tok, "<token>") + err[-300:])
    finally:
        if WIN:
            run(["cmdkey", "/delete:knowledge"], "", env)


def latest_file(home: Path, version: str = "0.0.1") -> Path:
    """What the plugin compares its own version with (GitHub's plugin.json in real life); old = nothing to do."""
    path = home / "latest-plugin.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": version}), encoding="utf-8")
    return path


def self_update(env: dict, home: Path, agent: str):
    """The plugin's own auto-update with the real CLI: GitHub 'has' a newer version, the update runs and says so."""
    script = CLIENT / "plugin" / "hooks" / "self_update.py"
    code, out, err = run([sys.executable, str(script), "run", agent], "",
                         {**env, "KNOWLEDGE_PLUGIN_LATEST": str(latest_file(home, "99.0.0"))}, timeout=400)
    state = json.loads((home / ".knowledge" / "auto-update.json").read_text(encoding="utf-8"))
    check(code == 0 and "updated to 99.0.0" in state.get(f"{agent}:result", ""), f"{agent}: the plugin updates itself with the CLI",
          json.dumps(state)[-600:] + err[-300:])


def codex_plugin(tok: str, home: Path, project: Path):
    """The Codex plugin (plugin/.codex-plugin): every hook command the way Codex runs it (sh, or cmd with commandWindows,
    PLUGIN_ROOT in the environment), its stdio MCP server, and with the CLI: install from this repo's marketplace."""
    root = CLIENT / "plugin"
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    hooks = json.loads((root / manifest["hooks"]).read_text(encoding="utf-8"))["hooks"]
    path = pythonorg_path(home) if WIN else os.environ["PATH"]
    # No PLUGIN_ROOT in the environment: the commands must work from Codex's own ${PLUGIN_ROOT} substitution alone.
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, PATH=path, KNOWLEDGE_PLUGIN_LATEST=str(latest_file(home)))
    payloads = {"SessionStart": {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(project)},
                "PostToolUse": {"hook_event_name": "PostToolUse", "cwd": str(project), "tool_input": {"command": "*** Begin Patch\n*** Update File: docs/taxi.md\n*** End Patch"}},
                "PreCompact": {"hook_event_name": "PreCompact", "session_id": "cx-1", "cwd": str(project), "transcript_path": ""},
                "SessionEnd": {**usage_hook(home, "codex", "cx-usage"), "cwd": str(project)},
                "UserPromptSubmit": {"hook_event_name": "UserPromptSubmit", "session_id": "cx-3", "cwd": str(project), "prompt": "hi"},
                "PreToolUse": {"hook_event_name": "PreToolUse", "session_id": "cx-4", "cwd": str(project), "tool_name": "spawn_agent"}}
    check(set(hooks) == set(payloads) and len(hooks["SessionStart"][0]["hooks"]) == 1,
          "codex plugin: the six hook events, one SessionStart handler (openai/codex#45999)", str(list(hooks)))
    for event, groups in hooks.items():
        for hk in (h for g in groups for h in g["hooks"]):
            for i, (shell, argv) in enumerate(codex_hook_argvs(hk, root)):
                code, out, err = run(argv, json.dumps(payloads[event]), env, cwd=str(project), timeout=hk["timeout"] + 30)
                check(code == 0 and "skipped" not in err, f"codex plugin: {event} hook runs ({shell})", err[-600:])
                if event == "SessionStart":
                    ctx = json.loads(out or "{}").get("hookSpecificOutput", {}).get("additionalContext", "")
                    check("project garden" in ctx and "Search first" in ctx, f"codex plugin: SessionStart gives the guest context with the house rules ({shell})", out[:300] + err[-300:])
                    check("your open tasks (Codex" in ctx and "CODEX-1" in ctx.split("===== knowledge")[0],
                          f"codex plugin: its own task comes first ({shell})", ctx[:300])
                    if i == 0:  # said once, at the first start only
                        check("auto-update is on for codex" in ctx, "codex plugin: SessionStart says auto-update is on", ctx[-300:])
    check(usage_logged(home, "codex", "cx-usage"), "codex plugin: SessionEnd sends the tokens since the last log, once")
    if WIN:  # python.org without py and without the PATH box: python on PATH is the Store stub, the install is only in the registry
        hk = hooks["SessionStart"][0]["hooks"][0]
        with registered_python():
            code, out, err = run(codex_hook_argvs(hk, root)[0][1], json.dumps(payloads["SessionStart"]),
                                 {**env, "PATH": store_stub_path(home)}, cwd=str(project), timeout=hk["timeout"] + 30)
        check(code == 0 and "additionalContext" in out, "codex plugin: python3.cmd finds a registered Python past the Store stub", out[:300] + err[-600:])
    server = json.loads((root / manifest["mcpServers"]).read_text(encoding="utf-8"))["mcpServers"]["knowledge"]
    # Codex resolves ./python3 in the plugin folder (Windows: with PATHEXT, so python3.cmd).
    exe = str(root / server["cwd"] / (server["command"] + (".cmd" if WIN else "")))
    check(os.path.isfile(exe), f"codex plugin: the MCP command {server['command']} is in the plugin folder", exe)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                          "clientInfo": {"name": "e2e", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "knowledge_search", "arguments": {"pattern": "taxi"}}}]
    # Codex starts it in the plugin folder with only the env_vars it lists (plus HOME, PATH and the like).
    code, out, err = run([exe, *server["args"]], "".join(json.dumps(m) + "\n" for m in msgs),
                         env, cwd=str(root / server["cwd"]))
    replies = {m.get("id"): m for m in map(json.loads, out.splitlines())} if code == 0 and out.strip() else {}
    check(set(replies) == {1, 2} and "taxi" in json.dumps(replies[2].get("result")), "codex plugin: the stdio MCP server answers initialize and knowledge_search", out[:400] + err[-400:])
    code, out, err = run([exe, *server["args"]], json.dumps(msgs[2]) + "\n", {**env, "KNOWLEDGE_TOKEN": "wrong"}, cwd=str(root))
    check("token invalid" in out, "codex plugin: a wrong token answers with a plain reason", out[:300] + err[-300:])
    codex = shutil.which("codex")
    if not codex:
        return print("skip codex plugin install (codex not installed)")
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, PATH=path)
    (home / ".codex").mkdir(parents=True, exist_ok=True)  # Codex refuses a CODEX_HOME that does not exist
    code, out, err = run([codex, "plugin", "marketplace", "add", str(REPO)], "", env, timeout=120)
    check(code == 0, "codex: marketplace add from this repo", out + err)
    code, out, err = run([codex, "plugin", "add", "knowledge@knowledge"], "", env, timeout=120)
    check(code == 0, "codex: plugin add knowledge@knowledge", out + err)
    code, out, err = run([codex, "mcp", "list"], "", env, timeout=60)
    check("agent_hooks.py mcp" in out, "codex: mcp list shows the plugin's knowledge server", out + err)
    self_update(env, home, "codex")
    # The hooks as Codex reads them (app-server hooks/list): all of them parse and wait for the user's trust (/hooks).
    reply = app_server(codex, env, "hooks/list", {"cwds": [str(project)]})
    data = reply.get("result", {}).get("data", [])
    listed = [h for e in data for h in e["hooks"] if h.get("pluginId") == "knowledge@knowledge"]
    problems = [w for e in data for w in e["warnings"] + [x["message"] for x in e["errors"]]]
    check(len(listed) == 8 and not problems, "codex: hooks/list shows the plugin's eight hooks, no warnings",
          f"{len(listed)} hooks, problems {problems}: " + json.dumps(reply)[:800])
    # The companion trusts them the way Codex's own /hooks screen does, and Codex then reports them trusted.
    code, out, err = run([sys.executable, str(root / "hooks" / "companion.py"), "trust", "codex"], "", env, timeout=120)
    reply = app_server(codex, env, "hooks/list", {"cwds": [str(project)]})
    mine = [h for e in reply.get("result", {}).get("data", []) for h in e["hooks"] if h.get("pluginId") == "knowledge@knowledge"]
    check(code == 0 and mine and all(h["trustStatus"] == "trusted" for h in mine), "codex: the companion trusts the plugin's hooks through app-server",
          out + err[-400:] + json.dumps([(h["key"], h["trustStatus"]) for h in mine])[:600])
    # Codex itself starts the plugin's MCP server (on Windows with a python.org PATH: py, no python3) and lists its tools.
    reply = app_server(codex, env, "mcpServerStatus/list", {"detail": "full"}, timeout=90)
    entry = next((e for e in reply.get("result", {}).get("data", []) if "knowledge" in e.get("name", "")), {})
    check("knowledge_search" in json.dumps(entry), "codex: starts the plugin's MCP server and lists knowledge_search", json.dumps(reply)[:1500])


def agy_go_start(env: dict, home: Path, root: Path, mcp: dict, stdin: str):
    """agy's own start of the plugin's MCP command, imitated in Go."""
    # agy (Go) starts mcp_config.json's command as given, with Dir = the plugin folder (seen on the VM, 24 Oct 2026:
    # $0 = ./python3; ${PLUGIN_ROOT} and ${extensionPath} are not filled in). agy needs a login, so a Go program
    # starts it the same way. On Windows ./python3 cannot work (Go hands cmd.exe "./python3.cmd"), so the command
    # stays python3 from PATH, and install agy checks that a real python3 is there.
    go = shutil.which("go")
    if not go:
        return print("skip agy MCP start through Go (go not installed)")
    src = home / "gostart" / "main.go"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("""package main

import (
	"os"
	"os/exec"
)

func main() {
	cmd := exec.Command(os.Args[2], os.Args[3:]...)
	cmd.Dir = os.Args[1]
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		os.Stderr.WriteString(err.Error() + "\\n")
		os.Exit(1)
	}
}
""", encoding="utf-8")
    exe = home / "gostart" / ("gostart.exe" if WIN else "gostart")
    code, out, err = run([go, "build", "-o", str(exe), str(src)], "", {**os.environ, "GO111MODULE": "off"}, timeout=300)
    check(code == 0, "agy plugin: Go starter builds", err[-600:])
    code, out, err = run([str(exe), str(root), mcp["command"], *mcp["args"]], stdin, env, cwd=str(home))
    replies = {r.get("id"): r for r in map(json.loads, out.splitlines())} if code == 0 else {}
    check("taxi" in json.dumps(replies.get(2, {})), f"agy plugin: Go starts {mcp['command']} for the plugin and it answers a knowledge_search", out[-600:] + err[-600:])
    if WIN:  # why install agy checks for python3: a python.org PATH with only py cannot start it
        code, out, err = run([str(exe), str(root), mcp["command"], *mcp["args"]], stdin, {**env, "PATH": pythonorg_path(home)}, cwd=str(home))
        check(code != 0 and "taxi" not in out, "agy plugin: python3 does not start on a PATH with only py", out[-300:] + err[-300:])
        code, out, err = run([sys.executable, str(CLIENT / "clients" / "knowledge-agent.py"), "install", "agy"], "",
                             {**env, "PATH": pythonorg_path(home)}, timeout=120)
        conf = home / ".gemini" / "config" / "mcp_config.json"
        check(code == 0 and "script setup" in out and "knowledge" in (conf.read_text(encoding="utf-8") if conf.exists() else ""),
              "install agy: without python3 on Windows it says so and uses the script setup", (out + err)[-800:])

def app_server(codex: str, env: dict, method: str, params: dict, timeout=60) -> dict:
    """One request to `codex app-server` (JSON-RPC over stdio) after the handshake; its reply, or {} on a timeout."""
    p = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                         text=True, encoding="utf-8")
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    try:
        for m in ({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "e2e", "version": "0"}}}, {"method": "initialized"},
                  {"id": 2, "method": method, "params": params}):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", **m}) + "\n")
        p.stdin.flush()
        return next((m for m in map(json.loads, p.stdout) if m.get("id") == 2), {})
    finally:
        timer.cancel()
        p.kill()


def pythonorg_path(home: Path) -> str:
    """PATH as on a pc with Python from python.org: the py launcher, no python3 (and here no python either). A py.cmd
    stands in for the launcher; every folder that holds a python.exe or python3.exe leaves PATH."""
    shim = home / "pylauncher"
    shim.mkdir(parents=True, exist_ok=True)
    (shim / "py.cmd").write_text(f'@"{sys.executable}" %*\r\n', encoding="utf-8")
    keep = [d for d in os.environ["PATH"].split(os.pathsep)
            if d and not any((Path(d) / n).exists() for n in ("python.exe", "python3.exe"))]
    return os.pathsep.join([str(shim), *keep])


def codex_hook_argvs(hk: dict, root: Path) -> list:
    """A plugin hook the way Codex runs it (codex-rs hooks discovery.rs, command_runner.rs): commandWindows on Windows,
    ${PLUGIN_ROOT} replaced in the text, then the session's shell: PowerShell (-NoProfile -Command <line>) as on most
    Windows pcs, or cmd (/c "<line>"); elsewhere sh -c."""
    if not WIN:
        return [("sh", [shutil.which("sh"), "-c", hk["command"].replace("${PLUGIN_ROOT}", str(root))])]
    line = hk["commandWindows"].replace("${PLUGIN_ROOT}", str(root))
    shells = [(n, [shutil.which(n), "-NoProfile", "-Command", line]) for n in ("powershell", "pwsh") if shutil.which(n)]
    return shells + [("cmd", f'cmd.exe /c "{line}"')]


def store_stub_path(home: Path) -> str:
    """PATH with no py and no real Python, only a python.cmd that fails like the Microsoft Store stub."""
    stub = home / "storestub"
    stub.mkdir(parents=True, exist_ok=True)
    (stub / "python.cmd").write_text("@echo Python was not found; run without arguments to install from the Microsoft Store 1>&2\r\n@exit /b 9009\r\n", encoding="utf-8")
    keep = [d for d in os.environ["PATH"].split(os.pathsep)
            if d and not any((Path(d) / n).exists() for n in ("python.exe", "python3.exe", "py.exe"))]
    return os.pathsep.join([str(stub), *keep])


@contextlib.contextmanager
def registered_python():
    """This Python registered under HKCU the way the python.org installer does it (PEP 514), removed afterwards."""
    import winreg
    key = r"SOFTWARE\Python\KnowledgeE2E\3.99"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + r"\InstallPath") as k:
        winreg.SetValueEx(k, "ExecutablePath", 0, winreg.REG_SZ, sys.executable)
    try:
        yield
    finally:
        for sub in (key + r"\InstallPath", key, r"SOFTWARE\Python\KnowledgeE2E"):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)


def agy_plugin(tok: str, home: Path, project: Path, garden: Path):
    """The agy plugin install path: agy reads plugin/hooks.json and plugin/mcp_config.json (not Claude's files) and runs
    each hook command with sh -c (Windows: cmd /c) in the plugin folder; with agy installed, validate and install it."""
    root = CLIENT / "plugin"
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, KNOWLEDGE_PLUGIN_LATEST=str(latest_file(home)))
    (home / ".knowledge" / "auto-update.json").unlink(missing_ok=True)  # agents() used this home for the script setup
    mcp = json.loads((root / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"]["knowledge"]
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                                                           "clientInfo": {"name": "e2e", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "knowledge_search", "arguments": {"pattern": "taxi"}}}]
    stdin = "".join(json.dumps(m) + "\n" for m in msgs)
    code, out, err = run([sys.executable, *mcp["args"]], stdin, env, cwd=str(root))
    replies = {r.get("id"): r for r in map(json.loads, out.splitlines())} if code == 0 else {}
    check("result" in replies.get(1, {}) and "taxi" in json.dumps(replies.get(2, {})), "agy plugin: stdio MCP bridge answers a knowledge_search", out[-600:] + err[-600:])
    agy_go_start(env, home, root, mcp, stdin)
    check("headers" not in mcp and "env" not in mcp, "agy plugin: no token in mcp_config.json (the bridge reads KNOWLEDGE_TOKEN or the keyring)")
    hooks = json.loads((root / "hooks.json").read_text(encoding="utf-8"))["knowledge"]
    cmds = [h["command"] for e in hooks.values() for g in e for h in (g.get("hooks") or [g])]
    common = {"conversationId": "agy-plugin", "workspacePaths": [str(project)]}
    payloads = {"context": common, "session-log": {**common, **usage_hook(home, "agy", "agy-plugin-usage")}, "hints": {**common, "invocationNum": 1},
                "docs-sync": {**common, "toolCall": {"name": "write_file", "args": {"Path": json.dumps(str(project / "docs" / "agy.md"))}}},
                "wake": {**common, "toolCall": {"name": "call_mcp_tool", "args": {"ToolName": "knowledge_search"}}},
                "gate": {**common, "toolCall": {"name": "invoke_subagent"}},
                "guard": {**common, "toolCall": {"name": "write_file", "args": {"Path": "a.md", "Content": "clean text"}}}}
    (project / "docs" / "agy.md").write_text("agy plugin → ✅\n", encoding="utf-8")
    for cmd in cmds:
        kind = next(k for k in payloads if f" {k}" in cmd)
        code, out, err = run(f'cmd /d /s /c "{cmd}"' if WIN else [shutil.which("sh"), "-c", cmd], json.dumps(payloads[kind]), env, cwd=str(root))
        check(code == 0 and "skipped" not in err, f"agy plugin: {kind} hook runs in the plugin folder", err[-600:])
        if kind == "gate":  # agy reads any output, even {}, as a verdict: {} denied every subagent (27 Oct 2026)
            check(out.strip() == "", "agy plugin: below the limit the subagent hook prints nothing", out[:300])
        if kind == "guard":
            check(out.strip() == "", "agy plugin: a clean edit gets no word from secret-guard", out[:300])
        if kind == "context":
            try:
                msg = json.loads(out)["injectSteps"][0]["systemMessage"]["systemMessage"]
                check("project garden" in msg, "agy plugin: context arrives as injectSteps with the guest context", out[:300])
                check("your open tasks (Gemini" in msg and "GEMINI-1" in msg.split("===== knowledge")[0],
                      "agy plugin: its own task comes first", msg[:300])
                check("auto-update is on for agy" in msg, "agy plugin: context says auto-update is on", msg[-300:])
            except (ValueError, KeyError, IndexError):
                check(False, "agy plugin: context prints injectSteps JSON", out[:300] + err[-300:])
        if kind == "hints":  # PostInvocation after the write: the pending topic hint (or {} when the project has no headings)
            try:
                got = json.loads(out)
                check(got == {} or "agy.md" in got["injectSteps"][0]["systemMessage"]["systemMessage"],
                      "agy plugin: PostInvocation shows docs-sync's pending hint as injectSteps", out[:300])
            except (ValueError, KeyError, IndexError):
                check(False, "agy plugin: hints prints injectSteps JSON or {}", out[:300] + err[-300:])
    check((garden / "docs" / "docs" / "agy.md").exists(), "agy plugin: PostToolUse mirrors the written file")
    check(usage_logged(home, "agy", "agy-plugin-usage"), "agy plugin: session-log sends the session's tokens")
    agy = shutil.which("agy")
    if not agy:
        print("skip agy plugin validate/install (agy not installed)")
        return
    code, out, err = run([agy, "plugin", "validate", str(root), "--json"], "", env, timeout=60)
    check(code == 0, "agy: plugin validate, no errors", out + err)
    code, out, err = run([agy, "plugin", "install", str(root)], "", env, timeout=120)
    check(code == 0 and "[ok]" in out, "agy: plugin install from this repo", out + err)
    self_update({**env, "KNOWLEDGE_AGY_PLUGIN": str(root)}, home, "agy")
    code, out, err = run([agy, "plugin", "list", "--json"], "", env, timeout=60)
    got = next((x for x in json.loads(out or "[]") if x.get("name") == "knowledge"), {}) if code == 0 else {}
    check(bool(got.get("enabled")) and got.get("mcpServers") == ["knowledge"] and got.get("hooks") == ["knowledge"],
          "agy: plugin listed and enabled, with its MCP server and hooks", out + err)


def installer(tok: str, tmp: Path):
    """knowledge-agent.py install as a person runs it: the keyring of the OS stores and returns a token (macOS Keychain,
    Windows Credential Manager; CI's Linux has no keyring), and with the CLIs installed, install claude/codex set up the
    plugin from this repo; Codex on Linux/macOS first removes an older setup of this script."""
    agent = CLIENT / "clients" / "knowledge-agent.py"
    sys.path.insert(0, str(CLIENT / "plugin" / "hooks"))
    import token_store
    if (WIN or sys.platform == "darwin") and os.environ.get("CI"):  # on a real machine this would replace its token
        try:
            token_store.store("e2e-store-test")
            check(token_store.from_keyring() == "e2e-store-test", f"keyring: store and read back on {sys.platform}")
        finally:
            run(["cmdkey", "/delete:knowledge"] if WIN else ["security", "delete-generic-password", "-s", "knowledge"], "")
    home = tmp / "Install Usér"
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, KNOWLEDGE_MARKETPLACE=str(REPO))
    if shutil.which("claude"):
        code, out, err = run([sys.executable, str(agent), "install", "claude"], "", env, timeout=300)
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8")) if code == 0 else {}
        check(code == 0 and settings.get("autoMemoryEnabled") is False, "install claude: plugin installed, auto memory off", (out + err)[-1500:])
        market = settings.get("extraKnownMarketplaces", {}).get("knowledge", {})
        check(market.get("autoUpdate") is True and market.get("source"), "install claude: auto-update on for the marketplace",
              json.dumps(market))
        code, out, err = run([shutil.which("claude"), "plugin", "list"], "", env, timeout=60)
        check("knowledge@knowledge" in out, "install claude: claude plugin list shows knowledge", out + err)
        code, out, err = run([shutil.which("claude"), "plugin", "configure", "knowledge@knowledge", "--json"], "", env, timeout=60)
        check(BASE in out, "install claude: the plugin's url option is the server it was installed against", out + err)
        # the token must land where Claude reads it (Windows over SSH has no keyring either); macOS keeps it in the Keychain
        if sys.platform != "darwin" and not token_store.from_keyring():
            stored = json.loads((home / ".claude" / ".credentials.json").read_text(encoding="utf-8")) if (home / ".claude" / ".credentials.json").exists() else {}
            check(stored.get("pluginSecrets", {}).get("knowledge@knowledge", {}).get("token") == tok,
                  "install claude without a keyring: the token is saved as Claude's plugin option", json.dumps(stored)[:300])
    if shutil.which("codex"):  # Windows too: the plugin since 0.9.3
        (home / ".codex" / "config.toml").write_text('[mcp_servers.knowledge]\nurl = "x"\nbearer_token_env_var = "KNOWLEDGE_TOKEN"\n', encoding="utf-8")
        (home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "python3 knowledge-agent.py context codex -"}]}]}}), encoding="utf-8")
        code, out, err = run([sys.executable, str(agent), "install", "codex"], "", env, timeout=300)
        conf = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
        hooks = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        check(code == 0 and "[mcp_servers.knowledge]" not in conf and not hooks.get("hooks"),
              "install codex: the old setup is removed first", (out + err + conf)[-1500:])
        code, out, err = run([shutil.which("codex"), "plugin", "list"], "", env, timeout=60)
        check(any(ln.startswith("knowledge@knowledge") and "installed" in ln for ln in out.splitlines()),
              "install codex: the plugin is installed", out + err)
    have = [a for a in ("claude", "codex") if shutil.which(a)]
    if have:  # agy is not installed on the CI runners
        code, out, err = run([sys.executable, str(agent), "setup"], "", env, timeout=600)
        check(code == 0 and "Already set up (" in out and all(f"== {a} ==" in out for a in have) and "== agy ==" not in out
              and "== companion (" in out and re.search(r'companion\.py"? trust', out),
              f"setup (the one-line installer), once set up: every agent with the plugin is updated ({', '.join(have)}), no questions", (out + err)[-1500:])
        # Set up before the URL was stored (review 27 Oct 2026): the token must not go to the default server.
        url_file, settings_path = home / ".config" / "knowledge" / "url", home / ".claude" / "settings.json"
        saved = (url_file.read_bytes() if url_file.exists() else None, settings_path.read_bytes() if settings_path.exists() else None)
        url_file.unlink(missing_ok=True)
        if saved[1]:
            data = json.loads(saved[1])
            data.get("pluginConfigs", {}).pop("knowledge@knowledge", None)
            settings_path.write_text(json.dumps(data), encoding="utf-8")
        bare = {k: v for k, v in env.items() if k != "KNOWLEDGE_URL"}
        for cmd in ("setup", "update"):
            code, out, err = run([sys.executable, str(agent), cmd], "", bare, timeout=120)
            check(code != 0 and "Which knowledge server?" in out + err and "== " not in out,
                  f"{cmd} with a token but no known server asks for it, never uses the default", (out + err)[-800:])
        for path, data in zip((url_file, settings_path), saved):
            if data is not None:
                path.write_bytes(data)
        code, out, err = run([sys.executable, str(agent), "install"], "", env)
        check(code != 0 and "Found" in (out + err) and "name one" in (out + err),
              "install without an agent and without a terminal names what it found and asks to choose", out + err)


def companion_setup(tmp: Path):
    """install-companion: autostart at logon, a menu shortcut to the page and a first hook-trust run. The dry run lists
    each OS's steps; on this OS it runs for real against fake systemctl/launchctl/schtasks/powershell, a rerun only
    finds everything unchanged, and uninstall-companion removes it again."""
    agent = CLIENT / "clients" / "knowledge-agent.py"
    home = tmp / "Autostart Usér"
    env = user_env(home, APPDATA=str(home / "AppData" / "Roaming"), XDG_DATA_HOME="", XDG_CONFIG_HOME="")
    want = {"linux": ["knowledge-companion.service", "Restart=on-failure", "Environment=", "systemctl --user enable",
                      "systemctl --user restart", "knowledge-companion.desktop", "Exec="],
            "darwin": ["LaunchAgents", "<key>RunAtLoad</key><true/>", "launchctl bootstrap", "Knowledge companion.app", "exec open"],
            "windows": ["<LogonTrigger>", "<LogonType>InteractiveToken</LogonType>", "pythonw.exe", "schtasks /Create",
                        "schtasks /Run", "Start Menu"]}
    for system, parts in want.items():
        code, out, err = run([sys.executable, str(agent), "install-companion", "--dry-run", "--os", system], "", env)
        missing = [p for p in parts + ["127.0.0.1:47321"] if p not in out] + ([] if re.search(r'companion\.py"? trust', out) else ["trust"])
        check(code == 0 and not missing, f"install-companion --dry-run --os {system}: lists autostart, shortcut and trust", f"missing {missing}\n{out[-1500:]}{err}")
        if system == "windows":
            task = out.split("schtasks")[0]
            check(not any(w in task.lower() for w in ("powershell", "-encodedcommand", "hidden")),
                  "install-companion windows: the logon task starts pythonw directly, no (hidden or encoded) PowerShell", task)
        check(not home.exists() or not any(home.rglob("*")), f"install-companion --dry-run --os {system}: writes nothing")
        code, out, err = run([sys.executable, str(agent), "uninstall-companion", "--dry-run", "--os", system], "", env)
        check(code == 0, f"uninstall-companion --dry-run --os {system}", out + err)

    fake, log = tmp / "fake tools", tmp / "fake tools.log"
    fake.mkdir(exist_ok=True)
    for tool in ("systemctl", "launchctl", "schtasks", "powershell"):
        if WIN:
            (fake / f"{tool}.cmd").write_text(f'@echo {tool} %*>>"%KN_FAKE_LOG%"\r\n', encoding="utf-8")
        else:
            (fake / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "$KN_FAKE_LOG"\n', encoding="utf-8")
            (fake / tool).chmod(0o755)
    # only the fakes and Python: the trust step finds no agent CLI in this home, so it only says so
    real = {k: v for k, v in env.items() if k != "KNOWLEDGE_COMPANION_DRY_RUN"}
    real.update(PATH=os.pathsep.join([str(fake), str(Path(sys.executable).parent)]), KN_FAKE_LOG=str(log))
    system = "windows" if WIN else "darwin" if sys.platform == "darwin" else "linux"
    files = {"linux": [".config/systemd/user/knowledge-companion.service", ".local/share/applications/knowledge-companion.desktop"],
             "darwin": ["Library/LaunchAgents/io.github.knowledge.companion.plist",
                        "Applications/Knowledge companion.app/Contents/MacOS/knowledge-companion"],
             "windows": [".knowledge/companion/autostart.xml"]}[system]
    code, out, err = run([sys.executable, str(agent), "install-companion"], "", real, timeout=120)
    calls = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    expect = {"linux": "systemctl --user enable knowledge-companion.service", "darwin": "launchctl bootstrap gui/",
              "windows": "schtasks /Create /F /TN"}[system]
    check(code == 0 and all((home / f).is_file() for f in files) and expect in calls and "runs at logon" in out,
          f"install-companion on {system}: writes the autostart and shortcut and registers it", out + err + calls)
    if WIN:
        check("CreateShortcut" in calls or (home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Knowledge companion.url").is_file(),
              "install-companion on windows: a Start menu shortcut (Edge/Chrome app window, else a .url)", calls)
    code, out, err = run([sys.executable, str(agent), "install-companion"], "", real, timeout=120)
    check(code == 0 and "unchanged" in out and "wrote" not in out and "updated " not in out,
          f"install-companion on {system}: a rerun only finds its files unchanged", out + err)
    log.unlink(missing_ok=True)
    code, out, err = run([sys.executable, str(agent), "uninstall-companion"], "", real, timeout=120)
    calls = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    undo = {"linux": "systemctl --user disable --now", "darwin": "launchctl bootout", "windows": "schtasks /Delete /F /TN"}[system]
    check(code == 0 and not any((home / f).exists() for f in files) and undo in calls
          and not (home / "Applications" / "Knowledge companion.app").exists(),
          f"uninstall-companion on {system}: autostart and shortcut are gone", out + err + calls)


def relay_errors(home: Path):
    """The stdio MCP relay survives a server that answers HTML instead of JSON: every request (also in a batch)
    gets an error reply and the relay keeps serving (review 23 Oct 2026: it used to exit and leave the session without
    knowledge tools)."""
    import http.server

    class Bad(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "20")
            self.end_headers()
            self.wfile.write(b"<html>maintenance</html>"[:20])

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bad)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = user_env(home, KNOWLEDGE_URL=f"http://127.0.0.1:{srv.server_address[1]}", KNOWLEDGE_TOKEN="x")
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
             [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}],
             {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}]
    stdin = "".join(json.dumps(x) + "\n" for x in lines)
    code, out, err = run([sys.executable, "-X", "utf8", str(CLIENT / "plugin" / "hooks" / "agent_hooks.py"), "mcp"], stdin, env, timeout=60)
    srv.shutdown()
    ids = sorted(json.loads(x)["id"] for x in out.splitlines() if x.strip())
    check(ids == [1, 2, 3] and "skipped" not in err, "relay: a non-JSON reply and a batch each get an error, the relay keeps serving", out + err)


def real_clis(tok: str, home: Path):
    """The real Claude Code and Codex CLIs, when installed: they accept what we install."""
    claude, codex = shutil.which("claude"), shutil.which("codex")
    if os.environ.get("CI") and not (claude and codex):
        check(False, "CI installs the Claude Code and Codex CLIs")  # never a silent skip in CI
    if not claude and not codex:
        print("skip real CLIs (not installed)")
        return
    env = user_env(home, KNOWLEDGE_TOKEN=tok)
    if claude:
        code, out, err = run([claude, "plugin", "marketplace", "add", str(REPO)], "", env, timeout=120)
        check(code == 0, "claude: marketplace add from this repo", out + err)
        code, out, err = run([claude, "plugin", "install", "knowledge@knowledge", "--scope", "user"], "", env, timeout=180)
        check(code == 0, "claude: plugin install", out + err)
        code, out, err = run([claude, "plugin", "list"], "", env, timeout=60)
        check("knowledge@knowledge" in out and "enabled" in out, "claude: plugin listed and enabled", out + err)
    if codex:
        code, out, err = run([codex, "mcp", "list"], "", env, timeout=60)
        check("knowledge" in out, "codex: mcp list shows the knowledge server", out + err)


def plugin_versions(tmp: Path):
    """Each agent's session start told the server which plugin version it runs (admin page, weekly report)."""
    want = json.loads((REPO / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
    seen = json.loads((tmp / "plugin_seen.json").read_text(encoding="utf-8")) if (tmp / "plugin_seen.json").exists() else {}
    got = {a: s["v"] for name, agents in seen.items() if name != "_latest" for a, s in agents.items()}
    for agent in ("claude", "codex", "agy"):
        check(got.get(agent) == want, f"{agent}: session start reports plugin version {want}", json.dumps(seen)[:300])


def claude_stored_token(tmp: Path):
    """A machine without a keyring keeps the token only in Claude's plugin option (a container): the setup and the
    Codex/agy hooks, which run outside Claude, find it where Claude Code stored it."""
    home = tmp / "Claude Only"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"pluginSecrets": {"knowledge@knowledge": {"token": "gategategate"}}}), encoding="utf-8")
    (home / ".claude" / "settings.json").write_text(json.dumps({"pluginConfigs": {"knowledge@knowledge": {"options": {"url": "http://example.invalid:1"}}}}), encoding="utf-8")
    env = user_env(home, PATH=str(Path(sys.executable).parent))  # no secret-tool or security: no keyring
    code, out, err = run([sys.executable, "-c", "import token_store as t; print(t.token(), t.url())"], env=env,
                         cwd=CLIENT / "plugin" / "hooks")
    check(out.split() == ["gategategate", "http://example.invalid:1"],
          "token and server from Claude's stored plugin option (no keyring)", out + err)
    probe = "import token_store as t; print(t.url_is_default(), t.url_is_default(claude=True))"
    code, out, err = run([sys.executable, "-c", probe], env=env, cwd=CLIENT / "plugin" / "hooks")
    code2, out2, err2 = run([sys.executable, "-c", probe], env=user_env(tmp / "Nothing Set", PATH=str(Path(sys.executable).parent)),
                            cwd=CLIENT / "plugin" / "hooks")
    check(out.split() == ["False", "False"] and out2.split() == ["True", "True"],
          "url_is_default: false once a server is named, true when only the default is left", out + err + out2 + err2)


def checkout_update(tmp: Path):
    """The setup's git checkout after a rewritten history (force-push): update resets it to GitHub's main, keeps the
    old HEAD as a branch, and refuses when a tracked file is changed (a pull would stop with conflicts everywhere)."""
    def git(cwd, *args):
        return run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main", *args],
                   cwd=cwd, timeout=120)
    origin, dev, setup = tmp / "origin.git", tmp / "dev clone", tmp / "setup clone"
    shutil.copytree(CLIENT, dev, ignore=shutil.ignore_patterns("__pycache__"))
    git(dev, "init", "-q")
    git(dev, "add", "-A")
    git(dev, "commit", "-qm", "one")
    git(tmp, "clone", "-q", "--bare", str(dev), str(origin))
    git(tmp, "clone", "-q", str(origin), str(setup))

    def rewrite(msg):
        git(dev, "commit", "-q", "--amend", "-m", msg)
        git(dev, "push", "-q", "--force", str(origin), "HEAD:main")
        return git(dev, "rev-parse", "HEAD")[1].strip()
    old, new = git(setup, "rev-parse", "HEAD")[1].strip(), rewrite("one, rewritten")
    backups = lambda: git(setup, "branch", "--list", "knowledge-before-update-*", "--format=%(refname:short)")[1].split()
    path = os.pathsep.join({str(Path(shutil.which("git")).parent), str(Path(sys.executable).parent),
                            *([] if WIN else ["/usr/bin", "/bin"])})  # sh and coreutils, no agent CLIs
    env = {**os.environ, "HOME": str(tmp), "USERPROFILE": str(tmp), "KNOWLEDGE_TOKEN": "", "KNOWLEDGE_URL": BASE, "PATH": path}
    code, out, err = run([sys.executable, str(setup / "clients" / "knowledge-agent.py"), "update"], env=env, timeout=120)
    check(git(setup, "rev-parse", "HEAD")[1].strip() == new and "up to date (version" in out
          and [git(setup, "rev-parse", b)[1].strip() for b in backups()] == [old],
          "update: a rewritten history resets the checkout, old HEAD kept as a branch", (out + err)[-600:])
    # A file git does not track that the new history adds: reset --hard would overwrite it without a copy.
    (dev / "notes.txt").write_text("upstream\n", encoding="utf-8")
    git(dev, "add", "notes.txt")
    rewrite("one, rewritten with notes")
    (setup / "notes.txt").write_text("mine\n", encoding="utf-8")
    code, out, err = run([sys.executable, str(setup / "clients" / "knowledge-agent.py"), "update"], env=env, timeout=120)
    check(code != 0 and (setup / "notes.txt").read_text(encoding="utf-8") == "mine\n",
          "update: refuses a rewrite that would overwrite a file git does not track", (out + err)[-600:])
    (setup / "notes.txt").unlink()
    code, out, err = run([sys.executable, str(setup / "clients" / "knowledge-agent.py"), "update"], env=env, timeout=120)
    branches = backups()
    check(code == 0 or "up to date (version" in out, "update: goes on once that file is moved away", (out + err)[-600:])
    check(len(branches) == 2, "update: every reset keeps its own backup branch", " ".join(branches))
    changed = setup / "clients" / "knowledge-agent.py"
    changed.write_text(changed.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    rewrite("one, rewritten again")
    code, out, err = run([sys.executable, str(setup / "clients" / "knowledge-agent.py"), "update"], env=env, timeout=120)
    check(code != 0 and "local changes" in out + err and changed.read_text(encoding="utf-8").endswith("# mine\n"),
          "update: refuses a rewritten history when a tracked file is changed", (out + err)[-600:])

    # The one-line setup (install.sh through a pipe, install.ps1 as a script block): first a clone, then a checkout
    # where someone's git pull stopped with conflicts after a rewrite (25 Oct 2026 on a Windows client).
    fresh = tmp / "one line"
    shell = shutil.which("powershell") if WIN else shutil.which("sh")
    def one_line(cmd, where=fresh):
        if WIN:
            argv = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                    f"& ([scriptblock]::Create((Get-Content -Raw '{REPO / 'install.ps1'}'))) {cmd}"]
        else:
            argv = [shell, "-c", f"sh -s {cmd} < '{REPO / 'install.sh'}'"]
        return run(argv, env={**env, "KNOWLEDGE_REPO": str(origin), "KNOWLEDGE_DIR": str(where)}, timeout=180)
    notes = tmp / "my notes"  # someone's own ~/knowledge, another repository
    git(tmp, "clone", "-q", str(dev), str(notes))
    head = git(notes, "rev-parse", "HEAD")[1]
    code, out, err = one_line("", notes)
    check(code != 0 and "another repository" in out + err and git(notes, "rev-parse", "HEAD")[1] == head,
          "one-line setup: leaves another repository in that folder alone", (out + err)[-600:])
    code, out, err = one_line("")
    if not check(fresh.exists(), "one-line setup: the checkout is cloned", (out + err)[-700:]):
        return
    check(git(fresh, "rev-parse", "HEAD")[1].strip() == git(dev, "rev-parse", "HEAD")[1].strip() and "installed version" in out
          and "None of claude, codex or agy is on PATH" in out + err, "one-line setup: clones the scripts, then runs the setup", (out + err)[-700:])
    (dev / "x.txt").write_text("1\n", encoding="utf-8")
    git(dev, "add", "x.txt")
    git(dev, "commit", "-qm", "two")
    git(dev, "push", "-q", str(origin), "HEAD:main")
    git(fresh, "pull", "-q")
    (dev / "x.txt").write_text("2\n", encoding="utf-8")
    git(dev, "add", "x.txt")
    new = rewrite("two, rewritten")
    git(fresh, "pull", "-q", "--no-rebase")
    stuck = (fresh / ".git" / "MERGE_HEAD").exists()
    code, out, err = one_line("update")
    check(stuck and git(fresh, "rev-parse", "HEAD")[1].strip() == new and not (fresh / ".git" / "MERGE_HEAD").exists()
          and (fresh / "x.txt").read_text(encoding="utf-8") == "2\n" and "up to date (version" in out,
          "one-line setup: repairs a pull that stopped with conflicts after a rewrite", f"stuck={stuck} " + (out + err)[-600:])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # this report only; the clients run as a stock Windows has them
    tmp = Path(tempfile.mkdtemp(prefix="kn e2e "))
    home = tmp / "Test Usér"
    project = home / "A350 Copilot"
    (project / "docs").mkdir(parents=True)
    (project / ".knowledge-project").write_bytes("garden\r\n".encode("utf-16"))  # PowerShell 5: echo garden > .knowledge-project
    (project / "README.md").write_text("# Copilot\n\nvoice first officer → ✅\n", encoding="utf-8")
    (project / "docs" / "taxi.md").write_text("taxi speed\n", encoding="utf-8")
    global CLIENT
    CLIENT = home / "knowledge repo"
    for part in ("plugin", "clients"):
        shutil.copytree(REPO / part, CLIENT / part, ignore=shutil.ignore_patterns("__pycache__"))
    proc, tok, garden = start_server(tmp)
    try:
        print(f"platform {sys.platform}, python {sys.version.split()[0]}, home {home}")
        mirror = claude_plugin(tok, home, project, garden)
        folder_link(tmp / "Link Usér")
        offline(tok, home, project, garden, mirror)
        skills(tmp, home)
        agents(tok, home, project, garden)
        script_mcp(tok, home)
        codex_plugin(tok, tmp / "Codex Usér", project)  # its own home: the plugin next to the setup's server would clash
        agy_plugin(tok, home, project, garden)
        limits_e2e(tok, tmp)
        plugin_versions(tmp)
        relay_errors(home)
        hook_errors(tmp, project)
        companion_e2e(tok, tmp, project)
        installer(tok, tmp)
        companion_setup(tmp)
        checkout_update(tmp)
        claude_stored_token(tmp)
        real_clis(tok, home)
    finally:
        proc.kill()
        if failures:
            print("server log tail:\n" + (tmp / "server.log").read_text(errors="replace")[-2000:])
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
