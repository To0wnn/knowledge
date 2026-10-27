#!/usr/bin/env python3
"""Usage-limit ladder for Claude Code, Codex and agy: says once per session and limit window that 50, 75 or 90% of the
agent's usage limit is used, and refuses new subagents from 95% on. Each agent's own numbers:
- Claude Code gives them only to its status line, so `limits.py statusline` sits in front of the configured one
  (put there by `limits.py install` at session start), keeps rate_limits in ~/.knowledge/limits-claude.json and passes
  everything on unchanged;
- Codex writes rate_limits into its rollout (token_count events);
- agy answers GetUserStatus on its language server (at the address its register step stored).
Unknown numbers mean: say nothing, allow everything. Standard library only; never fails a session.
  limits.py statusline   status line command (Claude Code)
  limits.py install      SessionStart (Claude Code): put the status line tap in front of the configured one, once
  limits.py --self-test  run the checks of this file
"""
from __future__ import annotations  # X | None on Python 3.9 (macOS's /usr/bin/python3)

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RUNGS = (50, 75, 90, 95)
GATE = 95  # from here on new subagents are refused
STATE = Path.home() / ".knowledge"
CLAUDE_FILE = STATE / "limits-claude.json"
SAID = STATE / "limits-said.json"
ORIGINAL = STATE / "statusline-original.json"  # the status line command the tap runs (and the way back)
TAP = STATE / "limits.py"  # a copy: the plugin folder changes with every version, the status line command must not
ADVICE = {50: "Just so you know; carry on.",
          75: "Work economically: no new subagents unless clearly needed, read only what you need.",
          90: "Tell the user in one line, finish the current step and keep a short hand-over note ready in case the "
              "limit runs out.",
          95: "New subagents are refused until it resets; tell the user in one line."}


def worst(windows: list, now: float) -> tuple | None:
    """(percent, window, resets_at) of the fullest window that has not reset yet; windows are (name, percent, resets_at)."""
    live = [(float(p), n, r) for n, p, r in windows if isinstance(p, (int, float)) and not isinstance(p, bool)
            and (not r or r > now)]
    return max(live, default=None)


def label(minutes) -> str:
    m = int(minutes or 0)
    return f"{m // 1440}d" if m >= 1440 else f"{m // 60}h" if m else "?"


def claude_used(now: float) -> tuple | None:
    d = json.loads(CLAUDE_FILE.read_text(encoding="utf-8"))
    if now - d.get("at", 0) > 900:  # the status line has not run for a while (claude -p has none): too old to act on
        return None
    rl = d.get("rate_limits") or {}
    return worst([(n.replace("five_hour", "5h").replace("seven_day", "7d"), v.get("used_percentage"), v.get("resets_at"))
                  for n, v in rl.items() if isinstance(v, dict)], now)


def codex_used(transcript: str, now: float) -> tuple | None:
    with open(transcript, "rb") as f:  # the newest event is at the end; a rollout can be tens of MB
        f.seek(max(0, f.seek(0, 2) - 512_000))
        tail = f.read().decode("utf-8", "replace")
    for line in reversed(tail.splitlines()):
        if '"rate_limits"' in line:
            try:
                rl = (json.loads(line).get("payload") or {}).get("rate_limits") or {}
            except ValueError:
                continue
            return worst([(label(v.get("window_minutes")), v.get("used_percent"), v.get("resets_at"))
                          for v in (rl.get("primary"), rl.get("secondary")) if isinstance(v, dict)], now)
    return None


def epoch(stamp) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def plain(text) -> str:
    return "".join(c for c in str(text).lower() if c.isalnum())


def agy_used(status: dict, now: float, model: str = "") -> tuple | None:
    """The quota of the model this session runs on (hook field modelName, e.g. gemini-3.8-flash-medium, against labels
    such as "Gemini 3.8 Flash"); without a match the models whose quota is in use, the one closest to its limit."""
    configs = ((status.get("userStatus") or {}).get("cascadeModelConfigData") or {}).get("clientModelConfigs") or []
    mine = sorted((c for c in configs if model and plain(c.get("label")) and plain(c.get("label")) in plain(model)),
                  key=lambda c: len(plain(c.get("label"))))
    configs = mine[-1:] if mine else configs  # shortcut: label format seen only in the docs, not from a live agy yet
    return worst([(c.get("label", "?"), round((1 - q["remainingFraction"]) * 100), epoch(q.get("resetTime")))
                  for c in configs if isinstance(q := c.get("quotaInfo"), dict)
                  and isinstance(q.get("remainingFraction"), (int, float)) and q["remainingFraction"] < 1], now)


def used(agent: str, hook: dict, agy_status=None, now: float | None = None) -> tuple | None:
    now = time.time() if now is None else now
    try:
        if agent == "claude":
            return claude_used(now)
        if agent == "codex":
            return codex_used(hook["transcript_path"], now)
        return agy_used(agy_status(), now, hook.get("modelName", "")) if agy_status else None
    except Exception:  # no status line yet, no rollout, agy not registered: no numbers, no verdict
        return None


def when(resets_at) -> str:
    if not resets_at:
        return ""
    t = time.localtime(resets_at)
    return f", resets {time.strftime('%H:%M' if resets_at - time.time() < 86400 else '%d %b %H:%M', t)}"


def notice(agent: str, hook: dict, agy_status=None, now: float | None = None) -> str:
    """The highest rung reached that this session has not heard in this window, or ''."""
    now = time.time() if now is None else now
    got = used(agent, hook, agy_status, now)
    if not got:
        return ""
    pct, window, resets = got
    rung = max((r for r in RUNGS if pct >= r), default=0)
    sid = str(hook.get("session_id") or hook.get("conversationId") or "")
    try:
        said = json.loads(SAID.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        said = {}
    key = f"{agent}:{sid}"
    mine = said.get(key) or {}
    if not rung or (mine.get("window") == resets and mine.get("rung", 0) >= rung):
        return ""
    said = {k: v for k, v in said.items() if now - v.get("at", 0) < 8 * 86400}
    said[key] = {"window": resets, "rung": rung, "at": now}
    tmp = SAID.with_suffix(f".{os.getpid()}.tmp")
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(said), encoding="utf-8")
        os.replace(tmp, SAID)  # a reader never sees half a file
    except OSError:  # unsaid next time would repeat it every turn: better to stay quiet
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return ""
    return (f"knowledge: {pct:.0f}% of your {agent} usage limit is used ({window} window{when(resets)}). "
            f"{ADVICE[rung]}")


def gate(agent: str, hook: dict, agy_status=None) -> str:
    """PreToolUse on the subagent tool: the agent's deny JSON from GATE% on, else '' (no say: agy reads even {} as deny,
    tested live with agy 1.4.8 on 27 Oct 2026)."""
    got = used(agent, hook, agy_status)
    if not got or got[0] < GATE:
        return ""
    reason = (f"knowledge: {got[0]:.0f}% of the {agent} usage limit is used ({got[1]} window{when(got[2])}); no new "
              "subagents until it resets. Do the step yourself, or stop and tell the user.")
    if agent == "agy":
        return json.dumps({"decision": "deny", "reason": reason})
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}})


def statusline():
    """Claude Code's status line command: keep rate_limits, then run the configured status line on the same input."""
    raw = sys.stdin.buffer.read()  # bytes: passed on as they came (a Windows pipe would decode as cp1252)
    try:
        rl = json.loads(raw).get("rate_limits")
        if rl:
            STATE.mkdir(parents=True, exist_ok=True)
            tmp = CLAUDE_FILE.with_suffix(f".{os.getpid()}.tmp")  # three status lines may write at once
            tmp.write_text(json.dumps({"at": time.time(), "rate_limits": rl}), encoding="utf-8")
            os.replace(tmp, CLAUDE_FILE)
    except Exception:  # never cost the user their status line
        pass
    try:
        cmd = json.loads(ORIGINAL.read_text(encoding="utf-8")).get("command")
    except (OSError, ValueError):
        cmd = None
    if cmd:  # Claude Code runs status line commands in a shell (Git Bash on Windows)
        shell = shutil.which("bash") or shutil.which("sh")
        argv = [shell, "-c", cmd] if shell else cmd
        r = subprocess.run(argv, input=raw, capture_output=True, shell=not shell, timeout=10)
        sys.stdout.buffer.write(r.stdout)


def install(config: Path) -> str:
    """Put the tap in front of the configured status line (or as the status line when there is none), once; a status
    line changed later (another plugin, the user) is wrapped again. Returns a line for the session context or ''."""
    STATE.mkdir(parents=True, exist_ok=True)
    me = Path(__file__).resolve()
    if me != TAP.resolve() and (not TAP.exists() or TAP.read_bytes() != me.read_bytes()):
        shutil.copyfile(me, TAP)
    settings = (config / "settings.json").resolve()  # a symlinked settings.json (dotfiles) stays a link
    data = json.loads(settings.read_text(encoding="utf-8-sig")) if settings.exists() else {}
    current = data.get("statusLine") or {}
    ours = f'"{sys.executable}" "{TAP}" statusline'
    command = str(current.get("command", ""))
    wrapped = "limits.py\" statusline" in command
    # Only a Python that is gone gets replaced: another live one (a venv, pyenv per folder) would rewrite settings.json
    # at every session start.
    if wrapped and Path(command.split('"')[1] if command.startswith('"') else command.split(" ")[0]).exists():
        return ""
    if not wrapped:  # shortcut: one saved original per user, not per CLAUDE_CONFIG_DIR; key it by config dir if needed
        ORIGINAL.write_text(json.dumps({"command": current.get("command"), "was": current or None}), encoding="utf-8")
    # already wrapped with another Python (an upgrade removed the old one): only the interpreter changes
    data["statusLine"] = {**current, "type": "command", "command": ours}
    tmp = settings.with_name(f"{settings.name}.knowledge-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, settings)
    if wrapped:
        return ""
    return ("knowledge plugin: the status line now also hands the usage limit to the plugin (your own status line "
            "still shows); the way back is in ~/.knowledge/statusline-original.json.")


def self_test():
    import tempfile
    global STATE, CLAUDE_FILE, SAID, ORIGINAL, TAP
    now = 1_800_000_000
    assert worst([("5h", 40, now + 60), ("7d", 80, now + 600), ("old", 99, now - 1), ("x", True, None)], now) == (80, "7d", now + 600)
    with tempfile.TemporaryDirectory() as t:
        STATE = Path(t)
        CLAUDE_FILE, SAID, ORIGINAL, TAP = STATE / "c.json", STATE / "s.json", STATE / "o.json", STATE / "limits.py"
        CLAUDE_FILE.write_text(json.dumps({"at": now, "rate_limits": {"five_hour": {"used_percentage": 77, "resets_at": now + 3600}}}))
        say = lambda sid, n=now: notice("claude", {"session_id": sid}, now=n)
        assert "77% of your claude usage limit" in say("a") and "economically" in say("b") and say("a") == ""
        CLAUDE_FILE.write_text(json.dumps({"at": now, "rate_limits": {"five_hour": {"used_percentage": 96, "resets_at": now + 3600}}}))
        assert "refused" in say("a") and say("a") == ""
        CLAUDE_FILE.write_text(json.dumps({"at": now, "rate_limits": {"five_hour": {"used_percentage": 52, "resets_at": now + 9000}}}))
        assert "Just so you know" in say("a"), "a new window starts the ladder again"
        CLAUDE_FILE.write_text(json.dumps({"at": now - 1000, "rate_limits": {"five_hour": {"used_percentage": 99}}}))
        assert say("z") == "", "a stale status line says nothing"
        rollout = STATE / "r.jsonl"
        rollout.write_text(json.dumps({"payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 91.0, "window_minutes": 300, "resets_at": now + 60},
            "secondary": {"used_percent": 20.0, "window_minutes": 10080, "resets_at": now + 6000}}}}) + "\nnot json\n")
        assert used("codex", {"transcript_path": str(rollout)}, now=now) == (91.0, "5h", now + 60)
        assert used("codex", {"transcript_path": str(STATE / "missing")}, now=now) is None
        status = {"userStatus": {"cascadeModelConfigData": {"clientModelConfigs": [
            {"label": "Pro", "quotaInfo": {"remainingFraction": 0.2, "resetTime": "2027-02-01T08:00:00Z"}},
            {"label": "Flash", "quotaInfo": {"remainingFraction": 1}}, {"label": "None"}]}}}
        assert used("agy", {}, lambda: status, now=now) == (80, "Pro", epoch("2027-02-01T08:00:00Z"))
        assert used("agy", {}, lambda: 1 / 0, now=now) is None
        assert used("agy", {"modelName": "gemini-3.8-flash-medium"}, lambda: {"userStatus": {"cascadeModelConfigData": {"clientModelConfigs": [{"label": "Gemini 3.8 Flash", "quotaInfo": {"remainingFraction": 0.9}}, {"label": "Gemini 3.8 Pro", "quotaInfo": {"remainingFraction": 0.01}}]}}}, now=now) == (10, "Gemini 3.8 Flash", None), "the session's own model counts"
        assert json.loads(gate("agy", {}, lambda: {**status, "userStatus": {"cascadeModelConfigData": {"clientModelConfigs": [
            {"label": "Pro", "quotaInfo": {"remainingFraction": 0.01, "resetTime": "2099-01-01T00:00:00Z"}}]}}}))["decision"] == "deny"
        assert gate("agy", {}, lambda: status) == "" and gate("codex", {"transcript_path": "missing"}) == ""
        config = STATE / "claude"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({"theme": "dark", "statusLine": {"type": "command", "command": "echo hi", "padding": 1}}))
        assert install(config) and install(config) == "", "wrapped once"
        s = json.loads((config / "settings.json").read_text())
        s["statusLine"]["command"] = f'"/gone/python3" "{TAP}" statusline'
        (config / "settings.json").write_text(json.dumps(s))
        assert install(config) == "" and sys.executable in json.loads((config / "settings.json").read_text())["statusLine"]["command"]
        assert json.loads(ORIGINAL.read_text())["command"] == "echo hi", "a re-wrap keeps the user's original"
        other = f'"{shutil.which("sh") or sys.executable}" "{TAP}" statusline'  # another Python that still exists
        s = json.loads((config / "settings.json").read_text())
        s["statusLine"]["command"] = other
        (config / "settings.json").write_text(json.dumps(s))
        assert install(config) == "" and json.loads((config / "settings.json").read_text())["statusLine"]["command"] == other, \
            "a live interpreter stays: no rewrite at every session start from a venv"
        sl = json.loads((config / "settings.json").read_text())["statusLine"]
        assert sl["padding"] == 1 and "statusline" in sl["command"] and json.loads(ORIGINAL.read_text())["command"] == "echo hi"
    print("ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    elif sys.argv[1:] == ["statusline"]:
        statusline()
    elif sys.argv[1:] == ["install"]:
        try:
            line = install(Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"))
            if line:
                print(line)
        except Exception as e:  # an unreadable settings file: say so, but never stop the session
            print(f"knowledge plugin: status line tap not installed ({type(e).__name__}: {e})", file=sys.stderr)
    else:
        sys.exit(__doc__)
