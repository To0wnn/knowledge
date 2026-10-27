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
from __future__ import annotations  # X | None on Python 3.9 (macOS's /usr/bin/python3)

import json
import token_store
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
        for line in conf.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    url = env.get("KNOWLEDGE_URL") or token_store.url()  # env includes session.env
    token = env.get("KNOWLEDGE_TOKEN") or token_store.token()  # env includes session.env (the VM's sessions token)
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


def codex_total(lines: list[str]) -> dict:
    """The last running total in a Codex rollout, as in/cache_write/cache_read/out (its input includes the cached)."""
    for line in reversed(lines):
        if '"token_count"' in line:
            try:
                t = (json.loads(line).get("payload", {}).get("info") or {}).get("total_token_usage")
            except ValueError:
                continue
            if t:
                return {"in": t.get("input_tokens", 0) - t.get("cached_input_tokens", 0), "out": t.get("output_tokens", 0),
                        "cache_write": t.get("cache_write_input_tokens", 0), "cache_read": t.get("cached_input_tokens", 0)}
    return {}


def usage(before: list[str], span: list[str]) -> dict:
    """Tokens used in this span. Claude logs one model call once per content block (same message id + request id),
    so each call counts once; Codex logs running totals, so the span is the last total minus the one before it; agy
    logs each call once on its PLANNER_RESPONSE line."""
    calls, total = {}, dict.fromkeys(("in", "cache_write", "cache_read", "out"), 0)
    if any('"token_count"' in line for line in span):
        end, start = codex_total(span), codex_total(before)
        return {k: max(0, end.get(k, 0) - start.get(k, 0)) for k in total}
    for line in span:
        if "input_tokens" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        u = (d.get("message") or {}).get("usage") if d.get("type") == "assistant" else None
        if u:
            calls[(d["message"].get("id"), d.get("requestId"))] = {
                "in": u.get("input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
                "cache_read": u.get("cache_read_input_tokens", 0), "out": u.get("output_tokens", 0)}
        elif d.get("type") == "PLANNER_RESPONSE":
            calls[len(calls), None] = {"in": d.get("input_tokens", 0), "cache_read": d.get("cache_read_tokens", 0),
                                       "out": d.get("output_tokens", 0)}
    for c in calls.values():
        for k, v in c.items():
            total[k] += int(v or 0)
    return total


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
    raw = Path(transcript).read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    if raw and not raw.endswith("\n"):
        lines = lines[:-1]  # a line still being written: read it next time
    prompts, files, summary = collect(lines[done:])
    if not prompts:
        return
    url, token = config()
    body = {"session_id": sid, "cwd": hook.get("cwd") or os.getcwd(), "reason": hook.get("reason")
            or hook.get("hook_event_name", ""), "prompts": prompts[-8:], "files": list(dict.fromkeys(files))[-20:],
            "summary": summary, "usage": usage(lines[:done], lines[done:]),
            "agent": "agy" if hook.get("reason") == "agy" else "codex" if str(hook.get("reason", "")).startswith("codex") else "claude"}
    # the latest prompts and files: they belong with the closing summary
    req = urllib.request.Request(f"{url}/sessions", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    # Codex gives SessionEnd hooks at most 3 seconds, Python start-up included
    codex_end = hook.get("reason") == "codex SessionEnd"
    with urllib.request.urlopen(req, timeout=2 if codex_end else 8) as r:
        if r.status == 200:
            mark.write_text(json.dumps({"lines": len(lines)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a log must never break or delay a session
        print(f"knowledge session log skipped: {e}", file=sys.stderr)
    sys.exit(0)
