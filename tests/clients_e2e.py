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
import socket
import socketserver
import tempfile
import http.server as http_server  # not 'import http': that hides http() below
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLIENT = REPO  # replaced in main() by a copy under a path with a space, like C:\Users\Test Usér\...
def free_port() -> int:
    """A port nobody uses now, so several checks can run at once (parallel agents, a commit hook)."""
    # shortcut: another process could take the port between close() and the server binding it; retry if that ever bites.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
WIN = os.name == "nt"
failures = []


def _bind_without_dns(self):
    """The stand-in servers below: HTTPServer's own bind asks reverse DNS for 127.0.0.1, about 35 s on a macOS runner."""
    socketserver.TCPServer.server_bind(self)
    self.server_name, self.server_port = self.server_address[:2]


http_server.HTTPServer.server_bind = _bind_without_dns


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


def state_of(home: Path, name: str):
    """A state file the companion may be replacing right now: on Windows opening it then gives PermissionError for a moment."""
    for _ in range(40):
        with contextlib.suppress(PermissionError):
            return json.loads((home / ".knowledge" / "companion" / name).read_text(encoding="utf-8"))
        time.sleep(0.05)
    return json.loads((home / ".knowledge" / "companion" / name).read_text(encoding="utf-8"))


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


def no_session_log_e2e(tok: str, tmp: Path):
    """KNOWLEDGE_NO_SESSION_LOG keeps a scripted run out of the session log, through each agent's own hook path; without
    it the same run is logged, so the check cannot pass by accident."""
    home = tmp / "NoLog"
    hooks = REPO / "plugin" / "hooks"
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "XDG_CACHE_HOME": str(home / ".cache"),
           "KNOWLEDGE_URL": BASE, "KNOWLEDGE_TOKEN": tok}
    env.pop("KNOWLEDGE_NO_SESSION_LOG", None)
    for agent, cmd in (("claude", [str(hooks / "session-log.py")]), ("codex", [str(hooks / "agent_hooks.py"), "session-log"]),
                       ("agy", [str(hooks / "agent_hooks.py"), "session-log"])):
        for sid, extra in ((f"{agent}-scripted", {"KNOWLEDGE_NO_SESSION_LOG": "1"}), (f"{agent}-normal", {})):
            hook = {**usage_hook(home, agent, sid), "cwd": str(tmp)}
            subprocess.run([sys.executable, "-X", "utf8", *cmd], input=json.dumps(hook), env={**env, **extra}, text=True,
                           encoding="utf-8", capture_output=True, timeout=60)
        rows = (tmp / "usage.jsonl").read_text(encoding="utf-8") if (tmp / "usage.jsonl").exists() else ""
        check(f"{agent}-scripted" not in rows, f"{agent}: KNOWLEDGE_NO_SESSION_LOG keeps a scripted run out of the session log")
        check(usage_logged(home, agent, f"{agent}-normal"), f"{agent}: without it the same run is logged")


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
            # the last three were missed until 1 Nov 2026: & ? # and a literal \n did not split the word
            for text in "pw=gategategate", "https://x/api?token=gategategate&page=2", "pw=gategategate&x=1", '"pw": "gategategate\\n"':
                code, out, err = go(guard, {**hook, **edit(text)})
                check(code == 0 and '"deny"' in out and "secret set NAME" in out, f"{label}: an edit with a known secret is refused ({text})", out[:400] + err[-400:])
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
    (tmp / "tokens.json").write_text(json.dumps({"owner": {"sha256": h("owner-token"), "write": True, "skills": True, "self": True},
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
           "KNOWLEDGE_ALLOWED_HOSTS": f"127.0.0.1:{PORT}", "PYTHONUTF8": "1", "KNOWLEDGE_WORKER_FOLLOW_SECONDS": "3", "KNOWLEDGE_CHAT_ASK_MINUTE": "3",
           "KNOWLEDGE_GITHUB_RAW": "http://127.0.0.1:9"}  # no newest-version lookup on GitHub from a test
    log = open(tmp / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, str(REPO / "server" / "server.py")], env=env, stdout=log, stderr=subprocess.STDOUT)
    if not wait_for(lambda: _health(), 60):
        proc.kill()
        raise SystemExit("server did not start:\n" + (tmp / "server.log").read_text(errors="replace")[-3000:])
    admin = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice", "X-Knowledge-UI": "1"}
    assert http("POST", "/ui/api/admin/project", {"name": "garden"}, admin)[0] == 200
    # a default (English) server hands "+ Add memo" its English sections; the Dutch ones are server/test_server.py's
    assert json.loads(http("GET", "/ui/api/me", headers=admin)[1])["sections"] == ["## Status", "## Decisions"]
    # the admin's own device ("self"): its task lines are trusted, its machines the admin's
    st, body = http("POST", "/ui/api/admin/token", {"name": "friend-pc", "write": True, "redact": True, "self": True,
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
               "UserPromptSubmit": {"hook_event_name": "UserPromptSubmit", "session_id": "s3", "cwd": str(project), "prompt": "hi"},
               "PermissionRequest": {"hook_event_name": "PermissionRequest", "session_id": "s4", "cwd": str(project), "tool_name": "Bash", "tool_input": {"command": "ls"}},
               "Stop": {"hook_event_name": "Stop", "session_id": "s4", "cwd": str(project)},
               "Notification": {"hook_event_name": "Notification", "notification_type": "idle_prompt", "session_id": "s4", "cwd": str(project)}}
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
        for event in ("SessionStart", "PostToolUse", "PreToolUse", "SessionEnd", "UserPromptSubmit", "PermissionRequest", "Stop", "Notification"):
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
        check("cannot reach" not in start and "skipped" not in start, f"[{mode}] SessionStart: no error notice", start[:500])
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
    check("## Status / ## Decisions" in out, "offline context names the English memo sections", out[:400])
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
    code, out, err = run([sys.executable, str(agent), "--selftest"], "", env)
    check(code == 0 and out.strip() == "ok", "knowledge-agent.py self-test (Codex config.toml: the old knowledge table goes with its subtables, nothing else)", out + err)
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
                    "asks": {"session_id": f"{name}-1", "conversationId": f"{name}-1", "cwd": str(project), "workspacePaths": [str(project)], "tool_name": "Bash", "toolCall": {"name": "run_command"}},
                    "settled": {"session_id": f"{name}-1", "conversationId": f"{name}-1", "cwd": str(project)},
                    "gate": {"hook_event_name": "PreToolUse", "session_id": f"{name}-1", "cwd": str(project), "tool_name": "spawn_agent",
                             "toolCall": {"name": "invoke_subagent"}},
                    "guard": {"hook_event_name": "PreToolUse", "cwd": str(project), "tool_name": "apply_patch",
                              "tool_input": {"command": "*** Begin Patch\n*** Add File: a.md\n+clean text\n*** End Patch"},
                              "toolCall": {"name": "write_file", "args": {"Path": "a.md", "Content": "clean text"}}},
                    "wake": {"hook_event_name": "PostToolUse", "session_id": f"{name}-1", "cwd": str(project),
                             "tool_name": "mcp__knowledge__knowledge_search"},
                    "watch": {"hook_event_name": "PostToolUse", "session_id": f"{name}-1", "cwd": str(project), "tool_name": "Bash",
                              "tool_input": {"command": "rm -rf ~/x"}}}
        # How a program starts a hook command on Windows: PowerShell -Command, or cmd /d /s /c "<command>" as one
        # string (a list would make Python add its own quoting, which cmd reads differently).
        shells = [("powershell", lambda c: ["powershell", "-NoProfile", "-Command", c]), ("cmd", lambda c: f'cmd /d /s /c "{c}"')] \
            if WIN else [("sh", lambda c: [shutil.which("sh"), "-c", c])]
        for label, wrap in shells:
            for cmd in cmds:
                kind = next(k for k in payloads if f" {k} " in f" {cmd} " or cmd.split(" ")[-2:-1] == [k])
                code, out, err = run(wrap(cmd), json.dumps(payloads[kind]), hook_env, cwd=str(project))
                check(code == 0, f"{name} via {label}: {kind} hook runs", err[-600:])
                if kind in ("asks", "settled"):
                    check(out.strip() == "", f"{name} via {label}: the {kind} hook prints nothing, so it decides nothing", out[:300])
                if kind == "gate":
                    check(out.strip() == "", f"{name} via {label}: below the limit the subagent hook prints nothing", out[:300])
                if kind == "watch":
                    check(out.strip() == "{}", f"{name} via {label}: no YOLO, no worker: the watch hook says nothing", out[:300])
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
        st, body = http("POST", "/ui/api/admin/token", {"name": f"resume-{agent}", "write": True, "self": True, "roots": ["garden", "garden-memory"]}, admin)
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
        said = home / f"resume {agent}" / ".knowledge" / "background.txt"  # the sync writes what it printed after the move: wait for it
        wait_for(lambda: said.exists() and "need a topic" in said.read_text(encoding="utf-8", errors="replace"), 30)  # (a slow Windows runner raced it)
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
        st, body = http("POST", "/ui/api/admin/token", {"name": f"status-{agent}", "write": True, "self": True, "roots": ["garden", "garden-memory"]}, admin)
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
        mine = [a for r in machines() if f"status-{agent}" in r["tokens"] for n, a in r["agents"].items() if n == agent]
        check(code == 0 and mine and any(e["hook"] == fail and e["source"] == "knowledge" for e in mine[0]["errors"])
              and mine[0].get("plugin", "") in ("", json.loads((CLIENT / "plugin" / ".claude-plugin" / "plugin.json").read_text())["version"]),
              f"{agent}: the next turn reports it, the Machines list carries it", json.dumps(mine)[:500] + err[-300:])
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


FAKE_AGENT = """import io, json, os, re, sys, time
calls, name, args = os.environ["KN_FAKE_CALLS"], sys.argv[1], sys.argv[2:]
# the server asks for a chat title on a chat's first turn: noted once (title_ask), then dropped, so checks see the message
_ASK = re.compile("(" + re.escape(chr(92) + "n" + chr(92) + "n") + "|" + chr(10) * 2 + ")" + re.escape("(knowledge web UI: ") + "[^)]*" + re.escape(")"))
def _cut(text):
    out = _ASK.sub("", text)
    if out != text:
        open(calls, "a", encoding="utf-8").write(json.dumps({"title_ask": os.environ.get("KNOWLEDGE_CHAT", "")}) + chr(10))
    return out
class _In:
    def __init__(self, f):
        self.f, self.buffer = f, self
    def readline(self):
        return _cut(self.f.readline())
    def __iter__(self):
        for ln in self.f:
            yield _cut(ln)
    def read(self):
        return _cut(self.f.buffer.read().decode("utf-8")).encode("utf-8")
sys.stdin = _In(sys.stdin)
def look():  # LOOK: the agent looks at an image (the chat shows it under the tool line)
    f = os.path.abspath(calls + "." + name + ".png")
    open(f, "wb").write(b"PNG " + name.encode())
    return f
rec = lambda x: open(calls, "a", encoding="utf-8").write(json.dumps(x) + "\\n")
if args == ["update"]:  # the companion's agent update
    rec({"update": name})
    print("agent update " + name)
    sys.exit(1 if os.environ.get("KN_FAKE_UPDATE_FAIL") == name else 0)
if name == "codex" and args == ["debug", "models"]:  # what a chat may pick: one listed model, one hidden
    print(json.dumps({"models": [{"slug": "fake-big", "display_name": "Fake Big", "visibility": "list", "default_reasoning_level": "high", "supported_reasoning_levels": [{"effort": "high"}, {"effort": "ultra"}]},
                                 {"slug": "fake-hidden", "visibility": "hide", "supported_reasoning_levels": []}]}))
    sys.exit(0)
if name == "agy" and args == ["models"]:
    print("Fetching available models...\\nfake-gem\\tFake Gem")
    sys.exit(0)
if name == "codex" and args[:1] == ["app-server"] and os.environ.get("KN_FAKE_RATE"):  # the companion asks for the limits (that check only)
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {}}), flush=True)
        elif m.get("method") == "account/rateLimits/read":
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"rateLimits": {"primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": int(time.time()) + 3600}}}}), flush=True)
    sys.exit(0)
if name == "codex" and args[:1] == ["app-server"] and os.environ.get("KNOWLEDGE_CHAT"):  # a chat: app-server v2, live (turns in one process)
    import queue, threading
    send = lambda x: print(json.dumps({"jsonrpc": "2.0", **x}), flush=True)
    note = lambda method, params: send({"method": method, "params": params})
    inbox, answers = queue.Queue(), queue.Queue()

    def reader():
        for ln in sys.stdin:
            if ln.strip():
                m = json.loads(ln)
                (answers if "method" not in m else inbox).put(m)
        inbox.put(None)
    threading.Thread(target=reader, daemon=True).start()
    loaded, perms, turns = "", {}, 0
    while (m := inbox.get()) is not None:
        if m.get("method") == "initialize":
            send({"id": m["id"], "result": {"userAgent": "fake"}})
        elif m.get("method") == "config/read":  # Default named: the configured model and effort
            send({"id": m["id"], "result": {"config": {"model": "fake-default", "model_reasoning_effort": "medium"}}})
        elif m.get("method") in ("thread/start", "thread/resume"):
            loaded, perms = m["params"].get("threadId", ""), m["params"]
            send({"id": m["id"], "result": {"thread": {"id": "sess-codex"}}})
        elif m.get("method") == "skills/list":  # slash commands: its skills (Codex 0.167's shape)
            send({"id": m["id"], "result": {"data": [{"cwd": m["params"]["cwds"][0], "skills": [
                {"name": "fake-skill", "description": "A fake skill\\nmore", "path": "/skills/fake-skill/SKILL.md", "scope": "user", "enabled": True}]}]}})
        elif m.get("method") == "thread/compact/start":  # its own compaction: a turn of its own
            turns += 1
            rec({"chat": os.environ["KNOWLEDGE_CHAT"], "compact": True, "pid": os.getpid()})
            send({"id": m["id"], "result": {}})
            note("turn/started", {"threadId": "sess-codex", "turn": {"id": f"t{turns}", "status": "inProgress"}})
            note("turn/completed", {"threadId": "sess-codex", "turn": {"id": f"t{turns}", "status": "completed"}})
        elif m.get("method") in ("turn/steer", "turn/interrupt"):  # no turn runs: as codex-rs, an error
            send({"id": m["id"], "error": {"code": -32600, "message": "no active turn"}})
        elif m.get("method") == "turn/start":
            turns += 1
            tid, texts = f"t{turns}", [next(x["text"] for x in m["params"]["input"] if x["type"] == "text")]
            rec({"chat": os.environ["KNOWLEDGE_CHAT"], "argv": [name] + args, "cwd": os.getcwd(), "stdin": texts[0], "load": loaded, "perms": perms, "pid": os.getpid(),
                 "input": m["params"]["input"],
                 "pick": [m["params"].get("model"), m["params"].get("effort")]})
            if "LATE" in texts[0]:  # the answer takes a while: a Stop comes before the turn is known
                time.sleep(2)
            send({"id": m["id"], "result": {"turn": {"id": tid, "status": "inProgress"}}})
            note("turn/started", {"threadId": "sess-codex", "turn": {"id": tid, "status": "inProgress"}})
            note("item/started", {"item": {"type": "commandExecution", "command": "git status"}})
            if "LOOK" in texts[0]:
                note("item/completed", {"item": {"type": "imageView", "id": "iv1", "path": look()}})
            status, verdict = "completed", ""
            if "ASK" in texts[0]:
                send({"id": 0, "method": "item/commandExecution/requestApproval", "params": {"reason": "run the tests", "command": "/bin/sh -lc 'npm test'",
                      "commandActions": [{"type": "unknown", "command": "npm test"}], "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"]}})
                got = answers.get()["result"]["decision"]
                verdict = " | npm test ran" if got == "accept" else " | npm test denied"
            if "SLOW" in texts[0]:  # works until steered to the end (DONE) or interrupted
                end = time.time() + 90
                while time.time() < end:
                    try:
                        x = inbox.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if x is None:
                        break
                    if x.get("method") == "turn/steer" and x["params"].get("expectedTurnId") == tid:
                        texts.append(x["params"]["input"][0]["text"])
                        send({"id": x["id"], "result": {"turnId": tid}})
                        rec({"chat": os.environ["KNOWLEDGE_CHAT"], "argv": [name] + args, "cwd": os.getcwd(), "stdin": texts[-1], "pid": os.getpid(), "into": True})
                        if "DONE" in texts[-1]:
                            break
                    elif x.get("method") == "turn/interrupt":
                        send({"id": x["id"], "result": {}})
                        status = "interrupted"
                        break
                    elif "id" in x:
                        send({"id": x["id"], "error": {"code": -32600, "message": "busy"}})
            if status == "completed":
                said = "echo: " + " + ".join(texts) + (" (resumed)" if loaded == "sess-codex" or turns > 1 else "") + verdict
                note("item/agentMessage/delta", {"delta": said})
                note("item/completed", {"item": {"type": "agentMessage", "text": said}})
            note("turn/completed", {"threadId": "sess-codex", "turn": {"id": tid, "status": status}})
elif name == "codex" and args[:1] == ["app-server"]:  # codex-rs app-server, as far as the companion uses it
    for line in sys.stdin:
        m = json.loads(line)
        rec({"rpc": m})
        if "id" not in m:
            continue
        res = {}
        if m["method"] == "hooks/list" and os.path.exists(calls + ".fail"):  # a Codex without hooks/list
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "unknown method"}}), flush=True)
            continue
        if m["method"] == "hooks/list":
            res = {"data": [{"cwd": m["params"]["cwds"][0], "warnings": [], "errors": [], "hooks": [
                {"key": "knowledge@knowledge:.codex-plugin/hooks.json:session_start:0:0", "pluginId": "knowledge@knowledge",
                 "currentHash": "sha256:aa", "trustStatus": "untrusted", "handlerType": "command", "command": "x"},
                {"key": "knowledge@knowledge:.codex-plugin/hooks.json:stop:0:0", "pluginId": "knowledge@knowledge",
                 "currentHash": "sha256:bb", "trustStatus": "trusted", "handlerType": "command", "command": "x"},
                {"key": "/repo/.codex/hooks.json:stop:0:0", "pluginId": None, "currentHash": "sha256:cc",
                 "trustStatus": "untrusted", "handlerType": "command", "command": "./evil.sh"}]}]}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
elif os.environ.get("KNOWLEDGE_CHAT") and name == "agy" and args[:1] == ["acp"]:  # agy without YOLO: ACP over stdio
    import time
    send = lambda x: print(json.dumps({"jsonrpc": "2.0", **x}), flush=True)
    upd = lambda u: send({"method": "session/update", "params": {"sessionId": "sess-agy", "update": u}})
    loaded, pick = "", [None, None]
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "initialize":
            send({"id": m["id"], "result": {"protocolVersion": 1}})
        elif m.get("method") == "session/set_model":
            pick[0] = m["params"]["modelId"]
            send({"id": m["id"], "result": {}})
        elif m.get("method") == "session/new":
            send({"id": m["id"], "result": {"sessionId": "sess-agy", "models": {"availableModels": [{"modelId": "fake-acp", "name": "Fake ACP"}]}}})
            upd({"sessionUpdate": "available_commands_update", "availableCommands": [{"name": "compact", "description": "Compact the conversation now."},
                                                                                {"name": "goal", "description": "Run until done.", "input": {"hint": "the goal"}}]})
        elif m.get("method") == "session/load":
            loaded = m["params"]["sessionId"]
            upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OLD HISTORY"}})  # a replay: never output
            send({"id": m["id"], "result": {}})
        elif m.get("method") == "session/prompt":
            text = m["params"]["prompt"][0]["text"]
            rec({"chat": os.environ["KNOWLEDGE_CHAT"], "argv": [name] + args, "cwd": os.getcwd(), "stdin": text, "load": loaded, "pid": os.getpid(), "pick": pick})
            if "SLOW" in text:  # works on; agy has no interrupt, a Force or Stop ends this process
                time.sleep(90)
            if "PAUSE" in text:  # a turn that takes a while (a companion restart falls in it)
                time.sleep(8)
            upd({"sessionUpdate": "tool_call", "toolCallId": "c1", "name": "run_command", "kind": "execute", "rawInput": {"CommandLine": "git status"}})
            if "LOOK" in text:
                upd({"sessionUpdate": "tool_call", "toolCallId": "c9", "name": "view_file", "kind": "read", "rawInput": {"AbsolutePath": look()}})
            send({"id": "p1", "method": "session/request_permission", "params": {"sessionId": "sess-agy", "toolCall": {
                "toolCallId": "c1", "name": "view_file", "kind": "read", "rawInput": {"AbsolutePath": os.path.join(os.getcwd(), "README.md")}},
                "options": [{"optionId": "allow", "kind": "allow_once"}, {"optionId": "deny", "kind": "reject_once"}]}})
            pre = json.loads(sys.stdin.readline())["result"]["outcome"]["optionId"]  # a read in its folder: allowed, never asked
            prompted = globals().get("prompted", 0) + 1; globals()["prompted"] = prompted  # a second prompt in this agy: the conversation goes on
            said = "echo: " + text + (" (resumed)" if loaded == "sess-agy" or prompted > 1 else "") + ("" if pre == "allow" else " PRE-DENIED")
            if "ASK" in text:
                send({"id": "p2", "method": "session/request_permission", "params": {"sessionId": "sess-agy", "toolCall": {
                    "toolCallId": "c2", "name": "run_command", "kind": "execute", "title": "npm test", "rawInput": {"CommandLine": "npm test", "toolSummary": "run the tests"}},
                    "options": [{"optionId": "allow", "kind": "allow_once"}, {"optionId": "deny", "kind": "reject_once"}]}})
                got = json.loads(sys.stdin.readline())["result"]["outcome"]["optionId"]
                said += " | npm test ran" if got == "allow" else " | npm test denied"
            upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": said}})
            send({"id": m["id"], "result": {"stopReason": "end_turn"}})
elif name == "claude" and "--input-format" in args and os.environ.get("KNOWLEDGE_CHAT"):  # a live chat: one process, messages in
    import queue, subprocess, threading
    sid, out, inbox = "sess-claude", (lambda x: print(json.dumps(x), flush=True)), queue.Queue()
    threading.Thread(target=lambda: ([inbox.put(json.loads(ln)) for ln in sys.stdin if ln.strip()], inbox.put(None)), daemon=True).start()
    said_before = "--resume" in args  # a resumed conversation, or one this process answered before

    def ask_owner():  # its permission prompt tool, as Claude calls it (see the one-turn branch below)
        cfg = json.load(open(args[args.index("--mcp-config") + 1], encoding="utf-8"))["mcpServers"]["knowledge_approval"]
        tool = args[args.index("--permission-prompt-tool") + 1].split("__")[-1]
        srv = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env={**os.environ, **cfg["env"]})
        srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}) + "\\n")
        srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": {
            "tool_name": "Bash", "input": {"command": "npm test", "description": "run the tests"}, "tool_use_id": "t1"}, "_meta": {"progressToken": 7}}}) + "\\n")
        srv.stdin.flush()
        for line in srv.stdout:
            r = json.loads(line)
            if r.get("id") == 2:
                srv.kill()
                v = json.loads(r["result"]["content"][0]["text"])
                return " | npm test ran" if v["behavior"] == "allow" else " | npm test denied: " + v.get("message", "")
        return ""

    out({"type": "system", "subtype": "init", "session_id": sid})
    while (m := inbox.get()) is not None:
        if m.get("type") == "control_request":  # an interrupt with no turn running: answered, nothing else
            cmds = [{"name": "fake-skill", "description": "A fake skill (user)", "argumentHint": "[what]"}, {"name": "context", "description": "Show context usage"}]
            out({"type": "control_response", "response": {"subtype": "success", "request_id": m.get("request_id"),
                 **({"response": {"commands": cmds}} if m["request"].get("subtype") == "initialize" else {})}})
            continue
        texts = [m["message"]["content"]]
        if texts[0] == "/context":  # a local command: its output and a result with no model turn, no echo (2.1.319)
            rec({"chat": os.environ["KNOWLEDGE_CHAT"], "stdin": texts[0], "pid": os.getpid()})
            out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Context Usage: fake"}]}})
            out({"type": "result", "subtype": "success", "num_turns": 0, "result": "Context Usage: fake", "session_id": sid})
            continue
        if "LATE" in texts[0]:  # it takes a while to take it: a Stop comes first
            rec({"chat": os.environ["KNOWLEDGE_CHAT"], "argv": [name] + args, "cwd": os.getcwd(), "stdin": texts[0], "pid": os.getpid()})
            time.sleep(2)
        out({"type": "user", "isReplay": True, "message": m["message"]})  # --replay-user-messages: taken now
        rec({"chat": os.environ["KNOWLEDGE_CHAT"], "argv": [name] + args, "cwd": os.getcwd(), "stdin": texts[0], "pid": os.getpid()})
        stopped = False
        if "SLOW" in texts[0]:  # a turn that keeps working: messages sent meanwhile go into it; an interrupt ends it
            out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "sleep 90"}}]}})
            end = time.time() + 90
            while time.time() < end:
                try:
                    x = inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                if x is None:
                    break
                if x.get("type") == "control_request":
                    out({"type": "control_response", "response": {"subtype": "success", "request_id": x.get("request_id")}})
                    stopped = True
                    break
                texts.append(x["message"]["content"])
                out({"type": "user", "isReplay": True, "message": x["message"]})
                rec({"chat": os.environ["KNOWLEDGE_CHAT"], "argv": [name] + args, "cwd": os.getcwd(), "stdin": texts[-1], "pid": os.getpid(), "into": True})
                if "DONE" in texts[-1]:
                    break
        if stopped:
            out({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": sid})
            continue
        said = "echo: " + " + ".join(texts) + (" (resumed)" if said_before else "")
        if "ASK" in texts[0] and "--permission-prompt-tool" in args:
            said += ask_owner()
        said_before = True
        out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": said}}})
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": said}, {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}})
        if "LOOK" in texts[0]:
            out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": look()}}]}})
        out({"type": "result", "is_error": False, "session_id": sid, "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "git push"}}]})
elif os.environ.get("KNOWLEDGE_CHAT"):  # one chat turn: the message on stdin, the agent's own JSON stream out
    import time
    msg = sys.stdin.buffer.read().decode("utf-8")
    rec({"chat": os.environ["KNOWLEDGE_CHAT"], "argv": [name] + args, "cwd": os.getcwd(), "stdin": msg, "pid": os.getpid()})
    text = json.loads(msg)["message"]["content"] if name == "agy" else msg
    sid = "sess-" + name
    said = "echo: " + text + (" (resumed)" if sid in args else "")
    out = lambda x: print(json.dumps(x), flush=True)
    if name == "claude" and "ASK" in text and "--permission-prompt-tool" in args:  # its permission prompt tool, as Claude calls it
        import subprocess
        cfg = json.load(open(args[args.index("--mcp-config") + 1], encoding="utf-8"))["mcpServers"]["knowledge_approval"]
        tool = args[args.index("--permission-prompt-tool") + 1].split("__")[-1]
        srv = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env={**os.environ, **cfg["env"]})
        srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}) + "\\n")
        srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": {
            "tool_name": "Bash", "input": {"command": "npm test", "description": "run the tests"}, "tool_use_id": "t1"}, "_meta": {"progressToken": 7}}}) + "\\n")
        srv.stdin.flush()
        for line in srv.stdout:
            r = json.loads(line)
            if r.get("id") == 2:
                verdict = json.loads(r["result"]["content"][0]["text"])
                break
        srv.kill()
        said += " | npm test ran" if verdict["behavior"] == "allow" else " | npm test denied: " + verdict.get("message", "")
    if name == "claude":
        out({"type": "system", "subtype": "init", "session_id": sid})
        out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": said}}})
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": said}, {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}})
    elif name == "codex":
        out({"type": "thread.started", "thread_id": sid})
        out({"type": "item.started", "item": {"type": "command_execution", "command": "git status"}})
        out({"type": "item.completed", "item": {"type": "agent_message", "text": said}})
    else:
        out({"event": "init", "conversation_id": sid})
        out({"event": "step_update", "step_update": {"conversation_id": sid, "step_type": "tool", "state": "ACTIVE", "tool_name": "run_command",
                                                     "tool_info": {"parameters": {"CommandLine": "git status"}}}})
        out({"event": "step_update", "step_update": {"conversation_id": sid, "step_type": "agent_response", "text_delta": said}})
    if "SLOW" in text:  # a turn that keeps working until it is stopped
        time.sleep(90)
        open(calls + ".slow-done", "w").write("not stopped")
    if name == "claude":
        out({"type": "result", "is_error": False, "session_id": sid, "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "git push"}}]})
    elif name == "codex":
        out({"type": "turn.completed"})
    else:
        out({"event": "result", "result": {"conversation_id": sid, "status": "SUCCESS"}})
elif os.environ.get("KN_FAKE_FOLLOW") and os.environ.get("KNOWLEDGE_WORKER"):  # a worker someone follows and steers
    import subprocess
    tid, sid = os.environ["KNOWLEDGE_WORKER"], "sess-" + name
    rec({"worker": tid, "argv": [name] + args, "cwd": os.getcwd(), "pid": os.getpid()})
    out = lambda x: print(json.dumps(x), flush=True)

    def say(text, tool):  # each agent's own headless output: Claude stream-json, Codex --json, agy -p plain text
        if name == "claude":
            out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}, {"type": "tool_use", "name": "Bash", "input": {"command": tool}}]}})
        elif name == "codex":
            out({"type": "item.started", "item": {"type": "command_execution", "command": tool}})
            out({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        else:
            print(text, flush=True)

    def step():  # after a tool call the agent runs the plugin's PostToolUse hook (watch.py), as the real ones do
        hook = {"conversationId": sid, "cwd": os.getcwd()} if name == "agy" else {"session_id": sid, "cwd": os.getcwd(), "tool_input": {"command": "git status"}}
        r = subprocess.run([sys.executable, os.environ["KN_FAKE_FOLLOW"], name], input=json.dumps(hook), capture_output=True, text=True)
        got = ((json.loads(r.stdout or "{}").get("hookSpecificOutput") or {}).get("additionalContext") or "") if r.stdout.strip() else ""
        parked = os.path.join(os.environ.get("KNOWLEDGE_HOME") or os.path.join(os.path.expanduser("~"), ".knowledge"), "pending-hints-" + sid + ".txt")
        if name == "agy" and os.path.exists(parked):  # agy: parked for its PostInvocation hook, injected before the next model turn
            got = open(parked, encoding="utf-8").read()
            os.remove(parked)
        if "steers this worker" in got:
            say("heard: " + got.strip().splitlines()[-1], "git status")
    if name == "claude":
        out({"type": "system", "subtype": "init", "session_id": sid})
    elif name == "codex":
        out({"type": "thread.started", "thread_id": sid})
    say(f"{name} starts {tid}", "git status")
    for stage in ("go", "end"):
        while not os.path.exists(f"{calls}.{tid}.{stage}"):
            step()
            time.sleep(0.4)
        say(f"{name} {stage} {tid} password=hunter22hunter Sup3rSecret99", "git add -A")
else:
    rec({"argv": [name] + args, "cwd": os.getcwd(), "env": {k: os.environ.get(k, "") for k in ("KNOWLEDGE_WORKER", "KNOWLEDGE_YOLO", "KNOWLEDGE_START_NONCE", "GIT_CONFIG_PARAMETERS")}})
    if os.path.exists(calls + ".sleep"):  # a worker that keeps working
        import time
        time.sleep(120)
"""


def machines() -> list:
    """The Machines list as the admin sees it."""
    return json.loads(http("GET", "/ui/api/machines", headers={"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice"})[1])["machines"]


def machine_errors(token: str) -> dict:
    """{agent: hook failures} over every machine of a token."""
    out = {}
    for r in machines():
        if token in r["tokens"]:
            for a, ag in r["agents"].items():
                out.setdefault(a, []).extend(ag.get("errors") or [])
    return {a: sorted(e, key=lambda x: x["at"]) for a, e in sorted(out.items())}


def knowledge_home(tok: str, tmp: Path, project: Path):
    """KNOWLEDGE_HOME moves ~/.knowledge (the companion's state, the session registry, what a session was told) for
    every agent's hooks and the companion alike."""
    home, kh = tmp / "Home Usér", tmp / "Elsewhere Usér" / "kn state"
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, KNOWLEDGE_HOME=str(kh))
    hooks = CLIENT / "plugin" / "hooks"
    for agent in ("claude", "codex"):
        run([sys.executable, str(hooks / "agent_hooks.py"), "tasks", agent],
            json.dumps({"session_id": f"kh-{agent}", "cwd": str(project), "prompt": "hi"}), env, cwd=str(project))
    run([sys.executable, str(hooks / "agent_hooks.py"), "hints"], json.dumps({"conversationId": "kh-agy", "cwd": str(project)}), env, cwd=str(project))
    run([sys.executable, str(hooks / "companion.py"), "tick"], "", env)
    check((kh / "tasks-seen").is_dir() and len(list((kh / "tasks-seen").glob("*.json"))) >= 3 and (kh / "companion" / "state.json").exists()
          and not (home / ".knowledge").exists(), "KNOWLEDGE_HOME: hooks of every agent and the companion keep their state there",
          str(sorted(p.name for p in kh.rglob("*"))[:20]))
    m = json.loads((kh / "machine.json").read_text(encoding="utf-8")) if (kh / "machine.json").exists() else {}
    check(re.fullmatch(r"[0-9a-f]{16}", m.get("id", "")) and m.get("label"), "machine id: the hooks make one random id per pc, with the host name as label", json.dumps(m))
    if not WIN:
        check((kh / "machine.json").stat().st_mode & 0o077 == 0, "machine id: machine.json is private (0600)")
    code, out, err = run([sys.executable, str(hooks / "token_store.py"), "header"], "", env)
    check(code == 0 and json.loads(out).get("X-Knowledge-Machine") == m.get("id"), "machine id: the MCP header helper sends it too", out + err)
    (kh / "machine.json").write_text(json.dumps({**m, "label": "Компьютер-7"}), encoding="utf-8")  # a Unicode computer name
    code, out, err = run([sys.executable, str(hooks / "token_store.py"), "header"], "", env)
    label = json.loads(out).get("X-Knowledge-Machine-Label", "") if code == 0 else ""
    check(label.isascii() and label.endswith("-7") and http("GET", "/tasks/ready", headers=json.loads(out))[0] == 200,
          "machine id: a non-Latin host name becomes an ASCII label, so every request still goes out", out + err)
    (kh / "machine.json").write_text(json.dumps(m), encoding="utf-8")
    return env, kh


def companion_e2e(tok: str, tmp: Path, project: Path):
    """The local companion (plugin/hooks/companion.py, started as `agent_hooks.py serve`) in a home of its own with fake
    agents: its local API answers only this pc's pages, it trusts the knowledge hooks the agents' own way (Codex through
    app-server hooks/list + config/batchWrite, again after a plugin update), YOLO writes each agent's native settings and
    unchecking restores them byte for byte (agy only while agy is closed), workers (a fresh headless session per ready
    task in its own worktree; off by default, limits, the 90% hand-over, remote actions, roll back, merged cleanup), and
    hook failures the agents log themselves reach the Machines list."""
    hooks = CLIENT / "plugin" / "hooks"
    home = tmp / "Companion Usér"
    bin_dir, calls = home / "bin", home / "calls.jsonl"
    bin_dir.mkdir(parents=True)
    (bin_dir / "fakeagent.py").write_text(FAKE_AGENT, encoding="utf-8")
    for name in ("codex", "claude", "agy", "tmux"):
        if WIN:
            if name != "tmux":
                # %~dp0, not the path: a new or no console (agy, a detached codex exec) reads this file in another code page
                (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0fakeagent.py" {name} %*\r\n', encoding="utf-8")
        else:
            (bin_dir / name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fakeagent.py"}" {name} "$@"\n', encoding="utf-8")
            (bin_dir / name).chmod(0o755)
    tools_dir = home / "tools"  # the tool add-on fixture (KNOWLEDGE_TOOLS_DIR) and its command, a .cmd on Windows
    tools_dir.mkdir()
    (bin_dir / "kntool.py").write_text("import os\nopen(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tool-mark.txt'), 'a').write('K')\n", encoding="utf-8")
    if WIN:  # %~dp0, not the path: the home's name is not ASCII (see the fake agents above)
        (bin_dir / "kntool.cmd").write_text(f'@"{sys.executable}" "%~dp0kntool.py"\r\n', encoding="utf-8")
    else:
        (bin_dir / "kntool").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "kntool.py"}"\n', encoding="utf-8")
        (bin_dir / "kntool").chmod(0o755)
    claude_json = home / ".claude.json"
    claude_json.write_text(json.dumps({"numStartups": 3, "projects": {}}), encoding="utf-8")
    (home / ".claude").mkdir()
    claude_settings = {"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"}
    (home / ".claude" / "settings.json").write_text(json.dumps(claude_settings), encoding="utf-8")
    codex_cfg = home / ".codex" / "config.toml"
    codex_cfg.parent.mkdir()
    toml = (f'# my codex settings\nmodel = "gpt-5"\napproval_policy = "on-request" # asked\n\n[notice]\nhide_rate_limit_model_nudge = true\n\n'
            f'[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n')
    codex_cfg.write_bytes(toml.encode())  # LF, as Codex writes it also on Windows
    agy_dir = home / ".gemini" / "antigravity-cli"
    (agy_dir / "log").mkdir(parents=True)
    agy_settings = {"model": "Gemini", "trustedWorkspaces": [str(home)]}
    (agy_dir / "settings.json").write_text(json.dumps(agy_settings), encoding="utf-8")
    md = time.strftime("%m%d")  # today: the server keeps hook failures for a week (a fixed 1031 failed from 7 Nov 2026)
    (agy_dir / "log" / "cli-1.log").write_text(f"I{md} 00:00:00.100000       1 hooks_manager.go:53] loaded 2 named hooks\n"
                                               f"E{md} 00:00:01.200000       1 hooks.go:90] post-tool hook failed: exit status 1: agy-boom\n", encoding="utf-8")
    import sqlite3
    con = sqlite3.connect(str(home / ".codex" / "logs_2.sqlite"))
    con.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, level TEXT, feedback_log_body TEXT)")
    con.execute("INSERT INTO logs (ts, level, feedback_log_body) VALUES (?, 'INFO', 'turn started')", (int(time.time()),))
    con.execute("INSERT INTO logs (ts, level, feedback_log_body) VALUES (?, 'WARN', ?)",
                (int(time.time()), "after_agent hook failed; continuing turn_id=t1 hook_name=knowledge-stop error=exit code 1: codex-kaput"))
    con.execute("INSERT INTO logs (ts, level, feedback_log_body) VALUES (?, 'WARN', ?)",  # another hook: stays on this pc
                (int(time.time()), "pre_tool_use hook failed; hook_name=work-lint error=exit code 1: private-work-text"))
    con.commit()
    con.close()
    fake_agy = fake_codex = None
    if not WIN:  # a process named like agy, ended later: YOLO for agy waits for it
        shutil.copy(shutil.which("sleep"), bin_dir / "knfakeagy")
        fake_agy = subprocess.Popen([str(bin_dir / "knfakeagy"), "300"])
        shutil.copy(shutil.which("sleep"), bin_dir / "knfakecodex")  # a running Codex: its registered sessions count as open
        fake_codex = subprocess.Popen([str(bin_dir / "knfakecodex"), "300"])
    port = free_port()
    self_url = f"http://127.0.0.1:{port}"
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                   KNOWLEDGE_COMPANION_PORT=str(port), KNOWLEDGE_COMPANION_EVERY="3600", KN_FAKE_CALLS=str(calls),
                   KNOWLEDGE_COMPANION_ACTIONS_EVERY="0.2", KNOWLEDGE_TOOLS_DIR=str(tools_dir), KNOWLEDGE_COMPANION_UPDATE_EVERY="1", KNOWLEDGE_WORKER_GRACE="1", KNOWLEDGE_COMPANION_SCAN_EVERY="0",
                   KNOWLEDGE_AGY_PROCESS="knfakeagy" if fake_agy else Path(sys.executable).stem,
                   KNOWLEDGE_CODEX_PROCESS="knfakecodex" if fake_codex else Path(sys.executable).stem)

    key_file = home / ".knowledge" / "companion" / "api-key"

    def call(method, path, body=None, origin=self_url, host=None, ctype="application/json", key=True):
        key = key_file.read_text(encoding="utf-8").strip() if key and key_file.exists() else ""
        headers = {**({"Origin": origin} if origin else {}), **({"Host": host} if host else {}), **({"Content-Type": ctype} if ctype else {}),
                   **({"X-Companion-Key": key} if key else {})}
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
        up = wait_for(lambda: _quiet(lambda: call("GET", "/local/api/state")[0] == 200 and json.loads(call("GET", "/local/api/state")[2])["status"].get("synced")), 30)
        check(up, "companion: serve answers on 127.0.0.1 and its first round reached the server", (home / "companion.log").read_text(errors="replace")[-600:])
        if not up:
            return
        second = subprocess.run([sys.executable, str(hooks / "agent_hooks.py"), "serve"], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=60)  # on Windows it waits 30 s for the port
        check(second.returncode != 0, "companion: a second one on this pc cannot bind the port (one minute loop per pc, also on Windows)",
              second.stdout[-300:] + second.stderr[-300:])
        # Only this pc's pages: a foreign origin, a rebound host name, a POST without Origin or not as JSON get nothing.
        check(call("GET", "/local/api/hello", origin="https://evil.example")[0] == 403, "companion: a foreign origin is refused")
        check(call("GET", "/local/api/hello", origin=None, host="evil.example:%d" % port)[0] == 403, "companion: a foreign Host (DNS rebinding) is refused")
        st, hdr, body = call("GET", "/local/api/hello", origin=BASE, key=False)
        mid = json.loads(call("GET", "/local/api/hello")[2]).get("machine", "")  # with the local key
        check(st == 200 and hdr.get("Access-Control-Allow-Origin") == BASE and "companion" in json.loads(body) and mid
              and "machine" not in json.loads(body) and json.loads(body)["machineHash"] == hashlib.sha256(mid.encode()).hexdigest(),
              "companion: hello answers the knowledge web UI's origin with CORS, the machine id only as a hash (the id with the key)", f"{st} {hdr} {body}")
        st, hdr, _ = call("OPTIONS", "/local/api/hello", origin=BASE, ctype=None)
        check(st == 204 and hdr.get("Access-Control-Allow-Private-Network") == "true" and hdr.get("Access-Control-Allow-Origin") == BASE,
              "companion: the preflight allows Private Network Access", f"{st} {hdr}")
        check(call("POST", "/local/api/trust", {}, origin=None)[0] == 403 and call("POST", "/local/api/trust", {}, ctype="text/plain")[0] == 403,
              "companion: a POST without Origin or not as JSON is refused")
        check(call("POST", "/local/api/yolo", {"agent": "codex", "on": True}, origin=BASE)[0] == 403 and codex_cfg.read_text(encoding="utf-8") == toml,
              "companion: YOLO only from the companion's own page, not from the web UI")
        # Any process here can set Origin: a change needs the key only this user can read (the shortcut carries it).
        check(call("POST", "/local/api/yolo", {"agent": "codex", "on": True}, key=False)[0] == 403
              and call("POST", "/local/api/settings", {"startWhenNone": {"codex": True}}, key=False)[0] == 403
              and codex_cfg.read_text(encoding="utf-8") == toml and not (home / ".knowledge" / "companion" / "settings.json").exists(),
              "companion: YOLO and settings without the local key are refused, also with the page's own Origin")
        check(call("POST", "/local/api/trust", {}, origin=BASE, key=False)[0] == 403, "companion: trusting hooks needs the local key too")
        big = {"agent": "codex", "pad": "x" * 60000}  # a refusal sent before the body was read made Windows reset the connection (10053)
        check(all(_quiet(lambda: call("POST", "/local/api/yolo", big, key=False)[0] == 403) for _ in range(20)),
              "companion: a refused POST reads its body first (no connection reset)")
        big = {"agent": "codex", "pad": "x" * 300000}  # past the 64 KiB it reads: the server's closing drain must save the answer
        check(all(_quiet(lambda: call("POST", "/local/api/yolo", big, key=False)[0] == 413) for _ in range(20)),
              "companion: a too large POST gets its 413, not a connection reset")
        st, _, body = call("GET", "/local/api/state", origin=None, key=False)
        check(st == 200 and json.loads(body).get("locked") and "tasks" not in json.loads(body) and "activity" not in json.loads(body)
              and json.loads(body)["agents"], "companion: without the key the state shows the agents only (the web UI's tab)", body[:300])
        if not WIN:
            check(key_file.stat().st_mode & 0o077 == 0, "companion: the local key file is private (0600)")
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
        rpcs, skip = [], False  # the companion's own limits read (codex_rate_read) is a separate app-server session: left out
        for c in (c["rpc"] for c in recorded() if "rpc" in c):
            if c.get("method") == "initialize":
                skip = ((c.get("params") or {}).get("clientInfo") or {}).get("name") == "knowledge-companion-limits"
            if not skip:
                rpcs.append(c)
        methods = [c.get("method") for c in rpcs]
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
        check(call("POST", "/local/api/yolo", {"agent": "codex", "on": True})[0] == 400 and codex_cfg.read_text(encoding="utf-8") == toml,
              "companion: YOLO on without the typed word YOLO is refused, settings untouched")
        call("POST", "/local/api/yolo", {"agent": "codex", "on": True, "confirm": "YOLO"})
        text = codex_cfg.read_text(encoding="utf-8")
        try:
            import tomllib
            parsed = tomllib.loads(text)
        except ImportError:
            parsed = {"approval_policy": "never", "sandbox_mode": "danger-full-access", "notice": {"hide_full_access_warning": True}, "model": "gpt-5"}
        check(parsed["approval_policy"] == "never" and parsed["sandbox_mode"] == "danger-full-access" and parsed["notice"]["hide_full_access_warning"] is True
              and parsed["model"] == "gpt-5" and text.startswith("# my codex settings\n"), "companion: YOLO on for Codex edits config.toml minimally", text)
        call("POST", "/local/api/yolo", {"agent": "codex", "on": False})
        check(codex_cfg.read_bytes() == toml.encode(), "companion: YOLO off restores config.toml byte for byte (LF stays LF)", repr(codex_cfg.read_bytes()[:300]))
        call("POST", "/local/api/yolo", {"agent": "claude", "on": True, "confirm": "YOLO"})
        cs = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        check(cs["permissions"] == {"allow": ["Bash(ls)"], "defaultMode": "bypassPermissions"} and cs["skipDangerousModePermissionPrompt"] is True,
              "companion: YOLO on for Claude Code (bypassPermissions)", json.dumps(cs))
        code, out, err = run([sys.executable, str(hooks / "companion.py"), "yolo-off"], "", env)
        check(code == 0 and json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8")) == claude_settings
              and not json.loads(call("GET", "/local/api/state")[2])["agents"][0]["yolo"],
              "companion: yolo-off (uninstall) restores Claude's settings", out + err)
        st, _, body = call("POST", "/local/api/yolo", {"agent": "agy", "on": True, "confirm": "YOLO"})
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
        # Workers: a fresh headless session per ready task, in its own git worktree; off until the owner allows them.
        repo = home / "Garden repo"
        repo.mkdir()
        (repo / ".knowledge-project").write_text("garden\n", encoding="utf-8")
        (repo / "README.md").write_text("garden\n", encoding="utf-8")
        (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
        (repo / ".worktreeinclude").write_text(".env\n", encoding="utf-8")
        (repo / ".env").write_text("X=1\n", encoding="utf-8")
        for args in (["init", "-q", "-b", "main"], ["config", "user.name", "t"], ["config", "user.email", "t@x"], ["add", "-A"], ["commit", "-qm", "one"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        run([sys.executable, str(hooks / "agent_hooks.py"), "tasks", "codex"], json.dumps({"session_id": "cmp-cx", "cwd": str(repo)}), env, cwd=str(repo))
        mid = json.loads((home / ".knowledge" / "machine.json").read_text(encoding="utf-8"))["id"]
        pc = {"Authorization": f"Bearer {tok}", "X-Knowledge-Machine": mid}
        mcp = lambda args: http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_edit" if "old_string" in args else "knowledge_write",
                                "arguments": args}}, {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
        tasks = "".join(f"- open tot 1 jan 2099 [{o}, {i}] from Claude, start now: worker test {i} \"a & b > c\n"
                        for o, i in (("Codex", "CMP-CODEX"), ("Codex", "CMP-CX2"), ("Gemini", "CMP-GEMINI"), ("Claude", "CMP-CLAUDE")))
        mcp({"path": "garden-memory/project_companion.md", "content": "---\nname: c\ndescription: d\nmetadata:\n  type: project\n---\n"
             f"## Stand\n{tasks}\n## Keuzes\n"})
        argvs = lambda n=0: [c for c in recorded()[n:] if "argv" in c and c["argv"][0] in ("codex", "claude", "agy") and c["argv"][1:2] != ["app-server"]]
        st, _, body = call("POST", "/local/api/tick", {})
        out = json.loads(body) if st == 200 else {}
        check(not argvs() and all(w.startswith("Workers are off") for k, w in out.get("waiting", {}).items() if k.startswith("garden/CMP-")) and out.get("waiting"),
              "workers: off by default, every ready task waits and says why", body[:600])
        check(call("POST", "/local/api/settings", {"workersAllowed": True}, origin=BASE)[0] == 403,
              "workers: the web UI cannot switch workers on through the local API (the owner does it on the page or remotely)")
        st, _, body = call("POST", "/local/api/settings", {"workersAllowed": True, "maxWorkers": {"codex": 2}})
        check(st == 200 and json.loads(body)["workersAllowed"] is True and json.loads(body)["maxWorkers"]["codex"] == 2,
              "workers: allowed on the companion page, max workers per agent", body)
        Path(str(calls) + ".sleep").write_text("", encoding="utf-8")
        yolo_file = home / ".knowledge" / "companion" / "yolo.json"
        yolo_file.write_text(json.dumps({"agy": {"on": True}}), encoding="utf-8")  # as the page leaves it, minus the restore data
        n = len(recorded())
        st, _, body = call("POST", "/local/api/tick", {})
        out = json.loads(body) if st == 200 else {}
        wait_for(lambda: len(argvs(n)) >= 3, 20)
        got = {c["env"]["KNOWLEDGE_WORKER"]: c for c in argvs(n)}
        cx_id = next((i for i in ("CMP-CODEX", "CMP-CX2") if i in got), "")
        cx = got.get(cx_id, {"argv": [], "cwd": ""})
        tag = hashlib.sha256(f"{repo.resolve()}\ngarden".encode()).hexdigest()[:6]  # repository and project: no two share one
        wt = home / ".knowledge" / "worktrees" / f"Garden-repo-{tag}-{cx_id}"
        check(cx["argv"][:2] == ["codex", "exec"] and "workspace-write" in cx["argv"] and "--json" in cx["argv"] and cx["argv"][cx["argv"].index("--cd") + 1] == str(wt)
              and Path(cx["cwd"]).resolve() == wt.resolve() and cx_id in cx["argv"][-1] and f"task/{cx_id}" in cx["argv"][-1]
              and "CMP-GEMINI" not in cx["argv"][-1] and cx["env"]["KNOWLEDGE_START_NONCE"],
              "workers: a Codex task starts `codex exec` in its own worktree on branch task/<ID>, with only its own task", json.dumps(cx)[:900])
        branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
        check(branch == f"task/{cx_id}" and (wt / "README.md").exists(), "workers: the worktree is on branch task/<ID>", branch)
        check((wt / ".env").read_text(encoding="utf-8") == "X=1\n", "workers: .worktreeinclude copies an ignored .env into the worktree")
        dirs = [Path(cx["argv"][i + 1]).resolve() for i, a in enumerate(cx["argv"]) if a == "--add-dir"]
        dot = (repo / ".git").resolve()
        check(dot / "worktrees" in [d.parent for d in dirs] and dot / "objects" in dirs and dot not in dirs
              and "plugins.knowledge@knowledge.mcp_servers.knowledge.default_tools_approval_mode=approve" in cx["argv"],
              "workers: Codex without YOLO may write its worktree's git folder, objects, refs and logs (not .git's config or hooks), "
              "and the knowledge tools are approved for that run", json.dumps(cx["argv"][2:-1]))
        check(len([i for i in got if i in ("CMP-CODEX", "CMP-CX2")]) == 1 and "(limit unknown: 1)" in json.dumps(out.get("waiting")),
              "workers: an agent whose usage limit is unknown runs one worker at most", body[:600])
        ag, cl = got.get("CMP-GEMINI", {"argv": []}), got.get("CMP-CLAUDE", {"argv": []})
        check(ag["argv"][:2] == ["agy", "-p"] and "--dangerously-skip-permissions" in ag["argv"] and ag["env"]["KNOWLEDGE_YOLO"] == "1",
              "workers: agy runs `agy -p`; with YOLO on its own flag and KNOWLEDGE_YOLO", json.dumps(ag)[:500])
        check(cl["argv"][:2] == ["claude", "-p"] and "stream-json" in cl["argv"] and "acceptEdits" in cl["argv"] and "--dangerously-skip-permissions" not in cl["argv"],
              "workers: Claude runs `claude -p` with stream-json (its usage limit) and acceptEdits without YOLO", json.dumps(cl)[:500])
        ck = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", "refs/knowledge/checkpoints/CMP-GEMINI"], capture_output=True, text=True)
        check(ck.returncode == 0, "workers: a YOLO worker gets a git checkpoint on refs/knowledge/checkpoints/<ID> before it starts", ck.stderr)
        leases = json.loads((tmp / "task_leases.json").read_text(encoding="utf-8"))["garden"]
        check(all(leases.get(i, {}).get("session", "").startswith(f"worker:{mid}:") and leases[i]["device"].endswith("#" + mid) for i in got),
              "workers: each worker holds its task's lease for this machine and its worker session", json.dumps(leases)[:600])
        # Usage known (Codex rollout at 40%): up to its max; the Machines list shows workers, limit and settings.
        d = time.localtime()
        rollout = home / ".codex" / "sessions" / f"{d.tm_year:04d}" / f"{d.tm_mon:02d}" / f"{d.tm_mday:02d}" / "rollout-x.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        write_rollout = lambda pct: rollout.write_text(json.dumps({"payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": pct, "window_minutes": 300, "resets_at": time.time() + 3600}}}}) + "\n", encoding="utf-8")
        write_rollout(40.0)
        n = len(recorded())
        call("POST", "/local/api/tick", {})
        wait_for(lambda: argvs(n), 15)
        check([c["env"]["KNOWLEDGE_WORKER"] for c in argvs(n)] == [next(i for i in ("CMP-CODEX", "CMP-CX2") if i != cx_id)],
              "workers: a known limit below 90% allows up to the agent's max (2)", json.dumps(argvs(n))[:400])
        row = next((r for r in machines() if r["id"] == mid), {})
        cxa = row.get("agents", {}).get("codex", {})
        check(row.get("companion", {}).get("workersAllowed") is True and cxa.get("limit") == 40.0
              and sum(1 for w in cxa.get("workers", []) if w["status"] == "running") == 2 and row["agents"]["agy"]["yolo"] is True,
              "workers: the Machines list shows the pc's workers, limit, YOLO and settings", json.dumps(row)[:900])
        running = [w for w in cxa.get("workers", []) if w["status"] == "running"]
        check(running and all(isinstance(w.get("progress"), dict) and isinstance(w["progress"].get("log"), list)
                              and isinstance(w["progress"].get("commits"), int) for w in running),
              "workers: each running worker reports a progress snapshot (commit, commits, changed files, log tail)", json.dumps(running)[:900])
        # YOLO watch: a risky action is flagged (activity log, once to the server), never refused.
        code, out_w, err = run([sys.executable, str(hooks / "watch.py"), "codex"], json.dumps({"session_id": "yolo-1", "cwd": str(repo),
                               "tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}), {**env, "KNOWLEDGE_YOLO": "1"})
        act = json.loads(call("GET", "/local/api/state")[2])["activity"]
        flags = json.loads((tmp / "machines.json").read_text(encoding="utf-8")).get(mid, {}).get("flags", [])
        check(code == 0 and out_w.strip() == "{}" and any(a["kind"] == "risk" and "force-push" in a["text"] for a in act) and "codex:yolo-1:force-push" in flags,
              "YOLO watch: a force push is flagged in the activity log and to the server, never refused", out_w + err + json.dumps(flags))
        # 90%: no new worker, the running ones are told to hand over to an agent with room, then stopped.
        (home / ".knowledge" / "limits-claude.json").write_text(json.dumps({"at": time.time(), "rate_limits": {"five_hour": {
            "used_percentage": 30, "resets_at": time.time() + 3600}}}), encoding="utf-8")
        mcp({"path": "garden-memory/project_companion.md", "old_string": "## Keuzes",
             "new_string": "- open tot 1 jan 2099 [Codex, CMP-CX3] from Claude, start now: after the limit\n## Keuzes"})
        write_rollout(95.0)
        n = len(recorded())
        st, _, body = call("POST", "/local/api/tick", {})
        cx_nonce = state_of(home, "workers.json")[f"garden/{cx_id}"]["nonce"]
        stop = home / ".knowledge" / "companion" / "workers" / f"{cx_nonce}.stop"  # by its start nonce: never a namesake's
        check(not argvs(n) and json.loads(body)["waiting"].get("garden/CMP-CX3", "").startswith("waiting for limit") and stop.exists()
              and "for Claude" in stop.read_text(encoding="utf-8"),
              "limit 90%: no new worker, a running one is told to hand over to an agent with room", body[:500])
        code, out_w, err = run([sys.executable, str(hooks / "watch.py"), "codex"], json.dumps({"session_id": "w", "cwd": str(wt), "tool_name": "Bash",
                               "tool_input": {"command": "ls"}}), {**env, "KNOWLEDGE_WORKER": cx_id, "KNOWLEDGE_START_NONCE": cx_nonce})
        said = json.loads(out_w)["hookSpecificOutput"]["additionalContext"] if out_w.strip().startswith('{"hook') else ""
        check(f"{cx_id}-cont" in said and "left:" in said, "limit 90%: the worker hears it after its next tool call (Claude, Codex: additionalContext)", out_w[:400])
        seen = stop.with_suffix(".seen")
        for who in ("claude", "codex", "agy"):
            seen.unlink(missing_ok=True)
            run([sys.executable, str(hooks / "watch.py"), who], json.dumps({"session_id": "w", "conversationId": "w", "workspacePaths": [str(wt)], "cwd": str(wt)}),
                {**env, "KNOWLEDGE_WORKER": cx_id, "KNOWLEDGE_START_NONCE": cx_nonce})
            check(seen.exists(), f"a worker's tool call marks it alive for the board ({who})")
        code, out_w, err = run([sys.executable, str(hooks / "watch.py"), "codex"], json.dumps({"session_id": "w2", "cwd": str(wt), "tool_name": "Bash",
                               "tool_input": {"command": "ls"}}), {**env, "KNOWLEDGE_WORKER": cx_id, "KNOWLEDGE_START_NONCE": "another-project"})
        check(out_w.strip() == "{}", "limit 90%: a worker of another project with the same task id does not read that stop note", out_w[:300])
        code, out_w, err = run([sys.executable, str(hooks / "watch.py"), "agy"], json.dumps({"conversationId": "w", "workspacePaths": [str(wt)]}),
                               {**env, "KNOWLEDGE_WORKER": cx_id, "KNOWLEDGE_START_NONCE": cx_nonce})
        parked = home / ".knowledge" / "pending-hints-w.txt"  # this conversation's own: another agy session never reads it
        check(out_w.strip() == "{}" and "-cont" in parked.read_text(encoding="utf-8") and "-cont" not in
              ((home / ".knowledge" / "pending-hints.txt").read_text(encoding="utf-8") if (home / ".knowledge" / "pending-hints.txt").exists() else ""),
              "limit 90%: agy's PostToolUse answers {} and the note waits for that conversation's next turn (PostInvocation)", out_w)
        code, out_h, err = run([sys.executable, str(hooks / "agent_hooks.py"), "hints"], json.dumps({"conversationId": "other"}), env)
        check("-cont" not in out_h and parked.exists(), "limit 90%: another agy conversation's next turn does not get that note", out_h[:300])
        check("CMP-CX3" in json.loads((tmp / "machines.json").read_text(encoding="utf-8")).get(mid, {}).get("limit_told", []),
              "limit 90%: a task that waits for the limit gets one notice")
        time.sleep(1.5)  # KNOWLEDGE_WORKER_GRACE=1
        call("POST", "/local/api/tick", {})
        wfile = home / ".knowledge" / "companion" / "workers.json"
        wait_for(lambda: (call("POST", "/local/api/tick", {}) and False) or all(
            w["status"] == "stopped" for w in json.loads(wfile.read_text(encoding="utf-8")).values() if w["agent"] == "codex"), 20)
        ws = json.loads(wfile.read_text(encoding="utf-8"))
        check(all(w["status"] == "stopped" for w in ws.values() if w["agent"] == "codex") and not stop.exists(),
              "limit 90%: after the grace the codex workers are stopped hard", json.dumps({k: w["status"] for k, w in ws.items()}))
        write_rollout(20.0)
        n = len(recorded())
        st, _, body = call("POST", "/local/api/tick", {})
        check(not [c for c in argvs(n) if c["env"]["KNOWLEDGE_WORKER"] == cx_id] and "not started again" in json.loads(body)["waiting"].get(f"garden/{cx_id}", ""),
              "workers: a task whose worker ended without finishing it is not started again every minute", body[:500])
        # A worker recorded before a reboot: its pid now names another process, which is never counted as it nor killed.
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            ws = json.loads(wfile.read_text(encoding="utf-8"))
            ws["garden/REBOOT-1"] = {**ws[f"garden/{cx_id}"], "id": "REBOOT-1", "status": "running", "pid": other.pid, "ident": "before the reboot",
                                     "told": time.time() - 600, "worktree": "", "branch": ""}
            wfile.write_text(json.dumps(ws), encoding="utf-8")
            call("POST", "/local/api/tick", {})
            rec = json.loads(wfile.read_text(encoding="utf-8"))["garden/REBOOT-1"]
            check(rec["status"] == "stopped" and rec["code"] == "unknown" and other.poll() is None,
                  "workers: a record whose pid now names another process (a reboot) ends, and that process is never killed", json.dumps(rec)[:300])
        finally:
            other.kill()
        # A knowledge write makes the companion look at once (the wake hook).
        code, out_w, err = run([sys.executable, str(hooks / "agent_hooks.py"), "wake", "claude"],
                               json.dumps({"session_id": "s", "cwd": str(repo), "tool_name": "mcp__knowledge__knowledge_edit"}), env)
        check("asked the companion" in out_w, "workers: a knowledge write asks the companion here to look now", out_w + err)
        # Remote actions (Machines list): pulled every few seconds, run with the companion's own code, result reported.
        ui = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice", "X-Knowledge-UI": "1"}
        act = lambda action, args: http("POST", "/ui/api/machines/action", {"machine": mid, "action": action, "args": args}, ui)
        done = lambda: [a for a in next((r for r in machines() if r["id"] == mid), {}).get("actions", [])]
        act("stop_worker", {"project": "garden", "id": "CMP-GEMINI"})
        act("yolo", {"agent": "claude", "on": True, "confirm": "YOLO"})
        act("workers_allowed", {"on": False})
        wait_for(lambda: all(a["status"] in ("done", "failed") for a in done()), 30)
        check([(a["action"], a["status"]) for a in done()] == [("stop_worker", "done"), ("yolo", "done"), ("workers_allowed", "done")]
              and json.loads(yolo_file.read_text(encoding="utf-8"))["claude"]["on"] is True
              and json.loads((home / ".knowledge" / "companion" / "settings.json").read_text(encoding="utf-8"))["workersAllowed"] is False,
              "remote: stop worker, YOLO on (typed word) and workers off run on the pc and report back", json.dumps(done())[:800])
        stopped = state_of(home, "workers.json").get("garden/CMP-GEMINI", {})
        check(stopped.get("owner_stopped") is True, "remote: a worker the owner stopped keeps its lease (no other pc restarts it at once)", json.dumps(stopped)[:300])
        act_log = json.loads(call("GET", "/local/api/state")[2])["activity"]
        check(any(a["kind"] == "remote" and "yolo" in a["text"] and "alice" in a["text"] for a in act_log), "remote: every action is in the activity log, with who asked")
        act("yolo", {"agent": "claude", "on": False})
        wait_for(lambda: done()[-1]["status"] != "waiting", 20)
        # A tool add-on: installed from the pc's own manifest (a .cmd on Windows, a step per agent), checked, listed, removed.
        tool_mark = home / "tool-mark.txt"
        put = lambda text: [sys.executable, "-c", f"open({str(tool_mark)!r}, 'a').write({text!r})"]
        (tools_dir / "fixture.json").write_text(json.dumps({"name": "Fixture", "kind": "program", "description": "a test tool", "needs": ["kntool"],
            "install": [{"run": ["kntool"]}, {"run": put("C"), "agent": "claude"}],
            "check": [{"run": [sys.executable, "-c", "import json, sys; print(json.dumps({'checks': [{'name': 'engine', 'ok': False}]})); sys.exit(1)"], "info": True}],
            "version": {"run": [sys.executable, "-c", "print('1.0.0')"]}, "remove": [{"run": put("R")}]}), encoding="utf-8")
        tool_row = lambda: (next((r for r in machines() if r["id"] == mid), {}).get("companion") or {}).get("tools", {}).get("fixture", {})
        act("install_tool", {"tool": "fixture"})
        wait_for(lambda: done()[-1]["status"] in ("done", "failed"), 30)
        wait_for(lambda: tool_row().get("installed") or call("POST", "/local/api/tick", {}) and False, 30)
        check(done()[-1]["status"] == "done" and tool_mark.read_text(encoding="utf-8") == "KC" and tool_row().get("version") == "1.0.0"
              and tool_row().get("ok") is True and "not ok: engine" in tool_row().get("detail", ""),
              "tools: install runs the manifest's steps (a .cmd on Windows, a step per agent), its check and version reach the Machines list",
              json.dumps(done()[-1]) + json.dumps(tool_row()))
        act("install_tool", {"tool": "nope"})
        wait_for(lambda: done()[-1]["status"] in ("done", "failed"), 30)
        check(done()[-1]["status"] == "failed" and "no tool nope" in done()[-1].get("result", ""), "tools: an id the pc's plugin does not know is refused there", json.dumps(done()[-1]))
        act("remove_tool", {"tool": "fixture"})
        wait_for(lambda: done()[-1]["status"] in ("done", "failed"), 30)
        wait_for(lambda: tool_row().get("installed") is False or call("POST", "/local/api/tick", {}) and False, 30)
        check(tool_mark.read_text(encoding="utf-8") == "KCR" and tool_row().get("installed") is False, "tools: remove runs its steps and the list says so", json.dumps(tool_row()))
        # Roll back a YOLO worker's last turn; once its branch is merged, the companion removes the worktree.
        call("POST", "/local/api/tick", {})
        gwt = home / ".knowledge" / "worktrees" / f"Garden-repo-{tag}-CMP-GEMINI"
        count = lambda: subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "refs/knowledge/checkpoints/CMP-GEMINI"], capture_output=True, text=True).stdout.strip()
        before = count()
        run([sys.executable, str(hooks / "agent_hooks.py"), "hints"], json.dumps({"conversationId": "wk", "workspacePaths": [str(gwt)]}),
            {**env, "KNOWLEDGE_WORKER": "CMP-GEMINI", "KNOWLEDGE_YOLO": "1"}, cwd=str(gwt))
        check(before and count() == str(int(before) + 1), "workers: each turn of a YOLO worker adds a checkpoint (here agy's turn hook)", f"{before} -> {count()}")
        (gwt / "README.md").write_text("broken by a turn\n", encoding="utf-8")
        (gwt / "junk.txt").write_text("junk\n", encoding="utf-8")
        act("rollback", {"project": "garden", "id": "CMP-GEMINI"})
        wait_for(lambda: done()[-1]["status"] != "waiting" and done()[-1]["status"] != "running", 20)
        check(done()[-1]["status"] == "done" and (gwt / "README.md").read_text(encoding="utf-8") == "garden\n" and not (gwt / "junk.txt").exists(),
              "remote: roll back puts a YOLO worker's worktree back as before its turn", json.dumps(done()[-1]))
        (gwt / "work.txt").write_text("done\n", encoding="utf-8")
        for args in (["add", "work.txt"], ["commit", "-qm", "feat: work"]):
            subprocess.run(["git", "-C", str(gwt), *args], check=True, capture_output=True)
        # The review card's Merge: a remote action; the companion merges --no-ff into the branch the worktree came from.
        (repo / "README.md").write_text("dirty\n", encoding="utf-8")
        act("merge", {"project": "garden", "id": "CMP-GEMINI"})
        wait_for(lambda: done()[-1]["status"] not in ("waiting", "running"), 20)
        check(done()[-1]["status"] == "failed" and "uncommitted changes" in done()[-1]["result"] and gwt.exists(),
              "review: Merge refuses a main checkout with uncommitted changes", json.dumps(done()[-1]))
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--", "README.md"], check=True, capture_output=True)
        act("merge", {"project": "garden", "id": "CMP-GEMINI"})
        wait_for(lambda: done()[-1]["status"] not in ("waiting", "running"), 20)
        parents = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%P"], capture_output=True, text=True).stdout.split()
        check(done()[-1]["status"] == "done" and "merged task/CMP-GEMINI into main" in done()[-1]["result"] and len(parents) == 2
              and (repo / "work.txt").exists(), "review: Merge runs git merge --no-ff into the worktree's base branch on the pc", json.dumps(done()[-1]))
        call("POST", "/local/api/tick", {})
        ws = json.loads(wfile.read_text(encoding="utf-8"))
        check(ws.get("garden/CMP-GEMINI", {}).get("status") == "merged" and not gwt.exists(),
              "workers: once the requester merged the branch, the companion removes the worktree", json.dumps(ws.get("garden/CMP-GEMINI"))[:400])
        # Roll back is refused after a merge; Archive only hides the record, which keeps the task's restart hold.
        act("rollback", {"project": "garden", "id": "CMP-GEMINI"})
        wait_for(lambda: done()[-1]["status"] not in ("waiting", "running"), 20)
        check(done()[-1]["status"] == "failed" and "no finished worker" in done()[-1]["result"], "workers: roll back is refused once merged", json.dumps(done()[-1]))
        act("archive_worker", {"project": "garden", "id": "CMP-GEMINI"})
        wait_for(lambda: done()[-1]["status"] not in ("waiting", "running"), 20)
        ws = json.loads(wfile.read_text(encoding="utf-8"))
        check(done()[-1]["status"] == "done" and ws["garden/CMP-GEMINI"].get("archived") is True, "workers: Archive keeps the record, marked", json.dumps(done()[-1]))
        check(ws["garden/CMP-GEMINI"].get("title"), "workers: a worker keeps its task's text as its title", json.dumps(ws["garden/CMP-GEMINI"])[:300])
        # An updated plugin takes effect in the running companion: it restarts itself and reports the new version.
        manifests = [hooks.parent / m / "plugin.json" for m in (".claude-plugin", ".codex-plugin") if (hooks.parent / m / "plugin.json").exists()]
        before = {m: m.read_text(encoding="utf-8") for m in manifests}
        reported = lambda: next((r for r in machines() if r["id"] == mid), {}).get("companion", {}).get("version")
        for m, t in before.items():
            m.write_text(re.sub(r'"version": "[^"]+"', '"version": "9.8.7"', t), encoding="utf-8")
        act("workers_allowed", {"on": True})  # a round reports soon after the restart
        def poke():  # the port is closed for a moment while it restarts
            try:
                call("POST", "/local/api/tick", {})
            except OSError:
                pass
        wait_for(lambda: reported() == "9.8.7" or poke(), 60)
        check(reported() == "9.8.7", "companion: an updated plugin restarts it, and the list shows the code that runs", str(reported()))
        for m, t in before.items():
            m.write_text(t, encoding="utf-8")
        wait_for(lambda: reported() != "9.8.7" or poke(), 60)
        check(reported() != "9.8.7", "companion: after going back it restarts again", str(reported()))
        code, out, err = run([sys.executable, str(hooks / "workers.py"), "--self-test"], "", env)
        check(code == 0 and out.strip() == "ok", "review: workers.py self-test on a real repository (clean merge, conflict aborted and said, "
              "dirty or other branch refused)", out[-400:] + err[-800:])
        Path(str(calls) + ".sleep").unlink()
        for w in json.loads(wfile.read_text(encoding="utf-8")).values():
            if w["status"] == "running":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(w["pid"])], capture_output=True) if WIN else _quiet(lambda: os.killpg(w["pid"], 9))
        # Without YOLO a worker gets exactly what its task needs, in each agent's own way; one that ends with exit 0 but
        # left its task open (its tools refused, say) is a failure, said in the activity log, not a quiet "finished".
        allowed = cl["argv"][cl["argv"].index("--allowedTools") + 1:] if "--allowedTools" in cl["argv"] else []
        check(allowed == ["mcp__plugin_knowledge_knowledge", "mcp__knowledge", "Bash(git status:*)", "Bash(git add:*)", "Bash(git commit:*)",
                          "Bash(git branch --show-current)", "Bash(git diff)", "Bash(git diff --stat)", "Bash(git diff --cached)", "Bash(git log)",
                          "Bash(git log --oneline)", "Bash(git log -1)", "Bash(git log --stat -1)"],
              "workers: Claude without YOLO may use the knowledge tools, git status/add/commit and diff/log only in fixed forms (no --output), nothing else (no push, no git -C)", json.dumps(cl["argv"][3:]))
        yolo_file.write_text(json.dumps({}), encoding="utf-8")
        mcp({"path": "garden-memory/project_companion.md", "old_string": "## Keuzes",
             "new_string": "- open tot 1 jan 2099 [Gemini, CMP-QUIT] from Claude, start now: a worker that does nothing\n## Keuzes"})
        call("POST", "/local/api/settings", {"workersAllowed": True})
        n = len(recorded())
        call("POST", "/local/api/tick", {})
        wait_for(lambda: [c for c in argvs(n) if c["env"]["KNOWLEDGE_WORKER"] == "CMP-QUIT"], 15)
        ag = next((c for c in argvs(n) if c["env"]["KNOWLEDGE_WORKER"] == "CMP-QUIT"), {"argv": []})
        rules = ag["argv"][ag["argv"].index("--settings") + 1] if "--settings" in ag["argv"] else ""
        rules = json.loads(Path(rules).read_text(encoding="utf-8")) if rules and Path(rules).is_file() else {}
        check(ag["argv"][:2] == ["agy", "-p"] and ag["argv"][3:5] == ["--mode", "accept-edits"] and "--dangerously-skip-permissions" not in ag["argv"]
              and rules.get("permissions", {}).get("allow") == ["command(git status)", "command(git add)",
                                                                 "command(git commit)", "command(git branch --show-current)", "mcp(knowledge_knowledge/*)", "mcp(knowledge/*)"]
              and {Path(d[11:-1]).name for d in rules["permissions"].get("deny", [])} == {".git", ".gitattributes", ".gitmodules"}
              and all(Path(d[11:-1]).parent.resolve() == Path(ag["cwd"]).resolve() for d in rules["permissions"]["deny"])
              and "'core.fsmonitor'='false'" in ag["env"].get("GIT_CONFIG_PARAMETERS", ""),
              "workers: agy without YOLO gets allow rules for this session only (--settings): knowledge tools and git status/add/commit (no diff/log: --output), "
              "and may never write git's own files in its worktree; its git runs without fsmonitor or hooks",
              json.dumps(ag["argv"][3:]) + json.dumps(rules))
        wait_for(lambda: (call("POST", "/local/api/tick", {}) and False) or json.loads(wfile.read_text(encoding="utf-8")).get("garden/CMP-QUIT", {}).get("status") != "running", 20)
        quit_rec = json.loads(wfile.read_text(encoding="utf-8")).get("garden/CMP-QUIT", {})
        act_log = json.loads(call("GET", "/local/api/state")[2])
        check(quit_rec.get("status") == "failed" and quit_rec.get("code") == 0
              and any(a["kind"] == "error" and "CMP-QUIT ended without claiming or handing back" in a["text"] for a in act_log["activity"])
              and any(s["id"] == "CMP-QUIT" and "without claiming" in s["why"] for s in act_log["tasks"]["stalled"]),
              "workers: exit 0 with the task left open is a failure (activity log error, stalled lane), not finished", json.dumps(quit_rec)[:400])
        quit_srv = next((w for r in machines() if r["id"] == mid for w in r["agents"].get("agy", {}).get("workers", []) if w["id"] == "CMP-QUIT"), {})
        check("without claiming" in (quit_srv.get("why") or ""), "workers: a failed worker's reason reaches the server's Machines list", json.dumps(quit_srv)[:400])
        call("POST", "/local/api/settings", {"workersAllowed": False})
        # The trust command: an agent that fails (a Codex without hooks/list) does not keep the next one untrusted.
        Path(str(calls) + ".fail").write_text("", encoding="utf-8")
        ag_now = json.loads((agy_dir / "settings.json").read_text(encoding="utf-8"))
        (agy_dir / "settings.json").write_text(json.dumps({**ag_now, "trustedWorkspaces": [str(home)]}), encoding="utf-8")
        code, out, err = run([sys.executable, str(hooks / "companion.py"), "trust"], "", env, timeout=120)
        check(code != 0 and "codex" in err and str(repo) in json.loads((agy_dir / "settings.json").read_text(encoding="utf-8"))["trustedWorkspaces"],
              "companion: trust goes on with agy after Codex failed, and says so", out + err)
        Path(str(calls) + ".fail").unlink()
        # Hook failures the agents log themselves: reported once, marked as agent-log.
        admin = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice"}
        errs = machine_errors("friend-pc")
        check(any(e["source"] == "agent-log" and e["hook"] == "knowledge-stop" and "codex-kaput" in e["stderr"] for e in errs.get("codex", []))
              and any(e["source"] == "agent-log" and "agy-boom" in e["stderr"] and e["event"] == "PostToolUse" for e in errs.get("agy", [])),
              "companion: hook failures from Codex's and agy's own logs reach the Tasks tab as agent-log", json.dumps(errs)[:800])
        check("private-work-text" not in json.dumps(errs), "companion: another hook's logged failure stays on this pc", json.dumps(errs)[:800])
        before = json.dumps(errs)
        call("POST", "/local/api/tick", {})
        check(json.dumps(machine_errors("friend-pc")) == before, "companion: a logged failure is reported once")
        # Another pc took the task between /tasks/ready and the start (the server answers 409): no worker, said once.
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Held(BaseHTTPRequestHandler):
            def answer(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.answer(200, {"tasks": [{"project": "garden", "path": "garden-memory/x.md", "id": "CMP-HELD", "owner": "Codex",
                                             "device": "", "agent": "codex", "from": "Claude", "start": "now", "text": "held", "mine": False}]}
                            if self.path.startswith("/tasks/ready") else {"actions": []})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.answer(409, {"held_by": {"device": "other-pc", "session": "s", "until": time.time() + 600}} if self.path == "/tasks/woken" else {})

            def log_message(self, *a):
                pass
        fake = ThreadingHTTPServer(("127.0.0.1", 0), Held)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        call("POST", "/local/api/settings", {"workersAllowed": True})
        n = len(recorded())
        code, out, err = run([sys.executable, str(hooks / "companion.py"), "tick"], "",
                             {**env, "KNOWLEDGE_URL": f"http://127.0.0.1:{fake.server_address[1]}"}, timeout=60)
        fake.shutdown()
        call("POST", "/local/api/settings", {"workersAllowed": False})
        activity = state_of(home, "activity.json")
        check(code == 0 and not argvs(n) and "Held by other-pc" in out and any("Held by other-pc" in a["text"] for a in activity),
              "workers: a 409 from /tasks/woken (another pc holds the task) starts nothing and logs who holds it", out[-400:] + err[-400:])
        # Folders: link a folder to a project from the companion page or remotely, only folders from its own list.
        app = home / "code" / "new app"
        outside = tmp / "Outside repo"
        for d in (app, outside):
            d.mkdir(parents=True)
            subprocess.run(["git", "-C", str(d), "init", "-q"], check=True, capture_output=True)
        (app / "notes.md").write_text("keep me\n", encoding="utf-8")
        call("POST", "/local/api/tick", {})  # fetches the projects this token may write
        fs = json.loads(call("GET", "/local/api/state")[2])
        cands = [c["path"] for c in fs.get("folders", {}).get("candidates", [])]
        check(str(app) in cands and str(outside) not in cands and {"path": str(repo), "project": "garden"} in fs["folders"]["linked"]
              and "garden" in fs.get("projects", []), "folders: the page lists linked folders and git checkouts under home as candidates", json.dumps(fs.get("folders"))[:600])
        st, _, body = call("POST", "/local/api/link", {"path": str(outside), "project": "garden"})
        check(st == 400 and not (outside / ".knowledge-project").exists(), "folders: a path outside the companion's own list is refused", body)
        # The folder chooser: any folder on the pc, from this pc's own page only.
        picked = tmp / "Gekozen map é"
        picked.mkdir()
        st, _, body = call("POST", "/local/api/dirs", {"path": ""})
        check(st == 200 and json.loads(body)["dirs"] and (not WIN or "C:\\" in [d["path"] for d in json.loads(body)["dirs"]]), "folders: the chooser starts at the drives of this pc", body[:300])
        st, _, body = call("POST", "/local/api/dirs", {"path": str(tmp)})
        check(st == 200 and str(picked) in [d["path"] for d in json.loads(body)["dirs"]] and not json.loads(body)["linkable"], "folders: the chooser lists the folders outside the home folder; the one above home is not linkable", body[:600])
        st_d, _, _ = call("POST", "/local/api/dirs", {"path": str(tmp)}, origin=BASE)
        st_l, _, _ = call("POST", "/local/api/link", {"path": str(picked), "project": "garden", "chosen": True}, origin=BASE)
        check(st_d == 403 and st_l == 403 and not (picked / ".knowledge-project").exists(), "folders: a web page gets no listing and no free path", f"{st_d} {st_l}")
        st, _, body = call("POST", "/local/api/link", {"path": str(home), "project": "garden", "chosen": True})
        st2, _, body2 = call("POST", "/local/api/link", {"path": home.anchor, "project": "garden", "chosen": True})
        check(st == 400 and st2 == 400 and not (home / ".knowledge-project").exists(), "folders: the chooser refuses the home folder and a drive root", body + body2)
        st, _, body = call("POST", "/local/api/link", {"path": str(picked), "project": "garden", "chosen": True})
        fs = json.loads(call("GET", "/local/api/state")[2])["folders"]
        check(st == 200 and (picked / ".knowledge-project").read_text(encoding="utf-8") == "garden\n" and {"path": str(picked), "project": "garden"} in fs["linked"],
              "folders: a chosen folder outside the home folder is linked and listed", body + json.dumps(fs)[:400])
        st, _, body = call("POST", "/local/api/unlink", {"path": str(picked)})
        check(st == 200 and not (picked / ".knowledge-project").exists(), "folders: a chosen folder can be unlinked", body)
        st, _, body = call("POST", "/local/api/link", {"path": str(app), "project": "homelab"})
        check(st == 400 and "may not write" in body and not (app / ".knowledge-project").exists(), "folders: only a project this token may write", body)
        st, _, body = call("POST", "/local/api/link", {"path": str(app), "project": "garden"})
        fs = json.loads(call("GET", "/local/api/state")[2])["folders"]
        check(st == 200 and (app / ".knowledge-project").read_text(encoding="utf-8") == "garden\n" and {"path": str(app), "project": "garden"} in fs["linked"]
              and "git add .knowledge-project" in body, "folders: linking writes .knowledge-project as knowledge-init does, with the git note", body)
        st, _, body = call("POST", "/local/api/link", {"path": str(app), "project": "garden"})
        check(st == 200, "folders: change the project of a linked folder", body)
        st, _, body = call("POST", "/local/api/unlink", {"path": str(app)})
        check(st == 200 and not (app / ".knowledge-project").exists() and (app / "notes.md").exists(), "folders: unlink removes only the .knowledge-project file", body)
        ui = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice", "X-Knowledge-UI": "1"}
        for path in (str(outside), str(app)):
            http("POST", "/ui/api/machines/action", {"machine": mid, "action": "link_folder", "args": {"path": path, "project": "garden"}}, ui)
        acts = lambda: [a for a in next((r for r in machines() if r["id"] == mid), {}).get("actions", []) if a["action"] == "link_folder"]
        wait_for(lambda: len(acts()) == 2 and all(a["status"] in ("done", "failed") for a in acts()), 30)
        check([a["status"] for a in acts()] == ["failed", "done"] and "own list" in acts()[0].get("result", "")
              and not (outside / ".knowledge-project").exists() and (app / ".knowledge-project").exists(),
              "folders: a remote link refuses a path outside the candidate list and links one from it", json.dumps(acts())[:600])
        call("POST", "/local/api/tick", {})
        row = next((r for r in machines() if r["id"] == mid), {})
        check({"path": str(app), "project": "garden"} in [{k: f[k] for k in ("path", "project")} for f in (row.get("companion") or {}).get("folders", {}).get("linked", [])],
              "folders: the Machines list shows the pc's linked folders", json.dumps((row.get("companion") or {}).get("folders"))[:400])
        # The folder chooser in the web UI: the same list and link, through the server, and the answer is not kept.
        def remote_dirs(path):
            rid = json.loads(http("POST", "/ui/api/git/diff", {"machine": mid, "dirs": path}, ui)[1])["id"]
            a = {}
            wait_for(lambda: a.update(json.loads(http("GET", f"/ui/api/git/diff?machine={mid}&id={rid}", headers=ui)[1])) or a.get("status") == "done", 30)
            return a, rid
        a, rid = remote_dirs(str(tmp))
        check(str(picked) in [d["path"] for d in json.loads(a.get("text") or "{}").get("dirs", [])] and http("GET", f"/ui/api/git/diff?machine={mid}&id={rid}", headers=ui)[0] == 400,
              "folders: the web UI's chooser lists a pc's folders, and the server does not keep them", json.dumps(a)[:400])
        a, _ = remote_dirs(str(tmp / "no such folder"))
        check("cannot open" in (a.get("error") or ""), "folders: the web UI's chooser says why a folder cannot be opened", json.dumps(a)[:300])
        http("POST", "/ui/api/machines/action", {"machine": mid, "action": "link_folder", "args": {"path": str(picked), "project": "garden", "chosen": True}}, ui)
        wait_for(lambda: (picked / ".knowledge-project").exists(), 30)
        check((picked / ".knowledge-project").exists(), "folders: the web UI links a folder its owner chose, also outside the home folder")
        call("POST", "/local/api/unlink", {"path": str(picked)})
        # A plugin update (a new cache folder) makes the companion trust Codex's hooks again by itself.
        manifest = home / ".codex" / "plugins" / "cache" / "knowledge" / "knowledge" / "9.9.9" / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name": "knowledge", "version": "9.9.9"}', encoding="utf-8")
        call("POST", "/local/api/tick", {})
        writes = [c for c in recorded() if c.get("rpc", {}).get("method") == "config/batchWrite"]
        codex_state = next(a for a in json.loads(call("GET", "/local/api/state")[2])["agents"] if a["id"] == "codex")
        check(len(writes) == 2 and codex_state["version"] == "9.9.9" and codex_state["hooksTrusted"],
              "companion: after a Codex plugin update the hooks are trusted again automatically", json.dumps(codex_state)[:300])
        if WIN:  # the restart under the real Scheduled Task: the task stays the owner, so uninstalling can stop it
            wrapper = tmp / "task.cmd"
            wrapper.write_text("@echo off\r\nchcp 65001 >nul\r\n" + "".join(f'set "{k}={v}"\r\n' for k, v in env.items() if k.startswith(("KNOWLEDGE", "HOME", "USERPROFILE", "APPDATA", "PATH", "PYTHON"))) + subprocess.list2cmdline([sys.executable, str(hooks / "agent_hooks.py"), "serve"]) + "\r\n", encoding="utf-8")
            name = "Knowledge companion"
            made = subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONCE", "/ST", "23:59", "/TN", name, "/TR", f'"{wrapper}"'], capture_output=True, text=True)
            if made.returncode:
                print(f"skip the Scheduled Task check: {made.stderr.strip()[:200]}")
            else:
                manifest_text = {f: f.read_text(encoding="utf-8") for f in manifests}  # `before` means something else by now
                try:
                    for f, t in manifest_text.items():
                        f.write_text(re.sub(r'"version": "[^"]+"', '"version": "9.8.6"', t), encoding="utf-8")
                    wait_for(lambda: reported() == "9.8.6" or poke(), 90)
                    state = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST"], capture_output=True, text=True).stdout
                    check(reported() == "9.8.6" and "Running" in state, "companion on Windows: the Scheduled Task runs it again after an update", f"{reported()} {state[:300]}")
                finally:
                    subprocess.run(["schtasks", "/End", "/TN", name], capture_output=True)
                    subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], capture_output=True)
                    for f, t in manifest_text.items():
                        f.write_text(t, encoding="utf-8")
    finally:
        proc.kill()
        if fake_codex:
            fake_codex.kill()
        if fake_agy:
            fake_agy.kill()


def chat_e2e(tok: str, tmp: Path):
    """Chats from the web UI (plugin/hooks/chat.py): the owner starts a chat with an agent on a pc, its companion pulls
    each message, hands it to that chat's running agent in the linked folder (message on stdin, the agent's own resume
    id when a new process continues it) and posts the output back; stop ends a running turn; only linked folders."""
    hooks = CLIENT / "plugin" / "hooks"
    home = tmp / "Chat Usér"
    bin_dir, calls = home / "bin", home / "calls.jsonl"
    bin_dir.mkdir(parents=True)
    (bin_dir / "fakeagent.py").write_text(FAKE_AGENT, encoding="utf-8")
    for name in ("codex", "claude", "agy"):
        if WIN:
            (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0fakeagent.py" {name} %*\r\n', encoding="utf-8")
        else:
            (bin_dir / name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fakeagent.py"}" {name} "$@"\n', encoding="utf-8")
            (bin_dir / name).chmod(0o755)
    repos = {}
    for n in ("Chat repo", "Second repo"):  # a space and R&D-like names: never on a command line anyway
        r = repos[n] = home / "code" / n
        r.mkdir(parents=True)
        subprocess.run(["git", "-C", str(r), "init", "-q"], check=True, capture_output=True)
        (r / ".knowledge-project").write_text("garden\n", encoding="utf-8")
    # The board's git pill: a branch 2 commits ahead of main with 3 changed files (one edited, two new; the link file
    # is committed so it does not count).
    pill = repos["Pill repo"] = home / "code" / "Pill repo"
    pill.mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(pill), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    (pill / ".knowledge-project").write_text("garden\n", encoding="utf-8")
    (pill / "a.txt").write_text("one\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-qm", "one")
    g("checkout", "-qb", "task/KN-12")
    for n in ("b", "c"):
        (pill / f"{n}.txt").write_text(n + "\n", encoding="utf-8")
        g("add", f"{n}.txt")
        g("commit", "-qm", n)
    clean = repos["Clean repo"] = home / "code" / "Clean repo"
    clean.mkdir(parents=True)
    gc = lambda *a: subprocess.run(["git", "-C", str(clean), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], check=True, capture_output=True)
    gc("init", "-q", "-b", "main")
    gc("config", "user.name", "t")  # Merge commits here, with git's identity of the repository
    gc("config", "user.email", "t@example.invalid")
    (clean / ".knowledge-project").write_text("garden\n", encoding="utf-8")
    (clean / ".gitignore").write_text(".knowledge-project\n", encoding="utf-8")
    (clean / "a.txt").write_text("one\n", encoding="utf-8")
    gc("add", "-A")
    gc("commit", "-qm", "one")
    (pill / "a.txt").write_text("two\n", encoding="utf-8")
    (pill / "new 1.txt").write_text("x", encoding="utf-8")
    (pill / "new2.txt").write_text("y", encoding="utf-8")
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                   KNOWLEDGE_COMPANION_PORT=str(free_port()), KNOWLEDGE_COMPANION_EVERY="3600", KN_FAKE_CALLS=str(calls),
                   KNOWLEDGE_COMPANION_ACTIONS_EVERY="0.2", KNOWLEDGE_COMPANION_SCAN_EVERY="0")
    ui = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice", "X-Knowledge-UI": "1"}
    recorded = lambda: [json.loads(ln) for ln in calls.read_text(encoding="utf-8").splitlines() if '"chat"' in ln] if calls.exists() else []
    log = open(home / "companion.log", "wb")
    proc = subprocess.Popen([sys.executable, str(hooks / "agent_hooks.py"), "serve"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    try:
        mid = ""

        def reported():
            nonlocal mid
            if not mid and (home / ".knowledge" / "machine.json").exists():
                mid = json.loads((home / ".knowledge" / "machine.json").read_text(encoding="utf-8"))["id"]
            row = next((r for r in machines() if r["id"] == mid), {})
            linked = {f["path"] for f in ((row.get("companion") or {}).get("folders") or {}).get("linked", [])}
            return {str(r) for r in repos.values()} <= linked and set((row.get("companion") or {}).get("agents") or []) == {"claude", "codex", "agy"}
        ok = wait_for(lambda: _quiet(reported), 60)
        check(ok, "chat: the companion reports its agents and linked folders (what a new chat may pick)", (home / "companion.log").read_text(errors="replace")[-600:])
        if not ok:
            return
        row = next(r for r in machines() if r["id"] == mid)
        got = next((f.get("git") for f in row["companion"]["folders"]["linked"] if Path(f["path"]).resolve() == pill.resolve()), None)
        check(got == {"branch": "task/KN-12", "base": "main", "changed": 3, "ahead": 2, "behind": 0},
              "board: the companion reports a folder's git pill (branch, 3 changed, 2 ahead of main, 0 behind)", json.dumps(got))
        new = lambda agent, folder: json.loads(http("POST", "/ui/api/chats", {"machine": mid, "agent": agent, "folder": str(folder), "worktree": False}, ui)[1]).get("chat", {}).get("id")
        chat = lambda cid: json.loads(http("GET", f"/ui/api/chat?machine={mid}&id={cid}", headers=ui)[1])
        send = lambda cid, text: http("POST", "/ui/api/chat/send", {"machine": mid, "id": cid, "text": text}, ui)[0]
        idle = lambda cid, n: wait_for(lambda: (c := chat(cid))["chat"]["status"] == "idle" and len(c["events"]) >= n, 30)
        st, body = http("POST", "/ui/api/chats", {"machine": mid, "agent": "claude", "folder": str(home / "elsewhere")}, ui)
        check(st == 400, "chat: a folder not linked on that pc is refused", body)
        repo = repos["Chat repo"]
        for agent in ("claude", "codex", "agy"):
            cid = new(agent, repo)
            msg1 = f'hello {agent} "R&D" 100% <b>x</b> → ✅\nline two'
            send(cid, msg1)
            got1 = idle(cid, 3)
            send(cid, "second")
            got2 = idle(cid, 6)
            turns = [c for c in recorded() if c["chat"] == cid]
            events = chat(cid)["events"]
            texts = "".join(e["text"] for e in events if e["kind"] == "text")
            first, second = (turns + [{}, {}])[:2]
            sent = lambda x: x  # agy without YOLO runs over ACP: the prompt is a session/prompt, not stream-json
            asked = [ln for ln in calls.read_text(encoding="utf-8").splitlines() if '"title_ask"' in ln and cid in ln]
            check(len(asked) == 1, f"chat {agent}: the first turn of an untitled chat asks for a title, the second does not", json.dumps(asked))
            check(got1 and got2 and len(turns) == 2 and sent(first.get("stdin", "{}")) == msg1 and sent(second.get("stdin", "{}")) == "second"
                  and Path(first["cwd"]).resolve() == repo.resolve() and not any(msg1[:5] in a for a in first["argv"]),
                  f"chat {agent}: two turns, each message on stdin (never on the command line), in the linked folder", json.dumps(turns)[:900])
            if agent in ("claude", "codex", "agy"):  # live: one process for both turns, the conversation simply goes on
                check(first.get("pid") == second.get("pid") and ("--input-format" in first["argv"] if agent == "claude" else first["argv"][:2] in (["codex", "app-server"], ["agy", "acp"])),
                      f"chat {agent}: live, the second turn goes into the same running agent", json.dumps([first.get("argv"), first.get("pid"), second.get("pid")]))
            else:
                check(not first.get("load") and second.get("load") == "sess-" + agent,
                      f"chat {agent}: the first turn starts a conversation, the second continues it with the agent's own id", json.dumps([first.get("argv"), second.get("argv")]))
            check(f"echo: {msg1}" in texts and "echo: second (resumed)" in texts and "OLD HISTORY" not in texts and "PRE-DENIED" not in texts and any(e["kind"] == "tool" and "git status" in e["text"] for e in events)
                  and [e["kind"] for e in events if e["kind"] == "user"] == ["user", "user"] and chat(cid)["chat"]["session"] == "sess-" + agent,
                  f"chat {agent}: the streamed text and tool lines reach the server", json.dumps(events)[:900])
            send(cid, "LOOK at the board")  # an image the agent looked at shows in the chat, under its tool line
            shots = lambda: [e for e in chat(cid)["events"] if e["kind"] == "tool" and "⟦file:" in e["text"]]
            wait_for(lambda: shots() and chat(cid)["chat"]["status"] == "idle", 30)
            shot = shots()
            ref = re.search(r"⟦file:([0-9a-f]{12}/[^⟧]+)⟧", shot[-1]["text"]) if shot else None
            got = http("GET", f"/ui/api/chat/file?machine={mid}&file={urllib.parse.quote(ref.group(1))}", headers=ui) if ref else (0, b"")
            check(bool(ref) and ref.group(1).endswith(f".{agent}.png") and got[0] == 200 and got[1] in (b"PNG " + agent.encode(), "PNG " + agent),
                  f"chat {agent}: an image the agent looked at reaches the server and shows under its tool line", json.dumps(shot)[:600] + str(got)[:200])
            if agent == "codex":
                check(first["argv"][:2] == ["codex", "app-server"] and first.get("perms", {}).get("sandbox") == "workspace-write"
                      and first["perms"].get("approvalPolicy") == "on-request" and not any("writable_roots" in a for a in first["argv"]),
                      "chat codex: without YOLO app-server, the workspace-write sandbox and on-request approvals, .git stays read-only", json.dumps(first))
            if agent == "claude":
                check(any(e["kind"] == "refused" and "git push" in e["text"] for e in events), "chat claude: a refused tool call shows as a line", json.dumps(events)[:600])
                check("acceptEdits" in first["argv"] and "--dangerously-skip-permissions" not in first["argv"] and "--permission-prompt-tool" in first["argv"],
                      "chat claude: without YOLO the worker permission set (acceptEdits + allowed tools) and the approval prompt tool", json.dumps(first["argv"]))
            if agent == "agy":
                check(first["argv"] == ["agy", "acp"], "chat agy: without YOLO it runs over ACP (the headless agy that can ask)", json.dumps(first["argv"]))
        # Codex's usage limit without a session: the companion asks `codex app-server` (account/rateLimits/read, no
        # prompt) in the background and reports the answer; here no rollout exists.
        probe = ("import companion, time; (companion.STATE / 'codex-rate.json').unlink(missing_ok=True); companion.codex_rate_read(time.time())\n"
                 "for _ in range(100):\n    got = companion.codex_rate_read(time.time())\n    if got: break\n    time.sleep(0.2)\n"
                 "print(got)")
        code, out, err = run([sys.executable, "-c", probe], "", {**env, "PYTHONPATH": str(hooks), "KN_FAKE_RATE": "1"}, cwd=str(hooks))
        check(code == 0 and "42" in out and "5h" in out, "companion: Codex's limit asked without a session (app-server account/rateLimits/read)", out + err[-400:])
        # An agent that closes its end of a stdio MCP server before the answer (its session ended) is no hook failure:
        # the relay and the approval server end quietly with 0 (Linux says EPIPE, Windows EINVAL).
        for script, args in (("agent_hooks.py", ["mcp", "-"]), ("approval_mcp.py", [])):
            p = subprocess.Popen([sys.executable, str(hooks / script), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env={**env, "KNOWLEDGE_URL": "http://127.0.0.1:9"}, cwd=str(hooks))
            p.stdout.close()
            p.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'); p.stdin.close()
            perr = p.stderr.read().decode("utf-8", "replace"); p.wait(timeout=60)
            check(p.returncode == 0 and "Traceback" not in perr, f"{script}: an agent that went away before the answer ends it quietly", f"exit {p.returncode}: {perr[-400:]}")
        # agy's usage limit without a running agy: its cloud quota call with the OAuth token agy keeps (a local stand-in
        # here; it must send agy's User-Agent, Google refuses Python's).
        tokf = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        tokf.parent.mkdir(parents=True, exist_ok=True)
        tokf.write_text(json.dumps({"token": {"access_token": "gategate", "expiry": "2099-01-01T00:00:00Z"}}), encoding="utf-8")
        probe = ("import faulthandler, sys; faulthandler.dump_traceback_later(100, exit=True, file=sys.stderr)\n"  # a hang shows where (macOS CI)
                 "import companion, json, threading, time, http.server\n"
                 "class H(http.server.BaseHTTPRequestHandler):\n"
                 "    def do_POST(self):\n"
                 "        self.rfile.read(int(self.headers.get('Content-Length') or 0))\n"  # read the body first: an unread one can reset the socket (macOS)
                 "        ok = self.headers.get('Authorization') == 'Bearer gategate' and self.headers.get('User-Agent') == 'antigravity-cli'\n"
                 "        body = json.dumps({'models': {'g': {'displayName': 'Gemini X', 'quotaInfo': {'remainingFraction': 0.25, 'resetTime': '2099-01-01T00:00:00Z'}}, 'tab': {'quotaInfo': {'remainingFraction': 1}}}}).encode()\n"
                 "        self.send_response(200 if ok else 403); self.end_headers(); self.wfile.write(body if ok else b'{}')\n"
                 "    def log_message(self, *a): pass\n"
                 "s = companion.LocalServer(('127.0.0.1', 0), H); threading.Thread(target=s.serve_forever, daemon=True).start()\n"
                 "companion.AGY_QUOTA_URL = f'http://127.0.0.1:{s.server_port}/q'\n"
                 "(companion.STATE / 'agy-rate.json').unlink(missing_ok=True)\n"
                 "companion.agy_rate_read(time.time())\n"
                 "for _ in range(300):\n    got = companion.agy_rate_read(time.time())\n    if got: break\n    time.sleep(0.2)\n"
                 "print(got)")
        code, out, err = run([sys.executable, "-c", probe], "", {**env, "PYTHONPATH": str(hooks)}, cwd=str(hooks), timeout=150)
        check(code == 0 and "75" in out and "Gemini X" in out, "companion: agy's limit from its cloud quota with agy's own token (no running agy)", out + err[-6000:])
        tokf.unlink()  # the running companion must never send a test token to Google
        # Model and effort: the pc reports what each agent offers; a chat's choice reaches the turn the agent's own way
        # (Claude flags, Codex turn/start, agy session/set_model + thought_level).
        reported = next(r for r in machines() if r["id"] == mid)["companion"].get("models") or {}
        models = {a: [(m["id"], m["efforts"]) for m in v] for a, v in reported.items()}
        check([m.get("default") for m in reported.get("codex", [])] == ["high"], "chat: Codex's own default effort per model is reported", json.dumps(reported.get("codex")))
        check(models.get("codex") == [("fake-big", ["high", "ultra"])] and models.get("agy") in ([("fake-gem", [])], [("fake-acp", [])])
              and [m for m, _ in models.get("claude", [])] == ["fable", "opus", "sonnet", "haiku"],
              "chat: the companion reports each agent's models and effort levels (hidden Codex models left out)", json.dumps(models))
        agy_model = models["agy"][0][0]  # agy models at first; once an ACP session ran, the list ACP offers
        for agent, model, effort in (("claude", "opus", "max"), ("codex", "fake-big", "ultra"), ("agy", agy_model, "")):
            cid = new(agent, repo)
            st = http("POST", "/ui/api/chat/send", {"machine": mid, "id": cid, "text": "pick", "model": model, "effort": effort}, ui)[0]
            done = st == 200 and idle(cid, 2)
            turn = next((c for c in recorded() if c["chat"] == cid), {})
            av = turn.get("argv", [])
            got = (av[av.index("--model") + 1], av[av.index("--effort") + 1]) if agent == "claude" and "--model" in av and "--effort" in av else tuple(turn.get("pick") or ())
            check(done and got == (model, effort or None), f"chat {agent}: the chosen model and effort reach the turn", json.dumps(turn)[:600])
            if agent == "codex":  # back to Default: Codex keeps a turn/start override for the thread, so Default is named
                st = http("POST", "/ui/api/chat/send", {"machine": mid, "id": cid, "text": "default", "model": "", "effort": ""}, ui)[0]
                back = st == 200 and idle(cid, 4)
                turn = ([c for c in recorded() if c["chat"] == cid] + [{}, {}])[1]
                check(back and turn.get("pick") == ["fake-default", "medium"], "chat codex: Default after a choice names the configured model and effort",
                      json.dumps(turn)[:600] + " | events: " + json.dumps(chat(cid)["events"])[-900:] + " | log: " + (home / "companion.log").read_text(errors="replace")[-1500:])
        # Slash commands: each agent's live chat lists its own (Claude initialize, Codex skills/list + compact, ACP's
        # available_commands_update); the pc reports them, the chat box gets them; each runs them its own way.
        cmds = lambda a: json.loads(http("GET", f"/ui/api/chat/commands?machine={mid}&agent={a}", None, ui)[1] or "{}").get("commands") or []
        want = {"claude": ["fake-skill", "context"], "codex": ["compact", "fake-skill"], "agy": ["compact", "goal"]}
        key = (home / ".knowledge" / "companion" / "api-key").read_text(encoding="utf-8").strip()
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{env['KNOWLEDGE_COMPANION_PORT']}/local/api/tick", method="POST", data=b"{}", headers={
            "Origin": f"http://127.0.0.1:{env['KNOWLEDGE_COMPANION_PORT']}", "Content-Type": "application/json", "X-Companion-Key": key}), timeout=120).read()  # a report round now
        ok = wait_for(lambda: all([c["name"] for c in cmds(a)] == n for a, n in want.items()), 30)
        check(ok and cmds("codex")[1]["description"] == "A fake skill" and cmds("claude")[0]["hint"] == "[what]" and cmds("agy")[1]["hint"] == "the goal",
              "chat: the pc reports each agent's slash commands for the box", json.dumps({a: cmds(a) for a in want})[:900])
        check("commands" not in json.dumps(next(r for r in machines() if r["id"] == mid)["agents"]), "chat: the machines list leaves the long command lists out")
        cid = new("claude", repo)
        st = http("POST", "/ui/api/chat/send", {"machine": mid, "id": cid, "text": "/context"}, ui)[0]
        check(st == 200 and idle(cid, 2) and any("Context Usage: fake" in e["text"] for e in chat(cid)["events"]),
              "chat claude: a local slash command (no echo, no model turn) ends its turn", json.dumps(chat(cid)["events"])[-600:])
        cid = new("codex", repo)
        st = http("POST", "/ui/api/chat/send", {"machine": mid, "id": cid, "text": "/fake-skill go"}, ui)[0]
        wait_for(lambda: any(c.get("chat") == cid and c.get("input") for c in recorded()), 30)
        turn = next((c for c in recorded() if c.get("chat") == cid and c.get("input")), None)
        check(st == 200 and idle(cid, 2) and (turn or {}).get("input") == [{"type": "skill", "name": "fake-skill", "path": "/skills/fake-skill/SKILL.md"}, {"type": "text", "text": "$fake-skill go"}],
              "chat codex: /skill goes in as Codex's own skill input", json.dumps(turn)[:600])
        n = len(chat(cid)["events"])
        st = http("POST", "/ui/api/chat/send", {"machine": mid, "id": cid, "text": "/compact"}, ui)[0]
        check(st == 200 and wait_for(lambda: any(c.get("chat") == cid and c.get("compact") for c in recorded()), 30) and idle(cid, n + 1),
              "chat codex: /compact runs Codex's own compaction and ends", json.dumps(chat(cid)["events"])[-600:])
        # Live chats (Claude Code and Codex): a message sent while it works goes into the running turn (the same process
        # takes it); Force stops the step and sends the message after it; Stop interrupts and the process stays.
        sendm = lambda cid, text, mode: http("POST", "/ui/api/chat/send", {"machine": mid, "id": cid, "text": text, "mode": mode}, ui)[0]
        running = lambda cid: wait_for(lambda: chat(cid)["chat"]["status"] == "running", 20)
        for agent in ("claude", "codex"):
            live = new(agent, repo)
            send(live, "SLOW one")
            ok = running(live) and wait_for(lambda: any(c.get("chat") == live and c.get("stdin") == "SLOW one" for c in recorded()), 20)
            check(ok and sendm(live, "also this", "now") == 200 and sendm(live, "DONE", "now") == 200 and idle(live, 4),
                  f"live {agent}: Send while it works is accepted", json.dumps(chat(live)["events"])[-600:])
            said = "".join(e["text"] for e in chat(live)["events"] if e["kind"] == "text")
            turns = [c for c in recorded() if c["chat"] == live]
            check("echo: SLOW one + also this + DONE" in said and len({c["pid"] for c in turns}) == 1 and sum(1 for c in turns if c.get("into")) == 2,
                  f"live {agent}: messages sent during a turn go into that turn of the same agent", said[-300:] + json.dumps(turns)[:300])
            send(live, "SLOW two")
            ok = running(live) and wait_for(lambda: any(c.get("chat") == live and c.get("stdin") == "SLOW two" for c in recorded()), 20)
            check(ok and sendm(live, "instead do this", "force") == 200 and idle(live, 8),
                  f"live {agent}: Force while it works", json.dumps(chat(live)["events"])[-600:])
            said = "".join(e["text"] for e in chat(live)["events"] if e["kind"] == "text")
            check("echo: instead do this" in said and "SLOW two +" not in said and any("Stopped this step" in e["text"] for e in chat(live)["events"]),
                  f"live {agent}: Force stops the step and its message goes in after it", said[-300:])
            send(live, "SLOW three")
            ok = running(live) and wait_for(lambda: any(c.get("chat") == live and c.get("stdin") == "SLOW three" for c in recorded()), 20)
            http("POST", "/ui/api/chat/stop", {"machine": mid, "id": live}, ui)
            check(ok and wait_for(lambda: chat(live)["chat"]["status"] == "idle", 30) and any(e["text"] == "Stopped." for e in chat(live)["events"]),
                  f"live {agent}: Stop interrupts the turn", json.dumps(chat(live)["events"])[-400:])
            send(live, "after the stop")
            check(idle(live, 12) and [c["pid"] for c in recorded() if c["chat"] == live][-1] == turns[0]["pid"],
                  f"live {agent}: after a Stop the same agent goes on (its process stayed)", json.dumps([c.get("pid") for c in recorded() if c["chat"] == live]))
        # Live agy: a message sent while it works runs as the next turn of the same agy (agy's own line); agy has no
        # interrupt, so Force ends that agy and a new one takes the conversation on with the message; Stop ends it too.
        live = new("agy", repo)
        send(live, "first")
        sendm(live, "right after", "now")
        ok = wait_for(lambda: sum(1 for c in recorded() if c.get("chat") == live) == 2 and chat(live)["chat"]["status"] == "idle", 30)
        turns = [c for c in recorded() if c.get("chat") == live]
        said = "".join(e["text"] for e in chat(live)["events"] if e["kind"] == "text")
        check(ok and "echo: first" in said and "echo: right after" in said and len({c["pid"] for c in turns}) == 1,
              "live agy: a message sent while it works runs next in the same agy", said[-300:] + json.dumps(turns)[:300])
        send(live, "SLOW agy")
        ok = running(live) and wait_for(lambda: any(c.get("chat") == live and c.get("stdin") == "SLOW agy" for c in recorded()), 20)
        check(ok and sendm(live, "instead", "force") == 200 and wait_for(lambda: "echo: instead" in "".join(e["text"] for e in chat(live)["events"]), 30)
              and [c["pid"] for c in recorded() if c.get("chat") == live][-1] != turns[0]["pid"] and any("Stopped this step" in e["text"] for e in chat(live)["events"]),
              "live agy: Force ends that agy and a new one answers in the same conversation", json.dumps(chat(live)["events"])[-500:])
        idle(live, 2)
        send(live, "SLOW again")
        ok = running(live) and wait_for(lambda: any(c.get("chat") == live and c.get("stdin") == "SLOW again" for c in recorded()), 20)
        http("POST", "/ui/api/chat/stop", {"machine": mid, "id": live}, ui)
        check(ok and wait_for(lambda: chat(live)["chat"]["status"] == "idle", 30) and any(e["text"] == "Stopped." for e in chat(live)["events"]),
              "live agy: Stop ends the turn", json.dumps(chat(live)["events"])[-300:])
        send(live, "and on")
        check(wait_for(lambda: "echo: and on" in "".join(e["text"] for e in chat(live)["events"]), 30) and [c.get("load") for c in recorded() if c.get("chat") == live][-1] == "sess-agy",
              "live agy: after a Stop the conversation goes on (session/load)", json.dumps([c.get("load") for c in recorded() if c.get("chat") == live]))
        # Own branch: a chat made with worktree runs its agent in a git worktree of its own on branch chat/<id>, said once.
        own = json.loads(http("POST", "/ui/api/chats", {"machine": mid, "agent": "claude", "folder": str(pill), "worktree": True}, ui)[1])["chat"]["id"]
        send(own, "hello branch")
        ok = idle(own, 2)
        cwd = next((c.get("cwd", "") for c in recorded() if c.get("chat") == own), "")
        branches = subprocess.run(["git", "-C", str(pill), "branch", "--list", f"chat/{own}"], capture_output=True, text=True).stdout
        check(ok and Path(cwd).resolve() != pill.resolve() and "worktrees" in cwd and f"chat/{own}" in branches
              and any(f"branch chat/{own}" in e["text"] for e in chat(own)["events"]),
              "chat: Own branch runs the agent in a worktree of its own on branch chat/<id>", cwd + " | " + branches + json.dumps(chat(own)["events"])[-400:])
        # The agents' own update: once a day by itself, once more by hand, each agent with its own command.
        updates = lambda: [json.loads(ln)["update"] for ln in calls.read_text(encoding="utf-8").splitlines() if '"update"' in ln]
        for a in ("claude", "codex", "agy"):
            check(wait_for(lambda: a in updates(), 30), f"update {a}: the companion runs its update command by itself once a day", str(updates()))
        before = len(updates())
        st = http("POST", "/ui/api/machines/action", {"machine": mid, "action": "update_agents", "args": {}}, ui)[0]
        check(st == 200 and wait_for(lambda: len(updates()) == before + 3, 30), "update: the button runs all three once more", str(updates()))
        # A closed Own-branch chat, per agent: a Review card, Merge, and its worktree and branch gone. Without a choice a
        # second chat in a folder another chat works in gets its own branch by itself; pressed off it stays in the checkout.
        gitp = lambda *args, cwd=clean: subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
        cards = lambda: json.loads(http("GET", "/ui/api/board", headers=ui)[1])["items"]
        action = lambda act, args: http("POST", "/ui/api/machines/action", {"machine": mid, "action": act, "args": args}, ui)
        report_now = lambda: action("workers_allowed", {"on": False})  # a state action makes the companion report at once
        report_now()
        wait_for(lambda: any(Path(f["path"]).resolve() == clean.resolve() and (f.get("git") or {}).get("changed") == 0
                             for f in next(r for r in machines() if r["id"] == mid)["companion"]["folders"]["linked"]), 30)
        for agent in ("claude", "codex", "agy"):
            alone = json.loads(http("POST", "/ui/api/chats", {"machine": mid, "agent": agent, "folder": str(clean)}, ui)[1])["chat"]["id"]
            check(not chat(alone)["chat"].get("worktree"), f"chat {agent}: alone in a clean checkout it stays there", json.dumps(chat(alone))[-300:])
            send(alone, "works in the checkout")
            idle(alone, 2)
            auto = json.loads(http("POST", "/ui/api/chats", {"machine": mid, "agent": agent, "folder": str(clean)}, ui)[1])["chat"]["id"]
            off = new(agent, clean)
            dirty = json.loads(http("POST", "/ui/api/chats", {"machine": mid, "agent": agent, "folder": str(pill)}, ui)[1])["chat"]["id"]
            check(chat(auto)["chat"].get("worktree") is True and not chat(off)["chat"].get("worktree") and any("another chat works" in e["text"] for e in chat(auto)["events"])
                  and any("uncommitted" in e["text"] for e in chat(dirty)["events"]) and chat(dirty)["chat"].get("worktree") is True,
                  f"chat {agent}: beside a working chat or with uncommitted files it gets its own branch, unless pressed off", json.dumps(chat(auto))[-400:])
            send(auto, "hello branch")
            done = idle(auto, 2)
            wt = next((c.get("cwd", "") for c in recorded() if c.get("chat") == auto), "")
            Path(wt, f"own-{agent}.txt").write_text("own\n")
            gitp("add", f"own-{agent}.txt", cwd=wt)
            gitp("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", f"own {agent}", cwd=wt)
            for c in (alone, off, dirty, auto):
                http("POST", "/ui/api/chat/close", {"machine": mid, "id": c, "record": False}, ui)
            card = lambda: next((i for i in cards() if i["kind"] == "review" and i.get("chat") == auto and i.get("can_merge")), None)
            report_now()
            check(done and wait_for(card, 30), f"chat {agent}: closed with a commit, its branch gets a Review card with Merge",
                  json.dumps({k: next(r for r in machines() if r["id"] == mid)["companion"].get(k) for k in ("chats", "chatReviews")}))
            st = action("merge", {"project": "garden", "id": auto, "chat": True})[0]
            merged_in = wait_for(lambda: Path(clean, f"own-{agent}.txt").exists(), 30)
            report_now()
            check(st == 200 and merged_in and wait_for(lambda: not Path(wt).exists() and not gitp("branch", "--list", f"chat/{auto}"), 30) and not card(),
                  f"chat {agent}: Merge lands in the checkout, then its worktree and branch go", f"{st} {merged_in} {wt} " + json.dumps(next(r for r in machines() if r["id"] == mid)["actions"])[-500:])
            # a chat that committed nothing has nothing to merge: closed, it leaves no worktree or card
            idle_own = json.loads(http("POST", "/ui/api/chats", {"machine": mid, "agent": agent, "folder": str(clean), "worktree": True}, ui)[1])["chat"]["id"]
            send(idle_own, "only a question")
            idle(idle_own, 2)
            http("POST", "/ui/api/chat/close", {"machine": mid, "id": idle_own, "record": False}, ui)
            report_now()
            check(wait_for(lambda: not gitp("branch", "--list", f"chat/{idle_own}"), 30), f"chat {agent}: closed without commits its branch goes by itself", gitp("branch", "--list", "chat/*"))
        # A companion restart (an update, a crash) in the middle of a turn: the agent runs on in its chat host, the new
        # companion reads on where the old one stopped, and the turn ends as it would have, in the same agent.
        def restart_companion():
            nonlocal proc
            proc.terminate()
            proc.wait(timeout=30)
            proc = subprocess.Popen([sys.executable, str(hooks / "agent_hooks.py"), "serve"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        for agent in ("claude", "codex", "agy"):
            live = new(agent, repo)
            word = "PAUSE" if agent == "agy" else "SLOW"  # agy takes nothing into a running turn: one that just takes a while
            send(live, f"{word} across a restart")
            ok = running(live) and wait_for(lambda: any(c.get("chat") == live and c.get("stdin") == f"{word} across a restart" for c in recorded()), 20)
            restart_companion()
            if agent != "agy":
                time.sleep(3)  # the new companion picked it up (its first poll), then the turn's end goes into the same agent
                ok = ok and sendm(live, "DONE", "now") == 200
            said = lambda: "".join(e["text"] for e in chat(live)["events"] if e["kind"] == "text")
            want = "echo: PAUSE across a restart" if agent == "agy" else "echo: SLOW across a restart + DONE"
            check(ok and wait_for(lambda: want in said() and chat(live)["chat"]["status"] == "idle", 40)
                  and len({c["pid"] for c in recorded() if c.get("chat") == live}) == 1
                  and not any("lost" in e["text"] for e in chat(live)["events"] if e["kind"] == "error"),
                  f"live {agent}: a turn goes on across a companion restart, in the same agent", said()[-300:] + json.dumps(chat(live)["events"])[-500:]
                  + (home / "companion.log").read_text(errors="replace")[-800:])
        # Approvals: a turn asks for a tool its allowed set lacks; the owner allows once, for the chat, or denies, from
        # the web UI; the agent goes on. One chat waits without an answer and times out (1 minute, the shortest setting).
        admin_ui = {**ui, "Content-Type": "application/json"}
        approvals = lambda cid: chat(cid).get("asks") or []
        board = lambda: json.loads(http("GET", "/ui/api/board", headers=ui)[1])["items"]
        http("POST", "/ui/api/admin/settings", {"chat_approval_minutes": 1}, admin_ui)
        late = new("claude", repo)
        send(late, "ASK and nobody answers")
        wait_for(lambda: approvals(late), 30)
        http("POST", "/ui/api/admin/settings", {"chat_approval_minutes": 30}, admin_ui)  # this ask keeps its own; the ones below get time to be answered
        for agent in ("claude", "codex", "agy"):
            cid = new(agent, repo)
            for n, (decision, said) in enumerate((("once", "npm test ran"), ("deny", "npm test denied"), ("chat", "npm test ran"))):
                send(cid, f"ASK {n}")
                waiting = wait_for(lambda: approvals(cid), 30)
                a = (approvals(cid) or [{}])[0]
                item = next((i for i in board() if i["kind"] == "ask" and i.get("chat") == cid), {})
                check(waiting and a.get("tool") in ("Bash", "shell", "run_command") and (a.get("detail") or "").replace("/bin/sh -lc ", "").strip("'") == "npm test" and item.get("col") == "needs"
                      and chat(cid)["chat"]["status"] == "running",
                      f"chat {agent}: the turn waits for its owner, the approval is in the chat and under Needs you ({decision})", json.dumps([a, item])[:500])
                st, body = http("POST", "/ui/api/chat/approve", {"machine": mid, "id": cid, "ask": a.get("id"), "decision": decision},
                                {**ui, "X-authentik-username": "bob"})
                check(st == 403, f"chat {agent}: another person cannot answer it", body)
                http("POST", "/ui/api/chat/approve", {"machine": mid, "id": cid, "ask": a.get("id"), "decision": decision}, ui)
                done = wait_for(lambda: chat(cid)["chat"]["status"] == "idle", 30)
                texts = "".join(e["text"] for e in chat(cid)["events"] if e["kind"] == "text").split(f"echo: ASK {n}")[-1]
                check(done and said in texts and (decision != "deny" or "denied this tool call" in texts or agent != "claude"),
                      f"chat {agent}: {decision} reaches the agent and it goes on", texts[-300:])
            send(cid, "ASK again")
            done = wait_for(lambda: chat(cid)["chat"]["status"] == "idle", 30)
            texts = "".join(e["text"] for e in chat(cid)["events"] if e["kind"] == "text")
            check(done and texts.split("echo: ASK again")[-1].strip().endswith("npm test ran") and len([e for e in chat(cid)["events"] if e["kind"] == "ask"]) == 3,
                  f"chat {agent}: allowed for this chat, the same call is not asked again in a later turn", texts[-200:])
        cid = new("claude", repo)
        send(cid, "ASK about a push")
        wait_for(lambda: approvals(cid), 30)
        st, body = http("POST", "/ui/api/chat/approve", {"machine": mid, "id": cid, "ask": (approvals(cid) or [{}])[0].get("id"), "decision": "deny"}, ui)
        wait_for(lambda: chat(cid)["chat"]["status"] == "idle", 30)
        timed = wait_for(lambda: chat(late)["chat"]["status"] == "idle", 150)
        events = chat(late)["events"]
        texts = "".join(e["text"] for e in events if e["kind"] == "text")
        check(timed and any(e["text"] == "No answer within 1 min: denied Bash." for e in events) and "npm test denied" in texts and "in time" in texts,
              "chat claude: an approval nobody answers is denied after its time, the chat says so and the agent is told", json.dumps(events)[-600:])
        activity = state_of(home, "activity.json")
        check(any("Approval asked" in a["text"] for a in activity) and any("allowed once" in a["text"] and "by alice" in a["text"] for a in activity)
              and any("denied (no answer in time)" in a["text"] for a in activity),
              "chat: every approval and who decided it is in the companion's activity log", json.dumps([a["text"] for a in activity[:12]]))
        audit = (tmp / "audit.log").read_text(encoding="utf-8") if (tmp / "audit.log").exists() else ""
        check("chat-approve" in audit and "chat-ask" in audit, "chat: every approval and its answer is in the server's audit log", audit[-400:])
        # The pill on the board's chat card, and its diff: asked through the chat channel, read-only, answered by the pc.
        cid = new("codex", pill)
        send(cid, "pill")
        board = lambda: json.loads(http("GET", "/ui/api/board", headers=ui)[1])["items"]
        item = {}  # waited for on the board, not by opening the chat: opening it would mark the reply read
        wait_for(lambda: item.update(next((i for i in board() if i["kind"] == "chat" and i["id"] == cid and i["col"] != "running"), {})) or item, 30)
        check(item.get("col") == "needs" and (item.get("git") or {}).get("ahead") == 2 and item["git"]["changed"] == 3,
              "board: the chat waits for its owner, with the git pill of its folder", json.dumps(item)[:500])
        before = g("status", "--porcelain").stdout
        rid = json.loads(http("POST", "/ui/api/git/diff", {"machine": mid, "chat": cid}, ui)[1]).get("id")
        diff = {}
        wait_for(lambda: (diff.update(json.loads(http("GET", f"/ui/api/git/diff?machine={mid}&id={rid}", headers=ui)[1])) or diff.get("status") == "done"), 30)
        text = diff.get("text") or ""
        check(diff.get("status") == "done" and "2 ahead, 0 behind" in text and "+two" in text and "+b" in text and "?? new 1.txt" in text
              and "b.txt" in text.split("\n\n")[1], "board: the diff (stat, commits ahead, edits, new files) comes back through the chat channel", json.dumps(diff)[:600])
        check(g("status", "--porcelain").stdout == before, "board: the diff changed nothing in the folder")
        # A chat turn's own hooks: no task list, no 'start your tasks', no agy register request, no lease (tasks run in workers).
        hooks_env = {**env, "KNOWLEDGE_CHAT": "c1", "CLAUDE_PLUGIN_OPTION_URL": BASE, "CLAUDE_PLUGIN_OPTION_TOKEN": tok}
        outs = {"claude": run([bash(), "-c", f'sh "{(hooks / "context.sh").as_posix()}" 1'], "", {**hooks_env, "CLAUDE_PROJECT_DIR": str(repo)})[1]}
        for agent in ("codex", "agy"):
            outs[agent] = run([sys.executable, str(hooks / "agent_hooks.py"), "context", agent], json.dumps({"cwd": str(repo), "workspacePaths": [str(repo)]}),
                              hooks_env, cwd=str(repo))[1]
        plain = run([sys.executable, str(hooks / "agent_hooks.py"), "context", "codex"], json.dumps({"cwd": str(repo)}), env, cwd=str(repo))[1]
        for agent, out in outs.items():
            check("a chat from the web UI" in out and not re.search(r"(CLAUDE|CODEX|GEMINI)-1", out) and " register" not in out,
                  f"chat {agent}: a chat turn's start context lists no tasks and asks nothing to register", out[:600])
        check("CODEX-1" in plain, "chat: the same folder outside a chat still lists its tasks", plain[:300])
        hints = run([sys.executable, str(hooks / "agent_hooks.py"), "hints"], json.dumps({"conversationId": "chat-conv", "workspacePaths": [str(repo)]}), hooks_env, cwd=str(repo))[1]
        run([sys.executable, str(hooks / "agent_hooks.py"), "tasks", "codex"], json.dumps({"session_id": "chat-s1", "cwd": str(repo), "prompt": "pak CODEX-1"}), hooks_env, cwd=str(repo))
        ready = json.loads(http("GET", "/tasks/ready", headers={"Authorization": f"Bearer {tok}", "X-Knowledge-Machine": mid})[1])["tasks"]
        check(" register" not in hints and not any(t["id"] == "CODEX-1" and t.get("mine") for t in ready),
              "chat: a chat turn's per-turn hooks take no task (not even 'pak') and ask agy nothing", hints[:300] + json.dumps(ready)[:300])
        # Stop: a turn that keeps working ends at once, the chat says so and is ready again.
        cid = new("codex", repo)
        send(cid, "SLOW please")
        started = wait_for(lambda: any(c["chat"] == cid for c in recorded()) and chat(cid)["chat"]["status"] == "running", 30)
        t0 = time.time()
        http("POST", "/ui/api/chat/stop", {"machine": mid, "id": cid}, ui)
        stopped = idle(cid, 3)
        events = chat(cid)["events"]
        check(started and stopped and time.time() - t0 < 30 and events[-1]["text"] == "Stopped." and not Path(str(calls) + ".slow-done").exists(),
              "chat: stop ends a running turn (the agent's process tree) and the chat says so", json.dumps(events)[-400:])
        # Stop before the agent took the message: its turn must not go on behind a chat that says Stopped.
        for agent in ("claude", "codex"):
            cid = new(agent, repo)
            send(cid, "LATE SLOW")
            started = wait_for(lambda: any(c.get("chat") == cid for c in recorded()), 30)
            http("POST", "/ui/api/chat/stop", {"machine": mid, "id": cid}, ui)
            time.sleep(4)  # the agent took it by now
            check(started and idle(cid, 2) and chat(cid)["events"][-1]["text"] == "Stopped." and not any("by itself" in e["text"] for e in chat(cid)["events"]),
                  f"chat {agent}: stop before the agent took the message interrupts its turn when it starts", json.dumps(chat(cid)["events"])[-400:])
        # A folder unlinked after the chat began: the companion refuses the turn, loudly (chat and activity log).
        second = repos["Second repo"]
        cid = new("agy", second)
        (second / ".knowledge-project").unlink()
        send(cid, "still there?")
        idle(cid, 2)
        events = chat(cid)["events"]
        activity = state_of(home, "activity.json")
        check(any(e["kind"] == "error" and "not a folder linked" in e["text"] for e in events) and not any(c["chat"] == cid for c in recorded()),
              "chat: a folder no longer linked on this pc is refused, the agent never starts", json.dumps(events)[-400:])
        check(any(a["kind"] == "chat" and "turn started" in a["text"] for a in activity) and any(a["kind"] == "chat" and "stopped" in a["text"] for a in activity)
              and any(a["kind"] == "error" and "chat turn failed" in a["text"] for a in activity),
              "chat: turns, stops and failures are in the companion's activity log", json.dumps(activity[:8])[:800])
    finally:
        proc.kill()


def follow_e2e(tok: str, tmp: Path):
    """Follow, steer and take over a worker from the web UI, for each agent (fake agents that print their real
    headless formats and run the plugin's watch hook after each tool call): a running worker nobody follows sends
    nothing; followed, its text and tool lines arrive (secrets masked); a steer message reaches the agent after its next
    step in its own session; one that ends unwatched leaves its last events; a take-over stops it and its conversation
    goes on as a chat in its worktree that resumes the worker's session, the line claimed for the owner."""
    hooks = CLIENT / "plugin" / "hooks"
    home = tmp / "Follow Usér"
    bin_dir, calls = home / "bin", home / "calls.jsonl"
    bin_dir.mkdir(parents=True)
    (bin_dir / "fakeagent.py").write_text(FAKE_AGENT, encoding="utf-8")
    for name in ("codex", "claude", "agy"):
        if WIN:
            (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0fakeagent.py" {name} %*\r\n', encoding="utf-8")
        else:
            (bin_dir / name).write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fakeagent.py"}" {name} "$@"\n', encoding="utf-8")
            (bin_dir / name).chmod(0o755)
    repo = home / "code" / "Follow repo"
    repo.mkdir(parents=True)
    (repo / ".knowledge-project").write_text("garden\n", encoding="utf-8")
    (repo / "README.md").write_text("follow\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "t"], ["config", "user.email", "t@x"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    port = free_port()
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                   KNOWLEDGE_COMPANION_PORT=str(port), KNOWLEDGE_COMPANION_EVERY="3600", KN_FAKE_CALLS=str(calls),
                   KNOWLEDGE_COMPANION_ACTIONS_EVERY="0.2", KNOWLEDGE_COMPANION_SCAN_EVERY="0", KN_FAKE_FOLLOW=str(hooks / "watch.py"))
    ui = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice", "X-Knowledge-UI": "1"}
    key_file = home / ".knowledge" / "companion" / "api-key"

    def local(path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=json.dumps(body).encode(), headers={
            "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json", "X-Companion-Key": key_file.read_text(encoding="utf-8").strip()})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    # a session in the folder makes it known here (before the lines exist: it must not take one of them)
    run([sys.executable, str(hooks / "agent_hooks.py"), "tasks", "codex"], json.dumps({"session_id": "fol-cx", "cwd": str(repo)}), env, cwd=str(repo))
    ids = {"claude": "FOL-CLAUDE", "codex": "FOL-CODEX", "agy": "FOL-GEMINI"}
    owners = {"claude": "Claude", "codex": "Codex", "agy": "Gemini"}
    lines = "".join(f"- open tot 1 jan 2099 [{owners[a]}, {i}] from Claude, start now: follow test {i}\n" for a, i in ids.items())
    http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write", "arguments": {
        "path": "garden-memory/project_follow.md", "content": f"---\nname: f\ndescription: d\nmetadata:\n  type: project\n---\n## Stand\n{lines}\n## Keuzes\n"}}},
         {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
    log = open(home / "companion.log", "wb")
    proc = subprocess.Popen([sys.executable, str(hooks / "agent_hooks.py"), "serve"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    started = lambda: [json.loads(x) for x in calls.read_text(encoding="utf-8").splitlines() if '"worker"' in x] if calls.exists() else []
    try:
        if not check(wait_for(lambda: key_file.exists(), 30), "follow: the companion runs", (home / "companion.log").read_text(errors="replace")[-600:]):
            return
        mid = json.loads((home / ".knowledge" / "machine.json").read_text(encoding="utf-8"))["id"]
        row = lambda: next((r for r in machines() if r["id"] == mid), {})
        # its first round is over (it reports last): one still running would see workers on and start other tests' lines
        check(wait_for(lambda: row().get("companion"), 60), "follow: the companion's first round reports", (home / "companion.log").read_text(errors="replace")[-600:])
        local("/local/api/settings", {"workersAllowed": True})
        for i in ids.values():  # only these (other tests' lines stay alone); off again after, so a later round starts nothing
            http("POST", "/ui/api/machines/action", {"machine": mid, "action": "start_worker", "args": {"project": "garden", "id": i}}, ui)
        wait_for(lambda: len(started()) == 3, 60)
        local("/local/api/settings", {"workersAllowed": False})
        workers_of = lambda: {w["id"]: w for a in row().get("agents", {}).values() for w in a.get("workers", [])}
        ok = wait_for(lambda: len(started()) == 3 and all(workers_of().get(i, {}).get("status") == "running" for i in ids.values()), 60)
        if not check(ok, "follow: one worker per agent runs and is reported", json.dumps({"started": [x.get("worker") for x in started()], "reported": {i: (w.get("status"), w.get("agent"), w.get("why")) for i, w in workers_of().items()}, "at": row().get("at"), "now": time.time(), "actions": [(a["action"], a["status"], str(a.get("result"))[:120]) for a in row().get("actions", [])][-6:]})
                     + (home / "companion.log").read_text(errors="replace")[-600:] + (home / ".knowledge" / "companion" / "activity.json").read_text(errors="replace")[:1500]):
            return
        view = lambda a, peek=False, after=0: json.loads(http("GET", f"/ui/api/worker?machine={mid}&worker=garden/{ids[a]}&after={after}" + ("&peek=1" if peek else ""), headers=ui)[1])
        texts = lambda a, peek=False: "".join(e["text"] for e in view(a, peek)["events"])
        time.sleep(4)
        check(all(not view(a, peek=True)["events"] for a in ids), "follow: running workers nobody follows upload nothing",
              json.dumps({a: view(a, peek=True)["events"][:3] for a in ids})[:600])
        for a in ids:
            got = wait_for(lambda: f"{a} starts {ids[a]}" in texts(a) and (a == "agy" or any(e["kind"] == "tool" and "git status" in e["text"] for e in view(a)["events"])), 30)
            check(got and view(a)["following"], f"follow {a}: followed, its text and tool lines arrive", json.dumps(view(a))[:600])
        # Steer: a user line now; the agent hears it after its next step (its PostToolUse hook), in its own session.
        for a in ids:
            st, body = http("POST", "/ui/api/worker/steer", {"machine": mid, "worker": f"garden/{ids[a]}", "text": f"use sqlite {a}"}, ui)
            check(st == 200, f"steer {a}: the owner's message is taken", body)
        for a in ids:
            got = wait_for(lambda: f"use sqlite {a}" in texts(a).split("heard:")[-1] and "Delivered" in texts(a), 40)
            ev = view(a)["events"]
            check(got and any(e["kind"] == "user" and e["text"] == f"use sqlite {a}" for e in ev),
                  f"steer {a}: a user line, then the worker heard it after its next tool call and the transcript says delivered", json.dumps(ev)[-700:])
        for a in ids:
            (Path(f"{calls}.{ids[a]}.go")).write_text("", encoding="utf-8")
        for a in ids:
            got = wait_for(lambda: f"{a} go {ids[a]}" in texts(a), 30)
            t = texts(a)
            check(got and "hunter22hunter" not in t and "Sup3rSecret99" not in t, f"follow {a}: new output streams in, secrets masked", t[-300:])
        # Codex and agy end while nobody follows: nothing until the companion's round sees them end, then their last events.
        time.sleep(5)  # the follow runs out (3 s in this test)
        for a in ("codex", "agy"):
            (Path(f"{calls}.{ids[a]}.end")).write_text("", encoding="utf-8")
        time.sleep(4)
        check(all(f"end {ids[a]}" not in texts(a, peek=True) for a in ("codex", "agy")), "follow: output of a worker nobody follows is not sent while it runs")
        local("/local/api/tick", {})
        for a in ("codex", "agy"):
            v = view(a, peek=True)
            check(f"{a} end {ids[a]}" in "".join(e["text"] for e in v["events"]) and v["worker"]["status"] != "running",
                  f"follow {a}: a worker that ended unwatched keeps its last events", json.dumps(v)[-500:])
        # Take over: Claude still runs (stopped now), Codex and agy ended; each becomes a chat that resumes its session.
        recorded = lambda: [json.loads(x) for x in calls.read_text(encoding="utf-8").splitlines() if '"chat"' in x]
        for a in ids:
            st, body = http("POST", "/ui/api/machines/action", {"machine": mid, "action": "takeover", "args": {"project": "garden", "id": ids[a]}}, ui)
            cid = ""
            if st == 200 and wait_for(lambda: bool(view(a, peek=True)["worker"].get("chat")), 40):
                cid = view(a, peek=True)["worker"]["chat"]
            wt = next((w for w in started() if w["worker"] == ids[a]), {}).get("cwd", "")
            if not check(bool(cid), f"take over {a}: the worker becomes a chat", body + (home / "companion.log").read_text(errors="replace")[-600:]):
                continue
            chat = lambda: json.loads(http("GET", f"/ui/api/chat?machine={mid}&id={cid}", headers=ui)[1])
            c = chat()
            check(c["chat"]["session"] == f"sess-{a}" and Path(c["chat"]["folder"]).resolve() == Path(wt).resolve() and any("Taken over" in e["text"] for e in c["events"]),
                  f"take over {a}: the chat holds the worker's session, worktree and transcript", json.dumps(c["chat"]) + wt)
            http("POST", "/ui/api/chat/send", {"machine": mid, "id": cid, "text": f"carry on {a}"}, ui)
            got = wait_for(lambda: (x := chat())["chat"]["status"] == "idle" and f"echo: carry on {a} (resumed)" in "".join(e["text"] for e in x["events"]), 40)
            turn = next((r for r in recorded() if r["chat"] == cid), {})
            check(got and Path(turn.get("cwd", ".")).resolve() == Path(wt).resolve(), f"take over {a}: the chat's turn resumes the worker's own session in its worktree",
                  json.dumps(chat()["events"][-3:]) + json.dumps(turn)[:300])
            if a == "claude":
                check(wait_for(lambda: all(r.get("pid") is None or r["worker"] != ids[a] or not _alive(r["pid"]) for r in started()), 20),
                      "take over claude: the running worker is stopped")
        memo = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_read", "arguments": {"path": "garden-memory/project_follow.md"}}},
                    {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})[1]
        check(all(re.search(rf"{i}\] from Claude, start now: follow test {i} \(claimed alice via chat, ", memo) for i in ids.values()),
              "take over: each line is claimed by the owner via chat (no worker starts it again)", memo[-600:])
    finally:
        for a in ids:
            for stage in ("go", "end"):
                Path(f"{calls}.{ids[a]}.{stage}").write_text("", encoding="utf-8")
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _alive(pid: int) -> bool:
    if WIN:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in r.stdout
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")  # a zombie has ended


def _quiet(f):
    try:
        return f()
    except OSError:
        return False


def hk_max(groups: list) -> int:
    return max(h["timeout"] for g in groups for h in g["hooks"])


def terminal_asks(tmp: Path):
    """A terminal session that asks approval leaves a mark the companion reports, in every agent, through the commands
    their own hook files name; the tool having run clears it, and a chat leaves none."""
    root, sh = str(REPO / "plugin"), bash()
    claude = json.loads((REPO / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    codex = json.loads((REPO / "plugin" / ".codex-plugin" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    agy = json.loads((REPO / "plugin" / "hooks.json").read_text(encoding="utf-8"))["knowledge"]
    one = lambda groups, word: next(h for g in groups for h in (g.get("hooks") or [g]) if word in h["command"])
    def as_codex(hk):
        return codex_hook_argvs(hk, REPO / "plugin")[0][1]
    check(hk_max(codex["Interrupt"]) <= 3 and "idle_prompt" == claude["Notification"][0]["matcher"],
          "Esc at a prompt ends no turn: Codex's Interrupt (at most 3 s, or it warns at each start) and Claude's idle_prompt clear the mark")
    agents = {"claude": (lambda hk: [sh, "-c", hk["command"]], one(claude["PermissionRequest"], "asks"), one(claude["Stop"], "settled"), {"session_id": "t-c", "tool_name": "Bash", "tool_input": {"command": "ls"}}, None),
              "codex": (as_codex, one(codex["PermissionRequest"], "asks"), one(codex["Stop"], "settled"), {"session_id": "t-x", "tool_name": "Bash", "tool_input": {"command": "ls"}}, None),
              "agy": ((lambda hk: f'cmd /d /s /c "{hk["command"]}"' if WIN else [shutil.which("sh"), "-c", hk["command"]]), one(agy["PreToolUse"], " asks"), one(agy["Stop"], "settled"),
                      {"conversationId": "t-a", "workspacePaths": [str(tmp)], "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}, root)}
    for name, (argv, asks, done, hook, cwd) in agents.items():
        kn = tmp / f"asks-{name}"
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("KNOWLEDGE_")}, "KNOWLEDGE_HOME": str(kn), "CLAUDE_PLUGIN_ROOT": root}
        marks = lambda: [json.loads(f.read_text(encoding="utf-8")) for f in (kn / "asks").glob("*.json")]
        code, out, err = run(argv(asks), json.dumps(hook), env, cwd=cwd or str(tmp))
        got = marks()
        check(code == 0 and out.strip() == "" and len(got) == 1 and got[0]["agent"] == name and got[0]["detail"] == "ls",
              f"{name}: a terminal session about to ask leaves a mark, and the hook decides nothing", f"{code} {out[:200]} {err[-300:]} {got}")
        code, out, err = run(argv(done), json.dumps(hook), env, cwd=cwd or str(tmp))
        check(code == 0 and out.strip() == "" and not marks(), f"{name}: the turn's end clears the mark", f"{code} {out[:200]} {err[-300:]}")
        code, out, err = run(argv(asks), json.dumps(hook), {**env, "KNOWLEDGE_CHAT": "abc"}, cwd=cwd or str(tmp))
        check(code == 0 and not marks(), f"{name}: a chat from the web UI leaves none: it has its own approvals", err[-300:])


def new_task(agent: Path, env: dict, project: Path, tok: str, home: Path):
    """A task handed to Codex while its session runs reaches the next prompt once; the first prompt only remembers."""
    ask = lambda: run([sys.executable, str(agent), "tasks", "codex", "-"],
                      json.dumps({"session_id": "long-1", "cwd": str(project), "prompt": "go on"}), env, cwd=str(project))[1]
    rewind = lambda: [f.write_text(json.dumps({**json.loads(f.read_text()), "at": 0})) for f in (home / ".knowledge" / "tasks-seen").glob("*.json")]
    first = ask()  # other sessions of this device took the open tasks in earlier checks: it hears to leave those alone
    said = json.loads(first)["hookSpecificOutput"]["additionalContext"] if first else ""
    check(all("is already being handled by" in p for p in said.split("\n\n") if p.strip()) and "new task" not in said,
          "tasks: the first prompt of a session says no task (the start context listed the open tasks), only which ones others hold", first[:600])
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
    http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_edit", "arguments":
         {"path": "garden-memory/project_handover.md", "old_string": "## Keuzes", "new_string": "- [Codex, RES-1-done]: clips installed\n- [Claude, RES-2-done]: not yours\n## Keuzes"}}},
         {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
    rewind()
    out = ask()
    check("RES-1-done: clips installed" in out and "remove that line with knowledge_edit" in out and "RES-2-done" not in out,
          "tasks: a result handed back to this agent reaches the next prompt, another agent's does not", out[:400])
    rewind()
    check(ask() == "", "tasks: a result is said once per session")


def wake_e2e(tok: str, home: Path, project: Path):
    """Tasks never go into a chat (they run in workers): a chat hears a new task once as "runs in workers, say 'pak
    <ID>'" and takes no lease; 'pak <ID>' in the user's words takes that task for the chat (Claude and Codex from the
    prompt, agy with the take command in its terminal); a knowledge write without a companion here says nothing. agy
    still registers its language server (its usage limit comes from there)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    hooks = CLIENT / "plugin" / "hooks" / "agent_hooks.py"
    csrf, busy = "gategategate", threading.Event()

    class LS(BaseHTTPRequestHandler):  # agy's language server, as far as the plugin uses it
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if busy.is_set():
                reply, code = {}, 503
            elif self.headers.get("x-codeium-csrf-token") != csrf:
                reply, code = {"code": "unauthenticated"}, 401
            elif self.path.endswith("/GetAllCascadeTrajectories"):
                reply, code = {"trajectorySummaries": {"conv-1": {"lastModifiedTime": "2026-10-27T08:00:00Z",
                               "workspaces": [{"workspaceFolderAbsoluteUri": project.as_uri()}]}}}, 200
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
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, KNOWLEDGE_COMPANION_PORT=str(free_port()))
    hook = lambda args, payload, extra=None: run([sys.executable, str(hooks), *args], json.dumps(payload), {**env, **(extra or {})}, cwd=str(project))
    rewind = lambda: [f.write_text(json.dumps({**json.loads(f.read_text()), "at": 0})) for f in (home / ".knowledge" / "tasks-seen").glob("*.json")]
    pc = lambda: {"X-Knowledge-Machine": json.loads((home / ".knowledge" / "machine.json").read_text(encoding="utf-8"))["id"]}
    leases = lambda: json.loads(http("GET", "/tasks/ready", headers={"Authorization": f"Bearer {tok}", **pc()})[1])["tasks"]  # "mine": this pc holds it
    try:
        hook(["tasks", "codex", "-"], {"session_id": "cx-1", "cwd": str(project)})
        hook(["tasks", "claude", "-"], {"session_id": "cl-1", "cwd": str(project)})
        code, out, err = hook(["hints", "-"], {"conversationId": "conv-w", "workspacePaths": [str(project)]}, {"KNOWLEDGE_WORKER": "W-1"})
        code2, out2, err2 = hook(["context", "agy", "-"], {"conversationId": "conv-w", "workspacePaths": [str(project)]}, {"KNOWLEDGE_WORKER": "W-1"})
        check(" register" not in out and " register" not in out2,
              "agy: a worker is never asked to register (headless it may not run that command, and a refusal ends its run)", out[:300] + out2[-300:])
        code, out, err = hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        check("agent_hooks.py" in out and " register" in out, "agy: an unregistered agy conversation is asked once to register", out[:300] + err[-300:])
        code, out, err = hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        check("register" not in out, "agy: the register request is not repeated", out[:300])
        code, out, err = hook(["register"], {}, {"ANTIGRAVITY_LS_ADDRESS": f"127.0.0.1:{ls.server_address[1]}", "ANTIGRAVITY_CSRF_TOKEN": csrf,
                                                 "ANTIGRAVITY_CONVERSATION_ID": "conv-1"})
        check(code == 0 and "registered" in out, "agy: registers from its terminal", out + err)
        code, out, err = hook(["register"], {})
        check(code != 0 and "not run by agy" in (out + err), "agy: register outside agy says so", out + err)
        entry = next((home / ".knowledge" / "agents").glob("agy-ls-*.json"))
        check(WIN or (entry.stat().st_mode & 0o077 == 0 and entry.parent.stat().st_mode & 0o077 == 0),
              "agy: its entry (its key) and the registry folder are private to the user")
        busy.set()
        hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        busy.clear()
        check(entry.exists(), "agy: a busy agy (HTTP 503) keeps its registration")
        # this pc's companion runs Codex and agy workers for the project; no pc runs Claude's: Claude's chats take its tasks
        http("POST", "/machine/report", {"version": "0.15.0", "workersAllowed": True, "maxWorkers": {"codex": 1, "agy": 1, "claude": 1},
                                         "agents": {"codex": {}, "agy": {}}, "folders": {"linked": [{"path": str(project), "project": "garden"}]}},
             {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", **pc()})
        tasks = "".join(f"- open tot 1 jan 2099 [{o}, WAKE-{o.upper()}1] from Claude, start now: chat test for {o} \"a & b > c %PATH%\n" for o in ("Codex", "Claude", "Gemini"))
        http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write", "arguments":
             {"path": "garden-memory/project_wake.md", "content": "---\nname: w\ndescription: d\nmetadata:\n  type: project\n---\n"
              f"## Stand\n{tasks}\n## Keuzes\n"}}}, {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
        code, out, err = hook(["wake", "codex", "-"], {"session_id": "sender", "cwd": str(project), "tool_name": "mcp__knowledge__knowledge_write"})
        check(code == 0 and out.strip() == "{}", "chat: a knowledge write with no companion on this pc says nothing and starts nothing", out + err)
        rewind()
        code, out, err = hook(["tasks", "codex", "-"], {"session_id": "cx-1", "cwd": str(project), "prompt": "go on"})
        said = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
        mine = {t["id"]: t for t in leases()}
        check("WAKE-CODEX1" in said and "run in workers" in said and "'pak <ID>'" in said and "WAKE-CLAUDE1" not in said
              and mine.get("WAKE-CODEX1", {}).get("mine") is False,
              "chat: a new task is said once as 'runs in workers, say pak', and the chat takes no lease", said[:500] + json.dumps(mine.get("WAKE-CODEX1")))
        code, out, err = hook(["context", "agy", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        ctx = json.loads(out)["injectSteps"][0]["systemMessage"]["systemMessage"] if out.strip() else ""
        check("WAKE-GEMINI1" in ctx and "'pak <ID>'" in ctx and " take agy <ID>" in ctx,
              "chat: agy's session start says how to take a task on 'pak <ID>' (the take command; its hooks never see the user's words)", ctx[-700:] + err[-300:])
        code, out, err = hook(["tasks", "claude", "-"], {"session_id": "cl-1", "cwd": str(project), "prompt": "pak wake-claude1 please"})
        said = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
        check("took task WAKE-CLAUDE1" in said and "(claimed Claude," in said and mine["WAKE-CLAUDE1"]["mine"] is False
              and next(t for t in leases() if t["id"] == "WAKE-CLAUDE1")["mine"] is True,
              "chat: 'pak <id>' in the user's words, typed in lowercase, takes that task for this Claude session", said[:500])
        check("start it right away" in said and "run in workers" not in said,
              "chat: with no pc running Claude workers for the project, a new task is said as 'start it right away' (as 0.14)", said[:600])
        code, out, err = hook(["tasks", "claude", "-"], {"session_id": "cl-2", "cwd": str(project), "prompt": "pak Wake-Claude1"})
        check("held by another session" in out, "chat: a second chat cannot take a task another session took", out[:400])
        code, out, err = hook(["take", "agy", "WAKE-GEMINI1"], {}, {"ANTIGRAVITY_CONVERSATION_ID": "conv-1"})
        check(code == 0 and "took task WAKE-GEMINI1" in out, "chat: agy takes a task with the take command in its terminal (its hooks never see the user's words)", out + err)
        code, out, err = hook(["hints", "-"], {"conversationId": "conv-2", "workspacePaths": [str(project)]})
        hook(["hints", "-"], {"conversationId": "conv-2", "workspacePaths": [str(project)]})
        hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})  # registered: the ask is reset
        ls.shutdown(); ls.server_close()  # agy resumed: its old port is gone
        code, out, err = hook(["hints", "-"], {"conversationId": "conv-1", "workspacePaths": [str(project)]})
        check(" register" in out and "restarted" in out, "agy: a resumed agy (its registration gone) is told so and asked again",
              out[:300] + err[-300:])
        for tid in ("WAKE-CLAUDE1", "WAKE-GEMINI1"):
            http("POST", "/tasks/release", {"project": "garden", "id": tid}, {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", **pc()})
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
                "PreToolUse": {"hook_event_name": "PreToolUse", "session_id": "cx-4", "cwd": str(project), "tool_name": "spawn_agent"},
                "PermissionRequest": {"hook_event_name": "PermissionRequest", "session_id": "cx-5", "cwd": str(project), "tool_name": "Bash", "tool_input": {"command": "ls"}},
                "Stop": {"hook_event_name": "Stop", "session_id": "cx-5", "cwd": str(project)},
                "Interrupt": {"hook_event_name": "Interrupt", "session_id": "cx-5", "cwd": str(project)}}
    check(set(hooks) == set(payloads) and len(hooks["SessionStart"][0]["hooks"]) == 1,
          "codex plugin: the nine hook events, one SessionStart handler (openai/codex#45999)", str(list(hooks)))
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
    if not WIN:  # a keyring tool that finds nothing: Windows has the real Credential Manager in-process
        fake = home / "No Keyring"
        fake.mkdir(exist_ok=True)
        for tool in ("secret-tool", "security"):
            (fake / tool).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (fake / tool).chmod(0o755)
        none = {k: v for k, v in env.items() if k != "KNOWLEDGE_TOKEN"}
        none = {**none, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "HOME": str(fake)}
        code, out, err = run([exe, *server["args"]], json.dumps(msgs[2]) + "\n", none, cwd=str(root))
        check("no token is stored" in out and "install.sh" in out and "token invalid" not in out,
              "codex plugin: no token says so and what to run, not that it is invalid", out[:300] + err[-300:])
        code, out, err = run([bash(), "-c", f'sh "{(root / "hooks" / "context.sh").as_posix()}" 1'], "",
                             {**none, "KNOWLEDGE_URL": BASE, "CLAUDE_PROJECT_DIR": str(fake)})
        check("no token is stored" in out and "token invalid" not in out,
              "claude: no token says so at session start, not that it is invalid", out[:300] + err[-300:])
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
    want = sum(len(m["hooks"]) for ms in json.loads((root / ".codex-plugin" / "hooks.json").read_text(encoding="utf-8"))["hooks"].values() for m in ms)
    check(len(listed) == want and not problems, "codex: hooks/list shows all of the plugin's hooks, no warnings",
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
    payloads = {"asks": {**common, "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}, "settled": common,
                "context": common, "session-log": {**common, **usage_hook(home, "agy", "agy-plugin-usage")}, "hints": {**common, "invocationNum": 1},
                "docs-sync": {**common, "toolCall": {"name": "write_file", "args": {"Path": json.dumps(str(project / "docs" / "agy.md"))}}},
                "wake": {**common, "toolCall": {"name": "call_mcp_tool", "args": {"ToolName": "knowledge_search"}}},
                "gate": {**common, "toolCall": {"name": "invoke_subagent"}},
                "guard": {**common, "toolCall": {"name": "write_file", "args": {"Path": "a.md", "Content": "clean text"}}},
                "watch": {**common, "toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf ~/x"}}}}
    (project / "docs" / "agy.md").write_text("agy plugin → ✅\n", encoding="utf-8")
    for cmd in cmds:
        kind = next(k for k in payloads if f" {k}" in cmd or f"/{k}.py" in cmd)  # watch.py runs as its own script
        code, out, err = run(f'cmd /d /s /c "{cmd}"' if WIN else [shutil.which("sh"), "-c", cmd], json.dumps(payloads[kind]), env, cwd=str(root))
        check(code == 0 and "skipped" not in err, f"agy plugin: {kind} hook runs in the plugin folder", err[-600:])
        if kind == "watch":  # agy's PostToolUse may only answer {}
            check(out.strip() == "{}", "agy plugin: the watch hook answers {}", out[:300])
        if kind == "gate":  # agy reads any output, even {}, as a verdict: {} denied every subagent (27 Oct 2026)
            check(out.strip() == "", "agy plugin: below the limit the subagent hook prints nothing", out[:300])
        if kind in ("asks", "settled"):  # the same: on every tool, {} would deny them all
            check(out.strip() == "", f"agy plugin: the {kind} hook prints nothing", out[:300])
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
    # A failed update of an older version left the folder without a plugin: another agent's hook puts it back.
    folder = home / ".gemini" / "config" / "plugins" / "knowledge"
    (home / ".gemini" / "config" / "hooks.json").write_text("{}", encoding="utf-8")
    (home / ".knowledge" / "auto-update.json").write_text(json.dumps({"agy:result": "knowledge plugin: the auto-update to 9.9.9 failed"}), encoding="utf-8")
    shutil.rmtree(folder)
    folder.mkdir()
    os.utime(folder, (1, 1))
    heal = "import self_update, sys; started = self_update.heal_agy(); sys.exit(0 if started else 3)"
    code, out, err = run([sys.executable, "-X", "utf8", "-c", heal], "", {**env, "KNOWLEDGE_AGY_PLUGIN": str(root)}, cwd=str(root / "hooks"))
    check(code == 0, "agy: another agent's hook sees the emptied plugin folder", out + err)
    check(wait_for(lambda: (folder / ".claude-plugin" / "plugin.json").exists(), 120), "agy: the emptied plugin folder is installed again", out + err)


def import_e2e(tok: str, tmp: Path, garden: Path):
    """knowledge-agent.py import: a dry run lists only the known places, the import goes through knowledge_new_memo
    (sections, index line, a known secret refused), a rerun imports nothing new and an existing memo is never replaced.
    The home has a space and an "é" (on Windows a real C:\\ path)."""
    agent, home = CLIENT / "clients" / "knowledge-agent.py", tmp / "Import Usér"
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok)
    house = "<!-- knowledge:house-rules (written by knowledge-agent.py install) -->\nHOUSE-BLOCK\n<!-- /knowledge:house-rules -->\n"
    files = {".claude/CLAUDE.md": "# Mine\n\nanswer short\n", ".codex/AGENTS.md": "codex rule one\n\n" + house,
             ".gemini/GEMINI.md": "deploy with Sup3rSecret99\n", ".claude/projects/-x-proj/memory/MEMORY.md": "# Index\n",
             ".claude/projects/-x-proj/memory/feedback_short.md": "---\nname: feedback_short\ndescription: Keep answers short\n"
             "metadata:\n  type: feedback\n---\n\n## Status\nshort answers\n\n## Decisions\n", "Linked Proj/CLAUDE.md": "linked rules\n",
             "Linked Proj/.knowledge-project": "garden\n", "Other/CLAUDE.md": "not linked\n", "notes.md": "elsewhere\n"}
    for rel, text in files.items():
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(text, encoding="utf-8")
    (home / ".claude.json").write_text(json.dumps({"projects": {str(home / "Linked Proj"): {}, str(home / "Other"): {}}}), encoding="utf-8")
    local_mem = home / ".claude/projects/-x-proj/memory"
    rnd = hashlib.sha256(b"e2e").hexdigest()[:24]  # built at run time: no secret-looking literal for the pre-push scan
    (local_mem / "reference_keys.md").write_text(f"deploy key\napi_key = {rnd}\n", encoding="utf-8")
    (home / "Elsewhere").mkdir()
    (home / "Elsewhere" / "private.md").write_text("private elsewhere\n", encoding="utf-8")
    try:
        os.symlink(home / "Elsewhere" / "private.md", local_mem / "reference_link.md")
        linked_file = True
    except OSError:  # Windows without the symlink privilege
        linked_file = False
    mem = garden / "memory"
    imported = lambda: sorted(f.name for f in mem.glob("*.md") if "knowledge-agent.py import" in f.read_text(encoding="utf-8"))
    code, out, err = run([sys.executable, str(agent), "import", "--dry-run"], "", env)
    listed = [ln for ln in out.splitlines() if re.match(r"  \d+\. ", ln)]
    want = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", "feedback_short.md", "Linked Proj")
    check(code == 0 and len(listed) == 5 and all(any(w in ln for ln in listed) for w in want) and "bytes) -> " in out
          and "MEMORY.md" not in out and "Other" not in out and "notes.md" not in out and imported() == [],
          "import --dry-run: lists the five known files (file, size, target), nothing else, writes nothing", out + err)
    check("reference_keys.md: looks like it holds a secret" in out, "import: a file that looks like it holds an unknown secret is skipped", out)
    check(not linked_file or ("reference_link.md: a link to a file elsewhere" in out and "private elsewhere" not in out),
          f"import: a symlink out of its folder is not read ({'tested' if linked_file else 'no symlink privilege'})", out)
    code, out, err = run([sys.executable, str(agent), "import"], "", env)
    names = imported()
    texts = {n: (mem / n).read_text(encoding="utf-8") for n in names}
    index = (mem / "MEMORY.md").read_text(encoding="utf-8") if (mem / "MEMORY.md").exists() else ""
    check(code == 0 and len(names) == 4 and out.count(": imported") == 4, "import: four memos through knowledge_new_memo", out + err)
    check("SKIPPED" in out and "known secret" in out and not any("Sup3rSecret99" in t for t in texts.values()),
          "import: a file with a known secret is refused and reported", out + err)
    check(all("## Status" in t and "## Decisions" in t and t.startswith("---") for t in texts.values())
          and "## Imported" in index and all(f"]({n})" in index for n in names) and not any("HOUSE-BLOCK" in t for t in texts.values())
          and any(n.startswith("feedback_") for n in names), "import: frontmatter, both sections, index lines; the house-rules block left out",
          json.dumps(texts)[:800] + index[-400:])
    code, out, err = run([sys.executable, str(agent), "import"], "", env)
    check(code == 0 and imported() == names and "imported before" in out and ": imported" not in out,
          "import again: nothing new is imported (local record)", out + err)
    (home / ".knowledge" / "imported.json").unlink()
    before = {n: (mem / n).read_text(encoding="utf-8") for n in names}
    code, out, err = run([sys.executable, str(agent), "import"], "", env)
    check(code == 0 and out.count("exists, left as it is") == 4 and before == {n: (mem / n).read_text(encoding="utf-8") for n in names},
          "import without its record: existing memos are never replaced", out + err)
    # a token for several projects: Claude memory goes to its folder's linked project; an unlinked folder's is skipped
    own = tmp / "Import Owner"
    for folder, linked in (("Linked Proj", True), ("Other", False)):
        (own / folder).mkdir(parents=True)
        if linked:
            (own / folder / ".knowledge-project").write_text("garden\n", encoding="utf-8")
        m = own / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(own / folder)) / "memory"
        m.mkdir(parents=True)
        (m / f"project_{folder[:5].lower().strip()}_note.md").write_text(f"note of {folder}\n", encoding="utf-8")
    (own / ".claude.json").write_text(json.dumps({"projects": {str(own / "Linked Proj"): {}, str(own / "Other"): {}}}), encoding="utf-8")
    code, out, err = run([sys.executable, str(agent), "import"], "", user_env(own, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN="owner-token"))
    check(code == 0 and (mem / "reference_project_linke_note.md").is_file() and "project_other_note.md: its folder is not linked" in out
          and not list(tmp.glob("**/reference_project_other_note.md")),
          "import with a multi-project token: memory goes to its folder's linked project, an unlinked folder's is skipped", out + err)
    # the machine the server reads its files from: its local memory is the server's copy, so nothing is imported
    src = tmp / "Import Source" / ".claude" / "projects" / "-s" / "memory"
    src.mkdir(parents=True)
    (src / "MEMORY.md").write_bytes((mem / "MEMORY.md").read_bytes())
    (src / "project_source_note.md").write_text("from the source\n", encoding="utf-8")
    code, out, err = run([sys.executable, str(agent), "import"], "", user_env(tmp / "Import Source", KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok))
    check(code == 0 and "holds the server's own files" in out and not (mem / "project_source_note.md").exists(),
          "import on the device that holds the server's files: nothing imported, and it says so", out + err)


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
        check(out.rstrip().splitlines()[-1].startswith("Try it:"), "setup: the last line says what to try", out[-300:])
        check(code == 0 and "Already set up (" in out and all(f"== {a} ==" in out for a in have) and "== agy ==" not in out
              and "== companion (" in out and re.search(r'companion\.py"? trust', out),
              f"setup (the one-line installer), once set up: every agent with the plugin is updated ({', '.join(have)}), no questions", (out + err)[-1500:])
        choice = home / ".knowledge" / "install.json"
        code, out, err = run([sys.executable, str(agent), "setup", "--plugin-only"], "", env, timeout=600)
        check(code == 0 and "== companion (" not in out and "not installed (plugin only)" in out
              and json.loads(choice.read_text(encoding="utf-8")) == {"companion": False},
              "setup --plugin-only: the plugin is updated, no companion steps, the choice is saved", (out + err)[-1500:])
        code, out, err = run([sys.executable, str(agent), "update"], "", env, timeout=600)
        check(code == 0 and "== companion (" not in out and "not installed (plugin only)" in out,
              "update after a plugin-only setup leaves the companion out", (out + err)[-1500:])
        code, out, err = run([sys.executable, str(agent), "install-companion"], "", env, timeout=120)
        check(code == 0 and "== companion (" in out and json.loads(choice.read_text(encoding="utf-8")) == {"companion": True},
              "install-companion saves the companion as the choice again", (out + err)[-1500:])
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
    want = {"linux": ["knowledge-companion.service", "Restart=on-failure", "KillMode=process", "Environment=", "systemctl --user enable",
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
        check(code == 0 and re.search(r'companion\.py"? yolo-off', out), f"uninstall-companion --dry-run --os {system}: YOLO off first", out + err)

    fake, log = tmp / "fake tools", tmp / "fake tools.log"
    fake.mkdir(exist_ok=True)
    for tool in ("systemctl", "launchctl", "schtasks", "powershell"):
        if WIN:
            (fake / f"{tool}.cmd").write_text(f'@echo {tool} %*>>"%KN_FAKE_LOG%"\r\n', encoding="utf-8")
        else:
            (fake / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "$KN_FAKE_LOG"\n'
                                     '[ "$2" = show-environment ] && [ -n "$KN_FAKE_NOBUS" ] && exit 1\nexit 0\n', encoding="utf-8")
            (fake / tool).chmod(0o755)
    # only the fakes and Python: the trust step finds no agent CLI in this home, so it only says so
    real = {k: v for k, v in env.items() if k != "KNOWLEDGE_COMPANION_DRY_RUN"}
    real.update(PATH=os.pathsep.join([str(fake), str(Path(sys.executable).parent)]), KN_FAKE_LOG=str(log))
    system = "windows" if WIN else "darwin" if sys.platform == "darwin" else "linux"
    files = {"linux": [".config/systemd/user/knowledge-companion.service", ".local/share/applications/knowledge-companion.desktop"],
             "darwin": ["Library/LaunchAgents/io.github.knowledge.companion.plist",
                        "Applications/Knowledge companion.app/Contents/MacOS/knowledge-companion"],
             "windows": [".knowledge/companion/autostart.xml"]}[system]
    if system == "linux":  # SSH without lingering, a container: no user bus, the setup still succeeds
        code, out, err = run([sys.executable, str(agent), "install-companion"], "", {**real, "KN_FAKE_NOBUS": "1"}, timeout=120)
        check(code == 0 and "no user session for an autostart here" in out and not (home / files[0]).exists(),
              "install-companion without a user systemd: skips the autostart, does not fail", out + err)
    if WIN:  # an OpenSSH session on a workgroup pc says USERDOMAIN=WORKGROUP, which schtasks cannot map to an account
        real["USERDOMAIN"] = "WORKGROUP"
    code, out, err = run([sys.executable, str(agent), "install-companion"], "", real, timeout=120)
    calls = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    if WIN:
        me = subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip().lower()
        xml = (home / files[0]).read_text(encoding="utf-16")
        check(f"<userid>{me}</userid>" in xml.lower() and "workgroup" not in xml.lower(),
              "install-companion on windows: the logon task names this account as Windows does, not %USERDOMAIN%", f"{me}\n{xml[:600]}")
    if system != "windows":
        key = (home / ".knowledge" / "companion" / "api-key").read_text(encoding="utf-8").strip()
        link = home / files[1]
        opener = home / ".knowledge" / "companion" / "open.html"
        check(f"#key={key}" in opener.read_text(encoding="utf-8") and opener.stat().st_mode & 0o077 == 0 and key not in out
              and key not in link.read_text(encoding="utf-8")
              and any(u in link.read_text(encoding="utf-8") for u in (opener.as_uri(), opener.as_uri().replace("%", "%%"))),  # .desktop doubles %
              f"install-companion on {system}: the shortcut opens a private file that forwards with the local key (never the key on a command line)", out[-600:])
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


def honest_errors(tok: str, tmp: Path):
    """A server that cannot be reached or a proxy that refuses: every client says so in the same generic words (no
    "home network" hint a friend cannot act on). A failed update check is told when asked (never "up to date") and
    reaches the admin page with the plugin report."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # not 'import http': that hides http() below

    class Refuse(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(403)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bad, home, hooks = f"http://127.0.0.1:{srv.server_address[1]}", tmp / "Err Usér", CLIENT / "plugin" / "hooks"
    (home / "proj").mkdir(parents=True)
    want = f"cannot reach {bad}: HTTP 403"
    try:
        start = next(h["command"] for g in json.loads((hooks / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"]
                     for h in g["hooks"] if "context.sh" in h["command"])
        code, out, err = run([bash(), "-c", start], "{}", user_env(home, CLAUDE_PLUGIN_ROOT=str(CLIENT / "plugin"),
                             CLAUDE_PLUGIN_OPTION_URL=bad, CLAUDE_PLUGIN_OPTION_TOKEN="x"), cwd=str(home / "proj"))
        check(code == 0 and want in out and "check the address" in out and "home network" not in out, "claude: a refused server is named, generic words", out + err)
        env = user_env(home, KNOWLEDGE_URL=bad, KNOWLEDGE_TOKEN="x", KNOWLEDGE_AUTO_UPDATE="0")
        for agent in ("codex", "agy"):
            code, out, err = run([sys.executable, "-X", "utf8", str(hooks / "agent_hooks.py"), "context", agent],
                                 json.dumps({"cwd": str(home / "proj")}), env, cwd=str(home / "proj"))
            check(code == 0 and want in out and "home network" not in out, f"{agent}: a refused server is named, generic words", out + err)
        code, out, err = run([sys.executable, "-X", "utf8", str(hooks / "agent_hooks.py"), "mcp"],
                             json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n", env)
        check(want in out and "home network" not in out, "relay: a refused server is named, generic words", out + err)
        code, out, err = run([sys.executable, "-X", "utf8", str(CLIENT / "clients" / "knowledge-agent.py"), "install", "codex"], "", env)
        check(code != 0 and want in out + err and "home network" not in out + err, "setup: a refused server is named, generic words", out + err)
        code, out, err = run([sys.executable, "-X", "utf8", str(CLIENT / "clients" / "knowledge-agent.py"), "install", "codex"], "",
                             {**env, "KNOWLEDGE_URL": "http://127.0.0.1:9"})
        check(code != 0 and "cannot reach http://127.0.0.1:9:" in out + err, "setup: a server that does not answer is named", out + err)
    finally:
        srv.shutdown()
    # The update check fails (GitHub away): the background run stays quiet, asking says so, the server hears why.
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, KNOWLEDGE_PLUGIN_LATEST=str(home / "no-such.json"))
    code, out, err = run([sys.executable, "-X", "utf8", str(hooks / "self_update.py"), "run", "codex"], "", env)
    state = json.loads((home / ".knowledge" / "auto-update.json").read_text(encoding="utf-8"))
    check(code == 0 and "version check failed" in state.get("codex:check", {}).get("error", "") and "codex:result" not in state,
          "self_update: a failed check is recorded, not told as a result", json.dumps(state) + err)
    code, out, err = run([sys.executable, "-X", "utf8", "-c", "import self_update; print(self_update.run('codex'))"], "", env, cwd=str(hooks))
    check("could not check" in out and "up to date" not in out, "self_update: asked, it says the check failed", out + err)
    for agent in ("codex", "agy"):
        run([sys.executable, "-X", "utf8", str(hooks / "self_update.py"), "run", agent], "", env)
        run([sys.executable, "-X", "utf8", str(hooks / "agent_hooks.py"), "context", agent], json.dumps({"cwd": str(home / "proj")}),
            {**env, "KNOWLEDGE_AUTO_UPDATE": "0"}, cwd=str(home / "proj"))
    admin = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "alice"}
    row = next(t for t in json.loads(http("GET", "/ui/api/admin", headers=admin)[1])["tokens"] if t["name"] == "friend-pc")
    got = {p["agent"]: p for p in row["plugin"]}
    check(all("version check failed" in got.get(a, {}).get("update_error", "") and got[a].get("checked") for a in ("codex", "agy")),
          "codex, agy: the admin page shows the last check and why it failed", json.dumps(row["plugin"])[:600])


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
          "url_is_default: false once a server is named, true when nothing names one", out + err + out2 + err2)
    bare = user_env(tmp / "Nothing Set", PATH=str(Path(sys.executable).parent))
    code, out, err = run([sys.executable, "-c", "import token_store as t; print(repr(t.url()))"], env=bare, cwd=CLIENT / "plugin" / "hooks")
    check(out.strip() == "''", "no built-in server: url() is empty when nothing names one", out + err)
    code, out, err = run([sys.executable, str(CLIENT / "plugin" / "hooks" / "agent_hooks.py"), "context", "codex"], "{}", bare)
    hooks = CLIENT / "plugin" / "hooks"
    installer = "install.sh | sh"
    check("no server address is set" in out and installer in out and "install.ps1 | iex" in out,
          "no server set: Codex's session start says how to connect, and calls no server", out + err)
    code, out, err = run([sys.executable, str(hooks / "agent_hooks.py"), "context", "agy"], "{}", bare)
    check("no server address is set" in out and installer in out, "no server set: agy's session start says how to connect", out + err)
    code, out, err = run([bash(), "-c", f'sh "{(hooks / "context.sh").as_posix()}" 1'], "",
                         {**bare, "PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(tmp)})
    check("no server address is set" in out and installer in out, "no server set: Claude's session start says how to connect", out + err)
    # Odd input never fails a hook: Codex shows "hook: PostToolUse Failed" on a non-zero exit or broken JSON (not all agents send the same).
    named = {**bare, "KNOWLEDGE_URL": "http://127.0.0.1:9", "KNOWLEDGE_TOKEN": "x", "KNOWLEDGE_YOLO": "1", "KNOWLEDGE_HOME": str(tmp / "Odd Hooks")}
    def clean(out):  # as Codex reads it: plain text is fine, what looks like JSON must parse
        try:
            return not out.lstrip().startswith(("{", "[")) or isinstance(json.loads(out), dict)
        except ValueError:
            return False
    for agent in ("claude", "codex", "agy"):
        bad = []
        for odd in ("", "nope", "[1]", "null", '{"cwd":5,"tool_input":[1]}', '{"toolCall":"x"}', '{"tool_input":"' + "x" * 200000 + '"}'):
            for cmd in ([str(hooks / "watch.py"), agent], [str(hooks / "agent_hooks.py"), "wake", agent], [str(hooks / "agent_hooks.py"), "docs-sync"]):
                code, out, err = run([sys.executable, "-X", "utf8", *cmd], odd, named, cwd=tmp)
                if code != 0 or not clean(out):
                    bad.append(f"{cmd[-2:]} {odd[:30]!r}: {code} {out[:200]} {err[-300:]}")
        check(not bad, f"{agent}: the PostToolUse hooks exit 0 with clean output on odd input", "; ".join(bad)[:1000])


def codex_worker_sandbox(tmp: Path):
    """A real Codex sandbox with the folders a worker gets: it commits on its task branch and cannot move main."""
    codex = shutil.which("codex")
    if not codex or not sys.platform.startswith("linux"):
        return print("skip codex worker sandbox (needs codex on Linux)")
    home, repo, wt = tmp / "Sandbox Home", tmp / "sandbox repo", tmp / "sandbox wt"
    (home / ".codex").mkdir(parents=True)
    env = user_env(home, PATH=os.environ["PATH"], GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    def git(*args, cwd=repo):
        return run(["git", *args], "", env, cwd=str(cwd), timeout=60)
    repo.mkdir()
    git("-c", "init.defaultBranch=main", "init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "one")
    git("worktree", "add", "-q", "-b", "task/T1", str(wt), "HEAD")
    main_at = git("rev-parse", "main")[1].strip()
    probe = ("import json, workers; "
             f"print(json.dumps(workers.codex_git_dirs({{'cwd': {str(wt)!r}, 'worktree': {str(wt)!r}}})))")
    code, out, err = run([sys.executable, "-c", probe], "", env, cwd=CLIENT / "plugin" / "hooks")
    roots = json.loads(out)[1::2] if code == 0 else []
    check(any(r.endswith("task") for r in roots) and not any(r.endswith("refs") for r in roots),
          "codex worker: of the refs only the task branches and checkpoints are writable", out + err)
    def boxed(*args):
        # as `exec -s workspace-write --add-dir`: everything readable, the folder and these roots writable
        writable = ", ".join(f"{json.dumps(r)} = \"write\"" for r in [str(wt), *roots])
        return run([codex, "sandbox", "-P", "worker", "-c", f'permissions.worker.filesystem={{":root" = "read", {writable}}}',
                    "-C", str(wt), "--", *args], "", env, cwd=str(wt), timeout=60)
    if boxed("true")[0] != 0:  # no user namespaces on this runner
        return print("skip codex worker sandbox (the sandbox does not start here)")
    (wt / "work.txt").write_text("x", encoding="utf-8")
    code, out, err = boxed("git", "add", "work.txt")
    code2, out2, err2 = boxed("git", "commit", "-q", "-m", "two")
    check(code == 0 and code2 == 0 and git("log", "-1", "--format=%s", "task/T1")[1].strip() == "two",
          "codex worker: a commit on its task branch works in the sandbox", out + err + out2 + err2)
    code, out, err = boxed("git", "update-ref", "refs/heads/main", "HEAD")
    code2, out2, err2 = boxed("git", "branch", "-f", "main", "HEAD")
    check(code != 0 and code2 != 0 and git("rev-parse", "main")[1].strip() == main_at,
          "codex worker: moving main is refused by the sandbox", out + err + out2 + err2)


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


def background_flags(tmp: Path):
    """The detached starts of the hooks (self-update, offline copy, background hook) open no console window on Windows:
    CREATE_NO_WINDOW, never DETACHED_PROCESS, whose console children (git, the agent CLIs) each got a window."""
    code = """import json, os, subprocess, sys
sys.path.insert(0, sys.argv[1])
import self_update, memory_mirror, agent_hooks
seen, cwds = [], []
class P:
    def __init__(self, *a, **kw):
        seen.append(kw.get("creationflags", 0) if os.name == "nt" else int(bool(kw.get("start_new_session"))))
        cwds.append(kw.get("cwd"))
        self.stdin = open(os.devnull, "wb")
subprocess.Popen = P
self_update.start("codex"); memory_mirror.start_background(); agent_hooks.start_hook("docs-sync.py", "", "{}")
print(json.dumps(seen)); print(json.dumps(cwds))"""
    home = tmp / "Flags Usér"
    home.mkdir(parents=True, exist_ok=True)
    rc, out, err = run([sys.executable, "-c", code, str(CLIENT / "plugin" / "hooks")], "", user_env(home, KNOWLEDGE_HOME=str(home / ".knowledge")),
                       cwd=str(home))  # not the repo root: its token.py would shadow the stdlib module
    seen, cwds = map(json.loads, out.splitlines()) if rc == 0 else ([], [])
    check(cwds[:2] == [str(home)] * 2,
          "self-update and offline copy do not stand in the plugin folder (it blocks agy's update on Windows)", out + err[-500:])
    ok = len(seen) == 3 and all((f & 0x08000000 and not f & 0x00000008) if WIN else f == 1 for f in seen)
    check(ok, "self-update, offline copy and background hook start detached without a console window", out + err[-500:])


def installer_progress(tmp: Path):
    """A step that can take minutes says what it does before it starts, not only afterwards (a silent installer looks stuck)."""
    code = """import importlib.util as u, io, subprocess, sys
s = u.spec_from_file_location("knowledge_agent", sys.argv[1])
m = u.module_from_spec(s)
s.loader.exec_module(m)
out, seen = io.StringIO(), []
sys.stdout = out
def fake(*a, **kw):
    seen.append(out.getvalue())
    return subprocess.CompletedProcess(a[0], 0, "fine", "")
subprocess.run = fake
m.run_cli(["claude", "plugin", "install"])
m.run_steps([("run", ["slow-tool"], False)], False)
sys.stderr.write(repr(seen))
assert "claude plugin install" in seen[0] and "slow-tool" in seen[1], seen
assert "claude plugin install: ... fine" in out.getvalue() and "slow-tool ... ok" in out.getvalue(), out.getvalue()"""
    home = tmp / "Progress Usér"
    home.mkdir(parents=True, exist_ok=True)
    rc, out, err = run([sys.executable, "-c", code, str(CLIENT / "clients" / "knowledge-agent.py")], "",
                       user_env(home, KNOWLEDGE_HOME=str(home / ".knowledge")), cwd=str(home))
    check(rc == 0, "installer: a slow step says what it does before it starts", out + err[-500:])


def main():
    compile(FAKE_AGENT, "FAKE_AGENT", "exec")  # a broken stand-in fails every agent test: say so at once
    import faulthandler  # a hang prints every thread's stack before the CI job's 20 min limit cancels it without a log
    faulthandler.dump_traceback_later(15 * 60, exit=True)
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
    started = time.monotonic()
    proc, tok, garden = start_server(tmp)
    try:
        print(f"platform {sys.platform}, python {sys.version.split()[0]}, home {home}")
        mirror = claude_plugin(tok, home, project, garden)
        time.sleep(max(0, started + 12 - time.monotonic()))  # past the server's STORAGE_TIMEOUT
        check(http("GET", "/health?disk=1")[0] == 200, "the server's check of its folders works on this system (else every request gets 503)")
        folder_link(tmp / "Link Usér")
        offline(tok, home, project, garden, mirror)
        skills(tmp, home)
        agents(tok, home, project, garden)
        script_mcp(tok, home)
        codex_plugin(tok, tmp / "Codex Usér", project)  # its own home: the plugin next to the setup's server would clash
        agy_plugin(tok, home, project, garden)
        no_session_log_e2e(tok, tmp)
        limits_e2e(tok, tmp)
        plugin_versions(tmp)
        relay_errors(home)
        honest_errors(tok, tmp)
        hook_errors(tmp, project)
        terminal_asks(tmp)
        knowledge_home(tok, tmp, project)
        companion_e2e(tok, tmp, project)
        chat_e2e(tok, tmp)
        follow_e2e(tok, tmp)
        background_flags(tmp)
        import_e2e(tok, tmp, garden)
        installer(tok, tmp)
        installer_progress(tmp)
        companion_setup(tmp)
        checkout_update(tmp)
        claude_stored_token(tmp)
        codex_worker_sandbox(tmp)
        real_clis(tok, home)
    finally:
        proc.kill()
        if os.name != "nt":  # live chat hosts outlive their companion by design: end the ones this run started
            subprocess.run(["pkill", "-f", re.escape(str(tmp))], check=False)
        if failures:
            print("server log tail:\n" + (tmp / "server.log").read_text(errors="replace")[-2000:])
        if not os.environ.get("KNOWLEDGE_E2E_KEEP"):  # ~370 MB a run: on a tmpfs with a quota the next run fails on a full disk
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
