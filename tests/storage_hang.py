#!/usr/bin/env python3
"""A really hanging disk: the server's memory folder is a FUSE mount that stops answering, as a hard NFS mount does when
the file server is gone. Requests must fail at once, /health?disk=1 must say so and it must recover by itself. Linux with
FUSE only, so not in the check; run it after changing the shared folders' gate:

  systemd-run --user --scope -q -p MemoryMax=3G uv run --project server --with fusepy python tests/storage_hang.py
"""
import errno
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server" / "server.py"
MEMO = "---\nname: project_x\ndescription: d\nmetadata:\n  type: project\n---\n\n## Status\nfirst\n\n## Decisions\n"


def serve_fuse(real: str, mount: str, flag: str):
    """A plain pass-through to `real`; while the file `flag` exists every call waits, as on a dead hard mount."""
    from fuse import FUSE, FuseOSError, Operations

    class Hanging(Operations):
        def __call__(self, op, *args):
            while os.path.exists(flag):
                time.sleep(0.05)
            try:
                return super().__call__(op, *args)
            except OSError as e:
                raise FuseOSError(e.errno or errno.EIO)

        def p(self, path): return real + path
        def getattr(self, path, fh=None):
            st = os.lstat(self.p(path))
            return {k: getattr(st, k) for k in ("st_mode", "st_nlink", "st_size", "st_uid", "st_gid", "st_atime", "st_mtime", "st_ctime")}
        def readdir(self, path, fh): return [".", "..", *os.listdir(self.p(path))]
        def open(self, path, flags): return os.open(self.p(path), flags)
        def create(self, path, mode, fi=None): return os.open(self.p(path), os.O_CREAT | os.O_RDWR | os.O_TRUNC, mode)
        def read(self, path, size, offset, fh): return os.pread(fh, size, offset)
        def write(self, path, data, offset, fh): return os.pwrite(fh, data, offset)
        def truncate(self, path, length, fh=None): os.truncate(self.p(path), length)
        def release(self, path, fh): os.close(fh)
        def flush(self, path, fh): return 0
        def fsync(self, path, datasync, fh): return 0
        def unlink(self, path): os.unlink(self.p(path))
        def mkdir(self, path, mode): os.mkdir(self.p(path), mode)
        def rmdir(self, path): os.rmdir(self.p(path))
        def rename(self, old, new): os.rename(self.p(old), self.p(new))
        def chmod(self, path, mode): os.chmod(self.p(path), mode)
        def chown(self, path, uid, gid): pass
        def utimens(self, path, times=None): os.utime(self.p(path), times)
        def access(self, path, amode): os.stat(self.p(path))
        def statfs(self, path):
            st = os.statvfs(self.p(path))
            return {k: getattr(st, k) for k in ("f_bavail", "f_bfree", "f_blocks", "f_bsize", "f_favail", "f_ffree", "f_files", "f_flag", "f_frsize", "f_namemax")}

    FUSE(Hanging(), mount, foreground=True, nothreads=False, attr_timeout=0, entry_timeout=0, negative_timeout=0)


def get(url: str, timeout: float = 3) -> tuple[int, float]:
    """(status, seconds); status 0 when nothing came back in time."""
    t = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t
    except OSError:
        return 0, time.time() - t


def call(url: str, token: str, tool: str, args: dict, timeout: float = 5) -> tuple[int, str, float]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(url + "/mcp", json.dumps(body).encode(), {
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), time.time() - t
    except OSError as e:
        return 0, str(e), time.time() - t


def wait_for(what: str, test, seconds: float = 40) -> float:
    t = time.time()
    while time.time() - t < seconds:
        if test():
            return time.time() - t
        time.sleep(0.25)
    sys.exit(f"FAILED: {what} not within {seconds} s")


def main():
    with tempfile.TemporaryDirectory() as t:
        base, real, mount, flag = Path(t), Path(t) / "real", Path(t) / "mnt", Path(t) / "hang"
        real.mkdir(); mount.mkdir()
        fuse = subprocess.Popen([sys.executable, __file__, "--fuse", str(real), str(mount), str(flag)])
        server = None
        try:
            wait_for("mount", lambda: os.path.ismount(mount), 10)
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            log = base / "server.log"
            env = {**{k: v for k, v in os.environ.items() if not k.startswith("KNOWLEDGE_")},
                   "KNOWLEDGE_MEMORY": str(mount), "KNOWLEDGE_HOMELAB": f"{t}/docs", "KNOWLEDGE_CLAUDE": f"{t}/shared", "KNOWLEDGE_PROJECTS_DIR": f"{t}/projects",
                   "KNOWLEDGE_ADMIN_DIR": f"{t}/admin", "KNOWLEDGE_TOKENS": f"{t}/admin/tokens.json", "KNOWLEDGE_SECRETS": f"{t}/admin/secrets.json",
                   "KNOWLEDGE_AUDIT": f"{t}/state/audit.log", "KNOWLEDGE_VECTORS": f"{t}/state/vectors.npz", "KNOWLEDGE_UI_PASSWORD": "gategategate",
                   "KNOWLEDGE_EMBED_MODEL": "hash", "KNOWLEDGE_BIND": "127.0.0.1", "KNOWLEDGE_PORT": str(port), "KNOWLEDGE_ALLOWED_HOSTS": f"127.0.0.1:{port}", "PYTHONUNBUFFERED": "1"}
            server = subprocess.Popen([sys.executable, str(SERVER)], env=env, stdout=log.open("w"), stderr=subprocess.STDOUT)
            url = f"http://127.0.0.1:{port}"
            wait_for("server", lambda: get(url + "/health", 1)[0] == 200, 60)
            token = re.search(r"^\s+(\S{32,})\s*$", log.read_text(), re.M).group(1)
            assert call(url, token, "knowledge_write", {"path": "memory/project_x.md", "content": MEMO})[0] == 200, log.read_text()[-500:]
            assert get(url + "/health?disk=1")[0] == 200

            print("-- the disk stops answering")
            flag.touch()
            took = wait_for("/health?disk=1 503", lambda: get(url + "/health?disk=1", 2)[0] == 503)
            print(f"ok   /health?disk=1 is 503 after {took:.1f} s")
            st, _, sec = call(url, token, "knowledge_read", {"path": "memory/project_x.md"})
            assert st == 503 and sec < 1, (st, sec)
            print(f"ok   a read gets 503 in {sec * 1000:.0f} ms")
            st, _, sec = call(url, token, "knowledge_edit", {"path": "memory/project_x.md", "old_string": "first", "new_string": "second"})
            assert st == 503 and sec < 1, (st, sec)
            print(f"ok   a write gets 503 in {sec * 1000:.0f} ms")
            assert get(url + "/health")[0] == 200 and "stopped answering" in log.read_text()
            print("ok   plain /health stays 200 and the log says what is wrong")

            flag.unlink()
            took = wait_for("recovery", lambda: get(url + "/health?disk=1", 2)[0] == 200)
            print(f"ok   recovers by itself after {took:.1f} s")
            assert "first" in call(url, token, "knowledge_read", {"path": "memory/project_x.md"})[1]

            # A write that is busy when the disk stops, before the gate closes, must not stop other requests.
            print("-- the disk stops while a write is busy")
            import threading
            flag.touch()
            writer = threading.Thread(target=call, args=(url, token, "knowledge_edit", {"path": "memory/project_x.md", "old_string": "first", "new_string": "second"}, 60), daemon=True)
            writer.start()
            time.sleep(1)
            st, sec = get(url + "/health", 3)
            assert st == 200, "/health gives no answer while a write hangs: the event loop is blocked"
            print(f"ok   /health while a write hangs: {st} in {sec * 1000:.0f} ms")
            flag.unlink()
            writer.join(30)
            wait_for("recovery after the write", lambda: get(url + "/health?disk=1", 2)[0] == 200)
            print("ok   back after the write finished")
        finally:
            if flag.exists():
                flag.unlink()
            if server:
                server.terminate()
                server.wait(20)
            subprocess.run(["fusermount", "-u", str(mount)], capture_output=True)
            fuse.terminate()
            fuse.wait(10)
    print("ok storage hang")


if len(sys.argv) > 1 and sys.argv[1] == "--fuse":
    serve_fuse(*sys.argv[2:5])
else:
    main()
