#!/usr/bin/env python3
"""Browser smoke test of the web UI against the live server (headless system Chrome via Playwright).

Checks what broke before: the graph loads without page errors; a click on a note zooms to it and its neighbours
(after the layout settled); the neighbours gather in a ring; a click beside the nodes (which usually lands on a link)
deselects, puts every node back exactly and zooms out; on a phone a tap selects, the Read chip appears and scrolls
to the note. Read-only: it opens notes, it never edits.

  systemd-run --user --scope -q -p MemoryMax=3G ~/.local/bin/uv run -q --with playwright python tests/ui_smoke.py
(talks to the container port directly and sends what Traefik sends after the Authentik sign-in: the proxy secret
from the vault plus the username; systemd-run does not see ~/.local/bin on PATH, hence the full path)
"""
import asyncio
import os
import subprocess
import sys

from playwright.async_api import async_playwright

URL = os.environ.get("KNOWLEDGE_URL", "http://<lan-ip>:8766")
USER = os.environ.get("KNOWLEDGE_TEST_USER", "to0wnn")
failures = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


async def page(browser, secret: str, path: str = "/ui", **opts):
    ctx = await browser.new_context(ignore_https_errors=True, **opts,
                                    extra_http_headers={"X-Knowledge-Proxy": secret, "X-authentik-username": USER})
    pg = await ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    await pg.goto(URL + path)
    return ctx, pg, errors


EMPTY_POINT = """() => { const w = Graph.width(), h = Graph.height(), ns = Graph.graphData().nodes.map(n => Graph.graph2ScreenCoords(n.x, n.y));
  for (let y = 20; y < h; y += 20) for (let x = 20; x < w; x += 20) if (ns.every(c => Math.hypot(c.x - x, c.y - y) > 60)) return {x, y};
  return null; }"""


async def desktop(browser, secret: str) -> None:
    ctx, pg, errors = await page(browser, secret, viewport={"width": 1500, "height": 900})
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 10", timeout=20000)
    nid = await pg.evaluate("Graph.graphData().nodes.find(n => (neighbors.get(n.id) || new Set()).size >= 4).id")
    box = await pg.evaluate("$('g2d').getBoundingClientRect().toJSON()")
    await pg.wait_for_timeout(8000)
    home = await pg.evaluate("Object.fromEntries(Graph.graphData().nodes.map(n => [n.id, [n.x, n.y]]))")
    overview = await pg.evaluate("Graph.zoom()")
    sc = await pg.evaluate("(id) => { const n = byId.get(id); return Graph.graph2ScreenCoords(n.x, n.y); }", nid)
    await pg.mouse.click(box["x"] + sc["x"], box["y"] + sc["y"])
    await pg.wait_for_timeout(2000)
    zoom = await pg.evaluate("Graph.zoom()")
    radii = await pg.evaluate("""(id) => { const c = byId.get(id); return [...neighbors.get(id)].map(i => byId.get(i))
        .filter(n => n.x != null).map(n => Math.round(Math.hypot(n.x - c.x, n.y - c.y))); }""", nid)
    in_view = await pg.evaluate("""() => { const w = Graph.width(), h = Graph.height(); return [...focusSet].map(i => byId.get(i))
        .filter(Boolean).every(n => { const c = Graph.graph2ScreenCoords(n.x, n.y); return c.x >= 0 && c.y >= 0 && c.x <= w && c.y <= h; }); }""")
    check(zoom > overview * 2, f"click zooms in ({overview:.2f} -> {zoom:.2f})")
    check(len(set(radii)) == 1, f"neighbours gather in a ring (radii {sorted(set(radii))})")
    check(in_view, "note and all neighbours in view")
    pt = await pg.evaluate(EMPTY_POINT)
    await pg.mouse.click(box["x"] + pt["x"], box["y"] + pt["y"])
    await pg.wait_for_timeout(2000)
    after = await pg.evaluate("Object.fromEntries(Graph.graphData().nodes.map(n => [n.id, [n.x, n.y, n.fx]]))")
    drift = max(abs(after[k][0] - v[0]) + abs(after[k][1] - v[1]) for k, v in home.items())
    check(await pg.evaluate("selected") is None, "click beside the nodes deselects")
    check(drift < 0.01 and not any(v[2] is not None for v in after.values()), f"every node back home (drift {drift:.3f})")
    check(abs(await pg.evaluate("Graph.zoom()") - overview) < 0.05, "zoomed back to the overview")
    check(not errors, f"no page errors on desktop {errors[:2]}")
    await ctx.close()


async def search(browser, secret: str) -> None:
    ctx, pg, errors = await page(browser, secret, viewport={"width": 1500, "height": 900})
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 10", timeout=20000)
    await pg.fill("#q", "velux")
    await pg.wait_for_selector("#hits li", timeout=5000)
    paths = await pg.locator("#hits .hit-sub").all_inner_texts()
    check(len(paths) == len(set(paths)) and any("velux" in p for p in paths[:3]), f"search: one row per note, the memo near the top {paths[:3]}")
    check(await pg.locator("#hits mark").count() > 0, "search marks the words")
    await pg.locator("#hits li button").first.click()
    await pg.wait_for_timeout(1200)
    check(await pg.locator("#note .md mark").count() == 1, "opening a hit marks the word in the note")
    check(not errors, f"no page errors while searching {errors[:2]}")
    await ctx.close()


async def phone(browser, devices, secret: str) -> None:
    ctx, pg, errors = await page(browser, secret, **devices["iPhone 14"])
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 10", timeout=20000)
    await pg.wait_for_timeout(8000)
    # Phone layout since 20 Oct: search and lists first, the note, the graph last.
    check(await pg.evaluate("$('tab-results').getBoundingClientRect().top < $('graph').getBoundingClientRect().top"),
          "phone shows the lists above the graph")
    await pg.evaluate("$('graph').scrollIntoView()")
    await pg.wait_for_timeout(500)
    t = await pg.evaluate("""() => { const r = $('g2d').getBoundingClientRect(), n = Graph.graphData().nodes.find(n => (neighbors.get(n.id) || new Set()).size >= 4),
        s = Graph.graph2ScreenCoords(n.x, n.y); return {x: r.x + s.x, y: r.y + s.y}; }""")
    await pg.touchscreen.tap(t["x"], t["y"])
    await pg.wait_for_timeout(2500)
    check(await pg.evaluate("selected") is not None, "tap selects a note")
    check(await pg.evaluate("!$('readchip').hidden"), "Read chip shows")
    await pg.tap("#readchip")
    await pg.wait_for_timeout(1500)
    check(await pg.evaluate("Math.abs($('note').getBoundingClientRect().top) < 5"), "the note is in view (opening scrolls to it; so does the Read chip)")
    check(not errors, f"no page errors on the phone {errors[:2]}")
    await ctx.close()


async def admin(browser, secret: str) -> None:
    ctx, pg, errors = await page(browser, secret, "/ui/admin", viewport={"width": 1300, "height": 900})
    await pg.wait_for_selector("#tokens tr")
    check(await pg.locator("#tokens tr").count() > 0, "admin page lists the device tokens")
    check(await pg.locator("#projects tr").count() >= 3, "admin page lists the projects")
    check(not await pg.locator("#error").is_visible(), "admin page shows no error")
    check(not errors, f"no page errors on the admin page {errors[:2]}")
    await ctx.close()


async def main() -> None:
    secret = subprocess.run(["secret", "KNOWLEDGE_PROXY_SECRET"], check=True, capture_output=True, text=True).stdout.strip()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        await desktop(browser, secret)
        await search(browser, secret)
        await phone(browser, p.devices, secret)
        await admin(browser, secret)
        await browser.close()
    print("ok" if not failures else f"{len(failures)} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
