#!/usr/bin/env python3
"""The backup procedure of docs/server.md, done for real: a server on a throwaway data folder gets a memo and a change, the
folder is copied, and a second server on the copy must know the same token, the memo, its history and the deleted file.
Run from server/: uv run --quiet python ../tests/backup_restore.py"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server" / "server.py"
MEMO = "---\nname: project_x\ndescription: d\nmetadata:\n  type: project\n---\n\n## Status\n{}\n\n## Decisions\n"


def start(data: Path) -> tuple[subprocess.Popen, str, Path]:
    """As compose.yaml lays out ./data, with the hash embedder so no model loads."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = data.parent / f"{data.name}.log"
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("KNOWLEDGE_")},
           "KNOWLEDGE_MEMORY": f"{data}/memory", "KNOWLEDGE_HOMELAB": f"{data}/docs", "KNOWLEDGE_CLAUDE": f"{data}/shared",
           "KNOWLEDGE_PROJECTS_DIR": f"{data}/projects", "KNOWLEDGE_ADMIN_DIR": f"{data}/admin", "KNOWLEDGE_TOKENS": f"{data}/admin/tokens.json",
           "KNOWLEDGE_SECRETS": f"{data}/admin/secrets.json", "KNOWLEDGE_AUDIT": f"{data}/state/audit.log", "KNOWLEDGE_VECTORS": f"{data}/state/vectors.npz",
           "KNOWLEDGE_UI_PASSWORD": "gategategate", "KNOWLEDGE_EMBED_MODEL": "hash", "KNOWLEDGE_BIND": "127.0.0.1", "KNOWLEDGE_PORT": str(port),
           "KNOWLEDGE_ALLOWED_HOSTS": f"127.0.0.1:{port}", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, str(SERVER)], env=env, stdout=log.open("w"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            urllib.request.urlopen(url + "/health", timeout=1).close()
            return proc, url, log
        except OSError:
            time.sleep(0.2)
    proc.kill()
    sys.exit(f"server did not start:\n{log.read_text()[-2000:]}")


def call(url: str, token: str, tool: str, args: dict) -> str:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(url + "/mcp", json.dumps(body).encode(), {
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=20) as r:
        result = json.loads(r.read().decode())["result"]
    text = result["content"][0]["text"]
    assert not result.get("isError"), text
    return text


def main():
    with tempfile.TemporaryDirectory() as t:
        data, copy = Path(t) / "data", Path(t) / "restored"
        data.mkdir()
        proc, url, log = start(data)
        try:
            token = next((m.group(1) for _ in range(50) if (m := re.search(r"^\s+(\S{32,})\s*$", log.read_text(), re.M)) or time.sleep(0.1)), None)
            assert token, log.read_text()[-2000:]
            call(url, token, "knowledge_write", {"path": "memory/project_x.md", "content": MEMO.format("first text")})
            call(url, token, "knowledge_edit", {"path": "memory/project_x.md", "old_string": "first text", "new_string": "second text"})
            call(url, token, "knowledge_write", {"path": "memory/project_y.md", "content": MEMO.format("to delete").replace("project_x", "project_y")})
            version = re.search(r"version (\w+)", call(url, token, "knowledge_read", {"path": "memory/project_y.md"})).group(1)
            call(url, token, "knowledge_delete", {"path": "memory/project_y.md", "base_version": version})
        finally:
            proc.terminate()
            proc.wait(20)
        shutil.copytree(data, copy)
        (copy / "state" / "vectors.npz").unlink(missing_ok=True)  # derived: docs/server.md says it may be left out
        shutil.rmtree(data)  # the disk is gone
        proc, url, log = start(copy)
        try:
            assert "second text" in call(url, token, "knowledge_read", {"path": "memory/project_x.md"}), "the memo and the token"
            history = call(url, token, "knowledge_history", {"path": "memory/project_x.md"})
            assert len(history.splitlines()) >= 2, history
            assert any("project_y" in f.name for f in (copy / "state" / "deleted").iterdir()), "the trash"
            assert "second text" in call(url, token, "knowledge_search", {"pattern": "second text"})
        finally:
            proc.terminate()
            proc.wait(20)
    print("ok backup and restore")


main()
