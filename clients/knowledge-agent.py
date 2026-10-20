#!/usr/bin/env python3
"""The knowledge plugin's hooks for agents other than Claude Code: agy (Antigravity CLI) and Codex.

Linux, macOS and Windows; standard library only. The token comes from KNOWLEDGE_TOKEN (TOKEN "-") or, on the owner's VM,
from the vault (TOKEN = a vault name). The session log falls back to ~/.config/knowledge-server/session.env, so the
agy/Codex tokens on the VM stay read-only (install always gives session-log "-").

  knowledge-agent.py install <agy|codex> [TOKEN]   write the agent's hooks.json for this machine (old file kept as .bak)
  knowledge-agent.py context <agy|codex> TOKEN     SessionStart: startup context + docs mirror, as the agent's JSON
  knowledge-agent.py docs-sync TOKEN               PostToolUse: mirror an edited .md (folder with .knowledge-project)
  knowledge-agent.py session-log TOKEN             SessionEnd/PreCompact (Codex), Stop (agy): post the session log
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "plugin" / "hooks"
URL = os.environ.get("KNOWLEDGE_URL", "https://knowledge.to0wnn.com").rstrip("/")
CONFIG = {"agy": Path.home() / ".gemini" / "config" / "hooks.json", "codex": Path.home() / ".codex" / "hooks.json"}


def token(name: str) -> str:
    if name == "-":
        return os.environ.get("KNOWLEDGE_TOKEN", "")
    return subprocess.run([str(Path.home() / ".local/bin/secret"), name], capture_output=True, text=True, check=True).stdout.strip()


def hook_config(agent: str, name: str) -> dict:
    # Windows: the py launcher (C:\Windows\py.exe, no spaces): a quoted interpreter path would not run in PowerShell.
    exe, me = ("py" if os.name == "nt" and shutil.which("py") else sys.executable), str(Path(__file__).resolve())
    q = lambda s: f'"{s}"' if " " in s else s  # unquoted where possible: PowerShell does not run a quoted path
    # Full paths: Codex runs hooks with a bare PATH. -X utf8: Windows pipes are cp1252 otherwise.
    cmd = lambda *a: " ".join([q(exe), "-X", "utf8", q(me), *a, "-" if a == ("session-log",) else name])
    # agy CLI edits with write_file/edit_file (arg Path); the documented names (TargetFile) are the app's and IDE's.
    if agent == "agy":  # agy has no session end: the log gets one entry per turn (Stop)
        return {"knowledge": {
            "SessionStart": [{"type": "command", "command": cmd("context", "agy"), "timeout": 30}],
            "PostToolUse": [{"matcher": "write_file|edit_file|write_to_file|replace_file_content|multi_replace_file_content",
                             "hooks": [{"type": "command", "command": cmd("docs-sync"), "timeout": 20}]}],
            "Stop": [{"type": "command", "command": cmd("session-log"), "timeout": 15}]}}
    # One SessionStart handler only: with several, Codex drops the context (openai/codex#45999).
    # SessionEnd hooks may take at most 3 seconds.
    return {"hooks": {
        "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [
            {"type": "command", "command": cmd("context", "codex"), "timeout": 30, "additionalContextLimit": 60000}]}],
        "PostToolUse": [{"matcher": "apply_patch", "hooks": [{"type": "command", "command": cmd("docs-sync"), "timeout": 20}]}],
        "PreCompact": [{"hooks": [{"type": "command", "command": cmd("session-log"), "timeout": 15}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": cmd("session-log"), "timeout": 3}]}]}}


def install(agent: str, name: str):
    path, new = CONFIG[agent], json.dumps(hook_config(agent, name), indent=2) + "\n"
    bak = path.with_suffix(".json.bak")
    if path.exists() and path.read_text(encoding="utf-8-sig") != new and not bak.exists():  # the first original stays
        bak.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        print(f"kept the old file as {bak}; merge your own hooks from it if it had any")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    print(f"wrote {path}" + ("; trust the hooks once with /hooks in a Codex session" if agent == "codex" else ""))


def context_text(tok: str, cwd: str) -> str:
    """Every part of the startup context (/context/<n>, empty past the last), or one short notice."""
    out = []
    for n in range(1, 10):
        q = urllib.parse.urlencode({"project": cwd})
        req = urllib.request.Request(f"{URL}/context/{n}?{q}", headers={"Authorization": f"Bearer {tok}"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return {401: "knowledge server: token invalid or revoked (KNOWLEDGE_TOKEN). Shared memory and docs were not loaded.",
                    403: "knowledge server: only reachable from the home network or WireGuard. Shared memory and docs were not loaded."
                    }.get(e.code, f"knowledge server ({URL}) answered HTTP {e.code}: shared memory and docs were not loaded.")
        except OSError as e:
            return f"knowledge server ({URL}) unreachable ({e}): shared memory and docs were not loaded for this session."
        if not text:
            break
        out.append(text)
    return "\n\n".join(out)


def run_hook(script: str, tok: str, stdin: str) -> str:
    env = {**os.environ, "KNOWLEDGE_URL": URL, **({"KNOWLEDGE_TOKEN": tok} if tok else {})}
    return subprocess.run([sys.executable, "-X", "utf8", str(HOOKS / script)], input=stdin, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def context(agent: str, tok: str):
    stdin = sys.stdin.read()
    hook = json.loads(stdin or "{}")
    cwd = hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0]
    ctx = context_text(tok, cwd)  # first: a slow mirror must not cost the session its context
    text = "\n\n".join(t for t in (run_hook("docs-sync.py", tok, stdin).strip(), ctx) if t)
    print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]} if agent == "agy" else
                     {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


def main(argv: list):
    cmd = argv[0] if argv else ""
    if cmd == "install" and len(argv) >= 2 and argv[1] in CONFIG:
        install(argv[1], argv[2] if len(argv) > 2 else "-")
    elif cmd == "context" and len(argv) == 3 and argv[1] in CONFIG:
        context(argv[1], token(argv[2]))
    elif cmd in ("docs-sync", "session-log") and len(argv) == 2:
        sys.stdout.write(run_hook(f"{cmd}.py", token(argv[1]), sys.stdin.read()))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        c = hook_config("codex", "-")["hooks"]
        assert len(c["SessionStart"]) == 1 and c["SessionEnd"][0]["hooks"][0]["timeout"] <= 3
        a = hook_config("agy", "X")["knowledge"]
        assert a["Stop"][0]["command"].endswith("session-log -") and a["SessionStart"][0]["command"].endswith("context agy X")
        print("ok")
    else:
        try:
            main(sys.argv[1:])
        except Exception as e:  # locked vault, bad stdin, network: say so, never fail the session
            print(f"knowledge hook skipped: {e}", file=sys.stderr)
