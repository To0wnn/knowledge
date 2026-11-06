#!/usr/bin/env python3
"""Load test for the web UI's cost on the rest of the server: open tabs refresh on every knowledge change while probes
time what must stay fast (health, session-start context). Run it before and after a change on the same data:

  KNOWLEDGE_PROXY_SECRET=... KNOWLEDGE_TEST_USER=... KNOWLEDGE_TEST_TOKEN=<write token> \\
    python3 tests/ui_load.py http://127.0.0.1:18790 [tabs] [seconds]

A change comes every CHANGE_EVERY seconds: a session-log entry (what dominates a busy day), or a memo write with
LOAD_CHANGE=memo (the worst case: it does invalidate graph and lint; creates memory/load_test_<n>.md, so never run that
against live data). The tabs do not wait for their own earlier requests, as the page did before it got a single refresh at a
time. Standard library only.
"""
import json
import os
import statistics
import sys
import threading
import time
import urllib.request

BASE = sys.argv[1].rstrip("/")
TABS, SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 4, int(sys.argv[3]) if len(sys.argv) > 3 else 60
CHANGE_EVERY = 10
HEADERS = {"X-Knowledge-Proxy": os.environ["KNOWLEDGE_PROXY_SECRET"], "X-authentik-username": os.environ["KNOWLEDGE_TEST_USER"],
           "X-Knowledge-UI": "1", "Content-Type": "application/json"}
AGENT = {"Authorization": f"Bearer {os.environ['KNOWLEDGE_TEST_TOKEN']}", "Content-Type": "application/json"}
PAGE = ("/ui/api/graph", "/ui/api/lint?v=2", "/ui/api/recent?project=all", "/ui/api/tasks")
times: dict[str, list[float]] = {}
failed: dict[str, int] = {}
stop = threading.Event()
lock = threading.Lock()


def timed(path: str, name: str = "", data: bytes | None = None, timeout: float = 120, headers: dict = HEADERS):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=headers), timeout=timeout) as r:
            refused = b'"isError":true' in r.read()  # a refused MCP call is still HTTP 200
        with lock:
            if refused:
                failed[name or path] = failed.get(name or path, 0) + 1
            else:
                times.setdefault(name or path, []).append(time.perf_counter() - t0)
    except OSError:
        with lock:
            failed[name or path] = failed.get(name or path, 0) + 1


def probe(path: str, headers: dict, every: float = 0.5):
    while not stop.wait(every):
        timed(path, timeout=30, headers=headers)


def change(n: int):
    if os.environ.get("LOAD_CHANGE") == "memo":
        memo = f"---\nname: load_test_{n}\ndescription: d\nmetadata:\n  type: project\n---\n\n## Status\n\nrun {n}\n\n## Decisions\n"
        body = json.dumps({"jsonrpc": "2.0", "id": n, "method": "tools/call",
                           "params": {"name": "knowledge_write", "arguments": {"path": f"memory/load_test_{n}.md", "content": memo}}}).encode()
        timed("/mcp", "POST /mcp write", body, headers={**AGENT, "Accept": "application/json, text/event-stream"})
        return
    body = json.dumps({"prompts": [f"load test {n}"], "session_id": f"load{n}", "reason": "end", "cwd": "/tmp"}).encode()
    timed("/sessions", "POST /sessions", body, headers=AGENT)


def main():
    threads = [threading.Thread(target=probe, args=a) for a in (("/health", HEADERS), ("/context/1?project=homelab", AGENT))]
    for t in threads:
        t.start()
    end, n = time.time() + SECONDS, 0
    pending = []
    while time.time() < end:
        n += 1
        change(n)
        for _ in range(TABS):
            for path in PAGE:
                t = threading.Thread(target=timed, args=(path,))
                t.start()
                pending.append(t)
        time.sleep(CHANGE_EVERY)
    stop.set()
    for t in threads + pending:
        t.join()
    print(f"{'route':34} {'n':>4} {'p50':>7} {'p95':>7} {'max':>7} fail")
    for name in sorted(times.keys() | failed.keys()):
        q = sorted(times.get(name, [0.0]))
        print(f"{name:34} {len(q):4} {statistics.median(q):7.2f} {q[int(len(q) * .95) - 1]:7.2f} {q[-1]:7.2f} {failed.get(name, 0)}")


if __name__ == "__main__":
    main()
