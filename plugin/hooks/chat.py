#!/usr/bin/env python3
"""Chats from the web UI: the owner talks to an agent on this pc as in a terminal, the companion runs each turn.

The companion pulls a chat's next message from the server (GET /machine/chats), runs one headless turn of the agent in
the chat's folder (a folder linked to a project here, nothing else) and posts what the agent prints back in small
batches (POST /machine/chats/events). The first turn starts a new conversation, every later turn continues it the
agent's own way: Claude `--resume <session>`, Codex `codex exec resume <thread>`, agy `--conversation <id>`. The message
goes in on stdin, never on the command line (a .cmd shim would hand it to cmd.exe). Permissions are a worker's: the
agent's YOLO flag when YOLO is on here, else the same narrow sets (workers.py); a refused tool call shows as a line.

  chat.py --self-test   the argv and the stream parsing per agent
Standard library only; Linux, macOS and Windows.
"""
import json
import os
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

RUNNING: dict = {}  # chat id -> {pid, ident, stop}: the turns this companion runs now
MAX_TURNS = 4  # turns at once on one pc
FLUSH = 0.7  # seconds: output is posted in batches at most this often


def argv(agent: str, exe: str, yolo: bool, session: str) -> list:
    """One turn's command line (flags checked against claude 2.1, codex-cli 0.164, agy 1.4.12); the message is stdin."""
    if agent == "claude":
        return [exe, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                *(["--resume", session] if session else []),
                *(["--dangerously-skip-permissions"] if yolo else ["--permission-mode", "acceptEdits", "--allowedTools", *workers.CLAUDE_ALLOWED])]
    if agent == "codex":  # `exec resume` takes no --sandbox: the config key does the same (a bare word is a TOML string to it)
        perms = ["--dangerously-bypass-approvals-and-sandbox"] if yolo else ["-c", "sandbox_mode=workspace-write", "-c", workers.CODEX_APPROVE]
        return [exe, "exec", *(["resume"] if session else []), *perms, "--skip-git-repo-check", "--json", *([session] if session else []), "-"]
    return [exe, "--input-format", "stream-json", "--output-format", "stream-json",
            *(["--dangerously-skip-permissions"] if yolo else workers.agy_rules()), *(["--conversation", session] if session else []), "-p", ""]


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


def merged(events: list) -> list:
    """Neighbouring text pieces as one event: fewer, larger events on the server."""
    out = []
    for k, t in events:
        if out and k == "text" and out[-1]["kind"] == "text":
            out[-1]["text"] += t
        else:
            out.append({"kind": k, "text": t})
    return out


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
        cmd = argv(agent, exe, yolo(agent), st["session"])
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
            try:
                p.stdin.write(stdin_for(agent, item.get("text", "")))
                p.stdin.close()
            except OSError:  # it ended before reading: its exit code and stderr say why
                pass
            for raw in p.stdout:
                buf += parse(agent, raw.decode("utf-8", "replace"), st)
                if time.time() - last >= FLUSH:
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
    workers.kill(rec, hard=True)
    return True


def start(item: dict, post, log, linked, yolo):
    if len(RUNNING) >= MAX_TURNS:
        post({"chat": item["chat"], "events": [{"kind": "error", "text": f"{MAX_TURNS} chat turns run on this pc already; send again later"}],
              "done": True, "error": "busy"})
        return
    RUNNING[item["chat"]] = {"pid": 0, "ident": "", "stop": False}  # taken: the server's next "busy" list must not call it lost
    threading.Thread(target=run_turn, args=(item, post, log, linked, yolo), daemon=True).start()


def self_test():
    w = workers.DIR
    with tempfile.TemporaryDirectory() as t:
        workers.DIR = Path(t)
        c = argv("claude", "claude", False, "")
        assert c[:3] == ["claude", "-p", "--output-format"] and "--resume" not in c and "acceptEdits" in c, c
        c = argv("claude", "claude", True, "s-1")
        assert c[c.index("--resume") + 1] == "s-1" and "--dangerously-skip-permissions" in c and "acceptEdits" not in c, c
        x = argv("codex", "codex", False, "")
        assert x[:2] == ["codex", "exec"] and x[-1] == "-" and "sandbox_mode=workspace-write" in x and "resume" not in x, x
        x = argv("codex", "codex", True, "th-1")
        assert x[:3] == ["codex", "exec", "resume"] and x[-2:] == ["th-1", "-"] and "--dangerously-bypass-approvals-and-sandbox" in x, x
        g = argv("agy", "agy", False, "cv-1")
        assert g[-2:] == ["-p", ""] and g[g.index("--conversation") + 1] == "cv-1" and "accept-edits" in g, g
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
    print("ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        sys.exit(__doc__)
