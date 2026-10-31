#!/usr/bin/env python3
"""PostToolUse on every tool, for every agent; returns at once unless the session is a worker or runs YOLO.

- A worker (KNOWLEDGE_WORKER) whose companion left a stop note (usage limit at 90%) hears it after its next tool call:
  hand over, then end. Claude Code and Codex read it as additionalContext; agy's PostToolUse may only answer {}, so it
  is parked for its PostInvocation hook (agent_hooks.py hints).
- A YOLO session (a YOLO worker, or an agent the companion turned YOLO on for) is watched, never stopped: a force
  push, rm -rf outside the project, curl|sh, reading key or token files, or reading the vault goes to the companion's
  activity log and once per session and kind to the server (one Matrix notice).

  watch.py <claude|codex|agy>     the hook (stdin: the agent's PostToolUse JSON)
  watch.py --self-test
Standard library only; never fails a turn.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

KN_HOME = Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge")
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


def main(agent: str):
    worker = os.environ.get("KNOWLEDGE_WORKER", "")
    yolo = yolo_on(agent)
    if not (worker or yolo):
        print("{}")
        return
    hook = json.loads(sys.stdin.read() or "{}")
    sid = str(hook.get("session_id") or hook.get("conversationId") or "")
    cwd = hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0]
    if yolo:
        text = json.dumps(hook.get("tool_input") or hook.get("toolCall") or hook.get("toolInput") or {}, ensure_ascii=False)
        text = text.replace('\\"', '"').replace("\\\\", "\\")
        for kind, detail in risky(text, cwd):
            flag(agent, sid, kind, detail)
    note = ""
    if worker and (nonce := os.environ.get("KNOWLEDGE_START_NONCE", "")):  # its own note, not one of a namesake task
        stop = KN_HOME / "companion" / "workers" / (re.sub(r"[^\w.-]", "-", nonce)[:60] + ".stop")
        try:
            note = stop.read_text(encoding="utf-8")
        except OSError:
            pass
    if note and agent == "agy":
        with (KN_HOME / "pending-hints.txt").open("a", encoding="utf-8") as f:
            f.write(note + "\n")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}} if note and agent != "agy" else {}))


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
    print("ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    elif sys.argv[1:2] and sys.argv[1] in ("claude", "codex", "agy"):
        try:
            main(sys.argv[1])
        except Exception as e:  # watching must never cost a turn
            print("{}")
            print(f"knowledge watch skipped: {e}", file=sys.stderr)
    else:
        sys.exit(__doc__)
