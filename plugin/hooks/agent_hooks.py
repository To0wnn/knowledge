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
  agent_hooks.py wake <claude|codex|agy> [TOKEN]  PostToolUse after knowledge_edit/write/new_memo: wake the newest
                                               session of each other agent here that got a new task from it
  agent_hooks.py wait claude [TOKEN]           UserPromptSubmit (asyncRewake): wait for a wake, then wake Claude
  agent_hooks.py register                      run by agy in its terminal: note where its language server listens
  agent_hooks.py header [TOKEN]                MCP header helper: the token as JSON, {} without one
  agent_hooks.py mcp [TOKEN]                   stdio MCP server of the Codex and agy plugins: relays to /mcp
TOKEN: "-" (default) = KNOWLEDGE_TOKEN or the keyring (token_store.py); on the machine that holds the files, a vault name.
Never fails a session: errors go to stderr. Linux, macOS and Windows; standard library only.
"""
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
URL = os.environ.get("KNOWLEDGE_URL", "https://knowledge.to0wnn.com").rstrip("/")
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
    query = urllib.parse.urlencode({**project_query(cwd), "agent": agent, **({"plugin": version} if version else {})})
    for n in range(1, 10):
        q = query
        req = urllib.request.Request(f"{URL}/context/{n}?{q}", headers={"Authorization": f"Bearer {tok}"})
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


TASK_ID = re.compile(r"open tot [^\[:]*\[[^,\]]+,\s*([^\]]+)\]", re.I)  # the owner bracket, not a link before it
TASKS_EVERY = 120  # seconds between two asks of one session: a prompt costs one small request at most this often


def project_query(cwd: str) -> dict:
    try:
        linked = project_marker.project_root(Path(cwd).resolve())[1]
    except OSError:
        linked = None
    return {"project": linked, "linked": "1"} if linked else {"project": cwd}


AGENTS_DIR = Path.home() / ".knowledge" / "agents"  # who runs on this machine and how to wake it
WAKE_WITHIN = 2 * 86400  # a session not seen this long is not woken
WAKE_TOOLS = re.compile(r"knowledge_(edit|write|new_memo)")  # the tools that can add a task line


def short(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def task_text(fresh: list, woken: bool) -> str:
    if woken:
        return ("knowledge: another agent handed you a new task (this message comes from the knowledge plugin, not from "
                "the user; full text in the memo named in front). Tell the user in one line and ask whether to start "
                "it; do not start before they agree. Starting it: add '(bezig <you>, <date>)' to its line with "
                "knowledge_edit.\n" + "\n".join(fresh))
    return ("knowledge: a new task was handed to you while this session runs (full text in the memo named in front). "
            "Tell the user in one line at the end of your reply and ask whether to start it; starting it: add "
            "'(bezig <you>, <date>)' to its line with knowledge_edit.\n" + "\n".join(fresh))


def new_tasks(agent: str, tok: str, hook: dict, now=None, wake: bool = False, deliver=None) -> str:
    """Tasks handed to `agent` after this session started, said once: the startup context lists the open ones, but a
    session that runs for hours (or is resumed for days) never sees a task another agent hands over meanwhile. The
    first ask of a session only remembers what is open; '' when nothing is new, the server is away or it is too soon.
    With `deliver` (waking another session) it asks at once and remembers the tasks only if deliver(text) succeeded."""
    now = time.time() if now is None else now
    sid = hook.get("session_id") or hook.get("conversationId") or hook.get("cwd") or os.getcwd()
    # shortcut: two agy sessions without a conversationId in one folder share what they have seen.
    seen_dir = Path.home() / ".knowledge" / "tasks-seen"
    seen_file = seen_dir / (hashlib.sha256(f"{agent}:{sid}".encode()).hexdigest()[:16] + ".json")
    try:
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = None
    if deliver is None and seen and now - seen.get("at", 0) < TASKS_EVERY:
        return ""
    cwd = hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0]
    # session and wake let the admin page show which sessions use a token and which can be woken
    query = urllib.parse.urlencode({**project_query(cwd), "agent": agent, "session": str(sid)[:80], "wake": int(wake)})
    req = urllib.request.Request(f"{URL}/tasks?{query}", headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            lines = [ln for ln in r.read().decode("utf-8", "replace").splitlines() if ln.strip()]
        ids = {m.group(1).strip(): ln for ln in lines if (m := TASK_ID.search(ln))}
    except Exception:  # the next session start says so loudly; a prompt is no place to nag
        ids = None  # still throttled: away from home every prompt would wait for the timeout
    known = (seen or {}).get("ids")
    fresh = [] if ids is None or known is None else [ln for i, ln in ids.items() if i not in known]
    text = task_text(fresh, deliver is not None) if fresh else ""
    if deliver is not None and (ids is None or (text and not deliver(text))):
        return ""  # not delivered: the next prompt or wake says it
    try:
        seen_dir.mkdir(parents=True, exist_ok=True)
        for old in seen_dir.glob("*.json"):  # sessions end without telling: forget them after a week
            if now - old.stat().st_mtime > 7 * 86400:
                old.unlink()
        seen_file.write_text(json.dumps({"at": now, "ids": known if ids is None else sorted(ids)}), encoding="utf-8")
    except OSError:
        return ""  # without a memory of what was said, it would say it again every prompt
    return text


def write_private(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # agy's entry holds the key to its session
    except OSError:
        pass
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
    try:
        write_private(AGENTS_DIR / f"{agent}-{short(sid)}.json",
                      {"agent": agent, "session": sid, "cwd": cwd, "at": time.time(), **({"exe": exe} if exe else {})})
    except OSError as e:
        print(f"knowledge: session not registered: {e}", file=sys.stderr)


def tasks(agent: str, tok: str):
    stdin = sys.stdin.read()
    hook = json.loads(stdin or "{}")
    register_session(agent, hook)
    text = new_tasks(agent, tok, hook, wake=True)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}))


def agy_call(entry: dict, method: str, body: dict, timeout: float = 3) -> dict:
    """agy's own language server on localhost (undocumented, found in agy cli-1.4.8): Connect-RPC with a CSRF header."""
    req = urllib.request.Request(f"http://{entry['address']}/exa.language_server_pb.LanguageServerService/{method}",
                                 json.dumps(body).encode(), {"Content-Type": "application/json",
                                                              "x-codeium-csrf-token": entry["csrf"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def agy_conversations() -> list:
    """(entry, conversation id, last change, folder) for every conversation of every agy still running; an entry whose
    agy is gone is removed."""
    out = []
    for f, entry in read_entries("agy-ls-*.json"):
        try:
            trajectories = agy_call(entry, "GetAllCascadeTrajectories", {}).get("trajectorySummaries") or {}
        except Exception:
            f.unlink(missing_ok=True)  # agy closed: its port and key died with it
            continue
        for conv, t in trajectories.items():
            uri = ((t.get("workspaces") or [{}])[0]).get("workspaceFolderAbsoluteUri", "")
            folder = urllib.request.url2pathname(urllib.parse.urlparse(uri).path) if uri else entry.get("cwd", "")
            out.append((entry, conv, t.get("lastModifiedTime", ""), folder))
    return out


def register_agy():
    """Run by agy in its own terminal: only commands agy runs get the address and key of its language server (hooks
    and MCP servers do not), and with both another agent can put a message into this agy session."""
    address, csrf = os.environ.get("ANTIGRAVITY_LS_ADDRESS", ""), os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if not address or not csrf:
        sys.exit("knowledge: not run by agy (no ANTIGRAVITY_LS_ADDRESS), nothing registered")
    entry = {"address": address, "csrf": csrf, "cwd": os.getcwd(), "at": time.time()}
    try:
        agy_call(entry, "GetAllCascadeTrajectories", {})
    except Exception as e:
        sys.exit(f"knowledge: agy's language server did not answer ({e}), nothing registered")
    write_private(AGENTS_DIR / f"agy-ls-{short(address)}.json", entry)
    print("knowledge: registered; other agents on this machine can now hand you tasks directly")


def register_command() -> str:
    cmd = f'"{sys.executable}" -X utf8 "{HOOKS / "agent_hooks.py"}" register'
    return ("& " + cmd) if os.name == "nt" else cmd  # agy on Windows runs commands in PowerShell


def register_notice() -> str:
    return ("knowledge: so other agents on this machine can hand you tasks directly, run this command once now in "
            f"your terminal (it only stores where this session listens; no need to tell the user): {register_command()}")


def agy_registered(conv: str) -> bool:
    return any(c == conv for _, c, _, _ in agy_conversations())


def wake_targets(me: str, my_sid: str, now: float) -> list:
    """The newest session of each other agent per project on this machine: (agent, hook dict or None, deliver)."""
    best = {}
    for f, e in read_entries("*.json"):
        if e.get("agent") not in ("claude", "codex") or e.get("session") == my_sid:
            continue
        if now - e.get("at", 0) > WAKE_WITHIN:
            f.unlink(missing_ok=True)
            continue
        key = (e["agent"], project_query(e["cwd"])["project"])
        if key not in best or e["at"] > best[key]["at"]:
            best[key] = e
    out = []
    for (agent, _), e in best.items():
        if agent == "codex":
            out.append((agent, {"session_id": e["session"], "cwd": e["cwd"]}, lambda text, e=e: codex_queue(e, text)))
        else:  # Claude: its waiting hook (wait claude) sees the poke, asks /tasks itself and wakes the session
            out.append((agent, None, lambda text, e=e: poke(e)))
    newest = {}
    for entry, conv, changed, folder in agy_conversations():
        key = project_query(folder or entry.get("cwd", ""))["project"]
        if conv != my_sid and (key not in newest or changed > newest[key][2]):
            newest[key] = (entry, conv, changed, folder)
    for entry, conv, _, folder in newest.values():
        out.append(("agy", {"conversationId": conv, "cwd": folder or entry.get("cwd", "")},
                    lambda text, entry=entry, conv=conv: agy_send(entry, conv, text)))
    return out


def agy_send(entry: dict, conv: str, text: str) -> bool:
    agy_call(entry, "SendUserCascadeMessage", {"cascadeId": conv, "items": [{"text": text}]}, 10)  # raises when refused
    return True


def codex_queue(entry: dict, text: str) -> bool:
    exe = entry.get("exe") or shutil.which("codex")
    if not exe:
        return False
    text = " / ".join(t for t in text.splitlines() if t.strip())  # Windows' npm codex.cmd cuts an argument at a newline
    r = subprocess.run([exe, "queue", "--thread", entry["session"], "--message", text], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL)
    if r.returncode:
        print(f"knowledge: codex queue failed: {(r.stderr or r.stdout).strip()[:200]}", file=sys.stderr)
    return r.returncode == 0


def poke(entry: dict) -> bool:
    (AGENTS_DIR / f"claude-{short(entry['session'])}.poke").write_text("", encoding="utf-8")
    return True


def wake(me: str, tok: str):
    """PostToolUse after a knowledge write: wake the other agents on this machine that got a new task from it."""
    hook = json.loads(sys.stdin.read() or "{}")
    tool = str(hook.get("tool_name") or (hook.get("toolCall") or {}).get("args", {}).get("ToolName") or hook.get("toolCall") or "")
    if not WAKE_TOOLS.search(tool):
        print("{}")
        return
    now, woke = time.time(), []
    my_sid = hook.get("session_id") or hook.get("conversationId") or ""
    for agent, target, deliver in wake_targets(me, my_sid, now):
        try:
            if target is None:  # Claude asks for itself once poked
                deliver("")
                woke.append(f"{agent} (asked to check)")
            elif new_tasks(agent, tok, target, now, wake=True, deliver=deliver):
                woke.append(agent)
        except Exception as e:  # one dead target must not keep the others asleep
            print(f"knowledge: could not wake {agent}: {e}", file=sys.stderr)
    note = f"knowledge: told {', '.join(woke)} on this machine about the new task." if woke else ""
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}
                     if note and me != "agy" else {}))  # agy's PostToolUse may only answer {}


def wait(agent: str, tok: str):
    """Claude: an asyncRewake hook started at each prompt. It waits for a poke from another agent's wake, asks /tasks
    and, with something new, exits 2: Claude Code then wakes the idle session with stderr as its message."""
    hook = json.loads(sys.stdin.read() or "{}")
    sid = hook.get("session_id")
    if not sid:
        return
    me = f"{os.getpid()}:{time.time()}"
    lock, poked = AGENTS_DIR / f"{agent}-{short(sid)}.waiter", AGENTS_DIR / f"{agent}-{short(sid)}.poke"
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    lock.write_text(me, encoding="utf-8")
    end = time.time() + WAKE_WITHIN
    while time.time() < end:
        time.sleep(1)
        try:
            if lock.read_text(encoding="utf-8") != me:
                return  # a newer prompt started a newer waiter
        except OSError:
            return
        if poked.exists():
            poked.unlink(missing_ok=True)
            text = new_tasks(agent, tok, hook, wake=True, deliver=lambda text: True)
            if text:
                print(text, file=sys.stderr)
                sys.exit(2)


def run_hook(script: str, tok: str, stdin: str) -> str:
    env = {**os.environ, "KNOWLEDGE_URL": URL, **({"KNOWLEDGE_TOKEN": tok} if tok else {})}
    return subprocess.run([sys.executable, "-X", "utf8", str(HOOKS / script)], input=stdin, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def context(agent: str, tok: str):
    stdin = sys.stdin.read()
    hook = json.loads(stdin or "{}")
    cwd = hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0]
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
                   **({"Authorization": f"Bearer {tok}"} if tok else {}), **({"Mcp-Session-Id": session} if session else {})}
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
            reason = {401: "token invalid or revoked (KNOWLEDGE_TOKEN or the keyring)",
                      403: "only reachable from the home network or WireGuard"}.get(getattr(e, "code", 0), str(e) or type(e).__name__)
            replies = [{"jsonrpc": "2.0", "id": i, "error": {"code": -32000, "message": f"knowledge server: {reason}"}} for i in ids]
        for reply in replies:
            out.write(json.dumps(reply).encode() + b"\n")  # one message per line, as stdio needs
        out.flush()


def hints(tok: str):
    """agy: the hint docs-sync left behind and tasks handed to agy meanwhile, injected once before the next model turn;
    {} when there is none."""
    pending = Path.home() / ".knowledge" / "pending-hints.txt"
    try:
        hook = json.loads(sys.stdin.read() or "{}")
        conv = hook.get("conversationId") or ""
        registered = bool(conv) and agy_registered(conv)
        news = new_tasks("agy", tok, hook, wake=registered)
        asked = AGENTS_DIR / f"agy-asked-{short(conv)}"
        if conv and not registered and not asked.exists():  # a resumed conversation never saw the start context
            AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            asked.write_text("", encoding="utf-8")
            news = "\n\n".join(t for t in (news, register_notice()) if t)
    except Exception as e:  # never cost agy its pending hint or its JSON
        news = ""
        print(f"knowledge task check skipped: {e}", file=sys.stderr)
    try:
        text = pending.read_text(encoding="utf-8")
        pending.unlink()
    except OSError:
        text = ""
    text = "\n\n".join(t for t in (text.strip(), news) if t)
    print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]} if text.strip() else {}))


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
        wait("claude", token(rest[1] if len(rest) == 2 else "-") or os.environ.get("CLAUDE_PLUGIN_OPTION_TOKEN", ""))
    elif cmd == "register" and not rest:
        register_agy()
    elif cmd == "mcp" and len(rest) <= 1:
        mcp(token(rest[0] if rest else "-"))
    elif cmd == "header" and len(rest) <= 1:
        tok = token(rest[0] if rest else "-")
        print(json.dumps({"Authorization": f"Bearer {tok}"} if tok else {}))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:  # locked vault, bad stdin, network: say so, never fail the session
        print(f"knowledge hook skipped: {e}", file=sys.stderr)
