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
  4. Optional, when the CLIs are installed (CI installs them): `claude plugin install` from this repo and
     `codex mcp list` see the knowledge server.

  uv run --project server python tests/clients_e2e.py        (exit 0 = all passed; every check prints a line)
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
           "KNOWLEDGE_PROXY_SECRET": "proxy-secret", "KNOWLEDGE_ADMINS": "anthony", "KNOWLEDGE_AUDIT": str(tmp / "audit.log"),
           "KNOWLEDGE_PORT": str(PORT), "KNOWLEDGE_BIND": "127.0.0.1", "KNOWLEDGE_RESCAN": "3600", "KNOWLEDGE_EMBED_MODEL": "hash",
           "KNOWLEDGE_SECRETS": str(tmp / "secrets.json"), "KNOWLEDGE_VECTORS": str(tmp / "vectors.npz"),
           "KNOWLEDGE_ALLOWED_HOSTS": f"127.0.0.1:{PORT}", "PYTHONUTF8": "1"}
    log = open(tmp / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, str(REPO / "server" / "server.py")], env=env, stdout=log, stderr=subprocess.STDOUT)
    if not wait_for(lambda: _health(), 60):
        proc.kill()
        raise SystemExit("server did not start:\n" + (tmp / "server.log").read_text(errors="replace")[-3000:])
    admin = {"X-Knowledge-Proxy": "proxy-secret", "X-authentik-username": "anthony", "X-Knowledge-UI": "1"}
    assert http("POST", "/ui/api/admin/project", {"name": "garden"}, admin)[0] == 200
    st, body = http("POST", "/ui/api/admin/token", {"name": "friend-pc", "write": True, "redact": True,
                                                    "roots": ["garden", "garden-memory"]}, admin)
    assert st == 200, body
    return proc, json.loads(body)["token"], tmp / "projects" / "garden"


def _health() -> bool:
    try:
        return http("GET", "/health")[0] == 200
    except OSError:
        return False


def user_env(home: Path, **extra) -> dict:
    """A clean user environment rooted at `home` (Path.home() reads USERPROFILE on Windows, HOME elsewhere)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KNOWLEDGE_", "CLAUDE_"))}
    # Codex on Windows finds its folder through Windows itself, not USERPROFILE: CODEX_HOME is its own override.
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / ".codex"), PYTHONUTF8="0", **extra)  # PYTHONUTF8 off: like a fresh Windows
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
               "SessionEnd": {"hook_event_name": "SessionEnd", "session_id": "s1", "transcript_path": str(transcript),
                              "cwd": str(project), "reason": "exit"},
               "PreCompact": {"hook_event_name": "PreCompact", "session_id": "s2", "transcript_path": str(transcript), "cwd": str(project)}}
    hashes = home / ".config" / "knowledge-server" / "secret-hashes.json"  # an owner device has them (token.py push-secrets)
    hashes.parent.mkdir(parents=True, exist_ok=True)
    hashes.write_text(json.dumps([hashlib.sha256(b"Sup3rSecret99").hexdigest()]), encoding="utf-8")
    for mode in ("env", "literal"):  # Claude Code may expand ${CLAUDE_PLUGIN_ROOT} itself or leave it to the shell
        env = user_env(home, CLAUDE_PLUGIN_ROOT=root, CLAUDE_PLUGIN_OPTION_URL=BASE, CLAUDE_PLUGIN_OPTION_TOKEN=tok,
                       CLAUDE_PROJECT_DIR=str(project))
        outs = {}
        for event in ("SessionStart", "PostToolUse", "PreToolUse", "SessionEnd"):
            for group in hooks.get(event, []):
                for hk in group["hooks"]:
                    cmd = hk["command"] if mode == "env" else hk["command"].replace("${CLAUDE_PLUGIN_ROOT}", root)
                    code, out, err = run([sh, "-c", cmd], json.dumps(payload[event]), env, cwd=str(project), timeout=hk.get("timeout", 30) + 30)
                    check(code == 0, f"[{mode}] {event} hook exits 0: {cmd[-60:]}", err)
                    outs.setdefault(event, []).append(out)
        start = "".join(outs["SessionStart"])
        check("project garden" in start and "Search first" in start, f"[{mode}] SessionStart: guest context with the house rules", start[:500])
        check("Development rules" in start, f"[{mode}] SessionStart: guest context with the development rules", start[:500])
        check("unreachable" not in start and "skipped" not in start, f"[{mode}] SessionStart: no error notice", start[:500])
        check((garden / "docs" / "README.md").exists() and "→ ✅" in (garden / "docs" / "README.md").read_text(encoding="utf-8"),
              f"[{mode}] SessionStart mirrors the folder's .md (UTF-16 marker, → ✅ intact)")
        check("deny" in "".join(outs["PreToolUse"]), f"[{mode}] PreToolUse: a known secret is refused", "".join(outs["PreToolUse"]))
        check((garden / "docs" / "docs" / "taxi.md").exists(), f"[{mode}] PostToolUse mirrors the edited file")
        logs = list((garden / "memory" / "sessions").glob("*.md"))
        check(any("fix the taxi speed" in p.read_text(encoding="utf-8") for p in logs), f"[{mode}] SessionEnd posts the session log")
        mirror = home / ".knowledge" / "mirror"
        check(wait_for(lambda: (mirror / ".state.json").exists()), f"[{mode}] the background sync leaves an offline copy",
              (mirror / "last-sync.log").read_text(encoding="utf-8") if (mirror / "last-sync.log").exists() else "no state, no log")
        transcript.write_text(transcript.read_text(encoding="utf-8") + json.dumps({"type": "user", "message": {"content": f"second {mode}"}}) + "\n", encoding="utf-8")
    return mirror


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
    (local / "mine" / "SKILL.md").write_text("---\nname: mine\n---\nmade on this device, é\n", encoding="utf-8")
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
    (local / "mine" / "SKILL.md").write_text("---\nname: mine\n---\nedited → ✅\n", encoding="utf-8")
    hook = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(local / "mine" / "SKILL.md")}})
    env2 = {**env, "CLAUDE_PLUGIN_ROOT": str(CLIENT / "plugin")}
    code, out, err = run([bash(), "-c", 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" "${CLAUDE_PLUGIN_ROOT}/hooks/memory_mirror.py" edited'], hook, env2)
    check(code == 0 and wait_for(lambda: "edited → ✅" in (server_skills / "mine" / "SKILL.md").read_text(encoding="utf-8")),
          "skills: an edit through the PostToolUse hook goes up", err + logged())


def agents(tok: str, home: Path, project: Path, garden: Path):
    agent = CLIENT / "clients" / "knowledge-agent.py"
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok)
    for name, conf, rules in (("codex", home / ".codex" / "hooks.json", home / ".codex" / "AGENTS.md"),
                              ("agy", home / ".gemini" / "config" / "hooks.json", home / ".gemini" / "GEMINI.md")):
        for attempt in (1, 2):
            code, out, err = run([sys.executable, str(agent), "install", name], "", env)
            check(code == 0 and "token ok" in out, f"{name}: install run {attempt}", out + err)
        check(conf.exists() and rules.read_text(encoding="utf-8").count("knowledge:house-rules (written") == 1,
              f"{name}: hooks written, house rules block once after a rerun")
        check("Development rules" in rules.read_text(encoding="utf-8"), f"{name}: the rules file carries the development rules")
        check((home / ".agents" / "skills" / "verify-done" / "SKILL.md").exists(), f"{name}: development skills in ~/.agents/skills")
        cfg = json.loads(conf.read_text(encoding="utf-8"))
        cmds = [h["command"] for g in (cfg.get("hooks") or cfg["knowledge"]).values() for e in g for h in (e.get("hooks") or [e])]
        payloads = {"context": {"hook_event_name": "SessionStart", "cwd": str(project), "workspacePaths": [str(project)]},
                    "docs-sync": {"hook_event_name": "PostToolUse", "cwd": str(project), "tool_input": {"command": "*** Begin Patch\n*** Update File: docs/taxi.md\n*** End Patch"}},
                    "session-log": {"hook_event_name": "SessionEnd", "session_id": f"{name}-1", "cwd": str(project), "transcript_path": ""}}
        # How a program starts a hook command on Windows: PowerShell -Command, or cmd /d /s /c "<command>" as one
        # string (a list would make Python add its own quoting, which cmd reads differently).
        shells = [("powershell", lambda c: ["powershell", "-NoProfile", "-Command", c]), ("cmd", lambda c: f'cmd /d /s /c "{c}"')] \
            if WIN else [("sh", lambda c: [shutil.which("sh"), "-c", c])]
        for label, wrap in shells:
            for cmd in cmds:
                kind = next(k for k in payloads if f" {k} " in f" {cmd} " or cmd.split(" ")[-2:-1] == [k])
                code, out, err = run(wrap(cmd), json.dumps(payloads[kind]), env, cwd=str(project))
                check(code == 0, f"{name} via {label}: {kind} hook runs", err[-600:])
                if kind == "context":
                    try:
                        msg = json.dumps(json.loads(out))
                        check("project garden" in msg, f"{name} via {label}: context JSON carries the guest context", out[:300])
                    except ValueError:
                        check(False, f"{name} via {label}: context prints valid JSON", out[:300] + err[-300:])
    st, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write", "arguments":
                    {"path": "garden-memory/leak.md", "content": "---\nname: l\ndescription: d\nmetadata:\n  type: project\n---\n## Stand\nSup3rSecret99\n## Keuzes\n"}}},
                    {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
    check("known secret" in body, "a guest (no local hashes): the server refuses a known secret", body[:300])
    code, out, err = run([sys.executable, str(agent), "install", "codex"], "", user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN="wrong"))
    check(code != 0 and "401" in (out + err), "install with a wrong token stops with a clear 401 message", out + err)


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
        offline(tok, home, project, garden, mirror)
        skills(tmp, home)
        agents(tok, home, project, garden)
        real_clis(tok, home)
    finally:
        proc.kill()
        if failures:
            print("server log tail:\n" + (tmp / "server.log").read_text(errors="replace")[-2000:])
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
