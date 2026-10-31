#!/usr/bin/env python3
"""The knowledge companion: a small local service on this pc that starts the tasks the server says may start now.

Every minute it asks GET /tasks/ready and wakes the newest open session of the task's agent in that project with the
plugin's own wake code (Codex: codex queue, agy: its language server, Claude: the waiting hook), or, when allowed in
its settings, starts a session in the project's folder. It also trusts the knowledge hooks the agents' own way, turns
the agents' native "run without asking" settings on and off (YOLO), and reports hook failures the agents log
themselves. Its page and local API listen on 127.0.0.1:47321 only; the knowledge web UI shows a "This pc" tab when
it answers.

  companion.py serve          the page, the local API and the minute loop (knowledge-agent.py serve runs this)
  companion.py tick           one round of the loop, its summary as JSON
  companion.py trust [AGENT]  trust the knowledge hooks of every agent here (or one), then keep them trusted
  companion.py yolo-off       put back the agents' own settings YOLO changed (uninstall-companion runs this)
Standard library only; Linux, macOS and Windows.
"""
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
import agent_hooks  # noqa: E402  (the wake code and the session registry)
import project_marker  # noqa: E402
import self_update  # noqa: E402
import token_store  # noqa: E402

PORT = int(os.environ.get("KNOWLEDGE_COMPANION_PORT") or 47321)  # fixed: the web UI looks for exactly this port
SELF = f"http://127.0.0.1:{PORT}"
STATIC = HOOKS.parent / "companion" / "static"
HOME = Path.home()
STATE = HOME / ".knowledge" / "companion"
BACKUPS = HOME / "backups" / "knowledge-companion"
WAKE_AGAIN = 30 * 60  # the server calls a woken task stalled after the same time
EVERY = float(os.environ.get("KNOWLEDGE_COMPANION_EVERY") or 60)
AGENTS = {"claude": ("Claude Code", "C"), "codex": ("Codex", "X"), "agy": ("agy", "A")}
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
CLAUDE_JSON = (CLAUDE_DIR if os.environ.get("CLAUDE_CONFIG_DIR") else HOME) / ".claude.json"
CODEX = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
AGY_DIR = HOME / ".gemini" / "antigravity-cli"
AGY_SETTINGS = AGY_DIR / "settings.json"
AGY_PROCESS = os.environ.get("KNOWLEDGE_AGY_PROCESS") or "agy"  # another name in the tests
MISSING = {"missing": True}  # a setting that was not there: unchecking YOLO removes it again
LOCK = threading.RLock()
TICK = threading.Lock()  # one round at a time: the minute loop and the page's "Wake ready tasks now"
TRUST_KEYS = ("keep_trusted", "codex_trusted")  # what trust() keeps in state.json; a round never writes them


def version() -> str:
    return ".".join(map(str, self_update.own()))


# ---------------------------------------------------------------- files

def load(name: str, default):
    try:
        return json.loads((STATE / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data, indent=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".companion-tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists():  # ~/.claude.json is private: the new file keeps the old one's mode
        os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def save(name: str, data):
    write_json(STATE / name, data)


def backup(path: Path):
    """A copy into ~/backups/knowledge-companion/<date>/ before a change; the first copy of a day is kept (the state
    before the companion touched it that day), the last five days are kept."""
    if not path.exists():
        return
    day = BACKUPS / time.strftime("%Y-%m-%d")
    rel = str(path.relative_to(HOME)) if path.is_relative_to(HOME) else path.name
    dest = day / rel.replace(os.sep, "__").replace("/", "__")
    if not dest.exists():
        day.mkdir(parents=True, exist_ok=True)
        os.chmod(BACKUPS, 0o700)  # the copies can hold what the agents keep private
        shutil.copy2(path, dest)
    for old in sorted(d for d in BACKUPS.iterdir() if d.is_dir())[:-5]:
        shutil.rmtree(old, ignore_errors=True)


def edit_json(path: Path, change):
    """Read, change, write back; a file that is not valid JSON is never overwritten."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    backup(path)
    out = change(data)
    write_json(path, data)
    return out


def api_key() -> str:
    """The local API's secret, readable by this user only: any process here can reach 127.0.0.1 and set Origin, so a
    change needs the key. The companion's shortcut opens the page with it (#key=), the page keeps it."""
    f = STATE / "api-key"
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        STATE.mkdir(parents=True, exist_ok=True)
    try:
        with os.fdopen(os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as fh:
            fh.write(key := secrets.token_urlsafe(24))
        return key
    except FileExistsError:  # made by the other thread meanwhile
        return f.read_text(encoding="utf-8").strip()


def log(kind: str, text: str, detail: str = ""):
    with LOCK:
        items = load("activity.json", [])
        items.insert(0, {"at": time.time(), "kind": kind, "text": text, "detail": detail})
        save("activity.json", items[:100])


# ---------------------------------------------------------------- agents

def installed(agent: str) -> bool:
    return bool(shutil.which(agent))


def plugin_version(agent: str) -> str:
    try:
        if agent == "claude":
            plugins = json.loads((CLAUDE_DIR / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))["plugins"]
            return next((str(v[0].get("version", "")) for k, v in plugins.items() if k.startswith("knowledge@") and v), "")
        paths = (list(CODEX.glob("plugins/cache/*/knowledge/*/.codex-plugin/plugin.json")) if agent == "codex"
                 else [HOME / ".gemini" / "config" / "plugins" / "knowledge" / ".claude-plugin" / "plugin.json"])
        found = [self_update.version(p.read_text(encoding="utf-8")) for p in paths if p.is_file()]
        return ".".join(map(str, max(found))) if found else ""
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return ""


def agy_running() -> bool:
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {AGY_PROCESS}.exe", "/NH"], capture_output=True, text=True)
        return f"{AGY_PROCESS}.exe".lower() in r.stdout.lower()
    return subprocess.run(["pgrep", "-x", AGY_PROCESS[:15]], capture_output=True).returncode == 0


def rfc3339(s: str) -> float:
    try:
        return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return 0.0


def alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill on Windows terminates the process: ask with OpenProcess instead
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claude_waits(entry: dict) -> bool:
    """A Claude session can be woken only while its waiting hook runs: the .waiter file names that hook's pid."""
    try:
        f = agent_hooks.AGENTS_DIR / f"claude-{agent_hooks.short(entry['session'])}.waiter"
        return alive(int(f.read_text(encoding="utf-8").split(":")[0]))
    except (OSError, ValueError, KeyError):
        return False


def sessions(agent: str, now: float) -> list:
    """The open sessions of an agent here, newest first: what agent_hooks' wake code can reach."""
    out = []
    if agent == "agy":
        try:
            convs = agent_hooks.agy_conversations()
        except Exception:
            convs = []
        for entry, conv, changed, folder in convs:
            cwd = folder or entry.get("cwd", "")
            out.append({"session": conv, "cwd": cwd, "project": agent_hooks.project_query(cwd)["project"],
                        "at": rfc3339(changed), "entry": entry})
    else:
        for _, e in agent_hooks.read_entries(f"{agent}-*.json"):
            if e.get("agent") == agent and e.get("cwd") and now - e.get("at", 0) <= agent_hooks.WAKE_WITHIN \
                    and (agent != "claude" or claude_waits(e)):
                out.append({"session": e["session"], "cwd": e["cwd"], "project": agent_hooks.project_query(e["cwd"])["project"],
                            "at": e["at"], "entry": e})
    return sorted(out, key=lambda s: -s["at"])


def project_folders() -> dict:
    """{project: folder} of the folders here linked to a project (.knowledge-project), never the home folder: from the
    session registry and the folders the agents already know."""
    cands = {e.get("cwd") for _, e in agent_hooks.read_entries("*.json")}
    try:
        cands |= set(json.loads(CLAUDE_JSON.read_text(encoding="utf-8")).get("projects") or {})
    except (OSError, ValueError, AttributeError):
        pass
    try:
        cands |= set(json.loads(AGY_SETTINGS.read_text(encoding="utf-8")).get("trustedWorkspaces") or [])
    except (OSError, ValueError, AttributeError):
        pass
    try:  # [projects."<path>"]: a TOML basic string, escaped like JSON
        cands |= {json.loads(f'"{p}"') for p in re.findall(r'^\[projects\."((?:[^"\\]|\\.)*)"\]', (CODEX / "config.toml").read_text(encoding="utf-8"), re.M)}
    except (OSError, ValueError):
        pass
    out = {}
    for c in sorted(c for c in cands if c):
        try:
            root, name = project_marker.project_root(Path(c).resolve())
        except OSError:
            continue
        if root and name and root != HOME.resolve():
            out.setdefault(name, str(root))
    return out


# ---------------------------------------------------------------- hook trust

def ours(hook: dict) -> bool:
    """Only the knowledge hooks: a repo's own .codex/hooks.json must keep asking its user."""
    return str(hook.get("pluginId") or "").startswith("knowledge@") or any(
        s in str(hook.get("command") or "") for s in ("agent_hooks.py", "knowledge-agent.py"))


def app_server_rpc(calls) -> list:
    """`codex app-server` over stdio (JSON-RPC lines): the handshake, then each (method, params) of `calls`, which is
    a function given the previous replies; the answers in order."""
    codex = shutil.which("codex")
    if not codex:
        raise LookupError("codex not found")
    p = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace")
    timer = threading.Timer(60, p.kill)
    timer.start()
    try:
        def ask(i, method, params):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
            p.stdin.flush()
            for line in p.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == i:
                    if "error" in msg:
                        raise RuntimeError(f"codex {method}: {msg['error'].get('message', msg['error'])}")
                    return msg.get("result") or {}
            raise RuntimeError(f"codex app-server ended before answering {method}")
        ask(0, "initialize", {"clientInfo": {"name": "knowledge-companion", "title": "Knowledge companion", "version": version() or "0"},
                              "capabilities": None})
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized"}) + "\n")
        replies = []
        for i, step in enumerate(calls, 1):
            call = step(replies)
            if call is None:
                break
            replies.append(ask(i, *call))
        return replies
    finally:
        timer.cancel()
        p.kill()


def trust_codex(folders: list) -> int:
    """codex-rs tui/src/hooks_rpc.rs, as the /hooks screen does it: hooks/list, then config/batchWrite of
    hooks.state.<key>.trusted_hash = currentHash (upsert) for each hook that is untrusted or modified."""
    def todo(replies):
        return {h["key"]: h["currentHash"] for e in replies[0].get("data", []) for h in e.get("hooks", [])
                if h.get("trustStatus") in ("untrusted", "modified") and ours(h)}

    def write(replies):
        if not todo(replies):
            return None
        backup(CODEX / "config.toml")
        return ("config/batchWrite", {"edits": [{"keyPath": "hooks.state", "mergeStrategy": "upsert",
                                                 "value": {k: {"trusted_hash": v} for k, v in todo(replies).items()}}],
                                      "filePath": None, "expectedVersion": None, "reloadUserConfig": True})
    replies = app_server_rpc([lambda r: ("hooks/list", {"cwds": folders or [str(HOME)]}), write])
    return len(todo(replies))


def codex_fingerprint() -> str:
    """Changes with every knowledge plugin update (a new cache folder) or a rewritten hooks.json: then trust again."""
    files = sorted(CODEX.glob("plugins/cache/*/knowledge/*/.codex-plugin/plugin.json")) + [CODEX / "hooks.json"]
    return hashlib.sha256(repr([(str(f), f.stat().st_mtime) for f in files if f.exists()]).encode()).hexdigest()[:16]


def claude_key(folder: str) -> str:
    # shortcut: Claude Code keys ~/.claude.json projects by the path with forward slashes on Windows; verify there.
    return Path(folder).as_posix() if os.name == "nt" else folder


def untrusted(agent: str, folders: list) -> list:
    """The knowledge folders this agent does not trust yet (Claude, agy); Codex: [] once trusted for this plugin."""
    try:
        if agent == "claude":
            if not CLAUDE_JSON.exists():  # Claude never ran here: nothing to trust yet, Claude asks itself
                return []
            known = json.loads(CLAUDE_JSON.read_text(encoding="utf-8")).get("projects") or {}
            return [f for f in folders if not (known.get(claude_key(f)) or {}).get("hasTrustDialogAccepted")]
        if agent == "agy":
            have = set(json.loads(AGY_SETTINGS.read_text(encoding="utf-8")).get("trustedWorkspaces") or [])
            return [f for f in folders if f not in have]
    except (OSError, ValueError, AttributeError):
        return list(folders)
    st = load("state.json", {})
    return [] if st.get("codex_trusted") == codex_fingerprint() else ["(plugin hooks)"]


def trust(agent: str) -> str:
    folders = sorted(project_folders().values())
    if agent == "claude":
        todo = untrusted("claude", folders)
        if todo:  # one write: Claude itself rewrites the file often
            edit_json(CLAUDE_JSON, lambda d: [d.setdefault("projects", {}).setdefault(claude_key(f), {}).update(hasTrustDialogAccepted=True) for f in todo])
        n = len(todo)
    elif agent == "agy":
        todo = untrusted("agy", folders)
        if todo:
            edit_json(AGY_SETTINGS, lambda d: d.__setitem__("trustedWorkspaces", list(d.get("trustedWorkspaces") or []) + todo))
        n = len(todo)
    else:
        n = trust_codex(folders)
        with LOCK:
            st = load("state.json", {})
            st["codex_trusted"] = codex_fingerprint()
            save("state.json", st)
    with LOCK:
        st = load("state.json", {})
        st.setdefault("keep_trusted", {})[agent] = True  # trusted once on request: kept trusted from now on
        save("state.json", st)
    what = f"{n} hook(s)" if agent == "codex" else f"{n} project folder(s)"
    log("trust", f"Trusted the knowledge hooks of {AGENTS[agent][0]}", f"{what} newly trusted")
    return what


def trust_all(agents) -> dict:
    """trust() for each agent; one agent's failure never leaves the others untrusted."""
    done = {}
    for a in agents:
        try:
            done[a] = trust(a)
        except Exception as e:
            log("error", f"Could not trust the hooks of {AGENTS[a][0]}", str(e)[:300])
            done[a] = f"failed: {e}"[:200]
    return done


def keep_trusted():
    """After a plugin update (Codex) or a new knowledge folder (Claude, agy): trust again, for the agents trusted on
    request before."""
    keep = load("state.json", {}).get("keep_trusted", {})
    folders = sorted(project_folders().values())
    for agent in AGENTS:
        if keep.get(agent) and installed(agent) and untrusted(agent, folders):
            try:
                trust(agent)
            except Exception as e:
                log("error", f"Could not trust the hooks of {AGENTS[agent][0]}", str(e)[:300])


# ---------------------------------------------------------------- YOLO

def toml_set(text: str, table, key: str, raw):
    """Set (raw = a TOML value as text) or remove (raw = None) one key of a table (None = top level), leaving every
    other line as it is; the old value as text (with its comment), or None when it was not there."""
    lines = text.splitlines(keepends=True)
    header = lambda ln: ln.lstrip().startswith("[")
    if table is None:
        start, end = 0, next((i for i, ln in enumerate(lines) if header(ln)), len(lines))
    else:
        at = next((i for i, ln in enumerate(lines) if ln.strip() == f"[{table}]"), None)
        if at is None:
            if raw is None:
                return text, None
            sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
            return f"{text}{sep}[{table}]\n{key} = {raw}\n", None
        start, end = at + 1, next((i for i in range(at + 1, len(lines)) if header(lines[i])), len(lines))
    pat = re.compile(rf"\s*{re.escape(key)}\s*=\s*(.*?)\s*$")
    for i in range(start, end):
        m = pat.fullmatch(lines[i].rstrip("\r\n"))
        if m:
            if raw is None:
                del lines[i]
            else:
                lines[i] = f"{key} = {raw}\n"
            return "".join(lines), m.group(1)
    if raw is None:
        return text, None
    last = end
    while last > start and not lines[last - 1].strip():
        last -= 1
    if last and not lines[last - 1].endswith("\n"):
        lines[last - 1] += "\n"
    lines.insert(last, f"{key} = {raw}\n")
    return "".join(lines), None


def json_set(data: dict, keys: tuple, value):
    *parents, last = keys
    for k in parents:
        data = data.setdefault(k, {})
    old = data.get(last, MISSING)
    if value == MISSING:
        data.pop(last, None)
    else:
        data[last] = value
    return old


def yolo_settings(agent: str) -> list:
    """(file, where, value when on): the agents' own settings for running without asking."""
    if agent == "claude":
        f = CLAUDE_DIR / "settings.json"
        return [(f, ("permissions", "defaultMode"), "bypassPermissions"), (f, ("skipDangerousModePermissionPrompt",), True)]
    if agent == "codex":
        f = CODEX / "config.toml"
        return [(f, (None, "approval_policy"), '"never"'), (f, (None, "sandbox_mode"), '"danger-full-access"'),
                (f, ("notice", "hide_full_access_warning"), "true")]
    return [(AGY_SETTINGS, ("toolPermission",), "always-proceed"), (AGY_SETTINGS, ("agentMode",), "accept-edits"),
            (AGY_SETTINGS, ("enableTerminalSandbox",), False)]


def apply_values(agent: str, values: list) -> list:
    """Write values (one per yolo_settings entry; MISSING removes); the values they replace."""
    spec = yolo_settings(agent)
    path = spec[0][0]
    if agent == "codex":
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        backup(path)
        old = []
        for (_, (table, key), _), v in zip(spec, values):
            text, prev = toml_set(text, table, key, None if v == MISSING else v)
            old.append(MISSING if prev is None else prev)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".companion-tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return old
    return edit_json(path, lambda d: [json_set(d, where, v) for (_, where, _), v in zip(spec, values)])


def set_yolo(agent: str, on: bool) -> str:
    with LOCK:
        yolo = load("yolo.json", {})
        mine = yolo.get(agent, {})
        if agent == "agy" and agy_running():  # agy rewrites its settings.json on exit: change it only while it is closed
            mine["pending"] = on
            yolo[agent] = mine
            save("yolo.json", yolo)
            log("trust", f"YOLO {'on' if on else 'off'} for agy queued", "Applies when agy is closed")
            return "pending"
        mine.pop("pending", None)
        if on and not mine.get("on"):
            mine = {"on": True, "prev": apply_values(agent, [v for *_, v in yolo_settings(agent)])}
        elif not on and mine.get("on"):
            apply_values(agent, mine.get("prev") or [MISSING] * len(yolo_settings(agent)))
            mine = {"on": False}
        yolo[agent] = mine
        save("yolo.json", yolo)
    log("trust", f"YOLO {'on' if on else 'off'} for {AGENTS[agent][0]}",
        "Woken tasks run without asking" if on else "The previous settings are back")
    return "on" if on else "off"


def yolo_off() -> list:
    """Every YOLO this companion turned on, off again; the agents that wait until they are closed (agy)."""
    return [a for a, mine in load("yolo.json", {}).items() if a in AGENTS and (mine.get("on") or mine.get("pending"))
            and set_yolo(a, False) == "pending"]


def pending_yolo():
    p = load("yolo.json", {}).get("agy", {}).get("pending")
    if p is not None and not agy_running():
        set_yolo("agy", p)


# ---------------------------------------------------------------- the server

def server(method: str, path: str, tok: str, body=None):
    req = urllib.request.Request(token_store.url() + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def one_line(text: str, exe: str) -> str:
    text = " / ".join(t for t in text.splitlines() if t.strip())  # Windows' npm .cmd shims cut an argument at a newline
    return re.sub(r'["%^&|<>!]', " ", text) if exe.lower().endswith((".cmd", ".bat")) else text  # cmd.exe parses it


def start_session(agent: str, folder: str, tasks: list) -> str:
    """A new session in the project's folder for its ready tasks: Codex through codex exec, Claude and agy as their CLI in a
    new terminal (a new console on Windows, tmux elsewhere). The session's label, or '' when there is no way."""
    exe = shutil.which(agent)
    lines = [f"- {t['path']}: [{t['owner']}, {t['id']}] from {t.get('from') or '?'}, start {t.get('start') or 'now'}: {t['text']}" for t in tasks]
    prompt = one_line(agent_hooks.task_text(lines, True), exe)
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    if agent == "codex":
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "codex-exec.log", "ab") as out:
            p = subprocess.Popen([exe, "exec", "--cd", folder, prompt], cwd=folder, stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, **kw)
        return f"codex exec {p.pid}"
    argv = [exe, prompt] if agent == "claude" else [exe, "-i", prompt]
    if os.name == "nt":
        p = subprocess.Popen(argv, cwd=folder, creationflags=0x00000010)  # CREATE_NEW_CONSOLE: a window to work in
        return f"console {p.pid}"
    tmux = shutil.which("tmux")
    if not tmux:
        return ""
    name = re.sub(r"[^\w-]", "-", f"knowledge-{agent}-{tasks[0]['id']}")[:60]
    # tmux 3+ runs a command given as several arguments without a shell: the task text stays text
    subprocess.run([tmux, "new-session", "-d", "-s", name, "-c", folder, *argv], check=True, timeout=15,
                   stdin=subprocess.DEVNULL, capture_output=True)
    return f"tmux {name}"


def wake_task(task: dict, tok: str, now: float, told: dict, ready: list, may_start: bool = True) -> tuple:
    """(session, how) when a session has the task now, (None, why not) else. `told`: sessions reached this round;
    `may_start`: False once a session was started for this task (it is not started again and again)."""
    agent, project = task["agent"], task["project"]
    mine = [s for s in sessions(agent, now) if s["project"] == project]
    if mine:
        s = mine[0]
        key = (agent, s["session"])
        if key in told:  # one wake says every new task of the session
            return s["session"], "with the same wake"
        if agent == "claude":  # its waiting hook asks /tasks itself once poked
            agent_hooks.poke(s["entry"])
            told[key] = True
            return s["session"], "poked its waiting hook"
        hook, deliver = (({"session_id": s["session"], "cwd": s["cwd"]}, lambda text: agent_hooks.codex_queue(s["entry"], text))
                         if agent == "codex" else
                         ({"conversationId": s["session"], "cwd": s["cwd"]}, lambda text: agent_hooks.agy_send(s["entry"], s["session"], text)))
        told[key], failed = True, []

        def tell(text):
            ok = deliver(text)
            failed.append(not ok)
            return ok
        text = agent_hooks.new_tasks(agent, tok, hook, now, wake=True, deliver=tell)
        if any(failed):  # new_tasks answers '' for "told before" and for a refused delivery alike
            raise RuntimeError(f"{AGENTS[agent][0]} did not take the message (session {s['session']})")
        return s["session"], ("sent to the session" if text else "the session was told before")
    if not load("settings.json", {}).get("startWhenNone", {}).get(agent):
        return None, "No open session; starting one is off in Settings"
    if not may_start:
        return None, "No open session; one was started for it already, not again"
    folder = project_folders().get(project)
    if not folder:
        return None, f"No folder linked to project {project} on this pc"
    key = (agent, f"start:{project}")
    if key in told:  # one new session gets all of its ready tasks
        return told[key], "with the same new session"
    label = start_session(agent, folder, [t for t in ready if t["agent"] == agent and t["project"] == project])
    if not label:
        return None, "No open session, and no tmux to start one in"
    told[key] = label
    return label, "started a session"


def agent_log_errors(st: dict, now: float) -> dict:
    """Hook failures the agents log themselves (not our own hooks): Codex in its SQLite log (logs_*.sqlite, lines
    "... hook failed" with hook_name= and error=), agy in its log folder ("pre-tool hook failed: ..."). Claude Code
    logs hook failures only with --debug, so there is nothing to read. Only what is new since the last round."""
    out = {}
    week = now - 7 * 86400
    dbs = sorted(CODEX.glob("logs_*.sqlite"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    if dbs:
        # shortcut: a log_dir/sqlite_home set in config.toml moves the database; read that setting if someone uses it.
        try:
            con = sqlite3.connect(dbs[-1].as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                last = st.get("codex_log", {}).get(dbs[-1].name, 0)
                rows = con.execute("SELECT id, ts, feedback_log_body FROM logs WHERE id > ? AND ts > ? AND feedback_log_body "
                                   "LIKE '%hook%failed%' ORDER BY id LIMIT 200", (last, week)).fetchall()
                top = con.execute("SELECT max(id) FROM logs").fetchone()[0] or 0
            finally:
                con.close()
            errs = []
            for _, ts, body in rows:
                body = body or ""
                name = re.search(r"\bhook_name=(\S+)", body)
                if not ("knowledge" in (name.group(1) if name else "").lower() or ours({"command": body})):
                    continue  # another hook's failure, maybe with its own output: it stays on this pc
                err = re.search(r"\berror=(.*)$", body, re.S)
                event = re.search(r"(\w+) hook failed", body)
                errs.append({"hook": name.group(1) if name else "codex hook", "event": event.group(1) if event else "",
                             "code": "", "stderr": (err.group(1) if err else body)[:2000], "at": float(ts), "source": "agent-log"})
            out["codex"] = (errs, ("codex_log", {dbs[-1].name: top}))
        except sqlite3.Error:
            pass
    files = sorted((AGY_DIR / "log").glob("*.log")) if (AGY_DIR / "log").is_dir() else []
    seen, errs, offsets = st.get("agy_log", {}), [], {}
    for f in files:
        try:
            size, mtime = f.stat().st_size, f.stat().st_mtime
            pos = seen.get(f.name, 0 if mtime > week else size)
            with f.open("rb") as fh:
                fh.seek(pos if pos <= size else 0)
                chunk = fh.read()
            offsets[f.name] = size
        except OSError:
            continue
        for line in chunk.decode("utf-8", "replace").splitlines():
            m = re.match(r"^[IWEF](\d\d)(\d\d) (\d\d:\d\d:\d\d)\.\d+\s+\d+ \S+\] (.*hooks? failed.*)$", line)
            if m:
                at = datetime.datetime.strptime(f"{time.localtime(now).tm_year}{m[1]}{m[2]} {m[3]}", "%Y%m%d %H:%M:%S").timestamp()
                event = "PreToolUse" if "pre-tool" in m[4] else "PostToolUse" if "post-tool" in m[4] else ""
                errs.append({"hook": m[4].split(":")[0][:80], "event": event, "code": "", "stderr": m[4][:2000], "at": at,
                             "source": "agent-log"})
    if files:
        out["agy"] = (errs, ("agy_log", offsets))
    return out


def report_errors(st: dict, tok: str, now: float):
    for agent, (errs, (key, mark)) in agent_log_errors(st, now).items():
        if errs:
            try:
                server("POST", "/status", tok, {"agent": agent, "plugin": plugin_version(agent), "errors": errs[-20:]})
            except Exception:  # the next round reads them again
                continue
            st.setdefault("last_errors", {})[agent] = errs[-1]
            log("error", f"{AGENTS[agent][0]} logged a failing hook", f"{errs[-1]['hook']}: {errs[-1]['stderr'][:160]}")
        st[key] = mark


def tick() -> dict:
    """One round: queued YOLO, trust after an update, the ready tasks, the agents' own hook errors."""
    with TICK:
        return one_round()


def one_round() -> dict:
    now, tok = time.time(), token_store.token()
    try:
        pending_yolo()
    except Exception as e:
        log("error", "Could not apply the queued YOLO setting of agy", str(e)[:300])
    keep_trusted()
    with LOCK:
        st = load("state.json", {})
    summary = {"woken": [], "waiting": {}}
    try:
        ready = server("GET", "/tasks/ready", tok)["tasks"]
        if st.get("sync_error"):
            log("sync", "The knowledge server answers again")
        st.update(synced=now, sync_error="", ready=ready)
    except Exception as e:
        if not st.get("sync_error"):
            log("error", "Could not reach the knowledge server", str(e)[:200])
        st["sync_error"], ready = str(e)[:200], []
    woken = {k: w for k, w in st.get("woken", {}).items() if now - w.get("since", w["at"]) < 86400}
    told = {}
    for t in ready:
        key = f"{t['project']}/{t['id']}"
        prev = woken.get(key, {})
        if t.get("agent") not in AGENTS or not installed(t["agent"]) or now - prev.get("at", 0) < WAKE_AGAIN:
            continue
        try:
            session, how = wake_task(t, tok, now, told, ready, may_start=not prev.get("started"))
        except Exception as e:
            session, how = None, f"Wake failed: {e}"[:200]
            if st.get("waiting", {}).get(key) != how:  # once, not every minute while it keeps failing
                log("error", f"{AGENTS[t['agent']][0]}: could not wake {t['id']}", how)
        if session is None:
            summary["waiting"][key] = how
            continue
        woken[key] = {"at": now, "since": prev.get("since", now), "session": session, "agent": t["agent"],
                      "started": bool(prev.get("started")) or how == "started a session"}
        summary["woken"].append({"id": t["id"], "project": t["project"], "agent": t["agent"], "session": session, "how": how})
        try:
            server("POST", "/tasks/woken", tok, {"project": t["project"], "id": t["id"], "agent": t["agent"], "session": session[:80]})
        except Exception as e:
            log("error", f"Could not tell the server that {t['id']} was woken", str(e)[:200])
        log("wake", f"{AGENTS[t['agent']][0]}: {t['id']} ({how})", f"Session {session}")
    st.update(woken=woken, waiting=summary["waiting"])
    report_errors(st, tok, now)
    with LOCK:
        save("state.json", {**load("state.json", {}), **{k: v for k, v in st.items() if k not in TRUST_KEYS}})  # trust() may have written meanwhile
    return summary


# ---------------------------------------------------------------- the page

def own_errors(agent: str) -> list:
    out = []
    try:
        for ln in (HOME / ".knowledge" / "hook-errors.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()[-100:]:
            e = json.loads(ln)
            if re.search(rf"\b{agent}\b", e.get("hook", "")):
                out.append(e)
    except (OSError, ValueError):
        pass
    return out


def first_line(text) -> str:
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")[:200]


def page_state() -> dict:
    now = time.time()
    st, yolo, settings = load("state.json", {}), load("yolo.json", {}), load("settings.json", {})
    folders = sorted(project_folders().values())
    agents = []
    for a, (name, mark) in AGENTS.items():
        here = installed(a)
        errs = own_errors(a) + ([st["last_errors"][a]] if a in st.get("last_errors", {}) else [])
        last = max(errs, key=lambda e: e.get("at", 0)) if errs else None
        agents.append({"id": a, "name": name, "mark": mark, "installed": here, "version": plugin_version(a),
                       "hooksTrusted": here and not untrusted(a, folders), "yolo": bool(yolo.get(a, {}).get("on")),
                       "yoloPending": yolo.get(a, {}).get("pending"),
                       "sessions": [{"title": f"{s['project']}: {Path(s['cwd']).name}", "at": s["at"], "wakeable": i == 0}
                                    for i, s in enumerate(sessions(a, now)) if here][:6],
                       "lastError": last and {"at": last.get("at", 0), "text": f"{last.get('hook', '')}: {first_line(last.get('stderr'))}"}})
    lanes = {"ready": [], "waiting": [], "stalled": []}
    for t in st.get("ready", []):
        if t.get("agent") not in AGENTS or not installed(t["agent"]):
            continue
        key, w = f"{t['project']}/{t['id']}", st.get("woken", {}).get(f"{t['project']}/{t['id']}")
        item = {"id": t["id"], "owner": t["agent"], "text": t["text"], "project": t["project"]}
        if w and now - w.get("since", w["at"]) >= WAKE_AGAIN:
            lanes["stalled"].append({**item, "why": f"Woken {int((now - w.get('since', w['at'])) // 60)} min ago, not claimed"})
        elif key in st.get("waiting", {}):
            lanes["waiting"].append({**item, "why": st["waiting"][key]})
        else:
            lanes["ready"].append({**item, "why": f"Woken, session {w['session']}" if w else ""})
    tok = token_store.token()
    return {"status": {"host": socket.gethostname(), "live": sum(len(a["sessions"]) for a in agents),
                       "stalled": len(lanes["stalled"]), "synced": st.get("synced"), "syncError": st.get("sync_error", "")},
            "agents": agents, "tasks": lanes, "activity": load("activity.json", [])[:50],
            "settings": {"serverUrl": token_store.url(), "token": {"present": bool(tok)},
                         "startWhenNone": {a: bool(settings.get("startWhenNone", {}).get(a)) for a in AGENTS}},
            "version": version()}


def origin_of(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def update_plugins():
    """The plugin of each agent here: Codex and agy with their own CLI (as self_update does daily); Claude Code updates
    its plugins itself."""
    for a in ("codex", "agy"):
        if installed(a) and plugin_version(a):
            msg = self_update.run(a) or f"{AGENTS[a][0]}: the knowledge plugin is up to date"
            log("sync", msg)
    keep_trusted()


class Handler(BaseHTTPRequestHandler):
    server_version = "knowledge-companion"

    def origin(self):
        """The calling page: the companion's own or the knowledge web UI; '' when none was sent (a same-origin GET)."""
        o = self.headers.get("Origin", "")
        return o if o in (SELF, origin_of(token_store.url())) else (None if o else "")

    def key_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Companion-Key", ""), api_key())

    def host_ok(self) -> bool:  # DNS rebinding: a foreign name that resolves to 127.0.0.1 gets nothing
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def send(self, code: int, body, ctype="application/json", origin=""):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        o = self.origin()
        if not self.host_ok() or not o or not self.path.startswith("/local/api/"):
            return self.send(403, {"error": "not allowed"})
        self.send_response(204)
        for k, v in (("Access-Control-Allow-Origin", o), ("Access-Control-Allow-Methods", "GET, POST"),
                     ("Access-Control-Allow-Headers", "Content-Type, X-Companion-Key"), ("Access-Control-Allow-Private-Network", "true"),
                     ("Access-Control-Max-Age", "600"), ("Vary", "Origin"), ("Content-Length", "0")):
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        o, path = self.origin(), urllib.parse.urlsplit(self.path).path
        if not self.host_ok() or o is None:
            return self.send(403, {"error": "not allowed"})
        if path == "/local/api/hello":
            return self.send(200, {"companion": version(), "host": socket.gethostname()}, origin=o)
        if path == "/local/api/state":
            s = page_state()
            if not self.key_ok():  # the web UI's tab (and anyone else here): the agents, no tasks, folders or activity
                s = {"agents": s["agents"], "status": {"host": s["status"]["host"]}, "version": s["version"], "locked": True}
            return self.send(200, s, origin=o)
        name = "companion.html" if path in ("/", "/index.html") else path.lstrip("/")
        files = {f.name: f for f in STATIC.iterdir() if f.is_file()}
        if name not in files:
            return self.send(404, {"error": "not found"})
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
        self.send(200, files[name].read_bytes(), types.get(files[name].suffix, "application/octet-stream"))

    def do_POST(self):
        o, path = self.origin(), urllib.parse.urlsplit(self.path).path
        # a POST always carries Origin in a browser; JSON only, so a foreign page cannot send it without a preflight
        if not self.host_ok() or not o or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(403, {"error": "not allowed"})
        if path != "/local/api/trust" and not self.key_ok():  # trusting the knowledge hooks is what the setup does anyway
            return self.send(403, {"error": "Open the companion from its shortcut: its link carries the key this change needs"}, origin=o)
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
            agent = body.get("agent")
            if agent is not None and agent not in AGENTS:
                raise ValueError(f"agent must be one of {', '.join(AGENTS)}")
            if path == "/local/api/trust":
                return self.send(200, {"trusted": trust_all([agent] if agent else [a for a in AGENTS if installed(a)])}, origin=o)
            if path == "/local/api/yolo":
                if o != SELF:  # switching approvals off is for this pc's own page, not for a web page elsewhere
                    return self.send(403, {"error": "YOLO can only be changed on the companion page itself"}, origin=o)
                if not agent:
                    raise ValueError("send {agent, on}")
                return self.send(200, {"yolo": set_yolo(agent, bool(body.get("on")))}, origin=o)
            if path == "/local/api/settings":
                start = body.get("startWhenNone", {})
                with LOCK:
                    s = load("settings.json", {})
                    s["startWhenNone"] = {a: bool(start.get(a)) for a in AGENTS}
                    save("settings.json", s)
                return self.send(200, {"ok": True}, origin=o)
            if path == "/local/api/tick":
                return self.send(200, tick(), origin=o)
            if path == "/local/api/update":
                threading.Thread(target=update_plugins, daemon=True).start()
                return self.send(200, {"started": True}, origin=o)
        except (ValueError, AttributeError) as e:
            return self.send(400, {"error": str(e)}, origin=o)
        except Exception as e:  # say it on the page instead of a dropped connection
            log("error", f"{path} failed", str(e)[:300])
            return self.send(500, {"error": str(e)[:300]}, origin=o)
        self.send(404, {"error": "not found"}, origin=o)

    def log_message(self, *a):
        pass


def loop():
    while True:
        try:
            tick()
        except Exception as e:  # never stop the loop; the page shows it
            log("error", "A round of the companion failed", f"{type(e).__name__}: {e}"[:300])
        time.sleep(EVERY)


def serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # never another interface: the API changes agent settings
    threading.Thread(target=loop, daemon=True).start()
    print(f"knowledge companion on {SELF}", flush=True)
    httpd.serve_forever()


def main(argv: list):
    cmd, rest = (argv[0] if argv else ""), argv[1:]
    if cmd in ("serve", "companion") and not rest:
        serve()
    elif cmd == "tick" and not rest:
        print(json.dumps(tick()))
    elif cmd == "trust" and len(rest) <= 1 and (not rest or rest[0] in AGENTS):
        done = trust_all(rest or [a for a in AGENTS if installed(a)])
        print(json.dumps(done))
        if failed := [a for a, r in done.items() if r.startswith("failed")]:
            sys.exit(f"could not trust the hooks of {', '.join(failed)}; the others are trusted")
    elif cmd == "yolo-off" and not rest:
        if waits := yolo_off():
            sys.exit(f"YOLO stays on for {', '.join(waits)} until it is closed: close it and run this again")
        print("YOLO is off for every agent; their own settings are back")
    elif "--self-test" in argv:
        t = '# mine\napproval_policy = "on-request" # keep\n\n[notice]\nother = 1\n\n[projects."/x"]\ntrust_level = "trusted"\n'
        t2, old = toml_set(t, None, "approval_policy", '"never"')
        assert old == '"on-request" # keep' and 'approval_policy = "never"\n' in t2, t2
        t3, old = toml_set(t2, None, "sandbox_mode", '"danger-full-access"')
        assert old is None and t3.index("sandbox_mode") < t3.index("[notice]"), t3
        t4, _ = toml_set(t3, "notice", "hide_full_access_warning", "true")
        assert t4.index("hide_full_access_warning") < t4.index("[projects"), t4
        back = toml_set(toml_set(toml_set(t4, "notice", "hide_full_access_warning", None)[0], None, "sandbox_mode", None)[0],
                        None, "approval_policy", '"on-request" # keep')[0]
        assert back == t, back
        assert toml_set("", "notice", "x", "true")[0] == "[notice]\nx = true\n"
        d = {"a": 1}
        assert json_set(d, ("p", "m"), "b") == MISSING and d == {"a": 1, "p": {"m": "b"}}
        assert json_set(d, ("p", "m"), MISSING) == "b" and d == {"a": 1, "p": {}}
        assert ours({"pluginId": "knowledge@knowledge"}) and not ours({"pluginId": "other@x", "command": "x.sh"})
        globals()["CLAUDE_JSON"] = HOME / ".knowledge-companion-self-test-missing.json"
        assert untrusted("claude", ["/x"]) == [], "Claude never ran here: nothing to trust, no trust round every minute"
        print("ok")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
