#!/usr/bin/env python3
"""Claude Code's permission prompt for a chat turn from the web UI: a tiny stdio MCP server with one tool, `ask`.

A chat turn without YOLO runs `claude -p --permission-prompt-tool mcp__knowledge_approval__ask` (chat.py); Claude calls
this tool for every tool call its allowed set does not cover, with {tool_name, input, tool_use_id}. The tool asks the
companion on this pc (127.0.0.1, its local API key), which asks the chat's owner in the web UI and blocks until they
answer or the approval times out, and returns Claude's documented answer: {"behavior": "allow", "updatedInput": ...}
or {"behavior": "deny", "message": ...}. Anything that goes wrong is a deny, never an allow.

  approval_mcp.py              the MCP server (Claude starts it; KNOWLEDGE_CHAT names the chat)
Standard library only; Linux, macOS and Windows.
"""
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import token_store  # noqa: E402

PORT = int(os.environ.get("KNOWLEDGE_COMPANION_PORT") or 47321)
TOOL = {"name": "ask", "description": "Ask the chat's owner in the knowledge web UI to allow a tool call.",
        "inputSchema": {"type": "object", "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                                                         "tool_use_id": {"type": "string"}}, "required": ["tool_name", "input"]}}


def ask(args: dict) -> dict:
    """The companion's decision as Claude's permission result; deny when it cannot be asked."""
    try:
        key = (token_store.KN_HOME / "companion" / "api-key").read_text(encoding="utf-8").strip()
        body = json.dumps({"chat": os.environ.get("KNOWLEDGE_CHAT", ""), "tool": str(args.get("tool_name") or ""),
                           "input": args.get("input") or {}}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/local/api/ask", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Companion-Key": key})
        with urllib.request.urlopen(req, timeout=24 * 3600) as r:  # the companion ends the wait (the approval's timeout)
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"behavior": "deny", "message": f"Could not ask the chat's owner ({type(e).__name__}: {e}); denied."}
    if d.get("decision") in ("once", "chat"):
        return {"behavior": "allow", "updatedInput": args.get("input") or {}}
    return {"behavior": "deny", "message": d.get("message") or "The chat's owner denied this tool call."}


LOCK = threading.Lock()  # one line at a time on stdout: answers come from several threads


def send(msg: dict):
    with LOCK:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def call(rid, params: dict):
    """One tools/call, in its own thread: progress every 30 s keeps Claude's idle timer from ending a long wait."""
    done, out = threading.Event(), {}
    th = threading.Thread(target=lambda: (out.update(r=ask(params.get("arguments") or {})), done.set()), daemon=True)
    th.start()
    token, n = (params.get("_meta") or {}).get("progressToken"), 0
    while not done.wait(30):
        if token is not None:
            n += 1
            send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": token, "progress": n,
                                                                                 "message": "waiting for the chat's owner"}})
    send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(out["r"])}]}})


def main():
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        rid, method = m.get("id"), m.get("method")
        if rid is None:  # a notification (initialized, cancelled): nothing to answer
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": (m.get("params") or {}).get("protocolVersion") or "2025-06-18",
                                                          "capabilities": {"tools": {}}, "serverInfo": {"name": "knowledge_approval", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOL]}})
        elif method == "tools/call" and (m.get("params") or {}).get("name") == "ask":
            threading.Thread(target=call, args=(rid, m["params"]), daemon=True).start()
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
