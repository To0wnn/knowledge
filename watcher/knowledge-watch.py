#!/usr/bin/env python3
"""Runs on the VM (where the shared files live): reports changed files to the knowledge server right away.

inotify (via inotifywait) tells us about every write, create, delete and rename under the shared trees, so the
server re-reads only those files and never has to poll NFS. Events are batched for 200 ms. If a report fails,
nothing is lost: the server's periodic rescan picks the change up. Run by the systemd user unit
knowledge-watch.service (Restart=always).
"""
import json
import os
import select
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~")
TREES = [  # local path -> path prefix on the knowledge server (longest first)
    (f"{HOME}/.claude/projects/-home-user-homelab/memory", "memory"),
    (f"{HOME}/.claude/gedeeld", "claude"),
    (f"{HOME}/.claude/skills", "claude/skills"),
    (f"{HOME}/.claude/agents", "claude/agents"),
    (f"{HOME}/homelab", "homelab"),
]
SERVER = os.environ.get("KNOWLEDGE_URL", "http://<lan-ip>:8766")
TOKEN = open(os.path.expanduser("~/.config/knowledge-server/watcher.token")).read().strip()
EXCLUDE = r"(/\.git/|/node_modules/|/\.venv/|/__pycache__/|/\.cache/|\.swp$|~$)"


def to_rel(path: str) -> str | None:
    for local, prefix in TREES:
        if path == local or path.startswith(local + "/"):
            return prefix + path[len(local):]
    return None


def post(paths: set[str]):
    req = urllib.request.Request(f"{SERVER}/invalidate", method="POST", data=json.dumps({"paths": sorted(paths)}).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as e:  # the server rescan is the safety net
        print(f"report failed ({len(paths)} paths): {e}", flush=True)


def main():
    cmd = ["inotifywait", "-m", "-r", "-q", "-e", "close_write,create,delete,moved_to,moved_from",
           "--format", "%w%f", "--exclude", EXCLUDE] + [t for t, _ in TREES if os.path.isdir(t)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    pending: set[str] = set()
    deadline = None
    while True:
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if ready:
            line = proc.stdout.readline()
            if not line:
                sys.exit(f"inotifywait exited ({proc.wait()})")  # systemd restarts us
            rel = to_rel(line.rstrip("\n"))
            if rel:
                pending.add(rel)
                deadline = deadline or time.monotonic() + 0.2
        elif pending:
            post(pending)
            pending, deadline = set(), None


if __name__ == "__main__":
    main()
