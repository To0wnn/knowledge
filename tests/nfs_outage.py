#!/usr/bin/env python3
"""A real NFS mount whose traffic is blocked, as when the file server is gone: the gate closes, also on a quiet mount where
stat answers from the client's cache (a probe using stat fails here); writes under way land afterwards, a retry hears so,
and what stays open does not wait for the disk. Linux with an NFS server and passwordless sudo, so not in the check; run
it after changing the shared folders' gate (about 2 min):

  uv run --project server python tests/nfs_outage.py

Only this test's own export and traffic to 127.0.0.2 are touched; an NFS client elsewhere is not affected.
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

from storage_hang import MEMO, SERVER, call, get, wait_for

PASSWORD = "gategategate"
BLOCK = "OUTPUT -d 127.0.0.2 -p tcp --dport 2049 -j DROP"


def sudo(command: str, check: bool = True):
    subprocess.run(["sudo", "-n", *command.split()], check=check, capture_output=not check)


def text_of(url: str, token: str, path: str) -> str:
    return json.loads(call(url, token, "knowledge_read", {"path": path})[1])["result"]["content"][0]["text"]


def sign_in(url: str) -> str:
    conn = http.client.HTTPConnection(url.removeprefix("http://"), timeout=10)
    conn.request("POST", "/ui/login", urllib.parse.urlencode({"password": PASSWORD}), {"Content-Type": "application/x-www-form-urlencoded"})
    cookie = conn.getresponse().getheader("Set-Cookie") or ""
    assert cookie, "no sign-in cookie"
    return cookie.split(";")[0]


def get_signed_in(url: str, path: str, cookie: str, timeout: float = 15) -> tuple[int, float]:
    t = time.time()
    conn = http.client.HTTPConnection(url.removeprefix("http://"), timeout=timeout)
    try:
        conn.request("GET", path, headers={"Cookie": cookie})
        return conn.getresponse().status, time.time() - t
    except OSError:
        return 0, time.time() - t


def main():
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory(dir="/var/tmp") as export:
        os.chmod(export, 0o755)  # a client of another uid reads a mkdtemp folder only with this
        mount, log, server = Path(t) / "mnt", Path(t) / "server.log", None
        mount.mkdir()
        target = f"127.0.0.0/8:{export}"
        sudo(f"exportfs -o rw,sync,no_subtree_check,fsid={os.getpid()} {target}")
        try:
            sudo(f"mount -t nfs -o nfsvers=4.2,rw,hard,timeo=50,actimeo=1 127.0.0.2:{export} {mount}")
            (mount / "shared").mkdir()
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            env = {**{k: v for k, v in os.environ.items() if not k.startswith("KNOWLEDGE_")},
                   "KNOWLEDGE_MEMORY": str(mount), "KNOWLEDGE_HOMELAB": f"{t}/docs", "KNOWLEDGE_CLAUDE": str(mount / "shared"), "KNOWLEDGE_PROJECTS_DIR": f"{t}/projects",
                   "KNOWLEDGE_ADMIN_DIR": f"{t}/admin", "KNOWLEDGE_TOKENS": f"{t}/admin/tokens.json", "KNOWLEDGE_SECRETS": f"{t}/admin/secrets.json",
                   "KNOWLEDGE_AUDIT": f"{t}/state/audit.log", "KNOWLEDGE_VECTORS": f"{t}/state/vectors.npz", "KNOWLEDGE_UI_PASSWORD": PASSWORD,
                   "KNOWLEDGE_EMBED_MODEL": "hash", "KNOWLEDGE_BIND": "127.0.0.1", "KNOWLEDGE_PORT": str(port), "KNOWLEDGE_ALLOWED_HOSTS": f"127.0.0.1:{port}", "PYTHONUNBUFFERED": "1"}
            server = subprocess.Popen([sys.executable, str(SERVER)], env=env, stdout=log.open("w"), stderr=subprocess.STDOUT)
            url = f"http://127.0.0.1:{port}"
            wait_for("server", lambda: get(url + "/health", 1)[0] == 200, 60)
            token = re.search(r"^\s+(\S{32,})\s*$", log.read_text(), re.M).group(1)
            cookie = sign_in(url)
            for name in ("project_x", "project_y"):
                st, said, _ = call(url, token, "knowledge_write", {"path": f"memory/{name}.md", "content": MEMO})
                assert st == 200 and '"isError":false' in said.replace(" ", ""), said[:300]
            time.sleep(8)  # quiet: nothing under way, so the client trusts what it has cached

            print("-- the traffic stops while nothing is under way")
            sudo(f"iptables -I {BLOCK}")
            t0 = time.time()
            wait_for("/health?disk=1 503 on a quiet mount", lambda: get(url + "/health?disk=1", 2)[0] == 503, 30)
            print(f"ok   /health?disk=1 is 503 after {time.time() - t0:.1f} s")
            sudo(f"iptables -D {BLOCK}")
            wait_for("recovery from the quiet outage", lambda: get(url + "/health?disk=1", 2)[0] == 200, 90)
            print("ok   recovers")

            print("-- the traffic stops while writes are under way")
            sudo(f"iptables -I {BLOCK}")
            t0 = time.time()
            time.sleep(1)
            swap = {"path": "memory/project_x.md", "old_string": "first", "new_string": "second"}
            insert = {"path": "memory/project_y.md", "old_string": "first", "new_string": "added\nfirst"}
            answers = []
            writers = [threading.Thread(target=lambda e=e: answers.append(call(url, token, "knowledge_edit", e, 5)), daemon=True) for e in (swap, insert)]
            [w.start() for w in writers]
            wait_for("/health?disk=1 503", lambda: get(url + "/health?disk=1", 2)[0] == 503, 30)
            print(f"ok   /health?disk=1 is 503 after {time.time() - t0:.1f} s")
            st, sec = get(url + "/health", 3)
            assert st == 200 and sec < 1, (st, sec)
            print(f"ok   plain /health stays 200 in {sec * 1000:.0f} ms, with writes hanging")
            [w.join(10) for w in writers]
            assert [a[0] for a in answers] == [0, 0], answers  # their clients gave up, the server keeps the writes
            st, _, sec = call(url, token, "knowledge_read", {"path": "memory/project_x.md"})
            assert st == 503 and sec < 1, (st, sec)
            print(f"ok   a read gets 503 in {sec * 1000:.0f} ms")
            # What stays open during an outage must not wait for the disk: the routes the gate lets through.
            for path in ("/ui/api/machines", "/ui/api/board"):
                st, sec = get_signed_in(url, path, cookie)
                assert st == 200 and sec < 2, (path, st, sec)
                print(f"ok   {path} answers {st} in {sec * 1000:.0f} ms")

            print("-- the traffic comes back")
            sudo(f"iptables -D {BLOCK}")
            t1 = time.time()
            wait_for("recovery", lambda: get(url + "/health?disk=1", 2)[0] == 200, 90)
            print(f"ok   recovers by itself after {time.time() - t1:.1f} s")
            wait_for("the writes to land", lambda: "second" in text_of(url, token, swap["path"]) and "added" in text_of(url, token, insert["path"]), 60)
            print("ok   both writes landed after their clients gave up")
            said = call(url, token, "knowledge_edit", swap)[1]
            assert "seems done already" in said, said[:300]
            assert text_of(url, token, swap["path"]).count("second") == 1
            said = call(url, token, "knowledge_edit", insert)[1]
            assert "seems done already" in said, said[:300]
            assert text_of(url, token, insert["path"]).count("added") == 1
            print("ok   a retry hears the change is already there, also at an anchor")
            assert not [p.name for p in mount.iterdir() if p.name.startswith(".knowledge-")]
            lines = [l for l in log.read_text().splitlines() if l.startswith("storage:")]
            assert len(lines) == 4 and "stopped answering" in lines[2] and "answer again" in lines[3], lines
            print("ok   no temporary files left, one notice each way")
        finally:
            sudo(f"iptables -D {BLOCK}", check=False)
            if server:
                server.terminate()
                server.wait(20)
            sudo(f"umount {mount}", check=False)
            if os.path.ismount(mount):
                sudo(f"umount -f -l {mount}", check=False)
            sudo(f"exportfs -u {target}", check=False)
    print("ok nfs outage")


if __name__ == "__main__":
    main()
