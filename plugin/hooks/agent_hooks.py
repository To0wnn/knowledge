#!/usr/bin/env python3
"""Hook runtime for agents other than Claude Code (Codex, agy): the startup context, the docs mirror, the session log and
the MCP header, from the same scripts the Claude plugin runs. It lives in plugin/ so a Codex or agy plugin ships it;
clients/knowledge-agent.py (the setup without a plugin) calls the same functions.

  agent_hooks.py context <agy|codex> [TOKEN]   SessionStart: startup context + docs mirror, as the agent's JSON
  agent_hooks.py docs-sync [TOKEN]             PostToolUse: mirror an edited .md (folder with .knowledge-project)
  agent_hooks.py session-log [TOKEN]           SessionEnd/PreCompact (Codex), Stop (agy): post the session log
  agent_hooks.py header [TOKEN]                MCP header helper: the token as JSON, {} without one
  agent_hooks.py mcp [TOKEN]                   stdio MCP server of the Codex and agy plugins: relays to /mcp
TOKEN: "-" (default) = KNOWLEDGE_TOKEN or the keyring (token_store.py); on the machine that holds the files, a vault name.
Never fails a session: errors go to stderr. Linux, macOS and Windows; standard library only.
"""
import hashlib
import json
import os
import subprocess
import sys
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


def context_text(tok: str, cwd: str) -> str:
    """Every part of the startup context (/context/<n>, empty past the last), or one short notice; with the server
    down, the context of the last online session and the offline copy of the memory."""
    key, out = hashlib.sha256(cwd.encode()).hexdigest()[:12], []
    try:
        linked = project_marker.project_root(Path(cwd).resolve())[1]
    except OSError:
        linked = None
    query = urllib.parse.urlencode({"project": linked, "linked": "1"} if linked else {"project": cwd})
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


def run_hook(script: str, tok: str, stdin: str) -> str:
    env = {**os.environ, "KNOWLEDGE_URL": URL, **({"KNOWLEDGE_TOKEN": tok} if tok else {})}
    return subprocess.run([sys.executable, "-X", "utf8", str(HOOKS / script)], input=stdin, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def context(agent: str, tok: str):
    stdin = sys.stdin.read()
    hook = json.loads(stdin or "{}")
    cwd = hook.get("cwd") or (hook.get("workspacePaths") or [os.getcwd()])[0]
    ctx = context_text(tok, cwd)  # first: a slow mirror must not cost the session its context
    run_hook("memory_mirror.py", tok, "")  # returns at once: the sync runs in the background
    try:
        update = self_update.notice(agent, mode=SETUP)
    except Exception as e:  # never cost the session its context
        update = f"knowledge plugin: auto-update check failed ({type(e).__name__}: {e})"
    text = "\n\n".join(t for t in (run_hook("docs-sync.py", tok, stdin).strip(), ctx, update) if t)
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


def main(argv: list):
    cmd, rest = (argv[0] if argv else ""), argv[1:]
    if cmd == "context" and rest[:1] and rest[0] in AGENTS and len(rest) <= 2:
        context(rest[0], token(rest[1] if len(rest) == 2 else "-"))
    elif cmd in ("docs-sync", "session-log") and len(rest) <= 1:
        sys.stdout.write(run_hook(f"{cmd}.py", token(rest[0] if rest else "-"), sys.stdin.read()))
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
