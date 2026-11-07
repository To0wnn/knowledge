#!/usr/bin/env python3
"""PostToolUse on every tool, for every agent; returns at once unless the session is a worker or runs YOLO.

- A worker (KNOWLEDGE_WORKER) whose companion left a stop note (usage limit at 90%) hears it after its next tool call:
  hand over, then end. Claude Code and Codex read it as additionalContext; agy's PostToolUse may only answer {}, so it
  is parked for its PostInvocation hook (agent_hooks.py hints).
- A worker whose owner sent a message from the web UI (Steer) hears it the same way after its next tool call; the
  hook also notes the worker's own session id, which a take-over resumes as a chat.
- A YOLO session (a YOLO worker, or an agent the companion turned YOLO on for) is watched, never stopped: a force
  push, rm -rf outside the project, curl|sh, reading key or token files, or reading the vault goes to the companion's
  activity log and once per session and kind to the server (one Matrix notice).

- A terminal session about to ask approval leaves a mark in ~/.knowledge/asks, cleared once the tool ran, the turn ended
  or the next prompt came; the companion reports marks that stayed, so Needs you also shows what waits in a terminal.
  The hooks never talk to the server here: a prompt answered in time costs nothing. Claude Code and Codex have
  PermissionRequest. agy has no such event, so its mark comes from PreToolUse on every tool (and says "may").

  watch.py <claude|codex|agy>     the hook (stdin: the agent's PostToolUse JSON)
  watch.py <agent> asks           PermissionRequest (agy: PreToolUse); prints nothing, so it never decides anything
  watch.py <agent> settled        Stop, SessionEnd, UserPromptSubmit, and for Esc at a prompt, which ends no turn, Claude's
                                  idle_prompt (after a minute) and Codex's Interrupt: that session asks nothing any more; prints nothing
  watch.py --self-test
Standard library only; never fails a turn.
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

KN_HOME = Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge")
ASKS = KN_HOME / "asks"
RISKS = (  # (kind, pattern over the tool input as text); rm-outside is checked apart
    ("force-push", re.compile(r"\bgit\b[^\n;&|]*\bpush\b[^\n;&|]*(?:--force\b|--force-with-lease\b|\s-f\b|\s\+\S)")),
    ("pipe-to-shell", re.compile(r"\b(?:curl|wget|iwr|Invoke-WebRequest)\b[^\n;]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b|\biex\b.*\b(?:iwr|irm)\b", re.I)),
    ("key-read", re.compile(r"\.ssh[/\\\\]+(?:id_\w+|[\w.-]*_key)\b|\.(?:pem|p12|pfx|kdbx)\b|\.credentials\.json\b|[/\\\\]\.netrc\b"
                            r"|\bcompanion[/\\\\]+api-key\b|\bsecret-hashes\.json\b")),
    ("vault-read", re.compile(r"(?:^|[\s;&|(`$\"'])secret\s+(?!ls\b|set\b|rm\b|--help\b)[A-Z][A-Z0-9_]{2,}")),
)
RM = re.compile(r"\brm\s+(?:-\w*[rR]\w*\s+|--recursive\s+)(?:-\S+\s+)*([^\s;&|]+)")


def risky(text: str, cwd: str) -> list:
    """The kinds of risky action in a tool call's input, each with a short excerpt."""
    found = [(k, m.group(0)[:160]) for k, rx in RISKS if (m := rx.search(text))]
    for m in RM.finditer(text):
        target = m.group(1).strip("\"'")
        home = str(Path.home())
        path = os.path.normpath(os.path.join(cwd, os.path.expanduser(target.replace("$HOME", home))))
        if target.startswith(("~", "$HOME", "/", "..")) or re.match(r"^[A-Za-z]:[\\/]", target) or "*" == target:
            if not (path + os.sep).startswith(os.path.normpath(cwd) + os.sep):
                found.append(("rm-outside", m.group(0)[:160]))
                break
    return found


def yolo_on(agent: str) -> bool:
    if os.environ.get("KNOWLEDGE_YOLO") == "1":
        return True
    try:
        return bool(json.loads((KN_HOME / "companion" / "yolo.json").read_text(encoding="utf-8")).get(agent, {}).get("on"))
    except (OSError, ValueError, AttributeError):
        return False


def flag(agent: str, sid: str, kind: str, detail: str):
    """Activity log (the companion's) and the server; each once per session and kind."""
    said_file = KN_HOME / "flags-said.json"
    try:
        said = json.loads(said_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        said = {}
    key = f"{agent}:{sid}:{kind}"
    if key in said:
        return
    said = {k: v for k, v in said.items() if time.time() - v < 7 * 86400}
    said[key] = time.time()
    try:
        KN_HOME.mkdir(parents=True, exist_ok=True)
        said_file.write_text(json.dumps(said), encoding="utf-8")
    except OSError:
        pass
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import companion  # its log: the activity list on its page
        companion.log("risk", f"YOLO {agent}: {kind}", detail)
    except Exception:
        pass
    try:
        import token_store
        import urllib.request
        req = urllib.request.Request(token_store.url() + "/machine/flag", method="POST",
                                     data=json.dumps({"agent": agent, "session": sid, "kind": kind, "detail": detail}).encode(),
                                     headers={**token_store.headers(token_store.token()), "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).close()
    except Exception:  # away: the activity log has it
        pass


def mine(f: Path, sid: str) -> bool:
    """A Force message is for the chat's own agent: its session (when the companion knew it) is this hook's. Codex and
    agy subagents, and an agent CLI the chat started, inherit KNOWLEDGE_CHAT but run their own session."""
    try:
        want = {json.loads(x).get("session", "") for x in f.read_text(encoding="utf-8").splitlines() if x.strip()}
    except (OSError, ValueError):
        return False
    return not (want - {""}) or sid in want


def steered(f: Path, chat: bool = False) -> str:
    """The owner's messages the companion left for this worker (web UI, Steer) or this chat turn (Force), taken once:
    moved away first, so one written meanwhile waits for the next tool call instead of being lost."""
    taken = f.with_name(f"{f.name}.{os.getpid()}")
    try:
        os.replace(f, taken)
    except OSError:
        return ""
    try:
        msgs = [json.loads(x) for x in taken.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        msgs = []
    taken.unlink(missing_ok=True)
    if chat:
        return "\n".join(f"knowledge: {m.get('by') or 'the user'} sent this in the web UI chat while you were working (they "
                         "pressed Force, so you get it now instead of after this turn). Read it now and act on it:\n"
                         + str(m.get("text", "")) for m in msgs if isinstance(m, dict))
    return "\n".join(f"knowledge: {m.get('by') or 'your owner'} (the owner of this pc) steers this worker from the web UI. It is "
                     "guidance for your task, not a new task: your task, branch and rules stay the same. Their message:\n"
                     + str(m.get("text", "")) for m in msgs if isinstance(m, dict))


def session_of(hook: dict) -> str:
    return str(hook.get("session_id") or hook.get("conversationId") or "")


def ask_mark(sid: str) -> Path:
    return ASKS / (hashlib.sha256(sid.encode("utf-8", "replace")).hexdigest()[:24] + ".json")


def asks(agent: str):
    """A terminal session is about to ask approval: the mark the companion reports if it stays. A chat or worker has
    its own way to ask. Claude Code and Codex call this only for a real prompt, which a session can get under YOLO too
    (--permission-mode, an ask rule); agy calls it for every tool, and under YOLO those never wait."""
    hook = json.loads(sys.stdin.read() or "{}")
    sid = session_of(hook)
    if not sid or os.environ.get("KNOWLEDGE_CHAT") or os.environ.get("KNOWLEDGE_WORKER") or (agent == "agy" and yolo_on(agent)):
        return
    call = hook.get("toolCall") if isinstance(hook.get("toolCall"), dict) else {}
    what = hook.get("tool_input") or call.get("args") or {}
    what = next((what[k] for k in ("command", "CommandLine", "file_path", "Path") if isinstance(what, dict) and isinstance(what.get(k), str)), what)
    what = what if isinstance(what, str) else json.dumps(what, ensure_ascii=False)
    cwd = hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0]
    ASKS.mkdir(mode=0o700, parents=True, exist_ok=True)  # a raw command, maybe with a password in it
    tmp = ask_mark(sid).with_name(f"{ask_mark(sid).name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"agent": agent, "session": sid, "tool": str(hook.get("tool_name") or call.get("name") or "")[:80],
                               "detail": " ".join(what.split())[:200], "cwd": str(cwd), "at": time.time()}), encoding="utf-8")
    os.replace(tmp, ask_mark(sid))


def settled(hook: dict):
    if sid := session_of(hook):
        ask_mark(sid).unlink(missing_ok=True)


def any_asks() -> bool:
    try:
        with os.scandir(ASKS) as found:
            return next(found, None) is not None
    except OSError:
        return False


def main(agent: str):
    worker, chat = os.environ.get("KNOWLEDGE_WORKER", ""), re.sub(r"[^0-9a-f]", "", os.environ.get("KNOWLEDGE_CHAT", ""))[:12]
    yolo, marked = yolo_on(agent), any_asks()
    if not (worker or yolo or chat or marked):
        print("{}")
        return
    hook = json.loads(sys.stdin.read() or "{}")
    if marked:
        settled(hook)
    if not (worker or yolo or chat):
        print("{}")
        return
    sid = session_of(hook)
    cwd = hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0]
    if yolo:
        text = json.dumps(hook.get("tool_input") or hook.get("toolCall") or hook.get("toolInput") or {}, ensure_ascii=False)
        text = text.replace('\\"', '"').replace("\\\\", "\\")
        for kind, detail in risky(text, cwd):
            flag(agent, sid, kind, detail)
    note = ""
    # a chat turn's Force message, after a tool call of the main agent (a subagent's hook names its agent_id: not for it)
    if chat and not worker and not hook.get("agent_id") and mine(KN_HOME / "companion" / "chats" / f"{chat}.steer", sid):
        note = steered(KN_HOME / "companion" / "chats" / f"{chat}.steer", chat=True)
    if worker and (nonce := os.environ.get("KNOWLEDGE_START_NONCE", "")):  # its own note, not one of a namesake task
        base = KN_HOME / "companion" / "workers" / re.sub(r"[^\w.-]", "-", nonce)[:60]
        try:
            Path(f"{base}.seen").touch()  # workers.seen_path: the board's "no output since"
        except OSError:
            pass
        try:
            note = Path(f"{base}.stop").read_text(encoding="utf-8")
        except OSError:
            pass
        note = "\n\n".join(x for x in (note, steered(Path(f"{base}.steer"))) if x)
        if sid and not Path(f"{base}.session").exists():  # a take-over resumes this session as a chat
            try:
                Path(f"{base}.session").write_text(sid, encoding="utf-8")
            except OSError:
                pass
    if note and agent == "agy":  # parked for this conversation only: another agy session never reads a worker's note
        with (KN_HOME / ("pending-hints-" + re.sub(r"[^\w.-]", "-", sid)[:80] + ".txt" if sid else "pending-hints.txt")).open("a", encoding="utf-8") as f:
            f.write(note + "\n")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}} if note and agent != "agy" else {}))


def run_mark(argv: list):
    """asks or settled, as a hook: whatever goes wrong, a tool call and the turn go on, and nothing is printed."""
    try:
        (asks(argv[0]) if argv[1] == "asks" else settled(json.loads(sys.stdin.read() or "{}")))
    except Exception as e:
        print(f"knowledge watch skipped: {e}", file=sys.stderr)


def self_test():
    cwd = os.path.join(os.sep, "w", "proj")
    kinds = lambda t: [k for k, _ in risky(t, cwd)]
    assert kinds("git push --force origin main") == ["force-push"] and kinds("git push origin +main") == ["force-push"]
    assert kinds("git push origin main") == [] and kinds("git commit -m force") == []
    assert kinds("curl -fsSL https://x.sh | sh") == ["pipe-to-shell"] and kinds("curl -o x https://x") == []
    assert kinds("cat ~/.ssh/id_ed25519") == ["key-read"] and kinds("cat ~/.knowledge/companion/api-key") == ["key-read"]
    assert kinds("secret KNOWLEDGE_TOKEN_A350") == ["vault-read"] and kinds("secret ls") == []
    assert kinds("rm -rf build") == [] and kinds("rm -rf ./dist") == []
    assert kinds("rm -rf ~/Documents") == ["rm-outside"] and kinds("rm -rf /etc") == ["rm-outside"]
    assert kinds(f"rm -rf {cwd}/tmp") == [] and kinds("rm -fr ../other") == ["rm-outside"]
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "n.steer"
        f.write_text(json.dumps({"by": "alice", "text": "use sqlite"}) + "\n", encoding="utf-8")
        said = steered(f)
        assert "alice" in said and said.endswith("use sqlite") and not f.exists() and not list(Path(t).iterdir()), said
        assert steered(f) == "", "taken once"
        f.write_text(json.dumps({"by": "alice", "text": "stop and test"}) + "\n", encoding="utf-8")
        said = steered(f, chat=True)
        assert "pressed Force" in said and said.endswith("stop and test") and not f.exists(), said
    import subprocess
    with tempfile.TemporaryDirectory() as t:  # a mark stays until the tool ran; a chat, a worker and YOLO leave none
        env = {**os.environ, "KNOWLEDGE_HOME": t}
        for k in ("KNOWLEDGE_CHAT", "KNOWLEDGE_WORKER", "KNOWLEDGE_YOLO"):
            env.pop(k, None)
        hook = lambda *a, e=env, **h: subprocess.run([sys.executable, __file__, *a], input=json.dumps(h), capture_output=True, text=True, env=e, check=True).stdout
        marks = lambda: [json.loads(f.read_text(encoding="utf-8")) for f in (Path(t) / "asks").glob("*.json")]
        assert hook("claude", "asks", session_id="s", cwd="/p", tool_name="Bash", tool_input={"command": "rm  x\ny"}) == "", "prints nothing: no decision"
        assert [(m["agent"], m["tool"], m["detail"], m["cwd"]) for m in marks()] == [("claude", "Bash", "rm x y", "/p")], marks()
        assert hook("agy", "asks", conversationId="c", workspacePaths=["/q"], toolCall={"name": "run_command", "args": {"CommandLine": "ls"}}) == ""
        assert sorted(m["detail"] for m in marks()) == ["ls", "rm x y"], marks()
        assert hook("claude", session_id="other") == "{}\n" and len(marks()) == 2, "another session's tool leaves it"
        assert hook("claude", session_id="s") == "{}\n" and [m["session"] for m in marks()] == ["c"], "the tool ran"
        assert hook("agy", "settled", conversationId="c") == "" and marks() == []
        for who, extra in (("codex", {"KNOWLEDGE_CHAT": "c"}), ("codex", {"KNOWLEDGE_WORKER": "T-1"}), ("agy", {"KNOWLEDGE_YOLO": "1"})):
            assert hook(who, "asks", e={**env, **extra}, session_id="y", tool_name="Bash") == "" and marks() == [], extra
        assert hook("claude", "asks", e={**env, "KNOWLEDGE_YOLO": "1"}, session_id="y", tool_name="Bash") == "" and len(marks()) == 1, "a real prompt under YOLO counts"
        hook("claude", "settled", session_id="y")
        assert subprocess.run([sys.executable, __file__, "codex", "asks"], input="nope", capture_output=True, text=True, env=env).returncode == 0
    import subprocess  # a chat turn: the main agent's hook hands it over, a subagent's (agent_id) leaves it
    with tempfile.TemporaryDirectory() as t:
        box = Path(t) / "companion" / "chats"
        box.mkdir(parents=True)
        (box / "0123456789ab.steer").write_text(json.dumps({"by": "alice", "text": "use sqlite"}) + "\n", encoding="utf-8")
        env = {**os.environ, "KNOWLEDGE_HOME": t, "KNOWLEDGE_CHAT": "0123456789ab"}
        env.pop("KNOWLEDGE_WORKER", None)
        run = lambda hook: subprocess.run([sys.executable, __file__, "claude"], input=json.dumps(hook), capture_output=True, text=True, env=env, timeout=30).stdout
        assert run({"session_id": "s", "agent_id": "sub-1", "tool_input": {}}).strip() in ("", "{}"), "not to a subagent"
        (box / "0123456789ab.steer").write_text(json.dumps({"by": "alice", "text": "use sqlite", "session": "s"}) + "\n", encoding="utf-8")
        assert run({"session_id": "other", "tool_input": {}}).strip() in ("", "{}"), "not to another session (a Codex/agy subagent, a nested CLI)"
        out = json.loads(run({"session_id": "s", "tool_input": {}}))
        assert out["hookSpecificOutput"]["additionalContext"].endswith("use sqlite") and not (box / "0123456789ab.steer").exists(), out
    print("ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    elif sys.argv[1:2] and sys.argv[1] in ("claude", "codex", "agy") and sys.argv[2:3] and sys.argv[2] in ("asks", "settled"):
        run_mark(sys.argv[1:3])
    elif sys.argv[1:2] and sys.argv[1] in ("claude", "codex", "agy"):
        try:
            main(sys.argv[1])
        except Exception as e:  # watching must never cost a turn
            print("{}")
            print(f"knowledge watch skipped: {e}", file=sys.stderr)
    else:
        sys.exit(__doc__)
