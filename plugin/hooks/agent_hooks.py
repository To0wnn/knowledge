#!/usr/bin/env python3
"""Hook runtime for agents other than Claude Code (Codex, agy): the startup context, the docs mirror, the session log and
the MCP header, from the same scripts the Claude plugin runs. It lives in plugin/ so a Codex or agy plugin ships it;
clients/knowledge-agent.py (the setup without a plugin) calls the same functions.

  agent_hooks.py context <agy|codex> [TOKEN]   SessionStart: startup context + docs mirror, as the agent's JSON
  agent_hooks.py docs-sync [TOKEN]             PostToolUse: mirror an edited .md (folder with .knowledge-project)
  agent_hooks.py session-log [TOKEN]           SessionEnd/PreCompact (Codex), Stop (agy): post the session log
  agent_hooks.py hints [TOKEN]                 PostInvocation (agy): show a hint docs-sync left (agy's PostToolUse
                                               may only answer {}), once; and tasks handed to agy since its last turn
  agent_hooks.py tasks <claude|codex> [TOKEN]  UserPromptSubmit: tasks handed to this agent since the session started;
                                               also notes the session so another agent on this machine can wake it
  agent_hooks.py wake <claude|codex|agy> [TOKEN]  PostToolUse after knowledge_edit/write/new_memo: the companion here
                                               looks for new tasks now (a worker starts where workers are allowed)
  agent_hooks.py gate <claude|codex|agy>        PreToolUse on the subagent tool: refused from 95% of the usage limit
  agent_hooks.py guard <claude|codex|agy>       PreToolUse on the edit tools: a known secret value is refused
  agent_hooks.py take <agent> <ID>             run by agy in its terminal when the user says 'pak <ID>': take that task
                                               into this conversation (Claude and Codex do it from the user's words)
  agent_hooks.py register                      run by agy in its terminal: note where its language server listens
  agent_hooks.py header [TOKEN]                MCP header helper: the token as JSON, {} without one
  agent_hooks.py mcp [TOKEN]                   stdio MCP server of the Codex and agy plugins: relays to /mcp
  agent_hooks.py serve                         the local companion on 127.0.0.1:47321 (alias: companion; companion.py)
TOKEN: "-" (default) = KNOWLEDGE_TOKEN or the keyring (token_store.py); on the machine that holds the files, a vault name.
Never fails a session: errors go to stderr. Linux, macOS and Windows; standard library only.
"""
from __future__ import annotations  # X | None on Python 3.9 (macOS's /usr/bin/python3)
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
import memory_mirror  # noqa: E402  (the offline copy of the memory, shared with the Claude plugin)
import token_store  # noqa: E402  (KNOWLEDGE_TOKEN or the keyring, shared with the Claude plugin)
import project_marker  # noqa: E402  (the folder-to-project link, shared with the Claude plugin)
import self_update  # noqa: E402  (auto-update of this plugin, the counterpart of Claude Code's own)
import limits  # noqa: E402  (the usage-limit ladder, shared with the Claude plugin)
import hook_status  # noqa: E402  (failures of these hooks, reported to the server)
URL = token_store.url()
KN_HOME = token_store.KN_HOME
AGENTS = ("agy", "codex")
SETUP = "plugin"  # clients/knowledge-agent.py sets "script": the setup without a plugin updates itself another way


def token(name: str) -> str:
    if name == "-":
        return token_store.token()
    return subprocess.run([str(Path.home() / ".local/bin/secret"), name], capture_output=True, text=True, check=True).stdout.strip()


def context_text(tok: str, cwd: str, agent: str = "") -> str:
    """Every part of the startup context (/context/<n>, empty past the last), or one short notice; with the server
    down, the context of the last online session and the offline copy of the memory."""
    key, out = hashlib.sha256(cwd.encode()).hexdigest()[:12], []
    version = ".".join(map(str, self_update.own()))  # the admin page shows who stays behind; the server says so here
    query = urllib.parse.urlencode({**project_query(cwd), "agent": agent, **({"plugin": version} if version else {}),
                                    **({"url": "default"} if token_store.url_is_default() else {})})
    for n in range(1, 10):
        q = query
        req = urllib.request.Request(f"{URL}/context/{n}?{q}", headers=token_store.headers(tok))
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {401: "knowledge server: token invalid or revoked (KNOWLEDGE_TOKEN). Shared memory and docs were not loaded.",
                        403: "knowledge server: only reachable from the home network or WireGuard. Shared memory and docs were not loaded."}[e.code]
            return memory_mirror.offline_context(key)  # 5xx: the server (or its proxy) is down
        except OSError:
            return memory_mirror.offline_context(key)
        if not text:
            break
        out.append(text)
    memory_mirror.save_context(key, "\n\n".join(out))
    return "\n\n".join(out + [n for n in [memory_mirror.sync_notice()] if n])  # a conflict or failed sync, said once per start


TASK_ID = re.compile(r"open (?:tot|until) [^\[:]*\[[^,\]]+,\s*([^\]]+)\]", re.I)  # the owner bracket, not a link before it
TASKS_EVERY = 120  # seconds between two asks of one session: a prompt costs one small request at most this often


def project_query(cwd: str) -> dict:
    try:
        linked = project_marker.project_root(Path(cwd).resolve())[1]
    except OSError:
        linked = None
    return {"project": linked, "linked": "1"} if linked else {"project": cwd}


AGENTS_DIR = KN_HOME / "agents"  # who runs on this machine and how to wake it
WAKE_WITHIN = 2 * 86400  # a session not seen this long is not listed
WAKE_TOOLS = re.compile(r"knowledge_(edit|write|new_memo)")  # the tools that can add a task line


def short(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


NAMES = {"claude": "Claude", "codex": "Codex", "agy": "Gemini"}
# "pak KN-12": do that task in this chat. A task ID starts with a capital (KN-12, ET-eval), so "pak e-mail van Linda" or
# "take follow-up actions" asks the server nothing
PAK = re.compile(r"(?:^|\s)(?i:pak|take)\s+([A-Z][A-Z0-9.]*-[\w.-]*\w)")
assert [m.group(1) for m in PAK.finditer("Pak KN-12 en take ET-eval; pak e-mail, take follow-up, take built-in")] == ["KN-12", "ET-eval"]


def take_command(tid: str = "<ID>") -> str:
    cmd = f'"{sys.executable}" -X utf8 "{HOOKS / "agent_hooks.py"}" take agy {tid}'
    return ("& " + cmd) if os.name == "nt" else cmd  # agy on Windows runs commands in PowerShell


def task_text(fresh: list, agent: str, workers: bool = True) -> str:
    """New tasks for a chat: they run in workers (a fresh session per task the companion starts), never here unless
    the user takes one with 'pak <ID>'; when no pc runs this agent's workers for the project, the chat starts them
    itself (the server says which: `workers`)."""
    if not workers:
        return ("knowledge: a new task was handed to you while this session runs (this message comes from the knowledge "
                "plugin, not from the user; full text in the memo named in front). Finish your current reply, then start it "
                "right away and tell the user in one line what you picked up; starting it: add '(claimed <you>, <date>)' to "
                "its line with knowledge_edit; done: remove the line, and hand the result back to the agent that waits on it "
                "with a line '[<requester>, <ID>-done]: <result>; <commit or path>'.\n" + "\n".join(fresh))
    how = (f"run this in your terminal first, it takes the task for this conversation: {take_command()}" if agent == "agy"
           else "the plugin then takes it for this session")
    return ("knowledge: new task(s) for you were handed over (this message comes from the knowledge plugin, not from the "
            "user; full text in the memo named in front). They run in workers: a fresh session per task that the knowledge "
            "companion starts on a pc that allows workers, not in this chat. Tell the user in one line, then carry on with "
            f"what you were doing. Only when the user says 'pak <ID>' (or 'take <ID>') do it here ({how}).\n" + "\n".join(fresh))


RESULTS_TEXT = ("knowledge: another agent handed a result back to you (this message comes from the knowledge plugin, not "
                "from the user). Read the result, act on it if needed (e.g. close your own follow-up), then remove that "
                "line with knowledge_edit (the file history keeps it):\n")


def task_post(tok: str, path: str, body: dict) -> tuple[int, dict]:
    """POST /tasks/woken or /tasks/release: (HTTP status, JSON answer); (0, {}) when the server was not reached."""
    req = urllib.request.Request(f"{URL}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={**token_store.headers(tok), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read()
    except Exception:
        return 0, {}
    try:
        return code, json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        return code, {}


def ask_tasks(tok: str, query: dict) -> dict:
    req = urllib.request.Request(f"{URL}/tasks?{urllib.parse.urlencode(query)}", headers=token_store.headers(tok))
    with urllib.request.urlopen(req, timeout=4) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:  # a server from before v2 (or rolled back): its answer is the task lines
        return {"tasks": [ln for ln in body.splitlines() if ln.strip()]}


def take(agent: str, tok: str, tid: str, sid: str, cwd: str) -> str:
    """The user said 'pak <ID>': take that task for this chat session on the server (a worker or another session may
    hold it already), and say what to do."""
    try:
        project = ask_tasks(tok, {**project_query(cwd), "agent": agent, "session": sid[:80], "v": 2, "claim": 0}).get("project", "")
    except Exception as e:
        return f"knowledge: could not take {tid}: the server did not answer ({type(e).__name__})."
    code, answer = task_post(tok, "/tasks/woken", {"project": project, "id": tid, "agent": agent, "session": sid[:80]})
    if code == 200:
        return (f"knowledge: the user took task {tid} into this chat ('pak {tid}'); the server gave it to this session, so no "
                f"worker starts it. Claim it now: add '(claimed {NAMES[agent]}, {time.strftime('%Y-%m-%d')})' to its line with "
                "knowledge_edit, then do it here. Done: remove the line, and hand the result back to the agent that waits on it "
                f"with a line '[<requester>, {tid}-done]: <result>; <commit or path>'.")
    if code == 409:
        h = answer.get("held_by") or {}
        where = f"{h.get('device', '?').partition('#')[0]}" + (f" on {h['label']}" if h.get("label") else "")
        what = "runs in a worker" if str(h.get("session", "")).startswith("worker:") else "is held by another session"
        return f"knowledge: {tid} {what} on {where}; tell the user it cannot be taken here now."
    return f"knowledge: no open task {tid} for {NAMES[agent]} in project {project or '?'} (HTTP {code}); tell the user."


def new_tasks(agent: str, tok: str, hook: dict, now=None, wake: bool = False) -> str:
    """Tasks handed to `agent` after this session started, said once: the startup context lists the open ones, but a
    session that runs for hours (or is resumed for days) never sees a task another agent hands over meanwhile. The
    first ask of a session only remembers what is open; '' when nothing is new, the server is away or it is too soon.
    A chat takes no task (chat=1: tasks run in workers); a worker (KNOWLEDGE_WORKER) hears no other tasks, but its ask
    with the companion's start nonce keeps its own task's lease."""
    now = time.time() if now is None else now
    worker = bool(os.environ.get("KNOWLEDGE_WORKER"))
    sid = hook.get("session_id") or hook.get("conversationId") or hook.get("cwd") or os.getcwd()
    # shortcut: two agy sessions without a conversationId in one folder share what they have seen.
    seen_dir = KN_HOME / "tasks-seen"
    seen_file = seen_dir / (hashlib.sha256(f"{agent}:{sid}".encode()).hexdigest()[:16] + ".json")
    try:
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = None
    if seen and now - seen.get("at", 0) < TASKS_EVERY:
        return ""
    cwd = hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0]
    # session and wake let the Machines list show the sessions per machine
    version = ".".join(map(str, self_update.own()))
    known = (seen or {}).get("ids")
    # seen: what this session was told before; start: the nonce the companion gave the worker it started
    extra = {"seen": ",".join(known)} if known is not None else {}
    if nonce := os.environ.get("KNOWLEDGE_START_NONCE"):
        extra["start"] = nonce
    try:
        beat = ask_tasks(tok, {**project_query(cwd), "agent": agent, "session": str(sid)[:80], "wake": int(wake), "v": 2,
                               **({} if worker else {"chat": 1}), **extra, **({"plugin": version} if version else {})})
        ids = {m.group(1).strip(): ln for ln in beat.get("tasks", []) if (m := TASK_ID.search(ln))}
    except Exception:  # the next session start says so loudly; a prompt is no place to nag
        beat, ids = {}, None  # still throttled: away from home every prompt would wait for the timeout
    fresh = [] if ids is None or known is None or worker else [ln for i, ln in ids.items() if i not in known]
    text = task_text(fresh, agent, beat.get("workers") is True) if fresh else ""
    # results handed back to this agent: said once per session, like new tasks (the start context listed the open ones)
    results = {r["id"]: r["text"] for r in beat.get("results", []) if r.get("id")} if ids is not None else None
    known_results = (seen or {}).get("results")
    new_results = [] if worker or results is None or known_results is None else [t for i, t in results.items() if i not in known_results]
    if new_results:
        text = "\n\n".join(t for t in (text, RESULTS_TEXT + "\n".join(new_results)) if t)
    # What the server wants this session to hear or do: said even on the first ask, since a resumed session (agy
    # --continue) never ran its start hooks.
    told = (seen or {}).get("notices", [])
    notices = [n for n in beat.get("notices", []) if n.get("id") not in told]
    said = "\n\n".join([n["text"] for n in notices] + [take_background()])
    text = "\n\n".join(t for t in (text, said) if t.strip())
    if beat.get("sync"):
        start_hook("docs-sync.py", tok, json.dumps({"hook_event_name": "SessionStart", "cwd": cwd}))
    try:
        seen_dir.mkdir(parents=True, exist_ok=True)
        for old in seen_dir.glob("*.json"):  # sessions end without telling: forget them after a week
            if now - old.stat().st_mtime > 7 * 86400:
                old.unlink()
        seen_file.write_text(json.dumps({"at": now, "ids": known if ids is None else sorted(ids),
                                         "results": known_results if results is None else sorted(results),
                                         "notices": (told + [n["id"] for n in notices])[-50:]}), encoding="utf-8")
    except OSError:  # without a memory of what was said, the tasks would come again every prompt; the server's
        return said.strip()  # notices are taken already, so they are said anyway
    return text


def private_dir():
    """The registry: only this user may read agy's key or plant an entry (an entry names the codex binary to run)."""
    AGENTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(AGENTS_DIR, 0o700)
    except OSError:
        pass


def write_private(path: Path, data: dict):
    private_dir()
    tmp = path.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(json.dumps(data))  # created 0600: agy's entry holds the key to its session
    os.replace(tmp, path)


def read_entries(pattern: str) -> list:
    out = []
    for f in AGENTS_DIR.glob(pattern):
        try:
            out.append((f, json.loads(f.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            pass
    return out


def register_session(agent: str, hook: dict):
    """Claude and Codex: note this session at each prompt so another agent on this machine can wake it."""
    sid = hook.get("session_id")
    if not sid:
        return
    cwd = hook.get("cwd") or os.getcwd()
    exe = shutil.which("codex") if agent == "codex" else None  # Codex puts its own folder on the hook's PATH
    home = os.environ.get("CODEX_HOME") if agent == "codex" else None  # the waker's shell may not have it
    try:
        write_private(AGENTS_DIR / f"{agent}-{short(sid)}.json",
                      {"agent": agent, "session": sid, "cwd": cwd, "at": time.time(), **({"exe": exe} if exe else {}),
                       **({"codex_home": home} if home else {})})
    except OSError as e:
        print(f"knowledge: session not registered: {e}", file=sys.stderr)


def worker_turn(hook: dict):
    """A YOLO worker's turn starts: a git checkpoint of its folder on refs/knowledge/checkpoints/<ID>, so its owner can
    roll back to before this turn (Machines list). Never fails the turn."""
    tid = os.environ.get("KNOWLEDGE_WORKER")
    if not (tid and os.environ.get("KNOWLEDGE_YOLO") == "1"):
        return
    try:
        import workers
        workers.checkpoint(hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0], tid)
    except Exception as e:
        hook_status.record_exception("agent_hooks.py checkpoint", e)


def tasks(agent: str, tok: str):
    stdin = sys.stdin.read()
    hook = json.loads(stdin or "{}")
    register_session(agent, hook)
    worker_turn(hook)
    took = [take(agent, tok, m.group(1), str(hook.get("session_id") or ""), hook.get("cwd") or os.getcwd())
            for m in PAK.finditer(str(hook.get("prompt") or ""))][:3] if not os.environ.get("KNOWLEDGE_WORKER") else []
    text = "\n\n".join(t for t in (*took, new_tasks(agent, tok, hook, wake=True), limits.notice(agent, hook)) if t)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}), flush=True)
    send_status(agent, tok)


def agy_call(entry: dict, method: str, body: dict, timeout: float = 3) -> dict:
    """agy's own language server on localhost (undocumented, found in agy cli-1.4.8): Connect-RPC with a CSRF header."""
    req = urllib.request.Request(f"http://{entry['address']}/exa.language_server_pb.LanguageServerService/{method}",
                                 json.dumps(body).encode(), {"Content-Type": "application/json",
                                                              "x-codeium-csrf-token": entry["csrf"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def agy_status() -> dict:
    """GetUserStatus (plan, quota per model) from any running agy that registered: the quota is per account."""
    for _, entry in read_entries("agy-ls-*.json"):
        try:
            return agy_call(entry, "GetUserStatus", {})
        except Exception:
            continue
    raise LookupError("no registered agy answers")


def agy_conversations() -> list:
    """(entry, conversation id, last change, folder) for every conversation of every agy still running; an entry whose
    agy is gone is removed."""
    out = []
    for f, entry in read_entries("agy-ls-*.json"):
        try:
            trajectories = agy_call(entry, "GetAllCascadeTrajectories", {}).get("trajectorySummaries") or {}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):  # a new agy on the old port: the key is no longer valid
                f.unlink(missing_ok=True)
            continue
        except Exception as e:  # agy closed: its port and key died with it; a busy or slow agy keeps its entry
            if isinstance(getattr(e, "reason", e), ConnectionRefusedError):
                f.unlink(missing_ok=True)
            continue
        for conv, t in trajectories.items():
            uri = ((t.get("workspaces") or [{}])[0]).get("workspaceFolderAbsoluteUri", "")
            folder = urllib.request.url2pathname(urllib.parse.urlparse(uri).path) if uri else entry.get("cwd", "")
            out.append((entry, conv, t.get("lastModifiedTime", ""), folder))
    return out


def register_agy():
    """Run by agy in its own terminal: only commands agy runs get the address and key of its language server (hooks
    and MCP servers do not); with both the companion reads agy's usage limit (GetUserStatus) for its workers."""
    address, csrf = os.environ.get("ANTIGRAVITY_LS_ADDRESS", ""), os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if not address or not csrf:
        sys.exit("knowledge: not run by agy (no ANTIGRAVITY_LS_ADDRESS), nothing registered")
    entry = {"address": address, "csrf": csrf, "cwd": os.getcwd(), "at": time.time()}
    try:
        agy_call(entry, "GetAllCascadeTrajectories", {})
    except Exception as e:
        sys.exit(f"knowledge: agy's language server did not answer ({e}), nothing registered")
    write_private(AGENTS_DIR / f"agy-ls-{short(address)}.json", entry)
    conv = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
    try:  # the hooks only see the registration at the next turn: tell the admin page now
        query = urllib.parse.urlencode({**project_query(os.getcwd()), "agent": "agy", "session": conv[:80], "chat": 1, "claim": 0})
        urllib.request.urlopen(urllib.request.Request(f"{URL}/tasks?{query}", headers=token_store.headers(token("-"))),
                               timeout=4).close()
    except Exception:  # the next turn's hook says it to the server anyway
        pass
    print("knowledge: registered; the knowledge companion on this machine can now read agy's usage limit")


def register_command() -> str:
    cmd = f'"{sys.executable}" -X utf8 "{HOOKS / "agent_hooks.py"}" register'
    return ("& " + cmd) if os.name == "nt" else cmd  # agy on Windows runs commands in PowerShell


def register_notice(again: bool = False) -> str:
    if again:  # agy otherwise reasons that it already ran it earlier in the conversation
        return ("knowledge: agy was restarted, so this conversation's earlier registration is gone and the companion can no "
                f"longer read agy's usage limit. Run this command again now in your terminal: {register_command()}")
    return ("knowledge: so the knowledge companion on this machine can read agy's usage limit, run this command once now in "
            f"your terminal (it only stores where this session listens; no need to tell the user): {register_command()}")


def agy_registered(conv: str) -> bool:
    return any(c == conv for _, c, _, _ in agy_conversations())


def wake(me: str, tok: str):
    """PostToolUse after a knowledge write: a new task line may be in it, so the companion on this pc looks at once
    (one round) instead of within a minute; a worker starts there when this pc allows workers. Chats are never told."""
    hook = json.loads(sys.stdin.read() or "{}")
    tool = str(hook.get("tool_name") or (hook.get("toolCall") or {}).get("args", {}).get("ToolName") or hook.get("toolCall") or "")
    if not WAKE_TOOLS.search(tool):
        print("{}")
        return
    port = os.environ.get("KNOWLEDGE_COMPANION_PORT") or "47321"
    try:
        key = (KN_HOME / "companion" / "api-key").read_text(encoding="utf-8").strip()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/local/api/tick", b"{}", method="POST", headers={
            "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}", "X-Companion-Key": key})
        urllib.request.urlopen(req, timeout=3).close()
        note = "knowledge: asked the companion on this pc to look for new tasks now (a worker starts where workers are allowed)."
    except Exception:  # no companion here, or it is busy: its minute round finds the task anyway
        note = ""
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}
                     if note and me != "agy" else {}))  # agy's PostToolUse may only answer {}


def run_hook(script: str, tok: str, stdin: str) -> str:
    env = {**os.environ, "KNOWLEDGE_URL": URL, **({"KNOWLEDGE_TOKEN": tok} if tok else {})}
    r = subprocess.run([sys.executable, "-X", "utf8", str(HOOKS / script)], input=stdin, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode not in (0, 2):
        hook_status.record(script, r.returncode, r.stderr)
    return r.stdout


def send_status(agent: str, tok: str):
    """From the per-turn hook: hook failures not reported yet, at most every two minutes; never fails the turn."""
    try:
        hook_status.send(URL, tok, agent, ".".join(map(str, self_update.own())))
    except Exception:
        pass


BACKGROUND = KN_HOME / "background.txt"  # what a background hook printed, said at the next turn


def start_hook(script: str, tok: str, stdin: str):
    """run_hook without waiting: a full docs mirror can take seconds, a prompt should not. What it prints (a failure,
    refused files, docs without a topic) is said at the next turn (take_background), so it never fails quietly."""
    env = {**os.environ, "KNOWLEDGE_URL": URL, **({"KNOWLEDGE_TOKEN": tok} if tok else {})}
    kw = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}  # as memory_mirror: CREATE_NO_WINDOW | NEW_PROCESS_GROUP
    try:
        BACKGROUND.parent.mkdir(parents=True, exist_ok=True)
        with BACKGROUND.open("ab") as out:
            p = subprocess.Popen([sys.executable, "-X", "utf8", str(HOOKS / script)], stdin=subprocess.PIPE, env=env,
                                 stdout=out, stderr=subprocess.STDOUT, **kw)
        p.stdin.write(stdin.encode("utf-8"))
        p.stdin.close()
    except OSError as e:
        print(f"knowledge: {script} not started: {e}", file=sys.stderr)


def take_background() -> str:
    try:
        text = BACKGROUND.read_text(encoding="utf-8", errors="replace").strip()
        if text:  # an empty file may still be written to: leave it
            BACKGROUND.unlink()
        return text
    except OSError:
        return ""


def context(agent: str, tok: str):
    stdin = sys.stdin.read()
    hook = json.loads(stdin or "{}")
    cwd = hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0]
    if not URL:
        print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": token_store.NO_URL}}]} if agent == "agy" else
                         {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": token_store.NO_URL}}))
        return
    ctx = context_text(tok, cwd, agent)  # first: a slow mirror must not cost the session its context
    run_hook("memory_mirror.py", tok, "")  # returns at once: the sync runs in the background
    try:
        update = self_update.notice(agent, mode=SETUP)
    except Exception as e:  # never cost the session its context
        update = f"knowledge plugin: auto-update check failed ({type(e).__name__}: {e})"
    try:
        worktree = project_marker.notice(Path(cwd).resolve())
    except OSError:
        worktree = ""
    reg = register_notice() if agent == "agy" else ""  # agy hooks do not get its server's key; its terminal does
    if agent == "agy" and "'pak <ID>'" in ctx:  # its hooks never see the user's words: the take command does it
        ctx += ("\n\nagy: the plugin cannot see what the user types. When the user says 'pak <ID>' (or 'take <ID>'), run this "
                f"in your terminal first; it takes that task for this conversation and says what to do: {take_command()}")
    text = "\n\n".join(t for t in (worktree, run_hook("docs-sync.py", tok, stdin).strip(), ctx, update, reg) if t)
    print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]} if agent == "agy" else
                     {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


def mcp(tok: str):
    """stdio MCP server of the Codex and agy plugins: relays each JSON-RPC line from stdin to the server's /mcp and the
    answer to stdout. Neither agent can hand a plugin's HTTP server the token without a plain file (Codex runs a plugin's
    header helper without the plugin folder or env, agy sends a ${VAR} header as is), so the token comes from
    token_store here and stays in memory."""
    # shortcut: one request at a time, so parallel tool calls wait on each other; use a thread per line if that hurts.
    session, out = "", sys.stdout.buffer
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   **token_store.headers(tok), **({"Mcp-Session-Id": session} if session else {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{URL}/mcp", raw, headers), timeout=120) as r:
                session = r.headers.get("Mcp-Session-Id") or session
                sse, body = r.headers.get("Content-Type", "").startswith("text/event-stream"), r.read().decode("utf-8")
            replies = [json.loads(d) for d in ([ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
                                               if sse else [body]) if d.strip()]
        except Exception as e:  # HTTP error, cut-off or non-JSON body: answer every request in it, keep serving
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = None
            ids = [m["id"] for m in (msg if isinstance(msg, list) else [msg]) if isinstance(m, dict) and m.get("id") is not None]
            reason = token_store.NO_URL if not URL else {401: "token invalid or revoked (KNOWLEDGE_TOKEN or the keyring)",
                      403: "only reachable from the home network or WireGuard"}.get(getattr(e, "code", 0), str(e) or type(e).__name__)
            replies = [{"jsonrpc": "2.0", "id": i, "error": {"code": -32000, "message": f"knowledge server: {reason}"}} for i in ids]
        for reply in replies:
            out.write(json.dumps(reply).encode() + b"\n")  # one message per line, as stdio needs
        out.flush()


def hints(tok: str):
    """agy: the hint docs-sync left behind and tasks handed to agy meanwhile, injected once before the next model turn;
    {} when there is none."""
    pending = KN_HOME / "pending-hints.txt"
    try:
        hook = json.loads(sys.stdin.read() or "{}")
        conv = hook.get("conversationId") or ""
        registered = bool(conv) and agy_registered(conv)
        worker_turn(hook)
        news = new_tasks("agy", tok, hook, wake=registered)
    except Exception as e:  # never cost agy its pending hint or its JSON
        hook, conv, registered, news = {}, "", False, ""
        print(f"knowledge task check skipped: {e}", file=sys.stderr)
    try:  # apart: a failure here must not cost the notices the server already handed to this session
        # "registered" once it was; a resumed agy listens on a new port and is then asked again
        asked = AGENTS_DIR / f"agy-asked-{short(conv)}"
        state = asked.read_text(encoding="utf-8") if asked.exists() else None
        if conv and registered and state != "registered":
            private_dir()
            asked.write_text("registered", encoding="utf-8")
        elif conv and not registered and state != "asked":  # a resumed conversation never saw the start context
            private_dir()
            asked.write_text("asked", encoding="utf-8")
            news = "\n\n".join(t for t in (news, register_notice(again=state == "registered")) if t)
    except Exception as e:
        print(f"knowledge register check skipped: {e}", file=sys.stderr)
    try:
        text = pending.read_text(encoding="utf-8")
        pending.unlink()
    except OSError:
        text = ""
    text = "\n\n".join(t for t in (text.strip(), news, limits.notice("agy", hook, agy_status)) if t)
    print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]} if text.strip() else {}), flush=True)
    send_status("agy", tok)


def main(argv: list):
    cmd, rest = (argv[0] if argv else ""), argv[1:]
    if cmd == "context" and rest[:1] and rest[0] in AGENTS and len(rest) <= 2:
        context(rest[0], token(rest[1] if len(rest) == 2 else "-"))
    elif cmd in ("docs-sync", "session-log") and len(rest) <= 1:
        sys.stdout.write(run_hook(f"{cmd}.py", token(rest[0] if rest else "-"), sys.stdin.read()))
    elif cmd == "hints" and len(rest) <= 1:
        try:
            tok = token(rest[0] if rest else "-")
        except Exception:  # a locked vault must not cost agy the pending hint
            tok = ""
        hints(tok)
    elif cmd == "tasks" and rest[:1] and rest[0] in ("claude", "codex") and len(rest) <= 2:
        # Claude without Python's keyring: the plugin setting, as context.sh does
        tasks(rest[0], token(rest[1] if len(rest) == 2 else "-") or os.environ.get("CLAUDE_PLUGIN_OPTION_TOKEN", ""))
    elif cmd == "wake" and rest[:1] and rest[0] in ("claude", "codex", "agy") and len(rest) <= 2:
        try:
            tok = token(rest[1] if len(rest) == 2 else "-") or os.environ.get("CLAUDE_PLUGIN_OPTION_TOKEN", "")
        except Exception:
            tok = ""
        wake(rest[0], tok)
    elif cmd == "wait" and rest[:1] == ["claude"] and len(rest) <= 2:
        pass  # plugins up to 0.14 ran a waiting hook here; a session started before an update may still call it
    elif cmd == "take" and len(rest) == 2 and rest[0] in ("claude", "codex", "agy"):
        # run by agy in its terminal on 'pak <ID>' (agy's hooks never see the user's words); the others take it themselves
        print(take(rest[0], token("-"), rest[1], os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or os.environ.get("KNOWLEDGE_SESSION", ""), os.getcwd()))
    elif cmd == "gate" and rest[:1] and rest[0] in ("claude", "codex", "agy") and len(rest) <= 2:
        out = limits.gate(rest[0], json.loads(sys.stdin.read() or "{}"), agy_status)
        if out:
            print(out)
    elif cmd == "guard" and rest[:1] and rest[0] in ("claude", "codex", "agy") and len(rest) <= 2:
        sys.stdout.write(subprocess.run([sys.executable, "-X", "utf8", str(HOOKS / "secret-guard.py"), rest[0]],
                                        input=sys.stdin.read(), stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                        errors="replace").stdout)  # its stderr passes on: agy only reads stdout
    elif cmd == "watch" and rest[:1] and rest[0] in ("claude", "codex", "agy") and len(rest) <= 2:
        import watch  # the script setup's hooks come through here; the plugins start watch.py directly (faster)
        watch.main(rest[0])
    elif cmd == "register" and not rest:
        register_agy()
    elif cmd == "mcp" and len(rest) <= 1:
        mcp(token(rest[0] if rest else "-"))
    elif cmd in ("serve", "companion") and not rest:
        import companion
        companion.main(["serve"])
    elif cmd == "header" and len(rest) <= 1:
        tok = token(rest[0] if rest else "-")
        print(json.dumps(token_store.headers(tok) if tok else {}))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:  # locked vault, bad stdin, network: say so, never fail the session
        hook_status.record_exception(" ".join(["agent_hooks.py"] + sys.argv[1:3]), e)
        print(f"knowledge hook skipped: {e}", file=sys.stderr)
