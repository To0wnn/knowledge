#!/usr/bin/env python3
"""Failures of the knowledge hooks themselves: kept in ~/.knowledge/hook-errors.jsonl and reported to the server
(POST /status) from the per-turn hook, so the Tasks tab shows a device whose hooks break instead of failing quietly.

  hook_status.py run <script> [ARGS]   run a hook script (py.sh does, for Claude Code) and record a crash or a
                                       non-zero exit; the exit code and output pass through unchanged
Standard library only; recording or sending never fails a hook.
"""
import json
import os
import runpy
import sys
import time
import traceback
import urllib.request
from pathlib import Path

ERRORS = Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge") / "hook-errors.jsonl"
SENT = Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge") / "hook-status.json"  # {"at": last try, "sent": "at" of the newest error sent}
SEND_EVERY = 120  # seconds between two sends: a turn costs one small request at most this often
KEEP = 100  # lines kept in the local file


def record(hook: str, code, stderr: str, event: str = "", source: str = "knowledge"):
    try:
        ERRORS.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"hook": hook[:80], "event": event[:40], "code": str(code)[:40], "stderr": stderr[-2000:],
                           "at": time.time(), "source": source})
        with os.fdopen(os.open(ERRORS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as f:
            f.write(line + "\n")  # created 0600 like the other ~/.knowledge files: stderr can name local paths
        if ERRORS.stat().st_size > 400_000:  # capped: a hook that fails every turn must not fill the disk
            lines = ERRORS.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP:]
            ERRORS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def record_exception(hook: str, e: BaseException, event: str = ""):
    """The exception line first: the Tasks tab shows the first line."""
    record(hook, type(e).__name__, "".join(traceback.format_exception_only(type(e), e)).strip() + "\n"
           + "".join(traceback.format_exception(type(e), e, e.__traceback__)), event)


def unsent() -> tuple:
    try:
        state = json.loads(SENT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    try:
        lines = ERRORS.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], state
    out = []
    for ln in lines[-KEEP:]:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("at", 0) > state.get("sent", 0):
            out.append(e)
    return out[-20:], state


def send(url: str, tok: str, agent: str, plugin: str, now=None) -> bool:
    """POST the errors not sent yet, at most every SEND_EVERY seconds; a failed send keeps them for the next try."""
    now = time.time() if now is None else now
    errors, state = unsent()
    if not errors or not tok or now - state.get("at", 0) < SEND_EVERY:
        return False
    ok = False
    try:
        req = urllib.request.Request(f"{url}/status", json.dumps({"agent": agent, "plugin": plugin, "errors": errors}).encode(),
                                     {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:  # the tasks hook already may wait 4 s of its 8
            ok = r.status == 200
    except Exception:  # away from home or a server without /status: silent, the next turn after SEND_EVERY tries again
        pass
    try:
        SENT.parent.mkdir(parents=True, exist_ok=True)
        SENT.write_text(json.dumps({"at": now, "sent": max(e.get("at", 0) for e in errors) if ok else state.get("sent", 0)}),
                        encoding="utf-8")
    except OSError:
        pass
    return ok


def run(argv: list):
    """Run a hook script as if started directly; exit 2 is a hook's way to block or wake, not a failure."""
    script = argv[0]
    name = " ".join([Path(script).name] + argv[1:2])
    sys.argv = list(argv)
    sys.path[0] = str(Path(script).resolve().parent)
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        if e.code not in (None, 0, 2):
            record(name, e.code if isinstance(e.code, int) else 1, "" if isinstance(e.code, int) else str(e.code))
        raise
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        record_exception(name, e)
        print(f"knowledge hook {name} failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"] and len(sys.argv) > 2:
        run(sys.argv[2:])
    else:
        sys.exit(__doc__)
