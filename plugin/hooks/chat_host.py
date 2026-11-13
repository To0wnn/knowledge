"""The agent of one live chat, kept apart from the companion so a companion restart (an update, a crash) does not end
it: started by chat.py (Hosted) as its own process group, it starts the agent with its output going straight to a
file, and feeds the agent's stdin from another file the companion appends whole lines to. A new companion reads on
from where the old one got to.

    chat_host.py <dir>     <dir>/cmd.json {argv, cwd}; writes <dir>/pid, <dir>/out, <dir>/err and, at the end, <dir>/exit

<dir>/close (made by the companion) closes the agent's stdin after every line written before it, so the agent ends.
<dir>/kill stops the agent and what it started at once: an elevated host (admin.py) cannot be stopped by the companion.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

POLL = 0.05  # seconds between looks at the input file


def main(d: Path):
    c = json.loads((d / "cmd.json").read_text(encoding="utf-8"))
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW; the group is this host's own
    with open(d / "out", "ab") as out, open(d / "err", "ab") as err:
        p = subprocess.Popen(c["argv"], cwd=c.get("cwd") or None, stdin=subprocess.PIPE, stdout=out, stderr=err, **kw)
    (d / "pid").write_text(str(p.pid), encoding="utf-8")
    pos, closed = 0, False
    while p.poll() is None:
        if (d / "kill").exists():
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=30)
            else:
                p.kill()
            p.wait(30)
            break
        if not closed:
            ending = (d / "close").exists()  # looked at first: every line written before it still goes in
            try:
                with open(d / "in", "rb") as f:
                    f.seek(pos)
                    data = f.read()
                end = data.rfind(b"\n") + 1  # whole lines only
                if end:
                    p.stdin.write(data[:end])
                    p.stdin.flush()
                    pos += end
                if ending:
                    p.stdin.close()
                    closed = True
            except FileNotFoundError:
                if ending:
                    p.stdin.close()
                    closed = True
            except OSError:  # its stdin is gone: it is ending
                closed = True
        time.sleep(POLL)
    tmp = d / "exit.part"
    tmp.write_text(str(p.returncode), encoding="utf-8")
    tmp.replace(d / "exit")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
