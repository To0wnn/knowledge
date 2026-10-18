#!/usr/bin/env python3
"""SessionEnd/PreCompact hook: post a short log of this Claude Code session to the knowledge server.

Logs the user's prompts, the files Claude changed and Claude's last answer (usually its summary) since the previous
log of the same session, so long-running tmux sessions get an entry at every compaction and nothing is logged twice.
The server picks the project from the working directory, redacts secrets and appends to
<project memory>/sessions/<YYYY-MM>.md. Standard library only; never blocks or fails a session.

Config: KNOWLEDGE_URL + KNOWLEDGE_TOKEN, or the plugin options (CLAUDE_PLUGIN_OPTION_URL/TOKEN), or
~/.config/knowledge-server/session.env on the VM.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "knowledge-session-log"
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


def config() -> tuple[str, str]:
    env = dict(os.environ)
    conf = Path.home() / ".config/knowledge-server/session.env"
    if conf.exists():
        for line in conf.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    url = env.get("KNOWLEDGE_URL") or env.get("CLAUDE_PLUGIN_OPTION_URL") or env.get("CLAUDE_PLUGIN_OPTION_url") \
        or "https://knowledge.to0wnn.com"
    token = env.get("KNOWLEDGE_TOKEN") or env.get("CLAUDE_PLUGIN_OPTION_TOKEN") or env.get("CLAUDE_PLUGIN_OPTION_token", "")
    return url.rstrip("/"), token


def prompt_text(msg: dict) -> str:
    """The user's own words, or '' for tool results, commands, system notes and summaries."""
    if msg.get("isMeta") or msg.get("isCompactSummary"):
        return ""
    content = msg.get("message", {}).get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
    text = (content or "").strip() if isinstance(content, str) else ""
    if not text or text.startswith(("<", "[Request interrupted", "Continue from where")):
        return ""
    return text


def collect(lines: list[str]) -> tuple[list[str], list[str], str]:
    prompts, files, summary = [], [], ""
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "user":
            text = prompt_text(d)
            if text:
                prompts.append(text)
        elif d.get("type") == "assistant":
            for c in d.get("message", {}).get("content", []) or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    summary = c["text"]
                elif c.get("type") == "tool_use":
                    inp = c.get("input", {}) or {}
                    path = inp.get("file_path", "")
                    if c.get("name") in EDIT_TOOLS and path and not path.startswith("/tmp/") and "/scratchpad/" not in path:
                        files.append(path.replace(str(Path.home()), "~"))  # scratch files are not worth logging
                    elif str(c.get("name", "")).endswith(("knowledge_edit", "knowledge_write")) and inp.get("path"):
                        files.append(inp["path"])
    return prompts, files, summary


def main():
    hook = json.load(sys.stdin)
    transcript, sid = hook.get("transcript_path", ""), hook.get("session_id", "")
    if not transcript or not os.path.exists(transcript):
        return
    STATE.mkdir(parents=True, exist_ok=True)
    mark = STATE / f"{sid or 'unknown'}.json"
    done = json.loads(mark.read_text()).get("lines", 0) if mark.exists() else 0
    lines = Path(transcript).read_text(errors="replace").splitlines()
    prompts, files, summary = collect(lines[done:])
    if not prompts:
        return
    url, token = config()
    body = {"session_id": sid, "cwd": hook.get("cwd") or os.getcwd(), "reason": hook.get("reason")
            or hook.get("hook_event_name", ""), "prompts": prompts[-8:], "files": list(dict.fromkeys(files))[-20:],
            "summary": summary}  # the latest prompts and files: they belong with the closing summary
    req = urllib.request.Request(f"{url}/sessions", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        if r.status == 200:
            mark.write_text(json.dumps({"lines": len(lines)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a log must never break or delay a session
        print(f"knowledge session log skipped: {e}", file=sys.stderr)
    sys.exit(0)
