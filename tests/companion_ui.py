"""The companion page in a real browser against a stub of its local API (no companion, no agents): YOLO on needs the
word typed in a dialog that says it is the agent's global setting; the minute refresh keeps a half-filled form.

  uv run -q --with playwright python tests/companion_ui.py [screenshot dir]
"""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

STATIC = Path(__file__).resolve().parents[1] / "plugin" / "companion" / "static"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else None
posts = []
LOCKED = None  # set: the answer without the shortcut's key (companion.py strips the counts)
STATE = {"version": "0.18", "status": {"host": "testpc", "live": 1, "synced": 0}, "projects": ["homelab"],
         "folders": {"linked": [], "candidates": [{"path": "/home/x/repo"}]},
         "agents": [{"id": "claude", "name": "Claude Code", "mark": "C", "installed": True, "version": "0.18.1", "hooksTrusted": True,
                     "yolo": False, "sessions": [], "workers": []},
                    {"id": "codex", "name": "Codex", "mark": "X", "installed": True, "version": "0.18.1", "hooksTrusted": True,
                     "yolo": True, "sessions": [], "workers": []}],
         "tasks": {"ready": [], "waiting": [], "stalled": []}, "activity": [],
         "settings": {"token": {"present": True}, "serverUrl": "https://kn.example", "workersAllowed": False, "maxAgents": 4}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/local/api/state"):
            b, ct = json.dumps(LOCKED or STATE).encode(), "application/json"
        else:
            f = STATIC / (self.path.split("?")[0].lstrip("/") or "companion.html")
            b, ct = f.read_bytes(), {"js": "text/javascript", "css": "text/css"}.get(f.suffix[1:], "text/html")
        self.send_response(200); self.send_header("Content-Type", ct); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        posts.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        b = json.dumps({"yolo": "on"}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)


fails = []
def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok: fails.append(what)


async def companion(browser):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    ctx = await browser.new_context(viewport={"width": 1100, "height": 900}); pg = await ctx.new_page()
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    await pg.goto(f"http://127.0.0.1:{srv.server_port}/")
    await pg.wait_for_selector("#yolo-claude")
    await pg.click("#yolo-claude")
    await pg.wait_for_selector("#yolo-dialog[open]")
    check(await pg.is_disabled("#yolo-ok"), "companion: Turn on is disabled until YOLO is typed")
    check("global setting" in await pg.inner_text("#yolo-text") and "your own sessions too" in await pg.inner_text("#yolo-text"), "companion: warning says global, own sessions too")
    await pg.fill("#yolo-word", "YOLO")
    if OUT: await pg.screenshot(path=str(OUT / "yolo-companion-dialog.png"))
    await pg.keyboard.press("Escape")
    await pg.wait_for_timeout(300)
    check(not posts, "companion: Escape sends nothing")
    await pg.click("#yolo-claude"); await pg.fill("#yolo-word", "yolo"); await pg.keyboard.press("Enter")
    await pg.wait_for_timeout(300)
    check(not posts and await pg.is_visible("#yolo-dialog"), "companion: Enter with a wrong word sends nothing")
    await pg.click("#yolo-cancel"); await pg.wait_for_timeout(300)
    check(not await pg.is_checked("#yolo-claude"), "companion: cancel leaves the checkbox off")
    await pg.click("#yolo-claude"); await pg.fill("#yolo-word", "YOLO"); await pg.click("#yolo-ok")
    await pg.wait_for_timeout(400)
    check(posts == [("/local/api/yolo", {"agent": "claude", "on": True, "confirm": "YOLO"})], f"companion: typed YOLO sends confirm: {posts}")
    posts.clear()
    await pg.click("#yolo-codex"); await pg.wait_for_timeout(400)
    check(posts == [("/local/api/yolo", {"agent": "codex", "on": False})], "companion: off needs no dialog")
    # no wipe: a half-filled form survives the minute refresh, also after focus left it
    await pg.fill('input[name="maxAgents"]', "5")
    await pg.click("h2#h-agents")
    await pg.evaluate("load()"); await pg.wait_for_timeout(200)
    check(await pg.input_value('input[name="maxAgents"]') == "5", "companion: refresh keeps an unsaved settings edit")
    await pg.evaluate("load(true)"); await pg.wait_for_timeout(200)
    check(await pg.input_value('input[name="maxAgents"]') == "4", "companion: explicit refresh (force) redraws")
    # opened without the key: the companion sends no counts, so no chip may read "undefined live"
    global LOCKED
    LOCKED = {"agents": STATE["agents"], "status": {"host": "testpc"}, "version": "0.18", "locked": True}
    await pg.evaluate("load(true)"); await pg.wait_for_timeout(200)
    chips = await pg.inner_text("#chips")
    check("undefined" not in chips and "not yet" not in chips, f"companion: a locked page shows no made-up counts {chips!r}")
    LOCKED = None
    check(not errs, f"companion: no page errors {errs}")
    await ctx.close(); srv.shutdown()


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        await companion(b)
        await b.close()
    print("failed", len(fails))
    sys.exit(1 if fails else 0)


asyncio.run(main())
