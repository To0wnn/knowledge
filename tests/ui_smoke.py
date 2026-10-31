#!/usr/bin/env python3
"""Browser smoke test of the web UI against the live server (headless system Chrome via Playwright).

Checks what broke before: the page loads without errors; the topic list opens a note in the middle with its
connected notes on the right; Changes lists lines added/removed. On the map (Map button) a click on a note zooms to it
and its neighbours (after the layout settled); the neighbours gather in a ring; a click beside the nodes (which usually
lands on a link) deselects, puts every node back exactly and zooms out; a second click on a note reads it. On a phone
the lists come first (on the home view the topic cards instead of the topic list) and opening a note scrolls to it. Read-only: it opens notes, it never edits.

  systemd-run --user --scope -q -p MemoryMax=3G ~/.local/bin/uv run -q --with playwright python tests/ui_smoke.py
(talks to the container port directly and sends what Traefik sends after the Authentik sign-in: the proxy secret
from the vault plus the username; systemd-run does not see ~/.local/bin on PATH, hence the full path)
"""
import asyncio
import os
import subprocess
import sys

from playwright.async_api import async_playwright


def local_env():
    """Addresses of this installation from local.env next to this repo (not in git); the environment wins."""
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "local.env")
    if os.path.exists(f):
        for line in open(f):
            k, _, v = line.split("#")[0].strip().partition("=")
            if k and v:
                os.environ.setdefault(k, v.strip())


local_env()

URL = os.environ["KNOWLEDGE_URL"]  # the server on the LAN, from local.env
USER = os.environ["KNOWLEDGE_TEST_USER"]  # an admin user name, from local.env
failures = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


async def page(browser, secret: str, path: str = "/ui", **opts):
    # bypass_csp: Playwright's wait_for_function evaluates strings, which the page's CSP forbids; csp() checks the
    # app itself under the real policy.
    ctx = await browser.new_context(ignore_https_errors=True, bypass_csp=True, **opts,
                                    extra_http_headers={"X-Knowledge-Proxy": secret, "X-authentik-username": USER})
    pg = await ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    await pg.goto(URL + path)
    return ctx, pg, errors


async def click_node(pg, box, sc):
    """Like a person: move onto the node first. force-graph finds the node under the pointer in its next frame, so a
    click in the same instant as the move can land "beside" it when nothing else is animating."""
    await pg.mouse.move(box["x"] + sc["x"], box["y"] + sc["y"])
    await pg.wait_for_timeout(300)
    await pg.mouse.click(box["x"] + sc["x"], box["y"] + sc["y"])


EMPTY_POINT = """() => { const w = Graph.width(), h = Graph.height(), ns = Graph.graphData().nodes.map(n => Graph.graph2ScreenCoords(n.x, n.y));
  for (let y = 20; y < h; y += 20) for (let x = 20; x < w; x += 20) if (ns.every(c => Math.hypot(c.x - x, c.y - y) > 60)) return {x, y};
  return null; }"""


async def desktop(browser, secret: str) -> None:
    ctx, pg, errors = await page(browser, secret, viewport={"width": 1500, "height": 900})
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 10", timeout=20000)
    # Reading: a note from the topic list opens in the middle, connected notes on the right.
    await pg.locator("#topics summary").first.click()
    await pg.locator("#topics details[open] .hits button").first.click()
    await pg.wait_for_selector("#side .connected a", timeout=5000)
    check(await pg.evaluate("!!current && $('note').querySelector('.md') && $('graph').hidden"), "topic list opens the note in the middle")
    check(await pg.evaluate("$('side').querySelector('h3').textContent.startsWith('Connected')"), "connected notes on the right")
    await pg.click('[data-tab="changes"]')
    await pg.wait_for_selector("#recent li button", timeout=5000)
    check(await pg.locator("#recent .plus").count() > 0, "Changes shows lines added/removed")
    await pg.click('[data-tab="tasks"]')
    await pg.wait_for_selector("#tasks h3, #tasks p", timeout=5000)
    check(await pg.evaluate("!$('tab-tasks').hidden && $('tab-lint').hidden"), "Tasks tab shows task lines and agent sessions")
    # The map.
    await pg.keyboard.press("Escape")
    await pg.click("#map-btn")
    await pg.wait_for_timeout(500)
    check(await pg.evaluate("!$('graph').hidden && $('note').hidden"), "Map swaps the middle column to the map")
    nid = await pg.evaluate("Graph.graphData().nodes.find(n => (neighbors.get(n.id) || new Set()).size >= 4).id")
    box = await pg.evaluate("$('g2d').getBoundingClientRect().toJSON()")
    await pg.wait_for_timeout(8000)
    home = await pg.evaluate("Object.fromEntries(Graph.graphData().nodes.map(n => [n.id, [n.x, n.y]]))")
    overview = await pg.evaluate("Graph.zoom()")
    sc = await pg.evaluate("(id) => { const n = byId.get(id); return Graph.graph2ScreenCoords(n.x, n.y); }", nid)
    await click_node(pg, box, sc)
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
    sc = await pg.evaluate("(id) => { const n = byId.get(id); return Graph.graph2ScreenCoords(n.x, n.y); }", nid)
    await click_node(pg, box, sc)
    await pg.wait_for_timeout(1500)
    check(await pg.evaluate("!!$('side').querySelector('.map-card')"), "on the map the right column shows the note card")
    sc = await pg.evaluate("(id) => { const n = byId.get(id); return Graph.graph2ScreenCoords(n.x, n.y); }", nid)
    await click_node(pg, box, sc)
    await pg.wait_for_timeout(500)
    state = await pg.evaluate("({map: !$('graph').hidden, note: current && current.path, selected, hover})")
    check(not state["map"] and state["note"] == nid, f"a second click reads the note {state if state['note'] != nid or state['map'] else ''}")
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
    # + Add: both dialogs open from the menu and close again (nothing is submitted).
    await pg.click("#add-menu summary")
    await pg.click("#new-memo")
    await pg.wait_for_timeout(500)
    check(await pg.locator("#new-dialog").is_visible() and await pg.locator("#new-heading option").count() > 1,
          "+ Add → New memo opens with the index headings")
    await pg.click("#new-cancel")
    await pg.click("#add-menu summary")
    await pg.click("#ingest-open")
    check(await pg.locator("#ingest-dialog").is_visible() and not await pg.locator("#add-menu").get_attribute("open"),
          "+ Add → Add a source opens (menu closed)")
    await pg.click("#ingest-cancel")
    check(not await pg.locator("#ingest-dialog").is_visible(), "the source dialog closes")
    check(not errors, f"no page errors while searching {errors[:2]}")
    await ctx.close()


async def phone(browser, devices, secret: str) -> None:
    ctx, pg, errors = await page(browser, secret, **devices["iPhone 14"])
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 10", timeout=20000)
    # Phone layout: search and lists first, then the note; the map only on request.
    check(await pg.evaluate("$('tab-notes').getBoundingClientRect().top < $('note').getBoundingClientRect().top && $('graph').hidden"),
          "phone shows the lists above the note, no map")
    await pg.wait_for_selector(".home-card .hits button")
    check(await pg.evaluate("!$('topics').offsetParent"), "phone home: the topic cards replace the topic list")
    await pg.locator(".home-card .hits button").first.tap()
    await pg.wait_for_timeout(1500)
    check(await pg.evaluate("Math.abs($('note').getBoundingClientRect().top) < 5"), "opening a note scrolls to it")
    # A phone widens its layout viewport to fit content that is too wide (and zooms out), so compare with the screen.
    check(await pg.evaluate("document.documentElement.scrollWidth <= screen.width"), "phone page fits the screen with a note open")
    await pg.tap("#map-btn")
    await pg.wait_for_timeout(8000)
    await pg.evaluate("$('graph').scrollIntoView()")
    t = await pg.evaluate("""() => { const r = $('g2d').getBoundingClientRect(), n = Graph.graphData().nodes.find(n => n.id !== selected && (neighbors.get(n.id) || new Set()).size >= 4),
        s = Graph.graph2ScreenCoords(n.x, n.y); return {x: r.x + s.x, y: r.y + s.y, id: n.id}; }""")
    await pg.touchscreen.tap(t["x"], t["y"])
    await pg.wait_for_timeout(2500)
    check(await pg.evaluate("selected") is not None, "tap on the map selects a note")
    check(not errors, f"no page errors on the phone {errors[:2]}")
    await ctx.close()


async def admin(browser, secret: str) -> None:
    ctx, pg, errors = await page(browser, secret, "/ui/admin", viewport={"width": 1300, "height": 900})
    await pg.wait_for_selector("#tokens tr")
    check(await pg.locator("#tokens tr").count() > 0, "admin page lists the device tokens")
    check("ago" in await pg.locator("#tokens").inner_text() or "last 15 min" in await pg.locator("#tokens").inner_text(),
          "admin page shows when tokens were last used")
    check(await pg.locator("#projects tr").count() >= 3, "admin page lists the projects")
    sizes = await pg.locator("#projects tr td:nth-child(5)").all_inner_texts()
    check(bool(sizes) and all(t.endswith((" KB", " MB")) for t in sizes), f"admin page shows each project's size {sizes[:3]}")
    # New token: the kind of token sets the switches; shared skills are greyed out with the reason where not allowed.
    await pg.click("#new-token-box summary")
    flags = "() => ({w: $('t-write').checked, r: $('t-redact').checked, s: $('t-skills').checked, sd: $('t-skills').disabled, why: $('t-skills-why').textContent, all: !!$('t-roots').querySelector('input[value=\"*\"]:checked')})"
    f = await pg.evaluate(flags)
    check(f["w"] and not f["r"] and f["s"] and f["all"], f"token kind 'own computer' sets write + skills + all projects {f}")
    await pg.check('input[name="t-kind"][value="guest"]')
    f = await pg.evaluate(flags)
    check(f["w"] and f["r"] and not f["s"] and f["sd"] and "masked" in f["why"], f"token kind 'someone else' masks and greys out skills {f}")
    await pg.check('input[name="t-kind"][value="reader"]')
    f = await pg.evaluate(flags)
    check(not f["w"] and f["r"] and f["sd"], f"token kind 'only reads' never writes {f}")
    await pg.check('input[name="t-kind"][value="own"]')
    await pg.click("#t-advanced summary")
    await pg.uncheck("#t-write")
    f = await pg.evaluate(flags)
    kind = await pg.evaluate("document.querySelector('input[name=\"t-kind\"]:checked')")
    check(kind is None and f["sd"] and "write" in f["why"], f"a hand-changed switch clears the kind and explains skills {f}")
    check(not await pg.locator("#error").is_visible(), "admin page shows no error")
    check(not errors, f"no page errors on the admin page {errors[:2]}")
    await ctx.close()


async def csp(browser, secret: str) -> None:
    """The app under its real Content-Security-Policy: the map and a note render, nothing is blocked."""
    for path in ("/ui", "/ui/admin"):
        ctx = await browser.new_context(ignore_https_errors=True,
                                        extra_http_headers={"X-Knowledge-Proxy": secret, "X-authentik-username": USER})
        pg, blocked = await ctx.new_page(), []
        pg.on("console", lambda m: blocked.append(m.text) if "Content Security Policy" in m.text else None)
        pg.on("pageerror", lambda e: blocked.append(str(e)))
        await pg.goto(URL + path)
        await pg.wait_for_timeout(2500)
        if path == "/ui":
            await pg.click("#map-btn")
            await pg.wait_for_timeout(1500)
        check(not blocked, f"nothing blocked by the CSP on {path} {blocked[:2]}")
        await ctx.close()


async def main() -> None:
    secret = subprocess.run(["secret", "KNOWLEDGE_PROXY_SECRET"], check=True, capture_output=True, text=True).stdout.strip()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        await csp(browser, secret)
        await desktop(browser, secret)
        await search(browser, secret)
        await phone(browser, p.devices, secret)
        await admin(browser, secret)
        await browser.close()
    print("ok" if not failures else f"{len(failures)} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
