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
    known = home / ".claude" / "plugins" / "known_marketplaces.json"  # installed by hand: marketplace known, no autoUpdate
    known.parent.mkdir(parents=True, exist_ok=True)
    known.write_text(json.dumps({"knowledge": {"source": {"source": "directory", "path": str(REPO)}, "installLocation": str(REPO),
                                               "lastUpdated": "2026-10-24T00:00:00.000Z"}}), encoding="utf-8")  # as Claude Code writes it
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
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        check(settings["extraKnownMarketplaces"]["knowledge"].get("autoUpdate") is True
              and ("auto-update is now on" in start) == (mode == "env"),
              f"[{mode}] SessionStart: auto-update turned on once, said once", json.dumps(settings) + start[-300:])
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
        cmds = [h["command"] for g in (cfg.get("hooks") or cfg["knowledge"]).values() for e in g for h in (e.get("hooks") or [e])]
        payloads = {"context": {"hook_event_name": "SessionStart", "cwd": str(project), "workspacePaths": [str(project)]},
                    "docs-sync": {"hook_event_name": "PostToolUse", "cwd": str(project), "tool_input": {"command": "*** Begin Patch\n*** Update File: docs/taxi.md\n*** End Patch"}},
                    "session-log": {"hook_event_name": "SessionEnd", "session_id": f"{name}-1", "cwd": str(project), "transcript_path": ""},
                    "hints": {"invocationNum": 1, "workspacePaths": [str(project)]}}
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
                        if label == shells[0][0]:  # the first start says it, later ones not
                            check("auto-update is on for " + name in msg, f"{name}: the script setup says auto-update is on", msg[-300:])
                    except ValueError:
                        check(False, f"{name} via {label}: context prints valid JSON", out[:300] + err[-300:])
                if kind == "hints":  # agy PostInvocation: {} or injectSteps, never anything else
                    try:
                        check(json.loads(out) == {} or "injectSteps" in json.loads(out), f"{name} via {label}: hints prints agy JSON", out[:300])
                    except ValueError:
                        check(False, f"{name} via {label}: hints prints valid JSON", out[:300] + err[-300:])
    st, body = http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "knowledge_write", "arguments":
                    {"path": "garden-memory/leak.md", "content": "---\nname: l\ndescription: d\nmetadata:\n  type: project\n---\n## Stand\nSup3rSecret99\n## Keuzes\n"}}},
                    {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
    check("known secret" in body, "a guest (no local hashes): the server refuses a known secret", body[:300])
    code, out, err = run([sys.executable, str(agent), "install", "codex"], "", user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN="wrong"))
    check(code != 0 and "401" in (out + err), "install with a wrong token stops with a clear 401 message", out + err)


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
    env = user_env(home, KNOWLEDGE_URL=BASE, KNOWLEDGE_TOKEN=tok, PLUGIN_ROOT=str(root), PATH=path,
                   KNOWLEDGE_PLUGIN_LATEST=str(latest_file(home)))
    payloads = {"SessionStart": {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(project)},
                "PostToolUse": {"hook_event_name": "PostToolUse", "cwd": str(project), "tool_input": {"command": "*** Begin Patch\n*** Update File: docs/taxi.md\n*** End Patch"}},
                "PreCompact": {"hook_event_name": "PreCompact", "session_id": "cx-1", "cwd": str(project), "transcript_path": ""},
                "SessionEnd": {"hook_event_name": "SessionEnd", "session_id": "cx-2", "cwd": str(project), "transcript_path": ""}}
    check(set(hooks) == set(payloads) and len(hooks["SessionStart"][0]["hooks"]) == 1,
          "codex plugin: the four hook events, one SessionStart handler (openai/codex#45999)", str(list(hooks)))
    for event, groups in hooks.items():
        for hk in (h for g in groups for h in g["hooks"]):
            # Codex: COMSPEC /C "<commandWindows>" on Windows, $SHELL -lc "<command>" elsewhere.
            argv = f'cmd /d /s /c "{hk["commandWindows"]}"' if WIN else [shutil.which("sh"), "-c", hk["command"]]
            code, out, err = run(argv, json.dumps(payloads[event]), env, cwd=str(project), timeout=hk["timeout"] + 30)
            check(code == 0 and "skipped" not in err, f"codex plugin: {event} hook runs", err[-600:])
            if event == "SessionStart":
                ctx = json.loads(out or "{}").get("hookSpecificOutput", {}).get("additionalContext", "")
                check("project garden" in ctx and "Search first" in ctx, "codex plugin: SessionStart gives the guest context with the house rules", out[:300] + err[-300:])
                check("auto-update is on for codex" in ctx, "codex plugin: SessionStart says auto-update is on", ctx[-300:])
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
    # The hooks as Codex reads them (app-server hooks/list): all four parse and wait for the user's trust (/hooks).
    reply = app_server(codex, env, "hooks/list", {"cwds": [str(project)]})
    data = reply.get("result", {}).get("data", [])
    listed = [h for e in data for h in e["hooks"] if h.get("pluginId") == "knowledge@knowledge"]
    problems = [w for e in data for w in e["warnings"] + [x["message"] for x in e["errors"]]]
    check(len(listed) == 4 and not problems, "codex: hooks/list shows the plugin's four hooks, no warnings", json.dumps(reply)[:800])
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
    payloads = {"context": common, "session-log": {**common, "transcriptPath": ""}, "hints": {**common, "invocationNum": 1},
                "docs-sync": {**common, "toolCall": {"name": "write_file", "args": {"Path": json.dumps(str(project / "docs" / "agy.md"))}}}}
    (project / "docs" / "agy.md").write_text("agy plugin → ✅\n", encoding="utf-8")
    for cmd in cmds:
        kind = next(k for k in payloads if f" {k}" in cmd)
        code, out, err = run(f'cmd /d /s /c "{cmd}"' if WIN else [shutil.which("sh"), "-c", cmd], json.dumps(payloads[kind]), env, cwd=str(root))
        check(code == 0 and "skipped" not in err, f"agy plugin: {kind} hook runs in the plugin folder", err[-600:])
        if kind == "context":
            try:
                msg = json.loads(out)["injectSteps"][0]["systemMessage"]["systemMessage"]
                check("project garden" in msg, "agy plugin: context arrives as injectSteps with the guest context", out[:300])
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
        relay_errors(home)
        installer(tok, tmp)
        real_clis(tok, home)
    finally:
        proc.kill()
        if failures:
            print("server log tail:\n" + (tmp / "server.log").read_text(errors="replace")[-2000:])
    print(f"\n{len(failures)} failed" if failures else "\nall passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
