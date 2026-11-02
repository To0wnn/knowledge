#!/usr/bin/env python3
"""The knowledge companion: a small local service on this pc that starts the tasks the server says may start now.

Every minute it asks GET /tasks/ready and, when this pc allows workers (off by default), starts a worker for each
ready task: a fresh headless session of the task's agent in its own git worktree (workers.py), never a message into
someone's chat. It hands a worker over at 90% of the agent's usage limit, removes a worktree once its branch is
merged, and reports the machine's state (POST /machine/report) for the web UI's Machines list. About every 10 s it
fetches the fixed remote actions its owner queued there (GET /machine/actions), and the messages of the owner's chats
with an agent here (GET /machine/chats, every ~2 s while a chat is active; chat.py runs each turn). It also trusts the knowledge hooks the
agents' own way, turns the agents' native "run without asking" settings on and off (YOLO), and reports hook failures
the agents log themselves. Its page and local API listen on 127.0.0.1:47321 only; the knowledge web UI shows a
"This pc" tab when it answers.

  companion.py serve          the page, the local API and the minute loop (knowledge-agent.py serve runs this)
  companion.py tick           one round of the loop, its summary as JSON
  companion.py trust [AGENT]  trust the knowledge hooks of every agent here (or one), then keep them trusted
  companion.py yolo-off       put back the agents' own settings YOLO changed (uninstall-companion runs this)
Standard library only; Linux, macOS and Windows.
"""
import atexit
import contextlib
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
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
import agent_hooks  # noqa: E402  (the wake code and the session registry)
import chat  # noqa: E402  (chats from the web UI)
import project_marker  # noqa: E402
import self_update  # noqa: E402
import token_store  # noqa: E402
import workers  # noqa: E402  (a fresh agent session per task)

CREATE_NO_WINDOW, DETACHED_PROCESS = 0x08000000, 0x00000008


def no_window(flags: int) -> int:
    """Windows creation flags for a child of the companion: under pythonw it has no console, so every console program
    it starts (git, tasklist, codex, agy, a status line) would open a window of its own."""
    return flags if flags & DETACHED_PROCESS else flags | CREATE_NO_WINDOW


if os.name == "nt":  # every subprocess of this process, also those of the modules it imports (self_update, limits)
    _popen_init = subprocess.Popen.__init__

    def _popen_no_window(self, *args, creationflags=0, **kw):
        _popen_init(self, *args, creationflags=no_window(creationflags), **kw)
    subprocess.Popen.__init__ = _popen_no_window

PORT = int(os.environ.get("KNOWLEDGE_COMPANION_PORT") or 47321)  # fixed: the web UI looks for exactly this port
SELF = f"http://127.0.0.1:{PORT}"
STATIC = HOOKS.parent / "companion" / "static"
HOME = Path.home()
# A login service gets the PATH of whoever installed it, not the one a shell profile builds, so agents installed with
# npm/mise/bun/volta (~/.zshrc adds their bin folder) stay invisible: add the usual user install folders that exist.
AGENT_DIRS = [HOME / d for d in (".npm-global/bin", ".local/bin", ".local/share/mise/shims", ".bun/bin", ".volta/bin",
                                 ".cargo/bin", "bin")] + [Path(d) for d in ("/opt/homebrew/bin", "/usr/local/bin")]
if os.name == "nt" and os.environ.get("APPDATA"):
    AGENT_DIRS.append(Path(os.environ["APPDATA"]) / "npm")
_have = os.environ.get("PATH", "").split(os.pathsep)
os.environ["PATH"] = os.pathsep.join(_have + [str(d) for d in AGENT_DIRS if str(d) not in _have and d.is_dir()])
KN_HOME = token_store.KN_HOME  # KNOWLEDGE_HOME relocates it (tests)
STATE = KN_HOME / "companion"
BACKUPS = HOME / "backups" / "knowledge-companion"
EVERY = float(os.environ.get("KNOWLEDGE_COMPANION_EVERY") or 60)
AGENTS = {"claude": ("Claude Code", "C"), "codex": ("Codex", "X"), "agy": ("agy", "A")}
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
CLAUDE_JSON = (CLAUDE_DIR if os.environ.get("CLAUDE_CONFIG_DIR") else HOME) / ".claude.json"
CODEX = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
AGY_DIR = HOME / ".gemini" / "antigravity-cli"
AGY_SETTINGS = AGY_DIR / "settings.json"
PROCESS = {"agy": os.environ.get("KNOWLEDGE_AGY_PROCESS") or "agy",  # other names in the tests
           "codex": os.environ.get("KNOWLEDGE_CODEX_PROCESS") or "codex"}
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


def newline_of(path: Path) -> str:
    """The file's own line ending: Python's text mode would turn an agent's LF file into CRLF on Windows."""
    try:
        return "\r\n" if b"\r\n" in path.read_bytes() else "\n"
    except OSError:
        return "\n"


def write_text(path: Path, text: str):
    """Replace a file at once through a temp file of its own: two writers (threads, or a hook process logging) never
    write into one temp file, so neither can leave a torn mix of both."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".companion-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline_of(path)) as f:
            f.write(text)
        if path.exists():  # ~/.claude.json is private: the new file keeps the old one's mode
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, data, indent=2):
    write_text(path, json.dumps(data, indent=indent, ensure_ascii=False) + "\n")


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
    """Read, change, write back; a file that is not valid JSON is never overwritten. One at a time in this process (the
    minute loop, the remote actions and the page all trust and switch YOLO)."""
    with LOCK:
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


def running(agent: str) -> bool:
    name = PROCESS[agent]
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/NH"], capture_output=True, text=True)
        return f"{name}.exe".lower() in r.stdout.lower()
    return subprocess.run(["pgrep", "-x", name[:15]], capture_output=True).returncode == 0


def rfc3339(s: str) -> float:
    try:
        return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return 0.0


def sessions(agent: str, now: float) -> list:
    """The interactive sessions of an agent here, newest first (shown on the page; tasks never go into them)."""
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
    elif agent == "codex" and not running("codex"):
        # Codex leaves no mark when a session ends: with no Codex running at all, no registered session is open
        pass
    else:
        for _, e in agent_hooks.read_entries(f"{agent}-*.json"):
            if e.get("agent") == agent and e.get("cwd") and now - e.get("at", 0) <= 2 * 3600:
                out.append({"session": e["session"], "cwd": e["cwd"], "project": agent_hooks.project_query(e["cwd"])["project"],
                            "at": e["at"], "entry": e})
    return sorted(out, key=lambda s: -s["at"])


def known_folders() -> set:
    """Folders the agents worked in (session registry, Claude's projects, agy's trusted folders, Codex's projects) and
    the folders linked on the companion page."""
    cands = {e.get("cwd") for _, e in agent_hooks.read_entries("*.json")} | set(load("linked.json", []))
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
    return {c for c in cands if c}


def project_folders() -> dict:
    """{project: folder} of the folders here linked to a project (.knowledge-project), never the home folder: from the
    known folders, read anew each time (a folder linked a moment ago counts at once)."""
    out = {}
    DENIED.clear()
    for c in sorted(known_folders()):
        try:
            # the path as the agent wrote it, not resolve()d: a macOS /var or a Windows 8.3 name would not match its own key
            root, name = project_marker.project_root(Path(os.path.abspath(c)))
            if not (root and name) or root.resolve() == HOME.resolve():
                continue
        except PermissionError:  # macOS: ~/Documents, ~/Desktop, ~/Downloads need consent for the python launchd starts
            DENIED.add(c)
            continue
        except OSError:
            continue
        # a project linked in several folders: a worker needs a git checkout for its own worktree, so that one wins
        if name not in out or (in_git(root) and not in_git(Path(out[name]))):
            out[name] = str(root)
    return out


def in_git(p: Path) -> bool:
    return any((q / ".git").exists() for q in (p, *p.parents))


DENIED: set = set()  # known folders the companion may not read (project_folders); why_not names them


# ---------------------------------------------------------------- folders (the link to a project)

_scan: dict = {"at": 0.0, "repos": []}
SCAN_EVERY = float(os.environ.get("KNOWLEDGE_COMPANION_SCAN_EVERY") or 60)


def git_repos(depth: int = 3) -> list:
    """Git checkouts under the home folder, at most `depth` levels down, skipping hidden folders and heavy trees;
    scanned at most once a minute (a link or unlink always looks again)."""
    if time.time() - _scan["at"] < SCAN_EVERY:
        return _scan["repos"]
    found, skip = [], {"node_modules", "AppData", "Library", "snap", "venv", "__pycache__", "worktrees"}
    if sys.platform == "darwin":  # protected (TCC): walking them at logon would ask "python3 would like to access ..."
        skip |= {"Documents", "Desktop", "Downloads"}

    def walk(d: Path, left: int):
        try:
            subs = [s for s in d.iterdir() if s.is_dir() and not s.is_symlink()]
        except OSError:
            return
        if any(s.name == ".git" for s in subs) or (d / ".git").is_file():
            found.append(str(d))
            return
        for s in subs:
            if left and not s.name.startswith(".") and s.name not in skip:
                walk(s, left - 1)
    walk(HOME, depth)
    _scan.update(at=time.time(), repos=sorted(found))
    return _scan["repos"]


def under_home(p: Path) -> bool:
    try:
        return p.resolve() != HOME.resolve() and p.resolve().is_relative_to(HOME.resolve())
    except OSError:
        return False


def folder_links() -> dict:
    """{linked: [{path, project}], candidates: [{path}]}: the folders linked here, and the folders that could be
    (git checkouts under the home folder and folders agents worked in) that are not linked yet."""
    linked, cands = {}, []
    for c in sorted(known_folders() | set(git_repos())):
        try:
            p = Path(os.path.abspath(c))
            root, name = project_marker.project_root(p)
            if root and name and under_home(root):
                linked.setdefault(str(root), name)
            elif not root and p.is_dir() and under_home(p):
                # git only for a folder inside a checkout: no .git up the tree, or .git right here, means the folder itself
                git = next((q for q in (p, *p.parents) if (q / ".git").exists()), p) != p
                top = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"], capture_output=True, text=True,  # git prints UTF-8, never the ANSI code page
                                     encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=10) if git and shutil.which("git") else None
                # the checkout's root, as knowledge-init links it; git prints it with / on Windows too
                top = Path(top.stdout.strip()) if top and top.returncode == 0 and top.stdout.strip() else None
                # git prints the resolved path (macOS /private/var, a Windows long name): keep the spelling the agent used
                cands.append(next((str(q) for q in (p, *p.parents) if q.resolve() == top.resolve()), str(top)) if top else str(p))
        except (OSError, subprocess.SubprocessError):
            continue
    cands = sorted({c for c in cands if c not in linked and under_home(Path(c))})
    return {"linked": [{"path": k, "project": v} for k, v in sorted(linked.items())], "candidates": [{"path": c, "project": ""} for c in cands]}


def link_folder(path: str, project: str) -> str:
    """Link a folder the companion listed (candidate or already linked) to a project this token may write: the
    .knowledge-project file, as the knowledge-init skill writes it. Never a path from elsewhere."""
    _scan["at"] = 0
    f = folder_links()
    if path not in {x["path"] for x in f["linked"] + f["candidates"]}:
        raise ValueError("only a folder from the companion's own list can be linked")
    projects = server("GET", "/machine/projects", token_store.token()).get("projects", [])
    if project not in projects:
        raise ValueError(f"this token may not write project {project}; one of: {', '.join(projects) or 'none'}")
    marker = Path(path) / project_marker.MARKER
    marker.write_text(project + "\n", encoding="utf-8")
    with LOCK:
        save("linked.json", sorted(set(load("linked.json", [])) | {path}))
    _scan["at"] = 0
    note = (" The file holds only the name, so it may be committed (git add .knowledge-project), or kept out."
            if (Path(path) / ".git").exists() else "")
    log("folder", f"Linked {path} to project {project}", "Sessions started there from now on get this project." + note)
    return f"linked {path} to {project}." + note


def unlink_folder(path: str) -> str:
    """Remove the link of a folder the companion lists as linked: only its .knowledge-project file."""
    if path not in {x["path"] for x in folder_links()["linked"]}:
        raise ValueError("only a linked folder from the companion's own list can be unlinked")
    (Path(path) / project_marker.MARKER).unlink()
    with LOCK:
        save("linked.json", [p for p in load("linked.json", []) if p != path])
    log("folder", f"Unlinked {path}", "Only its .knowledge-project file was removed")
    return f"unlinked {path}"


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
        write_text(path, text)
        return old
    return edit_json(path, lambda d: [json_set(d, where, v) for (_, where, _), v in zip(spec, values)])


def set_yolo(agent: str, on: bool) -> str:
    with LOCK:
        yolo = load("yolo.json", {})
        mine = yolo.get(agent, {})
        if agent == "agy" and running("agy"):  # agy rewrites its settings.json on exit: change it only while it is closed
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
    if p is not None and not running("agy"):
        set_yolo("agy", p)


# ---------------------------------------------------------------- the server

def server(method: str, path: str, tok: str, body=None, timeout: float = 15):
    req = urllib.request.Request(token_store.url() + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={**token_store.headers(tok), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def server_file(tok: str, ref: str) -> bytes:
    """A file attached to a chat message in the web UI (chat.attach puts it on disk)."""
    req = urllib.request.Request(token_store.url() + "/machine/chats/file?file=" + urllib.parse.quote(ref), headers=token_store.headers(tok))
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def claim(task: dict, tok: str, session: str, start: str = "") -> str:
    """Take the task for this pc on the server before waking anything: '' when this pc holds it now, else the device
    that does (another pc already woke a session for it, or the line is pinned to another device)."""
    try:
        server("POST", "/tasks/woken", tok, {"project": task["project"], "id": task["id"], "agent": task["agent"],
                                             "session": session[:80], **({"start": start} if start else {})})
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
        return (json.loads(e.read().decode("utf-8") or "{}").get("held_by") or {}).get("device") or "another device"
    return ""


def release(task: dict, tok: str):
    """Give the task's lease back after a failed start, so another pc may take it at once; never raises."""
    try:
        server("POST", "/tasks/release", tok, {"project": task["project"], "id": task["id"]})
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409: another pc holds it, nothing of ours to give back
            log("error", f"Could not give {task['id']} back on the server", f"{e}"[:200])
    except Exception as e:
        log("error", f"Could not give {task['id']} back on the server", f"{e}"[:200])


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


def settings() -> dict:
    """Workers: off on a new pc (a work laptop or the family's gaming pc must never run them by surprise); at most one
    per agent unless the owner says more."""
    s = load("settings.json", {})
    return {"workersAllowed": s.get("workersAllowed") is True,
            "maxWorkers": {a: max(0, min(8, int((s.get("maxWorkers") or {}).get(a, 1)))) for a in AGENTS}}


def save_settings(change: dict):
    with LOCK:
        s = load("settings.json", {})
        if "workersAllowed" in change:
            s["workersAllowed"] = change["workersAllowed"] is True
        for a, n in (change.get("maxWorkers") or {}).items():
            if a in AGENTS:
                s.setdefault("maxWorkers", {})[a] = max(0, min(8, int(n)))
        save("settings.json", s)


def usage(agent: str, ws: dict, now: float):
    """The agent's usage limit in percent, or None (unknown)."""
    got = usage_info(agent, ws, now)
    return got[0] if got else None


def usage_info(agent: str, ws: dict, now: float):
    """(percent, window, resets_at) of the agent's fullest usage window, or None (unknown). Codex and agy only tell it
    while a session runs, so the last reading is kept until its window resets: usage does not go down before that."""
    got = usage_live(agent, ws, now)
    f = STATE / f"usage-{agent}.json"
    if got:
        try:
            write_json(f, {"got": list(got), "at": now})
        except OSError:
            pass
        return got
    try:
        old = json.loads(f.read_text(encoding="utf-8"))
        pct, window, resets = old["got"]
        return (pct, window, resets) if resets and resets > now else None  # shortcut: another pc may have used more since
    except (OSError, ValueError, KeyError, TypeError):
        return None


CODEX_READ_EVERY = 900  # seconds between asking Codex for its limits without a session


def codex_rate_read(now: float):
    """Codex's limits when no session ran lately: `codex app-server`, then account/rateLimits/read (no prompt, no
    tokens), in the background at most every CODEX_READ_EVERY; the last answer until then, None when unknown."""
    f = STATE / "codex-rate.json"
    try:
        old = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    if now - old.get("at", 0) >= CODEX_READ_EVERY:
        write_json(f, {**old, "at": now})  # one read at a time
        threading.Thread(target=_codex_rate_ask, args=(f,), daemon=True).start()
    got = old.get("got")
    return tuple(got) if got and got[2] and got[2] > now else None


def _codex_rate_ask(f):
    exe, got = shutil.which("codex"), None
    if exe:
        cmd = [exe, "app-server"]
        if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
            cmd = workers.shim_line(cmd)
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                 **({"creationflags": workers.WIN_FLAGS} if os.name == "nt" else {"start_new_session": True}))
            lines = []
            reader = threading.Thread(target=lambda: lines.extend(iter(p.stdout.readline, "")), daemon=True)
            reader.start()
            for m in ({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "knowledge-companion-limits", "version": "1"}}},
                      {"method": "initialized"}, {"id": 2, "method": "account/rateLimits/read"}):
                p.stdin.write(json.dumps({"jsonrpc": "2.0", **m}) + "\n")
            p.stdin.flush()
            end = time.time() + 20
            while time.time() < end and not any('"id": 2' in ln or '"id":2' in ln for ln in lines):
                time.sleep(0.2)
            with contextlib.suppress(OSError, ValueError):
                p.stdin.close()  # EOF: the app-server ends by itself
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:  # else its whole tree (cmd.exe → node → codex on Windows), as a worker's stop does
                workers.kill({"pid": p.pid, "ident": workers.identity(p.pid)}, hard=True)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(timeout=5)
            for ln in lines:
                try:
                    m = json.loads(ln)
                except ValueError:
                    continue
                if m.get("id") == 2:
                    rl = (m.get("result") or {}).get("rateLimits") or {}
                    got = agent_hooks.limits.worst([(agent_hooks.limits.label(v.get("windowDurationMins")), v.get("usedPercent"), v.get("resetsAt"))
                                                    for v in (rl.get("primary"), rl.get("secondary")) if isinstance(v, dict)], time.time())
        except (OSError, ValueError, TypeError):
            got = None
    try:
        write_json(f, {"at": time.time(), "got": list(got) if got else None})
    except OSError:
        pass


AGY_TOKEN = HOME / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
AGY_QUOTA_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"  # agy's own --cloud_code_endpoint


def agy_rate_read(now: float):
    """agy's quota without a running agy: the cloud call agy itself makes (fetchAvailableModels, no prompt, no tokens)
    with the OAuth token agy keeps; when that token expired, `agy models` (no prompt, no conversation) renews it first.
    In the background at most every CODEX_READ_EVERY; the last answer until then, None when unknown. Undocumented API:
    a failure goes to the activity log once per new error."""
    f = STATE / "agy-rate.json"
    try:
        old = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    if now - old.get("at", 0) >= CODEX_READ_EVERY and AGY_TOKEN.exists():
        write_json(f, {**old, "at": now})
        threading.Thread(target=_agy_rate_ask, args=(f, old.get("error", "")), daemon=True).start()
    got = old.get("got")
    return tuple(got) if got and got[2] and got[2] > now else None


def _agy_token() -> tuple:
    """(access token, its expiry as epoch) from agy's token file."""
    tok = json.loads(AGY_TOKEN.read_text(encoding="utf-8")).get("token") or {}
    return str(tok.get("access_token") or ""), agent_hooks.limits.epoch(tok.get("expiry")) or 0


def _agy_rate_ask(f, last_error: str):
    got, err = None, ""
    try:
        access, expiry = _agy_token()
        if (not access or expiry < time.time() + 60) and (exe := shutil.which("agy")):
            cmd = [exe, "models"]
            if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
                cmd = workers.shim_line(cmd)
            subprocess.run(cmd, capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                           **({"creationflags": workers.WIN_FLAGS} if os.name == "nt" else {}))  # renews the token, asks no model
            access, expiry = _agy_token()
        if not access:
            raise LookupError("agy has no access token (signed out?)")
        req = urllib.request.Request(AGY_QUOTA_URL, data=b"{}", method="POST",
                                     headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json",
                                              "User-Agent": "antigravity-cli"})  # Python's own agent gets 403 PERMISSION_DENIED
        # proxies from the environment only: macOS's system proxy lookup can hang in a background thread (CI, 2 Nov 2026)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies_environment()))
        with opener.open(req, timeout=15) as r:
            models = json.loads(r.read().decode("utf-8") or "{}").get("models") or {}
        configs = [{"label": m["displayName"], "quotaInfo": m["quotaInfo"]} for m in models.values()
                   if isinstance(m, dict) and m.get("displayName") and isinstance(m.get("quotaInfo"), dict)]
        if not configs:
            raise ValueError("no model with a quota in the answer (the API changed?)")
        got = agent_hooks.limits.agy_used({"userStatus": {"cascadeModelConfigData": {"clientModelConfigs": configs}}}, time.time())
        if not got:  # nothing used yet in any window: 0%, known (the chip shows it), with the first reset
            resets = [(x, r) for x in configs if (r := agent_hooks.limits.epoch(x["quotaInfo"].get("resetTime"))) and r > time.time()]
            got = (0.0, min(resets, key=lambda xr: xr[1])[0]["label"], min(r for _, r in resets)) if resets else None
    except (OSError, ValueError, LookupError, KeyError, TypeError, subprocess.SubprocessError) as e:
        err = f"{type(e).__name__}: {e}"[:300]
        if err != last_error:
            log("error", "Could not read agy's usage limit", err)
    try:
        write_json(f, {"at": time.time(), "got": list(got) if got else None, "error": err})
    except OSError:
        pass


def usage_live(agent: str, ws: dict, now: float):
    """Claude the higher of its workers' own stream-json output (until that window resets) and an interactive Claude's
    status line under 15 min old; Codex from its newest rollout of the last 15 min, else asked without a session
    (codex_rate_read); agy from a registered agy's language server."""
    try:
        if agent == "claude":
            logs = sorted((Path(w["log"]) for w in ws.values() if w.get("agent") == "claude" and w.get("log") and Path(w["log"]).exists()),
                          key=lambda f: f.stat().st_mtime)
            # a worker's reading stays true until its window resets (claude_usage drops those), however old the log
            got = next((g for f in reversed(logs) if (g := workers.claude_usage(str(f), now))), None)
            line = agent_hooks.limits.claude_used(now)
            try:  # a web chat's turn (chat.py keep_usage): fresh while the person only chats
                chat_got = tuple(json.loads((STATE / "usage-claude-chat.json").read_text(encoding="utf-8"))["got"])
                chat_got = chat_got if chat_got[2] and chat_got[2] > now else None
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                chat_got = None
            # usage never drops inside a window and expired ones are left out: the fullest reading is the true one
            return max((x for x in (got, line, chat_got) if x), key=lambda x: x[0], default=None)
        if agent == "codex":
            days = [time.localtime(now - d * 86400) for d in (0, 1)]
            files = [f for d in days for f in CODEX.glob(f"sessions/{d.tm_year:04d}/{d.tm_mon:02d}/{d.tm_mday:02d}/rollout-*.jsonl")
                     if now - f.stat().st_mtime < 900]
            return (agent_hooks.limits.codex_used(str(max(files, key=lambda f: f.stat().st_mtime)), now) if files else None) or codex_rate_read(now)
        try:  # a running agy's own language server first; else agy's cloud quota (agy_rate_read)
            return agent_hooks.limits.agy_used(agent_hooks.agy_status(), now) or agy_rate_read(now)
        except LookupError:
            return agy_rate_read(now)
    except Exception:  # no numbers: unknown, which allows one worker
        return None


RESTART_AFTER = 6 * 3600


def shared_running(ws: dict, w: dict) -> bool:
    """Another running worker uses the same worktree (a hand-over <ID>-cont goes on in it)."""
    return any(o is not w and o["status"] == "running" and o.get("worktree") and o.get("worktree") == w.get("worktree") for o in ws.values())


def worker_session(tid: str) -> str:
    return f"worker:{token_store.machine().get('id', '')}:{tid}"[:80]


def why_not(t: dict, ws: dict, s: dict, lim: dict, game: str) -> str:
    """'' when this pc may start a worker for task t now, else why not (shown on the page and the Machines list)."""
    a = t["agent"]
    if not re.fullmatch(r"[\w.-]{1,60}", t["id"]):  # it names a branch, a folder and a file
        return f"Task id {t['id'][:40]!r} cannot name a branch; give it a short id (letters, digits, - . _)"
    if not s["workersAllowed"]:
        return "Workers are off on this pc (Settings)"
    if busy := next((w["id"] for w in ws.values() if w["status"] == "running" and w.get("project") == t["project"]
                     and workers.base_id(w["id"]) == workers.base_id(t["id"])), None):
        return f"{busy} still runs in this task's worktree; the hand-over starts once it has ended"
    if game:
        return f"No workers while {game}"
    if (lim.get(a) or 0) >= 90:
        return f"waiting for limit: {AGENTS[a][0]} is at {lim[a]:.0f}% of its usage limit"
    cap = s["maxWorkers"][a] if lim.get(a) is not None else min(1, s["maxWorkers"][a])  # unknown limit: one at most
    if sum(1 for w in ws.values() if w["agent"] == a and w["status"] == "running") >= cap:
        return f"{AGENTS[a][0]} runs {cap} worker(s) already, its maximum here" + ("" if lim.get(a) is not None else " (limit unknown: 1)")
    if not project_folders().get(t["project"]):
        return (f"No folder linked to project {t['project']} on this pc: link one under Machines → this pc → Folders "
                "(or the companion page), then a worker knows where to work") + (
            f"; the companion may not read {', '.join(sorted(DENIED)[:3])}: give {sys.executable} Full Disk Access "
            "(System Settings, Privacy & Security), or link a folder outside Documents, Desktop and Downloads" if DENIED else "")
    return ""


def start_worker(t: dict, tok: str, now: float, ws: dict = None) -> tuple:
    """Take the task on the server for a new worker session, then start it: (record, '') or (None, why not)."""
    nonce = secrets.token_hex(8)
    if held := claim(t, tok, worker_session(t["id"]), nonce):
        return None, f"Held by {held}"
    try:
        where = workers.worktree(project_folders()[t["project"]], t["id"], t["project"])
        # a hand-over goes on in the worktree of the first session: its branch counts from where that one started, so
        # a branch rolled back to that point is never taken for merged
        first = min((w for w in (ws or {}).values() if where.get("worktree") and w.get("worktree") == where["worktree"] and w.get("base")),
                    key=lambda w: w.get("started", 0), default=None)
        where = {**where, **{k: first[k] for k in ("base", "base_branch", "config") if first.get(k)}} if first else where
        yolo = bool(load("yolo.json", {}).get(t["agent"], {}).get("on"))
        rec = workers.start(t["agent"], t, token_store.machine().get("label") or socket.gethostname(), where, yolo, nonce)
    except Exception as e:
        release(t, tok)
        raise RuntimeError(f"could not start a {AGENTS[t['agent']][0]} worker: {e}") from e
    return rec, ""


def left_open(w: dict, tok: str) -> bool:
    """The worker's task may still start (its line is there, not claimed, not handed over): it ended without doing its
    job. Asked fresh, after its lease went back; unknown (server away) counts as not open."""
    try:
        return any(t["project"] == w["project"] and t["id"] == w["id"] for t in server("GET", "/tasks/ready", tok)["tasks"])
    except Exception:
        return False


def check_workers(ws: dict, tok: str, now: float, lim: dict):
    """Finished workers, leases of running ones, the 90% hand-over (note first, hard stop after the grace), and the
    worktrees of merged ones."""
    for w in ws.values():
        if w["status"] == "running":
            p = workers.PROCS.get(w["pid"])  # started by this companion: its exit code (and no zombie)
            if p:
                code = p.poll()
            else:  # started before a restart of the companion or the pc: by its pid and start time, its exit code unknown
                code = None if workers.running(w) else "unknown"
            if code is not None:
                workers.PROCS.pop(w["pid"], None)
                # exit code unknown (the companion restarted while it ran): its outcome decides, below: a task handed
                # back or gone is finished, one still open failed
                w.update(status="stopped" if w.get("told") or w.get("chat") else "finished" if code in (0, "unknown") else "failed", ended=now, code=code)
                if not w.get("owner_stopped"):  # stopped on purpose: no other pc may take it at once, only after the lease
                    release(w, tok)  # a line it claimed but left open may start again at once (the server's idle notice)
                workers.stop_path(w).unlink(missing_ok=True)
                if w["status"] == "finished" and left_open(w, tok):  # exit 0 is not done: e.g. its tools were refused
                    w.update(status="failed", why="ended without claiming or handing back its task" if code == 0 else
                             "the companion restarted while it ran; its task is still open")
                    log("error", f"{AGENTS[w['agent']][0]}: the worker for {w['id']} ended without claiming or handing back its task",
                        f"Not started again for {RESTART_AFTER // 3600} h unless asked. " + w.get("open", ""))
                else:
                    log("worker", f"{AGENTS[w['agent']][0]}: the worker for {w['id']} ended ({w['status']})", w.get("open", ""))
                send_final(tok, w)
                continue
            try:  # a turn longer than the lease must not let another pc start the same task
                held = claim({"project": w["project"], "id": w["id"], "agent": w["agent"]}, tok, worker_session(w["id"]), w["nonce"])
            except Exception as e:
                held = ""
                if not (isinstance(e, urllib.error.HTTPError) and e.code == 404):  # 404: the worker removed its line, it is ending
                    log("error", f"Could not renew the lease of {w['id']}", f"{e}"[:200])
            if held:  # this pc slept past its lease and another took the task over: two workers must not run one task
                try:
                    workers.kill(w, hard=True)
                    log("worker", f"{AGENTS[w['agent']][0]}: the worker for {w['id']} was stopped", f"{held} took the task over while this pc slept")
                except Exception as e:
                    log("error", f"Could not stop the worker for {w['id']}", f"{e}"[:200])
                continue
            if (lim.get(w["agent"]) or 0) >= 90 and not w.get("told"):
                room = [b for b, v in lim.items() if b != w["agent"] and v is not None and v < 90]
                target = workers.NAMES[room[0] if room else w["agent"]]
                workers.stop_note(w, target)
                w["told"] = now
                log("worker", f"{AGENTS[w['agent']][0]}: usage limit at 90%, the worker for {w['id']} hands over to {target}",
                    f"Stopped in {workers.GRACE // 60} min if it has not ended")
            elif w.get("told") and now - w["told"] >= workers.GRACE:
                try:  # one worker that cannot be stopped must not cost the round (the save, the report)
                    workers.kill(w, hard=True)
                    log("worker", f"{AGENTS[w['agent']][0]}: the worker for {w['id']} was stopped (usage limit)", "")
                except Exception as e:
                    log("error", f"Could not stop the worker for {w['id']}", f"{e}"[:200])
        elif w["status"] != "merged" and w.get("worktree") and not shared_running(ws, w) and workers.merged(w):
            w["status"], w["result"] = "merged", workers.remove(w)
            for o in ws.values():  # the sessions before or after it in that worktree (a hand-over) share its branch
                if o is not w and o.get("worktree") == w["worktree"] and o["status"] != "running":
                    o["status"], o["result"] = "merged", w["result"]
            log("worker", f"{w['id']}: merged by its requester", w["result"])


def run_workers(ready: list, tok: str, now: float, only: str = "") -> dict:
    """The worker part of a round: check the running ones, then start one per ready task this pc may run; `only`
    (project/id): just that task (a remote "start worker")."""
    with LOCK:
        ws = load("workers.json", {})
    s, out = settings(), {"started": [], "waiting": {}, "limitWait": []}
    info = {a: usage_info(a, ws, now) for a in AGENTS if installed(a)}
    lim = {a: (i[0] if i else None) for a, i in info.items()}
    check_workers(ws, tok, now, lim)
    game = workers.game_session()
    for t in ready:
        key = f"{t['project']}/{t['id']}"
        if t.get("agent") not in AGENTS or not installed(t["agent"]) or only and key != only:
            continue
        if ws.get(key, {}).get("status") == "running":
            continue
        prev = ws.get(key, {})
        # a worker that ended without taking the task off the list failed or stopped: no new one every minute (the
        # owner can start one again from the Machines list)
        why = (f"A worker for it ended {int((now - prev['ended']) // 60)} min ago ({prev['status']}); not started again "
               f"for {RESTART_AFTER // 3600} h unless asked" if prev.get("ended") and now - prev["ended"] < RESTART_AFTER and not only
               else why_not(t, ws, s, lim, game))
        if not why:
            try:
                rec, why = start_worker(t, tok, now, ws)
            except Exception as e:
                rec, why = None, str(e)[:200]
                log("error", f"{AGENTS[t['agent']][0]}: no worker for {t['id']}", why)
            if rec:
                ws[key] = rec
                out["started"].append({"id": t["id"], "project": t["project"], "agent": t["agent"]})
                log("worker", f"{AGENTS[t['agent']][0]}: started a worker for {t['id']}", rec["open"])
                continue
        out["waiting"][key] = why
        if why.startswith("waiting for limit"):
            out["limitWait"].append(t["id"])
    ws = {k: w for k, w in ws.items() if w["status"] == "running" or now - w.get("ended", now) < 30 * 86400}
    with LOCK:
        save("workers.json", ws)
    out["limits"] = lim
    out["resets"] = {a: i[2] for a, i in info.items() if i and isinstance(i[2], (int, float))}
    return out


# ---------------------------------------------------------------- git: the board's pill and its diff (read-only)

GIT_RO = {"GIT_OPTIONAL_LOCKS": "0"}  # status and diff never take the index lock or rewrite the index
DIFF_CHARS = 200_000


def git_base(path: str, branch: str) -> str:
    """What a folder is compared with: the repo's main (or master) branch, or on that branch itself its upstream."""
    run = lambda *a: workers.git(path, *a, env=GIT_RO, timeout=10)
    for b in ("main", "master"):
        if b != branch and run("rev-parse", "-q", "--verify", f"refs/heads/{b}").returncode == 0:
            return b
    up = run("rev-parse", "--abbrev-ref", "-q", "@{upstream}")
    return up.stdout.strip() if up.returncode == 0 else ""


def git_state(path) -> dict | None:
    """{branch, base, changed, ahead, behind} of a chat folder or worker worktree; None outside git."""
    if not path or not shutil.which("git") or not Path(path).is_dir():
        return None
    run = lambda *a: workers.git(path, *a, env=GIT_RO, timeout=10)
    try:
        if run("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            return None
        branch = run("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "detached"
        # "-- .": this folder only, also when it sits inside a bigger repo (a home folder kept in git)
        changed = sum(1 for ln in run("status", "--porcelain", "--", ".").stdout.splitlines() if ln.strip())
        base, ahead, behind = git_base(path, branch), 0, 0
        if base:
            n = run("rev-list", "--left-right", "--count", f"{base}...HEAD").stdout.split()
            behind, ahead = (int(n[0]), int(n[1])) if len(n) == 2 else (0, 0)
        return {"branch": branch, "base": base, "changed": changed, "ahead": ahead, "behind": behind}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def git_diff(path: str) -> str:
    """What changed in a folder against where it started: its commits ahead of the base plus the uncommitted edits
    (git diff <merge-base>), the stat first, untracked files by name; capped."""
    st = git_state(path)
    if not st:
        raise RuntimeError("not a git checkout")
    run = lambda *a: workers.git(path, *a, env=GIT_RO, timeout=30)
    frm = run("merge-base", st["base"], "HEAD").stdout.strip() if st["base"] else ""
    frm = frm or ("HEAD" if run("rev-parse", "-q", "--verify", "HEAD").returncode == 0 else "")
    opts = ("--no-color", "--no-ext-diff", "--no-textconv")
    stat = run("diff", "--stat", *opts, frm, "--", ".").stdout if frm else ""
    new = [f for f in run("ls-files", "--others", "--exclude-standard", "--", ".").stdout.splitlines() if f]
    text = (f"{st['branch']}" + (f" against {st['base']}: {st['ahead']} ahead, {st['behind']} behind" if st["base"] else "")
            + f", {st['changed']} changed\n\n{stat}" + ("".join(f" ?? {f}\n" for f in new[:200])) + "\n")
    left = DIFF_CHARS - len(text)
    if frm and left > 0:
        text += git_head(path, left + 1, "diff", *opts, frm, "--", ".")
    return text if len(text) <= DIFF_CHARS else text[:DIFF_CHARS] + f"\n... cut: the diff is longer than {DIFF_CHARS} characters"


def git_head(path: str, limit: int, *args) -> str:
    """At most `limit` characters of a git command's output, read as it comes: a huge diff is never read whole."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.Popen(["git", "-C", str(path), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         env={**base, **GIT_RO}, text=True, encoding="utf-8", errors="replace")
    try:
        return p.stdout.read(limit)
    finally:
        p.kill()
        p.wait(timeout=10)


def with_git(folders: dict, ws: dict, now: float) -> tuple:
    """The linked folders and the recent workers with their git numbers, each folder once per round."""
    seen = {}
    once = lambda p: seen[p] if p in seen else seen.setdefault(p, git_state(p))
    linked = [{**f, "git": once(f["path"])} for f in folders["linked"][:40]] + folders["linked"][40:]
    recent = lambda w: w["status"] == "running" or now - (w.get("ended") or now) < 7 * 86400
    return {**folders, "linked": linked}, {k: once(w.get("worktree")) if recent(w) else None for k, w in ws.items()}


# a labelled value (password: x, token=x, Bearer x), `sshpass -p x`, the password in https://user:x@host, and keys with a
# known prefix (sk-, ghp_, xoxb-); hashes and file names stay (a commit id is no secret). The server masks its known
# secret values as well.
SECRETISH = re.compile(r"((?:password|passwd|token|api[-_ ]?key|secret)[\"']?\s*[:=]\s*[\"']?|(?:bearer|authorization:)\s+|sshpass\s+-p\s*"
                       r"|://[^/\s:@]+:)[^\s\"',}@]+"
                       r"|(?<![\w-])(?:sk|ghp|gho|ghs|ghu|github_pat|xox[abpr])[-_][A-Za-z0-9_-]{16,}", re.I)
TAIL_LINES, TAIL_WIDTH = 3, 200


def mask(text: str) -> str:
    return SECRETISH.sub(lambda m: (m.group(1) or "") + "‹masked›", text)


def event_text(line: str) -> str:
    """The words of one JSON event line (Claude's stream-json, Codex --json): its last text, result or message string;
    "" for an event without words (a tool call's bookkeeping). Any other line as it is."""
    try:
        ev = json.loads(line)
    except ValueError:
        return line
    found = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                found.append(v) if k in ("text", "result", "message") and isinstance(v, str) else walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(ev)
    return found[-1] if found else ""


def log_tail(path) -> list:
    """The last lines with words of a worker's log, secrets masked, each cut to TAIL_WIDTH; at most 16 kB read."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - 16_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return []
    out = []
    for line in reversed(lines):
        if text := " ".join(event_text(line.strip()).split()):
            out.append(mask(text)[:TAIL_WIDTH])
            if len(out) == TAIL_LINES:
                break
    return out[::-1]


def progress(w: dict, g) -> dict:
    """What a running worker did so far, for its board card: the subject of its newest commit (when its branch has one
    of its own), its commits and changed files (the git numbers), the last lines of its log."""
    commit = ""
    if g and g.get("ahead") and w.get("worktree"):
        try:
            commit = workers.git(w["worktree"], "log", "-1", "--format=%s", env=GIT_RO, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {"commit": mask(commit)[:TAIL_WIDTH], "commits": (g or {}).get("ahead", 0), "changed": (g or {}).get("changed", 0),
            "log": log_tail(w.get("log"))}


def review(w: dict, now: float):
    """For the review card of an ended worker (last 7 days, not merged): its branch against the branch its worktree
    was made from: commits ahead, changed files, the newest commit's subject. None without a recorded base branch."""
    b, into = w.get("branch"), w.get("base_branch")
    if not b or not into or w["status"] in ("running", "merged") or now - (w.get("ended") or now) > 7 * 86400:
        return None
    run = lambda *a: workers.git(w["repo"], *a, env=GIT_RO, timeout=10)
    try:
        n = run("rev-list", "--count", f"refs/heads/{into}..refs/heads/{b}")
        if n.returncode:
            return None
        files = run("diff", "--name-only", "--no-color", f"refs/heads/{into}...refs/heads/{b}").stdout.splitlines()
        return {"into": into, "ahead": int(n.stdout.strip() or 0), "files": files[:15], "nfiles": len(files),
                "commit": mask(run("log", "-1", "--format=%s", f"refs/heads/{b}").stdout.strip())[:TAIL_WIDTH],
                "blocked": workers.merge_block(w)}  # why Merge may not run here: the card says so instead of offering it
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def report(tok: str, ws: dict, lim: dict, wait: list, now: float, waiting=None, resets=None):
    """The machine's state for the Machines list: settings, game, per agent YOLO, hooks and limit, the workers, and
    why each ready task waits here (the Tasks tab shows it)."""
    yolo, folders, s = load("yolo.json", {}), sorted(project_folders().values()), settings()
    links, gits = with_git(folder_links(), ws, now)
    body = {"version": version(), "workersAllowed": s["workersAllowed"], "maxWorkers": s["maxWorkers"], "game": workers.game_session(),
            "live": list(chat.LIVE_AGENTS),  # agents whose chats run live: a message while they work goes in at once
            "agents": {a: {"yolo": bool(yolo.get(a, {}).get("on")), "trusted": not untrusted(a, folders), "limit": lim.get(a),
                     "resets": (resets or {}).get(a), **({"plugin": v} if (v := plugin_version(a)) else {}),  # known before a session
                     "models": chat.models(a)}  # what a chat may pick (cached for hours)
                       for a in AGENTS if installed(a)},
            "workers": [{**{k: w.get(k) for k in ("id", "project", "agent", "status", "started", "ended", "branch", "yolo", "open", "why", "chat")},
                         "checkpoint": bool(w.get("checkpoint")), "merged": w["status"] == "merged", "git": gits.get(k),
                         **({"progress": progress(w, gits.get(k))} if w["status"] == "running" else {}),
                         **({"review": r} if (r := review(w, now)) else {})} for k, w in sorted(ws.items(), key=lambda kw: -(kw[1].get("started") or 0))],  # newest first: the server keeps 40
            "limitWait": wait, "waiting": waiting or {}, "folders": links, "projects": load("state.json", {}).get("projects", [])}
    server("POST", "/machine/report", tok, body)


def tick() -> dict:
    """One round: queued YOLO, trust after an update, the ready tasks, the agents' own hook errors."""
    with TICK:
        return one_round()


def one_round(only: str = "") -> dict:
    now, tok = time.time(), token_store.token()
    try:
        pending_yolo()
    except Exception as e:
        log("error", "Could not apply the queued YOLO setting of agy", str(e)[:300])
    keep_trusted()
    with LOCK:
        st = load("state.json", {})
    try:
        ready = server("GET", "/tasks/ready", tok)["tasks"]
        if st.get("sync_error"):
            log("sync", "The knowledge server answers again")
        st.update(synced=now, sync_error="", ready=ready, projects=server("GET", "/machine/projects", tok).get("projects", []))
    except Exception as e:
        if not st.get("sync_error"):
            log("error", "Could not reach the knowledge server", str(e)[:200])
        st["sync_error"], ready = str(e)[:200], []
    summary = run_workers(ready, tok, now, only)
    for key, why in summary["waiting"].items():
        if why != st.get("waiting", {}).get(key) and not why.startswith("Workers are off"):  # once, not every minute
            log("wait", f"{key.split('/', 1)[1]}: {why}", "")
    st["waiting"] = summary["waiting"]
    report_errors(st, tok, now)
    try:
        report(tok, load("workers.json", {}), summary.pop("limits"), summary.pop("limitWait"), now, summary["waiting"], summary.pop("resets"))
    except Exception:  # the next round reports again; the server away is said above
        pass
    with LOCK:
        save("state.json", {**load("state.json", {}), **{k: v for k, v in st.items() if k not in TRUST_KEYS}})  # trust() may have written meanwhile
    return summary


# ---------------------------------------------------------------- remote actions (the Machines list)

def run_action(a: dict) -> str:
    """One action the owner queued in the web UI; only these, each with the companion's own code."""
    act, args = a.get("action"), a.get("args") or {}
    agent = args.get("agent")
    if agent is not None and agent not in AGENTS:
        raise ValueError(f"unknown agent {agent}")
    if act == "start_worker":
        with TICK:
            out = one_round(only=f"{args['project']}/{args['id']}")
        if out["started"]:
            return f"started a worker for {args['id']}"
        raise RuntimeError(out["waiting"].get(f"{args['project']}/{args['id']}") or f"{args['id']} is not ready for this pc")
    if act == "stop_worker":
        with LOCK:
            w = load("workers.json", {}).get(f"{args.get('project')}/{args['id']}")
        w = w if w and w["status"] == "running" else None
        if not w:
            raise RuntimeError(f"no running worker for {args['id']}")
        with TICK, LOCK:  # between rounds: a round holds its own copy of workers.json and saves it back
            ws = load("workers.json", {})
            ws[f"{w['project']}/{w['id']}"]["owner_stopped"] = True
            save("workers.json", ws)
        workers.kill(w)
        return (f"stopped the worker for {args['id']}; this pc keeps its lease until it runs out, then another pc may start the line again:"
                " change it to 'start after approval' or remove it to stop it for good")
    if act == "takeover":
        return take_over(args, token_store.token())
    if act == "rollback":
        with LOCK:
            ws = load("workers.json", {})
        w = ws.get(f"{args.get('project')}/{args['id']}")
        if not w or w["status"] == "running" or not w.get("branch"):
            raise RuntimeError(f"no finished worker with a worktree for {args['id']}")
        if shared_running(ws, w):
            raise RuntimeError(f"another worker runs in the worktree of {args['id']} now; stop it first")
        if later := [o["id"] for o in ws.values() if o is not w and o.get("worktree") == w["worktree"] and o.get("started", 0) > w.get("started", 0)]:
            raise RuntimeError(f"{', '.join(later)} went on in the worktree of {args['id']} after it: roll back {later[-1]} instead")
        return workers.rollback(w["cwd"], w["id"])
    if act == "merge":  # the review card's Merge: into the branch the worktree was made from, in the main checkout
        with TICK, LOCK:  # between rounds: a round's merged() cleanup must not remove the worktree halfway
            ws = load("workers.json", {})
            w = ws.get(f"{args.get('project')}/{args['id']}")
            if not w or w["status"] in ("running", "merged") or not w.get("branch"):
                raise RuntimeError(f"no finished worker with a branch for {args['id']}")
            if shared_running(ws, w):
                raise RuntimeError(f"another worker runs in the worktree of {args['id']} now; stop it first")
            return workers.merge(w)
    if act == "trust_hooks":
        return trust(agent)
    if act == "link_folder":
        return link_folder(str(args.get("path", "")), str(args.get("project", "")))
    if act == "unlink_folder":
        return unlink_folder(str(args.get("path", "")))
    if act == "update_plugin":
        update_plugins()
        return "updated"
    if act == "yolo":
        return f"YOLO {set_yolo(agent, args.get('on') is True)}"
    if act == "workers_allowed":
        save_settings({"workersAllowed": args.get("on") is True})
        return f"workers {'allowed' if args.get('on') is True else 'off'}"
    if act == "max_workers":
        save_settings({"maxWorkers": {agent: args["n"]}})
        return f"at most {args['n']} {AGENTS[agent][0]} worker(s)"
    raise ValueError(f"unknown action {act}")


def actions_round():
    """Fetch the waiting actions for this machine, run each, report how it went; all in the activity log."""
    tok, changed = token_store.token(), False
    # a long poll: the server answers as soon as an action waits, or after ACTIONS_WAIT s with none
    got = server("GET", f"/machine/actions?wait={ACTIONS_WAIT:g}", tok, timeout=ACTIONS_WAIT + 15)
    for a in got.get("actions", []):
        changed = changed or a.get("action") in STATE_ACTIONS
        log("remote", f"Remote action {a.get('action')} from {a.get('by') or '?'}", json.dumps(a.get("args") or {})[:200])
        try:
            ok, result = True, run_action(a)
        except Exception as e:
            ok, result = False, f"{type(e).__name__}: {e}"
            log("error", f"Remote action {a.get('action')} failed", result[:300])
        try:
            server("POST", "/machine/actions/result", tok, {"id": a["id"], "ok": ok, "result": str(result)[:300]})
        except Exception as e:
            log("error", "Could not report a remote action's result", str(e)[:200])
    if changed:  # the page shows the new state at once, not at the next minute's report
        threading.Thread(target=tick, daemon=True).start()
    return "waited" in got  # a server before the long poll answers at once: then the loop waits itself


STATE_ACTIONS = {"yolo", "workers_allowed", "max_workers", "link_folder", "unlink_folder", "trust_hooks", "stop_worker", "restart", "merge", "takeover"}


# ---------------------------------------------------------------- the page

def own_errors(agent: str) -> list:
    out = []
    try:
        for ln in (KN_HOME / "hook-errors.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()[-100:]:
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
    st, yolo, ws = load("state.json", {}), load("yolo.json", {}), load("workers.json", {})
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
                       "lastError": last and {"at": last.get("at", 0), "text": f"{last.get('hook', '')}: {first_line(last.get('stderr'))}"},
                       "workers": [{k: w.get(k) for k in ("id", "project", "status", "started", "ended", "branch", "yolo", "open")}
                                   for w in ws.values() if w["agent"] == a]})
    # ready: may start here now; waiting: why not (also "workers are off"); stalled: workers that failed or were stopped
    lanes = {"ready": [], "waiting": [], "stalled": []}
    for t in st.get("ready", []):
        if t.get("agent") not in AGENTS or not installed(t["agent"]):
            continue
        key, w = f"{t['project']}/{t['id']}", ws.get(f"{t['project']}/{t['id']}")
        item = {"id": t["id"], "owner": t["agent"], "text": t["text"], "project": t["project"]}
        if w and w["status"] == "running":
            lanes["ready"].append({**item, "why": f"Running in a worker for {int((now - w['started']) // 60)} min"})
        elif key in st.get("waiting", {}):
            lanes["waiting"].append({**item, "why": st["waiting"][key]})
        else:
            lanes["ready"].append(item)
    lanes["stalled"] = [{"id": w["id"], "owner": w["agent"], "text": w.get("open", ""), "project": w["project"],
                         "why": f"Worker {w['status']} {int((now - w.get('ended', now)) // 60)} min ago" + (f": {w['why']}" if w.get("why") else "")}
                        for w in ws.values() if w["status"] in ("failed", "stopped")]
    tok = token_store.token()
    return {"status": {"host": socket.gethostname(), "live": sum(len(a["sessions"]) for a in agents),
                       "workers": sum(1 for w in ws.values() if w["status"] == "running"),
                       "stalled": len(lanes["stalled"]), "synced": st.get("synced"), "syncError": st.get("sync_error", "")},
            "agents": agents, "tasks": lanes, "activity": load("activity.json", [])[:50],
            "settings": {"serverUrl": token_store.url(), "token": {"present": bool(tok)}, **settings(), "game": workers.game_session()},
            "folders": folder_links(), "projects": st.get("projects", []),
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
            # the machine id lets a token act as this pc: only with the key; the web UI finds its row by the hash
            mid = token_store.machine().get("id", "")
            return self.send(200, {"companion": version(), "host": socket.gethostname(), "machineHash": hashlib.sha256(mid.encode()).hexdigest(),
                                   **({"machine": mid} if self.key_ok() else {})}, origin=o)
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
        # read the body before any answer, also a refusal: unread bytes make Windows reset the connection (10053)
        size = self.headers.get("Content-Length") or "0"
        raw = self.rfile.read(min(int(size), 65536)) if size.isdigit() else b""
        if size.isdigit() and int(size) > 65536:
            self.close_connection = True  # the rest stays unread: do not keep the connection for a next request
            return self.send(413, {"error": "request body too large"}, origin=o or "")
        if path == "/local/api/ask":  # a chat turn's permission prompt (approval_mcp.py): no browser, the key file only
            if not self.host_ok() or o != "" or not self.key_ok():
                return self.send(403, {"error": "not allowed"})
            try:
                b = json.loads(raw or b"{}")
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                decision, message = chat.ask(str(b.get("chat", "")), str(b.get("tool", "")), inp, str(inp.get("description") or "")[:300])
            except Exception as e:  # never an allow by accident
                decision, message = "deny", f"the companion could not ask: {type(e).__name__}: {e}"[:300]
            return self.send(200, {"decision": decision, "message": message})
        # a POST always carries Origin in a browser; JSON only, so a foreign page cannot send it without a preflight
        if not self.host_ok() or not o or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(403, {"error": "not allowed"})
        if not self.key_ok():
            return self.send(403, {"error": "Open the companion from its shortcut: its link carries the key this change needs"}, origin=o)
        try:
            body = json.loads(raw or b"{}")
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
                if body.get("on") and body.get("confirm") != "YOLO":  # as the web UI's remote action: typed, never one click
                    raise ValueError("YOLO on needs the word YOLO typed in the confirm dialog")
                return self.send(200, {"yolo": set_yolo(agent, bool(body.get("on")))}, origin=o)
            if path == "/local/api/settings":
                if o != SELF:  # whether this pc runs workers is for this pc's own page (or its owner, remotely)
                    return self.send(403, {"error": "Worker settings can only be changed on the companion page itself"}, origin=o)
                save_settings({k: body[k] for k in ("workersAllowed", "maxWorkers") if k in body})
                return self.send(200, {"ok": True, **settings()}, origin=o)
            if path == "/local/api/link":
                return self.send(200, {"result": link_folder(str(body.get("path", "")), str(body.get("project", "")))}, origin=o)
            if path == "/local/api/unlink":
                return self.send(200, {"result": unlink_folder(str(body.get("path", "")))}, origin=o)
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


ACTIONS_EVERY = float(os.environ.get("KNOWLEDGE_COMPANION_ACTIONS_EVERY") or 10)
ACTIONS_WAIT = float(os.environ.get("KNOWLEDGE_COMPANION_ACTIONS_WAIT") or 25)  # how long the server may hold the ask


def actions_loop():
    """Remote actions from the web UI, pulled with a long poll (the server never connects to this pc)."""
    failed = False
    while True:
        try:
            held = actions_round()
            failed = False
        except Exception as e:  # the server away: said once, the minute loop says it too
            if not failed:
                log("error", "Could not fetch remote actions", f"{type(e).__name__}: {e}"[:300])
            failed, held = True, False
        if not held:  # the server held nothing (old server, or away): do not ask again at once
            time.sleep(ACTIONS_EVERY)


def chats_round() -> bool:
    """The owner's chats with an agent here: stop what was stopped, call a turn the server thinks runs but this
    companion does not (it restarted) lost, start a turn per new message. True while a chat is active."""
    tok = token_store.token()
    post = lambda body: server("POST", "/machine/chats/events", tok, body)
    r = server("GET", "/machine/chats", tok)
    chat.ASK_SERVER, chat.LOG = (lambda body: server("POST", "/machine/chats/ask", tok, body)), log
    for a in r.get("answers", []):  # the owner's answers to tool approvals (before the stops: a stop denies the rest)
        chat.answer(a)
    for cid in r.get("stop", []):
        if not chat.stop(cid):
            post({"chat": cid, "events": [{"kind": "info", "text": "Stopped (no turn ran here any more)."}], "done": True})
    chat.reap()  # live chats idle for a while end (the next message resumes them)
    for s in r.get("steer", []):  # Force: for the running turn, read by the agent's hook after its next step
        if chat.steer(str(s.get("chat", "")), str(s.get("by") or ""), chat.attach(str(s.get("text", "")), lambda ref: server_file(tok, ref))):
            log("remote", f"Chat message sent into a running turn by {s.get('by') or '?'}", f"chat {s.get('chat')}")
    for cid in r.get("busy", []):
        if cid not in chat.RUNNING:
            post({"chat": cid, "events": [{"kind": "error", "text": "The turn was lost: the companion on this pc restarted."}], "done": True, "error": "lost"})
            log("error", "A chat turn was lost (the companion restarted)", f"chat {cid}")
    # a worker taken over goes on as a chat in its worktree
    linked = lambda: {f["path"] for f in folder_links()["linked"]} | {w["cwd"] for w in load("workers.json", {}).values() if w.get("chat") and w.get("cwd")}
    yolo = lambda agent: bool(load("yolo.json", {}).get(agent, {}).get("on"))
    for item in r.get("work", []):
        item["text"] = chat.attach(item.get("text", ""), lambda ref: server_file(tok, ref))
        log("remote", f"Chat message from {item.get('by') or '?'} for {AGENTS.get(item.get('agent'), ('?',))[0]}", f"chat {item.get('chat')} in {item.get('folder')}")
        chat.start(item, post, log, linked, yolo)
    for d in r.get("diffs", []):  # after the turns (handed out once: never lost to a diff); read-only, linked folders and workers only
        try:
            w = load("workers.json", {}).get(d.get("worker", ""), {})
            path = d["folder"] if d.get("folder") in linked() else w.get("worktree") if d.get("worker") else None
            if not path:
                raise RuntimeError("not a folder linked on this pc, nor a worker here")
            answer = {"id": d["id"], "text": git_diff(path)}
        except Exception as e:
            answer = {"id": d.get("id"), "error": f"{e}"[:300]}
        try:
            server("POST", "/machine/chats/diff", tok, answer)
        except Exception as e:
            log("error", "Could not send a diff to the web UI", f"{type(e).__name__}: {e}"[:300])
    follow_round(tok, r)
    return bool(r.get("active") or chat.RUNNING)


# ---------------------------------------------------------------- follow and steer a worker from the web UI

FOLLOW_READ, FOLLOW_TAIL = 256_000, 200  # bytes of a worker's log read per send; events sent once when an unwatched worker ends
FOLLOW_LOCK = threading.Lock()  # the chat poll (followed workers) and the minute round (ended ones) share follow.json
FOLLOW_FAILED: set = set()  # workers whose send failed: said once in the activity log


def worker_events(w: dict, frm: int, final: bool) -> tuple:
    """(events, next offset): the chat events in a worker's log from byte `frm` on, complete lines only (at its end all
    of it), parsed as the chats parse each agent's stream; agy -p prints plain text, its lines are its answer. More
    than 4 reads behind (or at the end): only the newest part, said so. Secrets masked."""
    try:
        with open(w["log"], "rb") as fh:
            size = fh.seek(0, 2)
            frm = 0 if frm > size else frm  # a new log
            start = max(frm, size - FOLLOW_READ) if final or size - frm > 4 * FOLLOW_READ else frm
            fh.seek(start)
            data = fh.read(FOLLOW_READ)
    except (OSError, KeyError, TypeError):
        return [], frm
    end = len(data) if final and start + len(data) >= size else data.rfind(b"\n") + 1
    if not end and len(data) == FOLLOW_READ:  # one line longer than a read: sent cut, never stuck on it
        end = len(data)
    lines = data[:end].decode("utf-8", "replace").splitlines()
    if start > frm:
        lines = lines[1:]  # the read began inside a line
    # parse state per log, across reads: a subagent's hand-back in a later read still closes its block
    st = PARSE_STATE.setdefault(str(w.get("log") or ""), {})
    out = [("info", "Earlier output skipped: only the newest part is shown.")] if start > frm else []
    for line in lines:
        if w["agent"] == "agy" and not line.lstrip().startswith("{"):
            out += [("text", line + "\n")] if line.strip() else []
        else:
            out += chat.parse(w["agent"], line, st)
    return [(k, mask(t), *rest) for k, t, *rest in out], start + end  # a subagent's id and state stay


PARSE_STATE: dict = {}  # worker log -> chat.parse state (the subagents it opened)


def send_worker(tok: str, key: str, w: dict, final: bool = False, extra: list = ()):
    """Send what the worker printed since the last send (followed), or its last FOLLOW_TAIL events once when it ended;
    `extra` events go first. Remembers how far it got per run in follow.json."""
    with FOLLOW_LOCK:
        sent = load("follow.json", {})
        s = sent.get(key) if (sent.get(key) or {}).get("started") == w.get("started") else {}
        if final and s.get("final"):
            return
        evs, off = worker_events(w, s.get("offset", 0), final)
        if s.get("steer") and not workers.steer_path(w).exists():  # the watch hook took it
            evs, s["steer"] = [("info", "Delivered: the worker read your message after its last step.")] + evs, False
        if final and workers.steer_path(w).exists():
            workers.steer_path(w).unlink(missing_ok=True)
            evs.append(("info", "Not delivered: the worker ended before its next step. Take it over to go on as a chat."))
        evs = chat.merged([*extra, *evs])
        evs = evs[-FOLLOW_TAIL:] if final else evs
        parts = [evs[i:i + 400] for i in range(0, len(evs), 400)] or ([[]] if final else [])  # the server takes 500 at a time
        for i, part in enumerate(parts):
            server("POST", "/machine/chats/worker", tok, {"worker": key, "started": w.get("started"), "agent": w["agent"],
                                                         "status": w["status"], "events": part, "done": final and i == len(parts) - 1})
            if i == 0 and len(parts) > 1:  # a failure after the first part must not send it again: the rest is skipped
                sent[key] = {**s, "started": w.get("started"), "offset": off}
                save("follow.json", sent)
        sent.pop(key, None)
        sent[key] = {**s, "started": w.get("started"), "offset": off, "final": final}
        save("follow.json", dict(list(sent.items())[-100:]))


def send_final(tok: str, w: dict):
    """An ended worker's last events, once; a server without the route (404) is skipped quietly."""
    try:
        send_worker(tok, f"{w['project']}/{w['id']}", w, final=True)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log("error", f"Could not send the last output of the worker for {w['id']}", f"{e}"[:200])
    except Exception as e:
        log("error", f"Could not send the last output of the worker for {w['id']}", f"{type(e).__name__}: {e}"[:200])


def steer(tok: str, ws: dict, item: dict):
    """The owner's message for a running worker: a note its watch hook hands the agent after its next tool call (the
    way the 90% hand-over reaches it), so it carries on in its own session, worktree and permissions."""
    key, w = str(item.get("worker", "")), ws.get(str(item.get("worker", "")))
    if not w or w["status"] != "running" or not workers.running(w):
        if w:
            send_worker(tok, key, w, extra=[("info", "Not delivered: the worker does not run on this pc any more.")])
        return
    workers.steer_note(w, str(item.get("by") or "the owner"), str(item.get("text", "")))
    with FOLLOW_LOCK:
        sent = load("follow.json", {})
        if (sent.get(key) or {}).get("started") != w.get("started"):
            sent[key] = {"started": w.get("started"), "offset": 0}
        sent[key]["steer"] = True
        save("follow.json", sent)
    log("remote", f"Message from {item.get('by') or '?'} for the worker of {w['id']}", "handed over after its next tool call")


def follow_round(tok: str, r: dict):
    """The poll's part for workers: steer messages, then new output of the followed ones."""
    if not (r.get("steer") or r.get("follow")):
        return
    with LOCK:
        ws = load("workers.json", {})
    for item in r.get("steer", []):
        try:
            steer(tok, ws, item)
        except Exception as e:
            log("error", "Could not hand a message to a worker", f"{type(e).__name__}: {e}"[:300])
            if (w := ws.get(item.get("worker"))):  # the owner sees it in the transcript, not only on this pc
                with contextlib.suppress(Exception):
                    server("POST", "/machine/chats/worker", tok, {"worker": item["worker"], "started": w.get("started"), "agent": w["agent"],
                                                                 "status": w["status"], "events": [{"kind": "error", "text": f"Not delivered: {e}"[:300]}]})
    for key in r.get("follow", []):
        if key not in ws:
            continue
        try:
            send_worker(tok, key, ws[key])
            FOLLOW_FAILED.discard(key)
        except Exception as e:
            if key not in FOLLOW_FAILED:
                log("error", f"Could not send the output of the worker for {key}", f"{type(e).__name__}: {e}"[:300])
            FOLLOW_FAILED.add(key)


def worker_session_id(w: dict) -> str:
    """The agent's own session id of a worker: from its log (Claude's init, Codex's thread.started), else what its
    watch hook noted (agy -p prints none)."""
    st = {}
    try:
        with open(w["log"], "rb") as fh:
            for raw in fh.read(256_000).splitlines():
                chat.parse(w["agent"], raw.decode("utf-8", "replace"), st)
                if st.get("session"):
                    return st["session"]
    except (OSError, KeyError, TypeError):
        pass
    try:
        return workers.session_path(w).read_text(encoding="utf-8").strip()[:120]
    except OSError:
        return ""


def take_over(args: dict, tok: str) -> str:
    """The owner takes a worker over: it is stopped (its lease kept, as Stop) and its conversation becomes a chat on
    this pc in its worktree, which resumes the worker's own session; the server claims the line for the owner."""
    key = f"{args.get('project')}/{args['id']}"
    with TICK:  # between rounds: a round must not count it failed, nor save its own copy of workers.json over this
        with LOCK:
            ws = load("workers.json", {})
            w = ws.get(key)
            if not w or w["status"] == "merged" or w.get("chat"):
                raise RuntimeError(f"no worker for {args['id']} to take over here" + (" (taken over already)" if w and w.get("chat") else ""))
            if w["status"] == "running":
                w["owner_stopped"] = True
                save("workers.json", ws)
        if w["status"] == "running":
            for hard in (False, True):
                workers.kill(w, hard=hard)
                for _ in range(20):  # its session file is complete once it has ended
                    if (p := workers.PROCS.get(w["pid"])) is not None:
                        p.poll()  # our own child: reaped, so macOS's ps no longer lists it as a zombie
                    if not workers.running(w):
                        break
                    time.sleep(0.5)
            if workers.running(w):  # never two agents in one session and worktree
                raise RuntimeError("the worker did not stop; nothing was taken over")
        session = worker_session_id(w)
        if not session:
            raise RuntimeError("its session id is unknown (it made no step yet); it was stopped: start it again or chat in its folder")
        r = server("POST", "/machine/chats/adopt", tok, {"worker": key, "started": w.get("started"), "agent": w["agent"], "folder": w["cwd"], "session": session})
        with LOCK:
            ws = load("workers.json", {})
            if key in ws:
                ws[key]["chat"] = r["chat"]
                save("workers.json", ws)
    return f"taken over as chat {r['chat']} in {w['cwd']}"


def chats_loop():
    """Chats from the web UI, pulled like the actions: every ~2 s while one is active, else every ~10 s."""
    failed = False
    while True:
        active = False
        try:
            active = chats_round()
            failed = False
        except urllib.error.HTTPError as e:
            if e.code != 404 and not failed:  # 404: a server older than chats
                log("error", "Could not fetch chat messages", f"{e}"[:300])
            failed = True
        except Exception as e:
            if not failed:
                log("error", "Could not fetch chat messages", f"{type(e).__name__}: {e}"[:300])
            failed = True
        time.sleep(min(2.0, ACTIONS_EVERY) if active else ACTIONS_EVERY)


class LocalServer(ThreadingHTTPServer):
    """One companion per pc: its port is the lock. On Windows SO_REUSEADDR would let a second one bind the same port
    (and both start the same tasks), so there the port is taken exclusively instead."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve():
    token_store.machine()  # this pc's id, made once; the plugin hooks make it too when the companion never ran
    api_key()  # made now: a chat turn's permission prompt (approval_mcp.py) reads it before any page opened
    try:
        httpd = LocalServer(("127.0.0.1", PORT), Handler)  # never another interface: the API changes agent settings
    except OSError as e:  # loudly, with a failing exit: the hooks' wrapper would otherwise call it skipped and exit 0
        sys.exit(f"knowledge companion: {SELF} is taken, so another companion (or program) runs here: {e}")
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=actions_loop, daemon=True).start()
    threading.Thread(target=chats_loop, daemon=True).start()
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
        tmp_state = tempfile.mkdtemp()  # the checks log and save: never into this pc's real activity log
        atexit.register(shutil.rmtree, tmp_state, True)
        globals()["STATE"] = Path(tmp_state)
        live = {"v": (42.0, "7d", 2000.0)}  # the last reading lasts until its window resets
        real_live, globals()["usage_live"] = usage_live, lambda a, ws, now: live["v"]
        assert usage_info("agy", {}, 1000) == (42.0, "7d", 2000.0)
        live["v"] = None
        assert usage_info("agy", {}, 1500) == (42.0, "7d", 2000.0) and usage_info("agy", {}, 2500) is None
        globals()["usage_live"] = real_live
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
        assert no_window(0) == CREATE_NO_WINDOW and no_window(0x200) == 0x200 | CREATE_NO_WINDOW and no_window(DETACHED_PROCESS) == DETACHED_PROCESS
        assert ours({"pluginId": "knowledge@knowledge"}) and not ours({"pluginId": "other@x", "command": "x.sh"})
        globals()["CLAUDE_JSON"] = HOME / ".knowledge-companion-self-test-missing.json"
        assert untrusted("claude", ["/x"]) == [], "Claude never ran here: nothing to trust, no trust round every minute"
        # Whether this pc may start a worker: off by default, game, limit (unknown: one), max, a linked folder.
        g, task, workers_mod = globals(), {"agent": "codex", "project": "p", "id": "T-1"}, workers
        g.update(project_folders=lambda: {"p": "/x"})
        on = {"workersAllowed": True, "maxWorkers": {"claude": 1, "codex": 2, "agy": 1}}
        run = {"k": {"agent": "codex", "status": "running"}}
        assert why_not(task, {}, {**on, "workersAllowed": False}, {}, "").startswith("Workers are off")
        assert mask("export API_KEY=gategategate and token: gategate") == "export API_KEY=‹masked› and token: ‹masked›", mask("export API_KEY=gategategate and token: gategate")
        assert mask("sk-" + "a1" * 20) == "‹masked›" and mask("ran the tests: 12 passed") == "ran the tests: 12 passed"
        assert mask("sshpass -p gategate ssh pi@h") == "sshpass -p ‹masked› ssh pi@h", mask("sshpass -p gategate ssh pi@h")
        assert mask("git clone https://bob:gategate@host/x") == "git clone https://bob:‹masked›@host/x", mask("git clone https://bob:gategate@host/x")
        keep = "commit " + "0123456789abcdef" * 2 + "01234567 by secret-guard.py, see ab12" * 1
        assert mask(keep) == keep, mask(keep)
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as f:
            f.write("\n".join(["old line", json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading   the memo"}]}}),
                                json.dumps({"type": "system", "subtype": "hook"}), "plain " + "word " * 80,
                                json.dumps({"type": "result", "result": "done, password=hunter2hunter2"})]) + "\n")
        tail = log_tail(f.name)
        os.unlink(f.name)
        assert tail == ["Reading the memo", ("plain" + " word" * 80)[:TAIL_WIDTH], "done, password=‹masked›"], tail
        assert log_tail("/nonexistent/x.log") == [] and log_tail(None) == []
        assert why_not(task, {}, on, {}, "a Wolf game session runs (1)").startswith("No workers while a Wolf")
        assert why_not(task, {}, on, {"codex": 90.0}, "").startswith("waiting for limit")
        old = {"k": {"agent": "claude", "status": "running", "project": "p", "id": "T-1"}}
        assert why_not({**task, "id": "T-1-cont"}, old, on, {"codex": 10.0}, "").startswith("T-1 still runs"), "a hand-over waits for the old worker"
        assert why_not(task, run, on, {"codex": None}, "").endswith("(limit unknown: 1)"), "unknown limit: one worker at most"
        assert why_not(task, run, on, {"codex": 40.0}, "") == "", "known limit: up to its max"
        assert why_not({**task, "project": "q"}, {}, on, {"codex": 10.0}, "").startswith("No folder linked")
        DENIED.add("/Users/x/Documents/app")
        assert "Full Disk Access" in why_not({**task, "project": "q"}, {}, on, {"codex": 10.0}, ""), "a folder macOS keeps from it is named"
        DENIED.clear()
        # Two writers at once (threads here, a hook process logging): each through its own temp file, never a torn file.
        with tempfile.TemporaryDirectory() as tmp:
            f, errs = Path(tmp) / "x.json", []

            def writer(n):
                for _ in range(100):
                    try:
                        write_json(f, {"n": n, "pad": "x" * 200_000})
                    except Exception as e:
                        errs.append(e)
            ts = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            assert not errs and json.loads(f.read_text(encoding="utf-8"))["n"] in (0, 1, 2) and len(list(Path(tmp).iterdir())) == 1, errs[:1]
        # A merged branch counts for every session that worked in its worktree (a hand-over shares it).
        gone = []  # the branch is deleted with the first record's worktree: merged() of the other finds no branch
        g["workers"] = type("W", (), {"merged": staticmethod(lambda w: not gone), "remove": staticmethod(lambda w: gone.append(1) or "removed"), "PROCS": {}})
        pair = {"a": {"id": "M-1", "agent": "codex", "status": "finished", "worktree": "/wt"},
                "b": {"id": "M-1-cont", "agent": "codex", "status": "finished", "worktree": "/wt"}}
        check_workers(pair, "", 1000, {})
        assert [w["status"] for w in pair.values()] == ["merged", "merged"], pair
        g["workers"] = workers_mod
        # A Claude worker's reading counts until its window resets, also when its log is older than 15 minutes.
        with tempfile.TemporaryDirectory() as tmp:
            f, now = Path(tmp) / "w.log", time.time()
            f.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
                "five_hour": {"utilization": 0.93, "resetsAt": now + 4 * 3600}}}}) + "\n")
            os.utime(f, (now - 3600, now - 3600))
            real, agent_hooks.limits.claude_used = agent_hooks.limits.claude_used, lambda now: None
            try:
                assert usage("claude", {"k": {"agent": "claude", "log": str(f)}}, now) == 93.0
            finally:
                agent_hooks.limits.claude_used = real
        # A failed start gives the task back at once.
        did = []
        g.update(claim=lambda t, tok, s, start="": did.append(("claim", s)) or "", release=lambda t, tok: did.append(("release",)))
        g["workers"] = type("W", (), {"worktree": staticmethod(lambda f, i, p: (_ for _ in ()).throw(OSError("no git"))), "start": None})
        try:
            start_worker(task, "", 1000)
            raise AssertionError("a failed start must raise")
        except RuntimeError:
            pass
        assert did[0][0] == "claim" and did[0][1].startswith("worker:") and did[1:] == [("release",)], did
        # A hand-over in the first session's worktree keeps that session's base; only the newest session rolls back.
        g["workers"] = type("W", (), {"worktree": staticmethod(lambda f, i, p: {"cwd": "/wt", "worktree": "/wt", "branch": "task/T-1", "base": "Y"}),
                                      "start": staticmethod(lambda a, t, m, where, y, n: {"id": t["id"], **where})})
        first = {"id": "T-1", "worktree": "/wt", "base": "X", "started": 1, "status": "finished", "branch": "task/T-1", "cwd": "/wt"}
        rec, _ = start_worker({**task, "id": "T-1-cont"}, "", 1000, {"p/T-1": first})
        assert rec["base"] == "X", rec
        with tempfile.TemporaryDirectory() as tmp:
            g["STATE"] = Path(tmp)
            save("workers.json", {"p/T-1": first, "p/T-1-cont": {**rec, "started": 2, "status": "finished"}})
            try:
                run_action({"action": "rollback", "args": {"project": "p", "id": "T-1"}})
                raise AssertionError("an older session's rollback would drop the hand-over's commits")
            except RuntimeError as e:
                assert "roll back T-1-cont instead" in str(e), e
        # The board's git pill: a branch 2 commits ahead of main, 3 changed files; the diff shows commits and edits.
        globals()["workers"] = workers_mod
        if shutil.which("git"):
            with tempfile.TemporaryDirectory() as tmp:
                g = lambda *a: workers.git(tmp, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a, check=True)
                g("init", "-q", "-b", "main")
                (Path(tmp) / "a.txt").write_text("one\n")
                g("add", "a.txt")
                g("commit", "-qm", "one")
                g("checkout", "-qb", "task/KN-12")
                for n in ("b", "c"):
                    (Path(tmp) / f"{n}.txt").write_text(n + "\n")
                    g("add", f"{n}.txt")
                    g("commit", "-qm", n)
                (Path(tmp) / "a.txt").write_text("two\n")
                (Path(tmp) / "new1.txt").write_text("x")
                (Path(tmp) / "new2.txt").write_text("y")
                assert git_state(tmp) == {"branch": "task/KN-12", "base": "main", "changed": 3, "ahead": 2, "behind": 0}, git_state(tmp)
                d = git_diff(tmp)
                assert "2 ahead, 0 behind" in d and "+two" in d and "+b" in d and "?? new1.txt" in d, d
                assert git_state(Path(tmp) / "nope") is None
                # A folder linked inside a bigger repo (a home folder kept in git): its own files only.
                (Path(tmp) / "sub").mkdir()
                (Path(tmp) / "sub" / "s.txt").write_text("s\n")
                g("add", "sub/s.txt")
                g("commit", "-qm", "sub")
                (Path(tmp) / "sub" / "s.txt").write_text("changed\n")
                sub = str(Path(tmp) / "sub")
                assert git_state(sub)["changed"] == 1, git_state(sub)
                d = git_diff(sub)
                assert "+changed" in d and "+two" not in d and "new1.txt" not in d and "b.txt" not in d, d
                assert len(git_head(tmp, 5, "log", "--format=%s")) == 5, "a capped read stops at its limit"
        # folder_links runs git only for a folder inside a checkout (its root found by git); others need no process.
        if shutil.which("git"):
            with tempfile.TemporaryDirectory() as tmp:
                h, calls, real = Path(tmp).resolve(), [], (HOME, known_folders, git_repos, subprocess.run)
                for d in ("repo/sub", "plain", "lk"):
                    (h / d).mkdir(parents=True)
                subprocess.run(["git", "init", "-q", str(h / "repo")], check=True)
                (h / "lk" / project_marker.MARKER).write_text("p\n")
                globals().update(HOME=h, known_folders=lambda: {str(h / d) for d in ("repo", "repo/sub", "plain", "lk")}, git_repos=lambda: [])
                subprocess.run = lambda a, *k, **kw: calls.append(a) or real[3](a, *k, **kw)
                try:
                    f1 = folder_links()
                    assert [c[2] for c in calls] == [str(h / "repo" / "sub")], calls
                    assert f1["linked"] == [{"path": str(h / "lk"), "project": "p"}], f1
                    assert [c["path"] for c in f1["candidates"]] == [str(h / "plain"), str(h / "repo")], f1
                finally:
                    globals().update(HOME=real[0], known_folders=real[1], git_repos=real[2])
                    subprocess.run = real[3]
        # A diff that fails, and whose failure cannot be sent, never costs a chat turn handed out in the same poll.
        started, real = [], (globals()["server"], globals()["folder_links"], chat.start)
        def fake_server(method, path, tok, body=None):
            if path == "/machine/chats":
                return {"work": [{"chat": "c1", "agent": "codex", "folder": "/x"}], "diffs": [{"id": "d1", "folder": "/not-linked"}]}
            raise OSError("server away")
        globals().update(server=fake_server, folder_links=lambda: {"linked": [{"path": "/x", "project": "p"}], "candidates": []})
        chat.start = lambda item, *a: started.append(item["chat"])
        try:
            chats_round()
        finally:
            globals().update(server=real[0], folder_links=real[1])
            chat.start = real[2]
        assert started == ["c1"], started
        print("ok")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
