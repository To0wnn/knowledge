#!/usr/bin/env python3
"""SessionEnd/PreCompact hook: post a short log of this Claude Code session to the knowledge server.

Also reads the transcripts of Codex (rollout JSONL, SessionEnd/PreCompact hooks) and agy (transcript.jsonl, Stop hook,
camelCase payload); the transcript format is the only difference. Neither format is a stable interface: a change there
makes the log skip, never the session fail.

Logs the user's prompts, the files Claude changed and Claude's last answer (usually its summary) since the previous
log of the same session, so long-running tmux sessions get an entry at every compaction and nothing is logged twice.
The server picks the project from the working directory, redacts secrets and appends to
<project memory>/sessions/<YYYY-MM>.md. Standard library only; never blocks or fails a session.

Config: KNOWLEDGE_URL + KNOWLEDGE_TOKEN, or the plugin options (CLAUDE_PLUGIN_OPTION_URL/TOKEN), or
~/.config/knowledge-server/session.env on the VM.
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "knowledge-session-log"
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
AGY_EDIT_TOOLS = {"write_file", "edit_file", "write_to_file", "replace_file_content", "multi_replace_file_content"}
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update) File: (.+)$", re.M)  # Codex apply_patch


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


def short(path: str) -> str:
    """'' for scratch files (not worth logging), else the path with ~ for the home folder."""
    return "" if not path or path.startswith("/tmp/") or "/scratchpad/" in path else path.replace(str(Path.home()), "~")


def arg(value):
    """agy sometimes JSON-encodes a tool argument a second time ("\"/path\""); undo that."""
    try:
        return json.loads(value) if isinstance(value, str) and value[:1] in '"{' else value
    except ValueError:
        return value


def collect_other(d: dict, prompts: list, files: list) -> str | None:
    """One line of a Codex or agy transcript: adds prompts/files, returns the answer text if the line holds one."""
    p = d.get("payload") or {}
    if d.get("type") == "event_msg":  # Codex: user_message/agent_message (older), item_completed (0.16x)
        item = p.get("item") or {}
        text = p.get("message") if p.get("type") in ("user_message", "agent_message") else " ".join(
            c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
        kind = {"user_message": "user", "agent_message": "agent"}.get(p.get("type")) or \
            {"UserMessage": "user", "AgentMessage": "agent"}.get(item.get("type"))
        if kind == "user" and text.strip() and not text.lstrip().startswith(("<", "# AGENTS.md")):
            prompts.append(text.strip())
        return text if kind == "agent" and text.strip() else None
    if d.get("type") == "response_item" and p.get("name") == "apply_patch":
        files.extend(PATCH_FILE.findall(str(p.get("input") or p.get("arguments") or "")))
    if d.get("type") == "USER_INPUT":  # agy
        text = re.sub(r"</?USER_REQUEST>|<ADDITIONAL_METADATA>.*", "", str(d.get("content", "")), flags=re.S).strip()
        if text and not text.startswith("<"):
            prompts.append(text)
    if d.get("type") == "PLANNER_RESPONSE":
        for c in d.get("tool_calls") or []:
            a = c.get("args") or {}
            if c.get("name") in AGY_EDIT_TOOLS:
                files.append(str(arg(a.get("Path") or a.get("TargetFile") or "")))
            elif c.get("name") == "call_mcp_tool" and arg(a.get("ToolName")) in ("knowledge_edit", "knowledge_write"):
                files.append(str((arg(a.get("Arguments")) or {}).get("path", "")))
        return d.get("content") if str(d.get("content", "")).strip() else None
    return None


def collect(lines: list[str]) -> tuple[list[str], list[str], str]:
    prompts, files, summary = [], [], ""
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") not in ("user", "assistant"):
            summary = collect_other(d, prompts, files) or summary
        elif d.get("type") == "user":
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
                    if c.get("name") in EDIT_TOOLS:
                        files.append(path)
                    elif str(c.get("name", "")).endswith(("knowledge_edit", "knowledge_write")) and inp.get("path"):
                        files.append(inp["path"])
    return list(dict.fromkeys(prompts)), [f for f in map(short, files) if f], summary


def main():
    hook = json.load(sys.stdin)
    if "conversationId" in hook:  # agy: camelCase fields, Stop after every turn, no event name
        hook = {"session_id": hook["conversationId"], "transcript_path": os.path.expanduser(hook.get("transcriptPath", "")),
                "cwd": (hook.get("workspacePaths") or [""])[0], "reason": "agy"}
    elif "/.codex/" in hook.get("transcript_path", "").replace("\\", "/"):  # Codex uses Claude's field names
        hook["reason"] = f"codex {hook.get('hook_event_name', '')}"
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
    # Codex allows SessionEnd hooks at most 3 seconds
    with urllib.request.urlopen(req, timeout=2.5 if hook.get("reason") == "codex SessionEnd" else 8) as r:
        if r.status == 200:
            mark.write_text(json.dumps({"lines": len(lines)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a log must never break or delay a session
        print(f"knowledge session log skipped: {e}", file=sys.stderr)
    sys.exit(0)
