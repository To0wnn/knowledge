#!/usr/bin/env python3
"""Start the server on the made-up demo notes in this folder and save screenshots of the web UI for the README.

  uv run --project server --with playwright python demo/screenshot.py
(needs a Chromium for Playwright: `uv run --with playwright playwright install chromium` once)
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

DEMO = Path(__file__).resolve().parent
PORT = 18767
URL = f"http://127.0.0.1:{PORT}"


def start(tmp: Path) -> subprocess.Popen:
    empty = tmp / "empty"
    empty.mkdir()
    env = {**os.environ, "KNOWLEDGE_MEMORY": str(DEMO / "memory"), "KNOWLEDGE_HOMELAB": str(DEMO / "homelab"),
           **{f"KNOWLEDGE_{k}": str(empty) for k in ("CLAUDE", "ANPITEK", "ANPITEK_MEMORY", "TRADE", "TRADE_MEMORY", "PLUGIN_SKILLS")},
           "KNOWLEDGE_TOKENS": str(tmp / "tokens.json"), "KNOWLEDGE_ADMIN_DIR": str(tmp), "KNOWLEDGE_PROJECTS_DIR": str(tmp / "projects"),
           "KNOWLEDGE_PROXY_SECRET": "demo", "KNOWLEDGE_ADMINS": "demo", "KNOWLEDGE_AUDIT": str(tmp / "audit.log"),
           "KNOWLEDGE_PORT": str(PORT), "KNOWLEDGE_BIND": "127.0.0.1", "KNOWLEDGE_ALLOWED_HOSTS": f"127.0.0.1:{PORT}",
           "KNOWLEDGE_EMBED_MODEL": "hash", "KNOWLEDGE_SECRETS": str(tmp / "secrets.json"), "KNOWLEDGE_VECTORS": str(tmp / "vectors.npz")}
    (tmp / "tokens.json").write_text("{}")
    proc = subprocess.Popen([sys.executable, str(DEMO.parent / "server" / "server.py")], env=env)
    for _ in range(100):
        try:
            urllib.request.urlopen(URL + "/health", timeout=1)
            return proc
        except OSError:
            time.sleep(0.2)
    proc.kill()
    sys.exit("demo server did not start")


async def shoot() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for scheme in ("light", "dark"):
            ctx = await browser.new_context(viewport={"width": 1400, "height": 820}, device_scale_factor=2, color_scheme=scheme,
                                            bypass_csp=True, extra_http_headers={"X-Knowledge-Proxy": "demo", "X-authentik-username": "demo"})
            pg = await ctx.new_page()
            await pg.goto(URL + "/ui")
            await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 10", timeout=20000)
            await pg.click("#map-btn")
            await pg.wait_for_timeout(8000)  # let the layout settle
            await pg.screenshot(path=str(DEMO / f"map-{scheme}.png"))
            await ctx.close()
        await browser.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t:
        server = start(Path(t))
        try:
            asyncio.run(shoot())
        finally:
            server.terminate()
            server.wait()
    print("saved", *sorted(DEMO.glob("map-*.png")))
