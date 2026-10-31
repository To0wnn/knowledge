#!/usr/bin/env python3
"""Chats from the web UI: the owner talks to an agent on this pc as in a terminal, the companion runs each turn.

The companion pulls a chat's next message from the server (GET /machine/chats), runs one headless turn of the agent in
the chat's folder (a folder linked to a project here, nothing else) and posts what the agent prints back in small
batches (POST /machine/chats/events). The first turn starts a new conversation, every later turn continues it the
agent's own way. The message goes in on stdin, never on the command line (a .cmd shim would hand it to cmd.exe). With
YOLO on here: the agent's YOLO flag (Claude `--resume <session>`, Codex `codex exec resume <thread>`, agy
`--conversation <id>`). Without: a worker's narrow set (workers.py), and for anything else the turn pauses and asks the
chat's owner in the web UI (ask()), each agent its native way: Claude's permission prompt tool (approval_mcp.py),
Codex over `codex app-server`, agy over `agy acp`.

  chat.py --self-test   the argv and the stream parsing per agent
Standard library only; Linux, macOS and Windows.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
import workers  # noqa: E402

RUNNING: dict = {}  # chat id -> {pid, ident, stop, allow, folder}: the turns this companion runs now
WAITS: dict = {}  # approval id -> {chat, done (Event), decision, by, expired}: asks waiting for the owner
ASK_SERVER = None  # the companion sets it: body -> the server's answer to POST /machine/chats/ask ({id, timeout})
LOG = lambda *a: None  # the companion's activity log
PATH_KEYS = ("file_path", "path", "Path", "notebook_path", "AbsolutePath", "TargetFile", "DirectoryPath", "cwd")
MAX_TURNS = 4  # turns at once on one pc
FLUSH = 0.7  # seconds: output is posted in batches at most this often


def approval_config(cid: str) -> str:
    """The --mcp-config file of a Claude turn: our permission prompt server (approval_mcp.py) for chat `cid`."""
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe" and (py.parent / "python.exe").exists():  # stdio needs the console python
        py = py.parent / "python.exe"
    workers.DIR.mkdir(parents=True, exist_ok=True)
    f = workers.DIR / f"approval-{cid}.json"  # a file: inline JSON would not pass a .cmd shim
    f.write_text(json.dumps({"mcpServers": {"knowledge_approval": {"command": str(py), "args": [str(HOOKS / "approval_mcp.py")],
                                                                    "env": {"KNOWLEDGE_CHAT": cid, **{k: os.environ[k] for k in (
                                                                        "KNOWLEDGE_HOME", "KNOWLEDGE_COMPANION_PORT") if os.environ.get(k)}}}}}), encoding="utf-8")
    return str(f)


def argv(agent: str, exe: str, yolo: bool, session: str, chat: str = "") -> list:
    """One turn's command line (flags checked against claude 2.1, codex-cli 0.164, agy 1.4.12); the message is stdin.
    Without YOLO, Claude asks the owner through its documented permission prompt tool (approval_mcp.py) for what its
    allowed set does not hold."""
    if agent == "claude":
        return [exe, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                *(["--resume", session] if session else []),
                *(["--dangerously-skip-permissions"] if yolo else [
                    "--mcp-config", approval_config(chat), "--permission-prompt-tool", "mcp__knowledge_approval__ask",
                    "--permission-mode", "acceptEdits", "--allowedTools", *workers.CLAUDE_ALLOWED])]
    if agent == "codex":
        if not yolo:  # app-server (JSON-RPC): the Codex that asks its client (`codex exec` never asks); sandbox and policy per thread
            return [exe, "app-server", "-c", workers.CODEX_APPROVE]
        return [exe, "exec", *(["resume"] if session else []), "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json",
                *([session] if session else []), "-"]
    if not yolo:  # ACP (agy acp): the only headless agy that asks its client for a permission (-p denies by itself)
        return [exe, "acp"]
    return [exe, "--input-format", "stream-json", "--output-format", "stream-json", "--dangerously-skip-permissions",
            *(["--conversation", session] if session else []), "-p", ""]


def stdin_for(agent: str, text: str) -> bytes:
    if agent == "agy":  # one NDJSON message per turn
        return (json.dumps({"event": "user", "message": {"role": "user", "content": text}}) + "\n").encode("utf-8")
    return text.encode("utf-8")


def summary(params) -> str:
    """A tool call in one short line: its command, path or query, else its arguments."""
    if isinstance(params, dict):
        for k in ("command", "CommandLine", "file_path", "path", "AbsolutePath", "TargetFile", "DirectoryPath", "pattern",
                  "Pattern", "query", "Query", "url", "Url", "description"):
            if isinstance(params.get(k), str) and params[k].strip():
                return " ".join(params[k].split())[:200]
    return json.dumps(params, ensure_ascii=False)[:200] if params else ""


def first_line(text) -> str:
    return next((x.strip() for x in str(text or "").splitlines() if x.strip()), "")[:300]


def parse(agent: str, line: str, st: dict) -> list:
    """The chat events [(kind, text)] in one line of the agent's JSON stream; st keeps the session id and the turn's
    state. Kinds: text (appended to the answer), tool, refused, error, info."""
    try:
        e = json.loads(line)
    except ValueError:
        return [("info", line.strip()[:500])] if line.strip() else []
    if not isinstance(e, dict):
        return []
    out = []
    if agent == "claude":
        t = e.get("type")
        if e.get("session_id"):
            st["session"] = e["session_id"]
        if t == "stream_event":
            d = (e.get("event") or {}).get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                st["streamed"] = True
                out.append(("text", d["text"]))
            elif (e.get("event") or {}).get("type") == "message_stop" and st.get("streamed"):
                out.append(("text", "\n\n"))
        elif t == "assistant":
            for b in (e.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    out.append(("tool", f"{b.get('name')}: {summary(b.get('input'))}".rstrip(": ")))
                elif b.get("type") == "text" and b.get("text") and not st.get("streamed"):  # without partial messages
                    out.append(("text", b["text"] + "\n\n"))
        elif t == "result":
            st["result"] = True
            for d in e.get("permission_denials") or []:
                out.append(("refused", f"refused: {d.get('tool_name')} {summary(d.get('tool_input'))}".strip()))
            if e.get("is_error"):
                out.append(("error", first_line(e.get("result")) or str(e.get("subtype") or "the turn failed")))
    elif agent == "codex":
        t, item = e.get("type"), e.get("item") or {}
        if t == "thread.started" and e.get("thread_id"):
            st["session"] = e["thread_id"]
        elif t == "item.started" and item.get("type") == "command_execution":
            out.append(("tool", f"$ {' '.join(str(item.get('command', '')).split())[:200]}"))
        elif t == "item.completed":
            k = item.get("type")
            if k == "agent_message" and item.get("text"):
                out.append(("text", item["text"] + "\n\n"))
            elif k == "command_execution" and item.get("exit_code") not in (0, None):
                out.append(("refused", f"exit {item['exit_code']}: {first_line(item.get('aggregated_output')) or item.get('command', '')}"))
            elif k == "file_change":
                out.append(("tool", "edit: " + ", ".join(str(c.get("path", "")) for c in item.get("changes") or [])[:200]))
            elif k == "mcp_tool_call":
                out.append(("tool", f"{item.get('server')}.{item.get('tool')}"))
            elif k == "web_search":
                out.append(("tool", f"search: {item.get('query', '')}"[:200]))
            elif k == "error":
                out.append(("error", first_line(item.get("message"))))
        elif t == "turn.completed":
            st["result"] = True
        elif t in ("turn.failed", "error"):
            out.append(("error", first_line((e.get("error") or {}).get("message") or e.get("message")) or t))
    else:  # agy
        ev = e.get("event")
        if ev == "init" and e.get("conversation_id"):
            st["session"] = e["conversation_id"]
        elif ev == "step_update":
            s = e.get("step_update") or {}
            st["session"] = s.get("conversation_id") or st.get("session", "")
            if s.get("step_type") == "agent_response" and s.get("text_delta"):
                out.append(("text", s["text_delta"]))
            elif s.get("step_type") == "tool":
                info = s.get("tool_info") or {}
                if s.get("state") == "ACTIVE":
                    out.append(("tool", f"{s.get('tool_name')}: {summary(info.get('parameters'))}".rstrip(": ")))
                elif s.get("state") == "ERROR":
                    out.append(("refused", f"{s.get('tool_name')}: {first_line((info.get('error') or {}).get('message'))}"))
        elif ev == "result":
            r = e.get("result") or {}
            st["session"], st["result"] = r.get("conversation_id") or st.get("session", ""), True
            if r.get("status") == "ERROR":
                out.append(("error", first_line(r.get("error")) or "the turn failed"))
    return out


def inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(os.path.join(folder, os.path.expanduser(path))), os.path.realpath(folder)]) == os.path.realpath(folder)
    except ValueError:  # another drive on Windows
        return False


def once_only(inp, folder: str) -> bool:
    """git push, or anything outside the chat's folder (a path argument, or an absolute, ~ or .. path in a command):
    the owner may allow it once, never for the whole chat."""
    if not isinstance(inp, dict):
        inp = {"command": " ".join(map(str, inp)) if isinstance(inp, list) else str(inp)}
    cmd = " ".join(str(inp.get(k) or "") for k in ("command", "CommandLine", "cmd") if not isinstance(inp.get(k), list)) + \
        " ".join(" ".join(map(str, inp[k])) for k in ("command", "cmd") if isinstance(inp.get(k), list))
    if re.search(r"\bgit\b[^|;&\n]*\bpush\b", cmd):
        return True
    paths = [str(inp[k]) for k in PATH_KEYS if isinstance(inp.get(k), str) and inp[k]]
    paths += re.findall(r"(?:(?<=\s)|(?<=^)|(?<=[=<>'\"]))((?:~|\.\.|/|[A-Za-z]:[\\/])[^\s'\";|&<>]*)", cmd)
    return any(not inside(p, folder) for p in paths if p not in ("/dev/null", "/dev/stdout", "/dev/stderr"))


def ask(cid: str, tool: str, inp, reason: str = "") -> tuple:
    """A turn of chat `cid` wants a tool call its allowed set does not hold: (decision, message), decision once, chat
    or deny. A rule the owner gave for this chat answers at once; else the owner is asked through the server and this
    blocks until they answer, the approval times out, or the turn stops. Every failure is a deny."""
    rec = RUNNING.get(cid)
    if not rec or rec.get("stop"):
        return "deny", "no turn of this chat runs on this pc"
    detail, once = summary(inp) or tool, once_only(inp, rec.get("folder") or "")
    if not once and {"tool": tool, "key": detail} in rec.get("allow", []):
        LOG("chat", f"Approval: {tool} allowed by this chat's rule", f"chat {cid}: {detail}"[:300])
        return "chat", ""
    try:
        r = ASK_SERVER({"chat": cid, "tool": tool, "detail": detail, "reason": reason, "key": detail, "once_only": once})
    except Exception as e:
        LOG("error", "Could not ask for an approval", f"chat {cid}: {type(e).__name__}: {e}"[:300])
        return "deny", f"could not ask the chat's owner ({type(e).__name__}); denied"
    w = WAITS[r["id"]] = {"chat": cid, "done": threading.Event()}
    LOG("chat", f"Approval asked: {tool}", f"chat {cid}: {detail}"[:300])
    got = w["done"].wait(float(r.get("timeout") or 1800) + 120)  # the server times it out first; this is the fallback
    WAITS.pop(r["id"], None)
    decision = w.get("decision") if got and w.get("decision") in ("once", "chat", "deny") else "deny"
    why = ("the turn was stopped" if w.get("stopped") else "no answer in time" if w.get("expired") or not got
           else f"by {w.get('by') or '?'}")
    LOG("chat", {"once": "Approval: allowed once", "chat": "Approval: allowed for this chat", "deny": "Approval: denied"}[decision]
        + f" ({why}): {tool}", f"chat {cid}: {detail}"[:300])
    if decision == "chat":
        rec.setdefault("allow", []).append({"tool": tool, "key": detail})
    return decision, "" if decision != "deny" else (
        "No answer from the chat's owner in time: denied. Do not retry it; say what you needed it for." if why == "no answer in time"
        else "The chat's owner denied this tool call. Do not retry it; continue without it or say what you need.")


def answer(a: dict):
    """The owner's decision for an approval (from the server's poll answer)."""
    w = WAITS.get(str(a.get("ask", "")))
    if w:
        w.update(decision=a.get("decision"), by=a.get("by", ""), expired=bool(a.get("expired")))
        w["done"].set()


class Rpc:
    """JSON-RPC 2.0 over an agent's stdin/stdout (agy acp, codex app-server): requests out, its own requests
    (approvals) answered by `on_request`, its notifications turned into chat events by `on_note`."""

    def __init__(self, p, on_note, on_request):
        self.p, self.n, self.on_note, self.on_request = p, 0, on_note, on_request

    def send(self, msg: dict):
        self.p.stdin.write((json.dumps({"jsonrpc": "2.0", **msg}) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def notify(self, method: str, params: dict = None):
        self.send({"method": method, **({"params": params} if params is not None else {})})

    def call(self, method: str, params: dict, out: list, until: str = ""):
        """Send a request and read until its answer; yields event lists on the way (None: flush now). The result
        lands in out[0]; a JSON-RPC error raises."""
        self.n += 1
        rid, answered = self.n, False
        self.send({"id": rid, "method": method, "params": params})
        for raw in self.p.stdout:
            try:
                m = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                yield [("info", raw.decode("utf-8", "replace").strip()[:500])] if raw.strip() else []
                continue
            if not isinstance(m, dict):
                continue
            if not answered and m.get("id") == rid and "method" not in m:
                if m.get("error"):
                    raise RuntimeError(f"{method}: {first_line((m['error'] or {}).get('message')) or m['error']}")
                out.append(m.get("result") or {})
                if not until:
                    return
                answered = True  # read on until the notification `until` (a turn's end)
            if "method" in m and "id" in m:  # the agent asks us (an approval): the owner may take minutes
                yield None
                try:
                    self.send({"id": m["id"], "result": self.on_request(m["method"], m.get("params") or {})})
                except OSError:
                    return
            elif "method" in m:
                yield self.on_note(m["method"], m.get("params") or {})
                if answered and m["method"] == until:
                    out.append(m.get("params") or {})
                    return
        raise RuntimeError(f"{method}: the agent ended before it answered")


def decide(cid: str, tool: str, inp, reason: str) -> str:
    """once, chat or deny for a tool call the agent's own allowed set does not cover (the owner, via ask())."""
    return ask(cid, tool, inp, reason)[0]


def auto_allowed(kind: str, name: str, inp, folder: str) -> bool:
    """What a chat turn without YOLO may do without asking, as its -p sets allowed it: the knowledge tools, file edits
    inside its folder, and the read-only or committing git commands (GIT_ALLOWED), nothing chained to them."""
    if "knowledge" in name.lower() and kind not in ("execute", "edit", "delete", "move"):
        return True
    if kind in ("edit", "delete", "move"):
        return not once_only(inp, folder)
    if kind == "execute":
        cmd = " ".join(str(x) for x in inp) if isinstance(inp, list) else str((inp or {}).get("CommandLine") or (inp or {}).get("command") or "")
        cmd = " ".join(cmd.split())
        return not re.search(r"[;&|<>`$()\\\n]", cmd) and any(cmd == g or cmd.startswith(g + " ") for g in workers.GIT_ALLOWED)
    return kind in ("read", "search", "think", "fetch") and not once_only(inp, folder)


def agy_acp(p, item: dict, st: dict):
    """One agy turn over ACP: initialize, continue the conversation (session/load) or start one (session/new), send
    the message (session/prompt). Its permission requests are answered by auto_allowed() or the owner."""
    cid, folder, live = item["chat"], item.get("folder", ""), [False]  # session/load replays the history: not new output

    def note(method, params):
        u = params.get("update") or {}
        k = u.get("sessionUpdate")
        if not live[0] or method != "session/update":
            return []
        if k == "agent_message_chunk" and (u.get("content") or {}).get("text"):
            return [("text", u["content"]["text"])]
        if k == "tool_call":
            return [("tool", f"{u.get('name') or u.get('title')}: {summary(u.get('rawInput') or u.get('args'))}".rstrip(": "))]
        if k == "tool_call_update" and u.get("status") == "failed":
            return [("refused", f"{u.get('name') or u.get('title')}: failed")]
        return []

    def request(method, params):
        if method != "session/request_permission":
            return {}
        tc, opts = params.get("toolCall") or {}, params.get("options") or []
        inp, name = tc.get("rawInput") or tc.get("args") or {}, str(tc.get("name") or tc.get("title") or "tool")
        ok = auto_allowed(str(tc.get("kind") or ""), name, inp, folder) or decide(
            cid, name, inp, str((inp or {}).get("toolSummary") or "") if isinstance(inp, dict) else "") != "deny"
        pick = next((o for o in opts if o.get("kind") == ("allow_once" if ok else "reject_once")), None)
        return {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}} if pick else {"outcome": {"outcome": "cancelled"}}
    rpc, out = Rpc(p, note, request), []
    yield from rpc.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, out)
    if st["session"]:
        yield from rpc.call("session/load", {"sessionId": st["session"], "cwd": folder, "mcpServers": []}, out)
    else:
        yield from rpc.call("session/new", {"cwd": folder, "mcpServers": []}, out)
        st["session"] = out[-1].get("sessionId") or ""
    live[0] = True
    yield from rpc.call("session/prompt", {"sessionId": st["session"], "prompt": [{"type": "text", "text": item.get("text", "")}]}, out)
    st["result"] = True
    if out[-1].get("stopReason") not in (None, "end_turn"):
        yield [("info", f"The turn ended: {out[-1]['stopReason']}")]
    p.stdin.close()


def merged(events: list) -> list:
    """Neighbouring text pieces as one event: fewer, larger events on the server."""
    out = []
    for k, t in events:
        if out and k == "text" and out[-1]["kind"] == "text":
            out[-1]["text"] += t
        else:
            out.append({"kind": k, "text": t})
    return out


def codex_app(p, item: dict, st: dict):
    """One Codex turn over `codex app-server` (JSON-RPC v2; `codex exec` never asks): initialize, resume the thread
    (thread/resume, also one `codex exec` began) or start one, then turn/start until turn/completed, all in the
    workspace-write sandbox with approvalPolicy on-request. Its approval requests are answered by auto_allowed() or
    the owner; only decisions it offers are sent."""
    cid, folder, said = item["chat"], item.get("folder", ""), [False]

    def note(method, params):
        it = params.get("item") or {}
        if method == "item/agentMessage/delta" and params.get("delta"):
            said[0] = True
            return [("text", params["delta"])]
        if method == "item/started" and it.get("type") == "commandExecution":
            return [("tool", f"$ {' '.join(str(it.get('command', '')).split())[:200]}")]
        if method == "item/completed":
            k = it.get("type")
            if k == "agentMessage":
                out, said[0] = ([("text", it.get("text", "") + "\n\n")] if it.get("text") and not said[0] else [("text", "\n\n")]), False
                return out
            if k == "commandExecution" and it.get("exitCode") not in (0, None):
                return [("refused", f"exit {it['exitCode']}: {first_line(it.get('aggregatedOutput')) or it.get('command', '')}")]
            if k == "fileChange":
                return [("tool", "edit: " + ", ".join(str(c.get("path", "")) for c in it.get("changes") or [])[:200])]
            if k == "mcpToolCall":
                return [("tool", f"{it.get('server')}.{it.get('tool')}")]
        if method == "error" and not params.get("willRetry"):
            return [("error", first_line((params.get("error") or {}).get("message")) or "error")]
        return []

    def pick(params, ok: bool):
        offered = params.get("availableDecisions") or ["accept", "decline", "cancel"]
        names = [d for d in offered if isinstance(d, str)]
        return ("accept" if "accept" in names else names[0]) if ok else ("decline" if "decline" in names else "cancel")

    def request(method, params):
        reason = str(params.get("reason") or "")
        if method == "item/commandExecution/requestApproval":
            acts = [a.get("command") for a in params.get("commandActions") or [] if a.get("command")]
            cmd = acts[0] if len(acts) == 1 else str(params.get("command") or "")
            inp = {"command": cmd, "cwd": params.get("cwd") or folder}
            ok = auto_allowed("execute", "shell", inp, folder) or decide(cid, "shell", inp, reason) != "deny"
            return {"decision": pick(params, ok)}
        if method == "item/fileChange/requestApproval":
            inp = {"path": params.get("grantRoot") or folder}
            ok = auto_allowed("edit", "edit", inp, folder) or decide(cid, "edit", inp, reason) != "deny"
            return {"decision": pick(params, ok)}
        if method == "item/permissions/requestApproval":
            ok = decide(cid, "permissions", params.get("permissions") or {}, reason) != "deny"
            return {"permissions": (params.get("permissions") or {}) if ok else {}, "scope": "turn"}
        if method == "mcpServer/elicitation/request":  # the knowledge tools are approved by the -c override; others are not ours to fill in
            return {"action": "decline"}
        return {}
    rpc, out = Rpc(p, note, request), []
    yield from rpc.call("initialize", {"clientInfo": {"name": "knowledge-companion", "version": "1"}}, out)
    rpc.notify("initialized")
    perms = {"cwd": folder, "approvalPolicy": "on-request", "sandbox": "workspace-write"}
    if st["session"]:
        yield from rpc.call("thread/resume", {"threadId": st["session"], **perms}, out)
    else:
        yield from rpc.call("thread/start", perms, out)
    st["session"] = ((out[-1].get("thread") or {}).get("id")) or st["session"]
    yield from rpc.call("turn/start", {"threadId": st["session"], "input": [{"type": "text", "text": item.get("text", "")}]}, out, until="turn/completed")
    turn = out[-1].get("turn") or {}
    st["result"] = True
    if turn.get("status") == "failed":
        yield [("error", first_line((turn.get("error") or {}).get("message")) or "the turn failed")]
    elif turn.get("status") == "interrupted":
        yield [("info", "The turn was interrupted.")]
    p.stdin.close()


RPC = {"agy": agy_acp, "codex": codex_app}  # the agents whose turns without YOLO run over JSON-RPC, so they can ask


def run_turn(item: dict, post, log, linked, yolo):
    """One turn of chat `item` ({chat, agent, folder, session, text}): run it, post its output as it comes (post(body)),
    and end with done (and the error, if any). Never raises; failures go to the chat and the activity log."""
    cid, agent, folder = item["chat"], item.get("agent"), item.get("folder", "")
    st, buf, rec, last, err = {"session": item.get("session") or ""}, [], None, time.time(), ""
    name = {"claude": "Claude Code", "codex": "Codex", "agy": "agy"}.get(agent, str(agent))

    def flush(**end):
        nonlocal buf, last
        body, buf, last = {"chat": cid, "events": merged(buf), "session": st["session"], **end}, [], time.time()
        if body["events"] or end:
            post(body)
    try:
        if agent not in ("claude", "codex", "agy"):
            raise RuntimeError(f"unknown agent {agent}")
        if folder not in linked():
            raise RuntimeError(f"{folder} is not a folder linked to a project on this pc")
        exe = shutil.which(agent)
        if not exe:
            raise RuntimeError(f"{agent} is not on PATH")
        cmd_rpc = agent in RPC and not yolo(agent)
        cmd = argv(agent, exe, yolo(agent), st["session"], cid)
        if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
            cmd = workers.shim_line(cmd)
        log("chat", f"{name}: chat turn started in {folder}", f"chat {cid}" + (f", continues {st['session']}" if st["session"] else ", a new conversation"))
        with tempfile.TemporaryFile() as errf:
            kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": workers.WIN_FLAGS}  # its own group: stop ends its children too
            p = subprocess.Popen(cmd, cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf,
                                 env={**os.environ, "KNOWLEDGE_CHAT": cid}, **kw)
            rec = RUNNING.setdefault(cid, {"stop": False})  # start() put it there: a stop that came meanwhile holds
            rec.update(pid=p.pid, ident=workers.identity(p.pid))
            if rec["stop"]:
                workers.kill(rec, hard=True)
            if cmd_rpc:  # it asks for approvals over JSON-RPC on stdin/stdout: the driver answers and keeps stdin open
                source = RPC[agent](p, item, st)
            else:
                try:
                    p.stdin.write(stdin_for(agent, item.get("text", "")))
                    p.stdin.close()
                except OSError:  # it ended before reading: its exit code and stderr say why
                    pass
                source = (parse(agent, raw.decode("utf-8", "replace"), st) for raw in p.stdout)
            for evs in source:  # None: about to wait for the owner, so what came so far goes out now
                buf += evs or []
                if evs is None or time.time() - last >= FLUSH:
                    flush()
            code = p.wait()
            errf.seek(0)
            tail = errf.read()[-2000:].decode("utf-8", "replace").strip()
        if rec["stop"]:
            buf.append(("info", "Stopped."))
            log("chat", f"{name}: chat turn stopped by its owner", f"chat {cid}")
        elif code != 0:
            err = f"{agent} ended with exit code {code}" + (f": {first_line(tail.splitlines()[-1] if tail else '')}" if tail else "")
        elif not st.get("result"):
            err = f"{agent} ended without a result" + (f": {first_line(tail)}" if tail else "")
        if not err and not rec["stop"]:
            log("chat", f"{name}: chat turn done", f"chat {cid}, session {st['session']}")
    except Exception as e:
        if rec and rec.get("stop"):  # a stop ended an RPC turn mid-answer: that is the stop, not a failure
            buf.append(("info", "Stopped."))
            log("chat", f"{name}: chat turn stopped by its owner", f"chat {cid}")
        else:
            err = f"{type(e).__name__}: {e}"[:300]
    if err:
        buf.append(("error", err))
        log("error", f"{name}: chat turn failed", f"chat {cid}: {err}")
    try:
        flush(done=True, **({"error": err} if err else {}))
    except Exception as e:  # the server away: it says the turn was lost when it next hands out work
        log("error", "Could not send a chat's last output to the server", f"chat {cid}: {e}"[:300])
    finally:
        RUNNING.pop(cid, None)  # after the done post: until then the server's "busy" list matches this


def stop(cid: str) -> bool:
    """Stop a running turn (the agent and what it started); False when none runs here."""
    rec = RUNNING.get(cid)
    if not rec:
        return False
    rec["stop"] = True
    for w in [w for w in WAITS.values() if w["chat"] == cid]:  # nobody answers a stopped turn's approvals
        w.update(decision="deny", stopped=True)
        w["done"].set()
    workers.kill(rec, hard=True)
    return True


def start(item: dict, post, log, linked, yolo):
    if len(RUNNING) >= MAX_TURNS:
        post({"chat": item["chat"], "events": [{"kind": "error", "text": f"{MAX_TURNS} chat turns run on this pc already; send again later"}],
              "done": True, "error": "busy"})
        return
    # taken: the server's next "busy" list must not call it lost
    RUNNING[item["chat"]] = {"pid": 0, "ident": "", "stop": False, "folder": item.get("folder", ""),
                             "allow": [x for x in item.get("allow") or [] if isinstance(x, dict)]}
    threading.Thread(target=run_turn, args=(item, post, log, linked, yolo), daemon=True).start()


def self_test():
    w = workers.DIR
    with tempfile.TemporaryDirectory() as t:
        workers.DIR = Path(t)
        c = argv("claude", "claude", False, "", "c1")
        assert c[:3] == ["claude", "-p", "--output-format"] and "--resume" not in c and "acceptEdits" in c, c
        assert c[c.index("--permission-prompt-tool") + 1] == "mcp__knowledge_approval__ask" and c.index("--allowedTools") > c.index("--mcp-config"), c
        mcp = json.loads(Path(c[c.index("--mcp-config") + 1]).read_text(encoding="utf-8"))["mcpServers"]["knowledge_approval"]
        assert mcp["args"][0].endswith("approval_mcp.py") and mcp["env"]["KNOWLEDGE_CHAT"] == "c1", mcp
        c = argv("claude", "claude", True, "s-1")
        assert c[c.index("--resume") + 1] == "s-1" and "--dangerously-skip-permissions" in c and "acceptEdits" not in c, c
        x = argv("codex", "codex", False, "")
        assert x == ["codex", "app-server", "-c", workers.CODEX_APPROVE], x
        x = argv("codex", "codex", True, "th-1")
        assert x[:3] == ["codex", "exec", "resume"] and x[-2:] == ["th-1", "-"] and "--dangerously-bypass-approvals-and-sandbox" in x, x
        g = argv("agy", "agy", True, "cv-1")
        assert g[-2:] == ["-p", ""] and g[g.index("--conversation") + 1] == "cv-1" and "--dangerously-skip-permissions" in g, g
        assert argv("agy", "agy", False, "cv-1") == ["agy", "acp"], "without YOLO agy runs over ACP, where it can ask"
        assert not [a for a in c + x + g if "\n" in a or "%" in a or '"' in a], "nothing a .cmd shim would refuse"
    workers.DIR = w
    assert json.loads(stdin_for("agy", 'a "b"\nc')) == {"event": "user", "message": {"role": "user", "content": 'a "b"\nc'}}
    st = {}
    lines = ['{"type":"system","subtype":"init","session_id":"S1"}',
             '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}}',
             '{"type":"assistant","message":{"content":[{"type":"text","text":"Hi"},{"type":"tool_use","name":"Bash","input":{"command":"git  status"}}]}}',
             '{"type":"result","is_error":false,"session_id":"S1","permission_denials":[{"tool_name":"Bash","tool_input":{"command":"rm x"}}]}']
    ev = [e for ln in lines for e in parse("claude", ln, st)]
    assert ev == [("text", "Hi"), ("tool", "Bash: git status"), ("refused", "refused: Bash rm x")] and st["session"] == "S1", ev
    st = {}
    lines = ['{"type":"thread.started","thread_id":"T1"}', '{"type":"item.started","item":{"type":"command_execution","command":"ls"}}',
             '{"type":"item.completed","item":{"type":"command_execution","command":"ls","exit_code":1,"aggregated_output":"denied\\n"}}',
             '{"type":"item.completed","item":{"type":"agent_message","text":"Done"}}', '{"type":"turn.completed"}']
    ev = [e for ln in lines for e in parse("codex", ln, st)]
    assert ev == [("tool", "$ ls"), ("refused", "exit 1: denied"), ("text", "Done\n\n")] and st == {"session": "T1", "result": True}, ev
    st = {}
    lines = ['{"event":"init","conversation_id":"C1"}',
             '{"event":"step_update","step_update":{"conversation_id":"C1","step_type":"tool","state":"ACTIVE","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"ls"}}}}',
             '{"event":"step_update","step_update":{"conversation_id":"C1","step_type":"tool","state":"ERROR","tool_name":"run_command","tool_info":{"error":{"message":"user denied\\nmore"}}}}',
             '{"event":"step_update","step_update":{"conversation_id":"C1","step_type":"agent_response","text_delta":"42"}}',
             '{"event":"result","result":{"conversation_id":"C1","status":"SUCCESS"}}', "jetski: plain text"]
    ev = [e for ln in lines for e in parse("agy", ln, st)]
    assert ev == [("tool", "run_command: ls"), ("refused", "run_command: user denied"), ("text", "42"), ("info", "jetski: plain text")], ev
    assert st["session"] == "C1" and st["result"]
    assert merged([("text", "a"), ("text", "b"), ("tool", "x"), ("text", "c")]) == [
        {"kind": "text", "text": "ab"}, {"kind": "tool", "text": "x"}, {"kind": "text", "text": "c"}]
    # What needs no approval, and what may be allowed once at most.
    f = "/home/x/garden"
    for inp, exp in [({"command": "npm test"}, False), ({"command": "git push origin x"}, True), ({"command": "cat /etc/passwd"}, True),
                     ({"command": "ls ../other"}, True), ({"command": "ls ./sub > /dev/null"}, False), ({"file_path": "/home/x/garden/a"}, False),
                     ({"file_path": "/home/x/other"}, True), ({"command": "cat ~/.ssh/id"}, True), ({"command": ["bash", "-lc", "git push"]}, True)]:
        assert once_only(inp, f) == exp, (inp, exp)
    assert auto_allowed("execute", "run_command", {"CommandLine": "git status"}, f)
    assert not auto_allowed("execute", "run_command", {"CommandLine": "git status; rm -rf x"}, f)
    assert not auto_allowed("execute", "run_command", {"CommandLine": "git push"}, f)
    assert auto_allowed("edit", "write_file", {"Path": "/home/x/garden/a"}, f) and not auto_allowed("edit", "write_file", {"Path": "/tmp/a"}, f)
    assert not auto_allowed("edit", "write_file", {"file_path": "/etc/x"}, f) and auto_allowed("edit", "write_file", {"file_path": "/home/x/garden/a"}, f)
    # ask(): a chat rule answers at once; else the server is asked and the owner's answer (or a stop) ends the wait.
    asked, logged = [], []
    real = (globals()["ASK_SERVER"], globals()["LOG"])
    globals().update(ASK_SERVER=lambda b: asked.append(b) or {"id": f"a{len(asked)}", "timeout": 5}, LOG=lambda *a: logged.append(a))
    RUNNING["c9"] = {"stop": False, "folder": f, "allow": [{"tool": "Bash", "key": "make"}]}
    try:
        assert ask("c9", "Bash", {"command": "make"}) == ("chat", "") and not asked
        threading.Timer(0.2, lambda: answer({"ask": "a1", "decision": "chat", "by": "alice"})).start()
        assert ask("c9", "Bash", {"command": "npm test"})[0] == "chat" and asked[0]["once_only"] is False and asked[0]["detail"] == "npm test"
        assert ask("c9", "Bash", {"command": "npm test"})[0] == "chat" and len(asked) == 1, "allowed for this chat: not asked again"
        threading.Timer(0.2, lambda: answer({"ask": "a2", "decision": "deny", "by": "", "expired": True})).start()
        d, msg = ask("c9", "Bash", {"command": "git push"})
        assert d == "deny" and "in time" in msg and asked[1]["once_only"], (d, msg)
        threading.Timer(0.2, lambda: stop("c9")).start()
        RUNNING["c9"]["pid"] = 0
        assert ask("c9", "Bash", {"command": "rm x"})[0] == "deny" and not WAITS
    finally:
        globals().update(ASK_SERVER=real[0], LOG=real[1])
        RUNNING.pop("c9", None)
    print("ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        sys.exit(__doc__)
