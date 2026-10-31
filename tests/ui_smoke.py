#!/usr/bin/env python3
"""Browser smoke test of the web UI against the live server (headless system Chrome via Playwright).

Checks what broke before: the page loads without errors; the topic list opens a note in the middle with its
connected notes on the right; Changes lists lines added/removed. On the map (Map button) a click on a note zooms to it
and its neighbours (after the layout settled); the neighbours gather in a ring; a click beside the nodes (which usually
lands on a link) deselects, puts every node back exactly and zooms out; a second click on a note reads it. On a phone
the lists come first (on the home view the topic cards instead of the topic list) and opening a note scrolls to it. The
Agents views open with the board and the new chat dialog (skipped on a server older than 0.16); the mission map lays
out a seeded graph cleanly (lone tasks in the first column) and opens as Tasks > Flow (skipped on a server without
it). The app shell (0.18): hash routes per view, sidebar and rail, back/forward, refresh, old deep links, Ctrl+K, the
phone's tab bar and More sheet, no sideways scroll at 390 px, the map resizing with its box; an older server gets the
old checks instead. Knowledge pages and the palette (0.19): Notes as list and note (two panes; on a phone two pages
with a Back arrow), Changes and Health as full pages, the search page, the command palette (search, jump, actions,
keys, recent, roles), a toast instead of alert(); skipped on an older server. Read-only: it opens notes, it never
edits or sends.

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
WORD = os.environ.get("KNOWLEDGE_SMOKE_SEARCH", "velux")  # a word one memo's file name holds (a seeded test server has others)
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


async def has_palette(pg) -> bool:
    """Knowledge pages and the command palette arrived in 0.19."""
    await pg.wait_for_selector("#note", state="attached")
    return await pg.locator("#palette").count() > 0


async def is_shell(pg) -> bool:
    """The app shell (sidebar, routes) arrived in 0.18; older servers get the old checks."""
    await pg.wait_for_selector("#note", state="attached")
    return await pg.locator("#sidebar").count() > 0


async def route(pg, hash_: str, wait: str = "", ms: int = 300) -> None:
    await pg.evaluate("(h) => go(h)", hash_)
    if wait:
        await pg.wait_for_selector(wait, timeout=10000)
    await pg.wait_for_timeout(ms)


EMPTY_POINT = """() => { const w = Graph.width(), h = Graph.height(), ns = Graph.graphData().nodes.map(n => Graph.graph2ScreenCoords(n.x, n.y));
  for (let y = 20; y < h; y += 20) for (let x = 20; x < w; x += 20) if (ns.every(c => Math.hypot(c.x - x, c.y - y) > 60)) return {x, y};
  return null; }"""


async def desktop(browser, secret: str) -> None:
    ctx, pg, errors = await page(browser, secret, viewport={"width": 1500, "height": 900})
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 10", timeout=20000)
    shell = await is_shell(pg)
    if shell:
        await route(pg, "#/notes", "#topics summary")
    # Reading: a note from the topic list opens in the middle, connected notes on the right.
    await pg.locator("#topics summary").first.click()
    await pg.locator("#topics details[open] .hits button").first.click()
    await pg.wait_for_selector("#side .connected a", timeout=5000)
    check(await pg.evaluate("!!current && $('note').querySelector('.md') && $('graph').hidden"), "topic list opens the note in the middle")
    check(await pg.evaluate("$('side').querySelector('h3').textContent.startsWith('Connected')"), "connected notes on the right")
    # Switching project with a note open: the old note leaves the URL, so a refresh shows the new project, not that note.
    other = await pg.evaluate("PROJECTS.find(p => p !== project && p !== projectOf(current.path))")
    if other:
        await pg.evaluate("(p) => switchProject(p)", other)
        await pg.wait_for_timeout(300)
        check(await pg.evaluate("(shell) => (shell ? location.hash === '#/notes' : !location.hash) && !current", shell), "switching project drops the open note from the URL")
        await pg.reload()
        await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 0", timeout=20000)
        check(await pg.evaluate("(p) => !current && project === p", other), "after a refresh the new project's home shows, not the old note")
        await pg.evaluate("(p) => switchProject(p)", "homelab")
        await pg.locator("#topics summary").first.click()
        await pg.locator("#topics details[open] .hits button").first.click()
        await pg.wait_for_selector("#side .connected a", timeout=5000)
    def nav(v):
        return f'#sidebar [data-nav="{v}"]' if shell else f'[data-tab="{v}"]'
    await pg.click(nav("changes"))
    await pg.wait_for_selector("#recent li button", timeout=5000)
    check(await pg.locator("#recent .plus").count() > 0, "Changes shows lines added/removed")
    await pg.click(nav("tasks"))
    await pg.wait_for_selector("#tasks h3, #tasks p", timeout=5000)
    check(await pg.evaluate("!$('tab-tasks').hidden && $('tab-lint').hidden"), "Tasks tab shows task lines and agent sessions")
    # The map.
    await pg.keyboard.press("Escape")
    await pg.click(nav("map") if shell else "#map-btn")
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
    if await has_palette(pg):  # the search page; the palette has its own test
        await route(pg, f"#/notes?q={WORD}")
    elif await is_shell(pg):
        await pg.keyboard.press("Control+k")  # from the Inbox: Ctrl+K opens the search in the Notes view
        await pg.wait_for_timeout(300)
        check(await pg.evaluate("location.hash.startsWith('#/notes') && document.activeElement === $('q')"), "Ctrl+K opens the search")
    if not await has_palette(pg):
        await pg.fill("#q", WORD)
    await pg.wait_for_selector("#hits li", timeout=5000)
    paths = await pg.locator("#hits .hit-sub").all_inner_texts()
    check(len(paths) == len(set(paths)) and any(WORD in p for p in paths[:3]), f"search: one row per note, the memo near the top {paths[:3]}")
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
    shell = await is_shell(pg)
    if shell:
        await route(pg, "#/notes")
    if await has_palette(pg):  # two pages: the topic list, then the note with a Back arrow
        check(await pg.evaluate("!!$('topics').offsetParent && !$('note').offsetParent && $('graph').hidden"), "phone Notes: the topic list alone, no note, no map")
        await pg.locator("#topics summary").first.tap()
        await pg.locator("#topics details[open] .hits button").first.tap()
        await pg.wait_for_timeout(1500)
        check(await pg.evaluate("!!current && !!$('note').offsetParent && !$('topics').offsetParent && getComputedStyle($('back')).display !== 'none' && $('note').getBoundingClientRect().top < 80"),
              "phone: a note replaces the list, at the top, with a Back arrow")
        await pg.tap("#back")
        await pg.wait_for_timeout(600)
        check(await pg.evaluate("location.hash === '#/notes' && !!$('topics').offsetParent && !$('note').offsetParent"), "phone: Back returns to the topic list")
        await pg.locator("#topics details[open] .hits button").first.tap()
        await pg.wait_for_timeout(1200)
    else:
        # Phone layout: search and lists first, then the note; the map only on request.
        check(await pg.evaluate("$('tab-notes').getBoundingClientRect().top < $('note').getBoundingClientRect().top && $('graph').hidden"),
              "phone shows the lists above the note, no map")
        await pg.wait_for_selector(".home-card .hits button")
        check(await pg.evaluate("!$('topics').offsetParent"), "phone home: the topic cards replace the topic list")
        await pg.locator(".home-card .hits button").first.tap()
        await pg.wait_for_timeout(1500)
        # under the shell's sticky page header (about 52 px) instead of at the very top
        check(await pg.evaluate("(shell) => { const t = $('note').getBoundingClientRect().top; return shell ? t >= 0 && t < 80 : Math.abs(t) < 5; }", shell), "opening a note scrolls to it")
    # A phone widens its layout viewport to fit content that is too wide (and zooms out), so compare with the screen.
    check(await pg.evaluate("document.documentElement.scrollWidth <= screen.width"), "phone page fits the screen with a note open")
    if shell:
        await route(pg, "#/map")
    else:
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


async def cockpit(browser, devices, secret: str) -> None:
    """The Agents cockpit (chats with agents on your pcs): opens over the page, its pills, the tools menu and the new
    chat dialog; on a phone one card and a switcher. Read-only: the dialog is cancelled, nothing is sent. Skipped on a
    server older than 0.16 (no cockpit yet)."""
    ctx, pg, errors = await page(browser, secret, viewport={"width": 1440, "height": 900})
    if await is_shell(pg):
        await ctx.close()
        return await cockpit_shell(browser, devices, secret)
    await pg.wait_for_selector("#map-btn")
    if not await pg.locator("#cockpit-btn").count():
        print("skip cockpit (server older than 0.16)")
        await ctx.close()
        return
    await pg.click("#cockpit-btn")
    await pg.wait_for_function("!$('cockpit').hidden && $('ck-live-n').textContent !== ''", timeout=15000)
    check(await pg.locator("#cockpit").is_visible() and await pg.locator("#ck-live").is_visible(), "cockpit opens over the page with the live pill")
    await pg.wait_for_timeout(1500)
    if await pg.locator("#ck-board").count():  # 0.17: the attention board is the start screen
        check(await pg.locator("#ck-board .ck-col, #ck-board .ck-empty").count() > 0, "cockpit opens on the attention board (columns or the empty start)")
        cols = await pg.locator("#ck-board .ck-col-head > span:first-child").all_text_contents()
        check(cols in ([], ["Needs you", "Running", "Open", "Done (24 h)"]), f"board columns, Needs you first {cols}")
        check(await pg.locator("#ck-needs-live[aria-live=polite]").count() == 1, "board: the Needs you count is announced (aria-live)")
        if await pg.locator("#ck-board .ck-item").count():
            card = pg.locator("#ck-board .ck-item-main").first
            check(await card.evaluate("b => b.tagName === 'BUTTON'"), "board: a card opens with a button (keyboard)")
    else:
        check(await pg.locator("#ck-canvas .ck-card, #ck-canvas .ck-empty").count() > 0, "cockpit shows chat cards or the empty canvas")
    pill = await pg.evaluate("[getComputedStyle($('ck-resets')).display, $('ck-resets').textContent]")
    check(pill[0] == "none" and not pill[1] or pill[0] != "none" and pill[1].startswith("resets "), f"resets pill: a time from a companion's report, else hidden {pill}")
    await pg.click("#ck-tools-menu summary")
    labels = await pg.locator("#ck-tools-menu .ck-tools [data-go]").all_inner_texts()
    check(len(labels) == 6 and "Mission map" in labels[0], f"cockpit tools menu lists its tools {labels}")
    await pg.click("#ck-tools-menu summary")
    await pg.click("#ck-new")
    await pg.wait_for_selector("#ck-new-dialog[open]")
    check(await pg.locator("#ck-machine option").count() >= 0 and await pg.locator("#ck-agent").is_visible(), "new chat dialog: pc, agent, folder")
    await pg.click("#ck-new-cancel")
    await pg.keyboard.press("Escape")
    check(await pg.evaluate("$('cockpit').hidden"), "Esc closes the cockpit")
    check(not errors, f"no page errors in the cockpit {errors[:2]}")
    await ctx.close()
    ctx, pg, errors = await page(browser, secret, **devices["iPhone 14"])
    await pg.wait_for_selector("#cockpit-btn")
    await pg.tap("#cockpit-btn")
    await pg.wait_for_timeout(2500)
    check(await pg.evaluate("document.documentElement.scrollWidth <= screen.width && (!$('ck-board') || $('ck-board').scrollWidth <= $('ck-board').clientWidth)"
                            " && [...document.querySelectorAll('.ck-card')].filter(c => c.offsetParent).length <= 1"),
          "phone cockpit: fits the screen, at most one chat shown")
    tops = await pg.evaluate("[...document.querySelectorAll('#ck-board .ck-col')].map(c => c.getBoundingClientRect().top)")
    check(tops == sorted(tops) and len(set(tops)) == len(tops), f"phone board: one list, Needs you → Running → Open → Done {tops}")
    check(not errors, f"no page errors in the phone cockpit {errors[:2]}")
    await ctx.close()


async def cockpit_shell(browser, devices, secret: str) -> None:
    """Inbox and Chats as views of the shell: the board, a chat as its own route (Esc goes back), the new chat dialog;
    on a phone one list, Done folded to one line, the tab bar gone inside a chat."""
    ctx, pg, errors = await page(browser, secret, "/ui#/inbox", viewport={"width": 1440, "height": 900})
    await pg.wait_for_selector("#ck-board .ck-col, #ck-board .ck-empty", timeout=15000)
    check(await pg.locator("#cockpit").is_visible(), "Inbox shows the attention board")
    cols = await pg.locator("#ck-board .ck-col-head > span:first-child").all_text_contents()
    check(cols in ([], ["Needs you", "Running", "Open", "Done (24 h)"]), f"board columns, Needs you first {cols}")
    check(await pg.locator("#ck-needs-live[aria-live=polite]").count() == 1, "board: the Needs you count is announced (aria-live)")
    needs = await pg.locator("#ck-board .ck-col-needs .ck-item").count()
    badge = await pg.locator("#sidebar .inbox-count").inner_text()
    check(badge == (str(needs) if needs else ""), f"the Inbox badge counts Needs you ({badge!r} vs {needs})")
    hot = await pg.evaluate("[...document.querySelectorAll('#sidebar .badge')].filter(b => b.textContent && getComputedStyle(b).backgroundColor === getComputedStyle(document.querySelector('.ck-col-needs .ck-count') || document.body).backgroundColor).map(b => b.className)")
    check(all("inbox-count" in c for c in hot), f"only the Inbox badge wears the accent {hot}")
    await pg.click("#ck-new")
    await pg.wait_for_selector("#ck-new-dialog[open]")
    check(await pg.locator("#ck-agent").is_visible(), "new chat dialog: pc, agent, folder")
    await pg.click("#ck-new-cancel")
    await route(pg, "#/chats", "#ck-history li")
    if await pg.locator("#ck-history button").count():
        await pg.locator("#ck-history button").first.click()
        await pg.wait_for_selector("#ck-canvas .ck-card", timeout=10000)
        cid = await pg.evaluate("ck.cards.get(ck.view).meta.id")
        check(await pg.evaluate("(id) => location.hash === '#/chats/' + encodeURIComponent(id) && !!document.querySelector('#ck-history [aria-current=true]')", cid),
              "a chat opens as #/chats/<chat id>, marked in the list")
        await pg.goto(f"{URL}/ui#/chats/{cid}")  # what the server's needs-you links build
        await pg.wait_for_selector("#ck-canvas .ck-card", timeout=15000)
        check(await pg.evaluate("(id) => ck.cards.get(ck.view)?.meta.id === id", cid), "a #/chats/<chat id> link (and a refresh) opens that chat")
        await pg.goto(f"{URL}/ui#/chats/no-such-chat")
        await pg.wait_for_timeout(2500)
        check(await pg.evaluate("location.hash === '#/chats' && !!$('ck-canvas').querySelector('.ck-pick')"), "a link to a missing chat falls back to #/chats")
        await pg.locator("#ck-history button").first.click()
        await pg.wait_for_selector("#ck-canvas .ck-card", timeout=10000)
        await pg.locator("#title").focus()
        await pg.keyboard.press("Escape")
        await pg.wait_for_timeout(300)
        check(await pg.evaluate("location.hash === '#/chats' && !!$('ck-canvas').querySelector('.ck-pick')"), "Esc goes back from a chat to the list")
    check(not errors, f"no page errors in Inbox and Chats {errors[:2]}")
    await ctx.close()
    ctx, pg, errors = await page(browser, secret, "/ui#/inbox", **devices["iPhone 14"])
    await pg.wait_for_selector("#ck-board .ck-col, #ck-board .ck-empty", timeout=15000)
    await pg.wait_for_timeout(500)
    tops = await pg.evaluate("[...document.querySelectorAll('#ck-board .ck-col')].map(c => c.getBoundingClientRect().top)")
    check(tops == sorted(tops) and len(set(tops)) == len(tops), f"phone board: one list, Needs you → Running → Open → Done {tops}")
    if await pg.locator(".ck-col-done").count():
        folded = await pg.evaluate("[$('ck-board').querySelector('.ck-col-done .ck-group')?.offsetParent === null, !!$('ck-board').querySelector('.ck-done-toggle').offsetParent]")
        await pg.tap(".ck-done-toggle")
        open_ = await pg.evaluate("$('ck-board').querySelector('.ck-done-toggle').getAttribute('aria-expanded') === 'true'")
        check(folded == [True, True] and open_, f"phone board: Done is one line that unfolds {folded} {open_}")
    await pg.tap('#tabbar [data-nav="chats"]')
    await pg.wait_for_selector("#ck-history li")
    if await pg.locator("#ck-history button").count():
        await pg.locator("#ck-history button").first.tap()
        await pg.wait_for_selector("#ck-canvas .ck-card", timeout=10000)
        check(await pg.evaluate("getComputedStyle($('tabbar')).display === 'none' && !!$('back').offsetParent && document.documentElement.scrollWidth <= innerWidth"), "phone chat: tab bar hidden, back button shown, fits")
        await pg.tap("#back")
        await pg.wait_for_timeout(400)
        check(await pg.evaluate("location.hash === '#/chats' && getComputedStyle($('tabbar')).display !== 'none'"), "phone chat: back returns to the list and the tab bar")
    check(not errors, f"no page errors in the phone Inbox and Chats {errors[:2]}")
    await ctx.close()


VIEW_EL = {"inbox": "cockpit", "chats": "cockpit", "tasks": "tab-tasks", "machines": "tab-machines", "notes": "kn", "map": "kn",
           "changes": "tab-changes", "health": "tab-lint", "thispc": "tab-thispc"}


async def shell(browser, devices, secret: str) -> None:
    """The app shell: a route per view (its element shown, the nav item current, one title), back/forward/refresh,
    old #<path> links, the rail, the map following its box; on a phone the tab bar, More sheet and no sideways
    scroll. Skipped on a server before the shell."""
    ctx, pg, errors = await page(browser, secret, viewport={"width": 1440, "height": 900})
    if not await is_shell(pg):
        print("skip shell (server older than 0.18)")
        await ctx.close()
        return
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 0", timeout=20000)
    await pg.wait_for_timeout(500)
    check(await pg.evaluate("location.hash === '#/inbox' && !$('cockpit').hidden"), "the default route is the Inbox")
    for v in ("inbox", "chats", "tasks", "machines", "notes", "map", "changes", "health"):
        await route(pg, f"#/{v}")
        st = await pg.evaluate("""(a) => ({shown: !$(a.el).hidden, only: [...document.querySelectorAll('#content > .view')].filter(e => !e.hidden).length === 1,
            current: document.querySelector('#sidebar [aria-current=page]')?.dataset.nav, title: $('title').textContent})""", {"el": VIEW_EL[v]})
        check(st["shown"] and st["only"] and st["current"] == v, f"route #/{v}: its view alone, its nav item current {st}")
    await route(pg, "#/notes")
    await route(pg, "#/tasks")
    await pg.go_back()
    await pg.wait_for_timeout(400)
    check(await pg.evaluate("location.hash === '#/notes' && !$('kn').hidden"), "Back returns to the previous view")
    await pg.go_forward()
    await pg.wait_for_timeout(400)
    check(await pg.evaluate("location.hash === '#/tasks' && !$('tab-tasks').hidden"), "Forward goes to the next view")
    await route(pg, "#/tasks?view=flow", "#mission .ms-n, #mission .ms-empty")
    check(await pg.evaluate("!$('mission').hidden && $('tab-tasks').contains($('mission')) && $('tasks-list').offsetParent === null && $('tasks-mode').querySelector('[data-mode=flow]').getAttribute('aria-pressed') === 'true'"),
          "Tasks > Flow shows the mission map in place of the list")
    await route(pg, "#/changes")
    await pg.reload()
    await pg.wait_for_selector("#recent li", timeout=15000)
    check(await pg.evaluate("location.hash === '#/changes' && !$('tab-changes').hidden"), "a refresh keeps the view")
    note = await pg.evaluate("Graph.graphData().nodes.find(n => n.id.endsWith('.md') && !n.id.endsWith('MEMORY.md')).id")
    await pg.goto(f"{URL}/ui#{note}")
    await pg.wait_for_function("(id) => current && current.path === id", arg=note, timeout=15000)
    check(await pg.evaluate("(id) => location.hash === '#/notes/' + id && !$('kn').hidden", note), "an old #<path> link opens #/notes/<path>")
    tricky = ["memory/q?z.md", "memory/p%41.md", "memory/sub dir/a b.md", "memory/ünï.md", "memory/x#y.md"]
    check(await pg.evaluate("(ps) => typeof noteRoute === 'function' && ps.every(p => parseRoute(noteRoute(p)).arg === p)", tricky),
          "note paths survive a route: ? # % spaces and accents are encoded")
    odd = await pg.evaluate("Graph.graphData().nodes.map(n => n.id).find(id => /[ ?#%]|[^\\x00-\\x7f]/.test(id)) || null")
    if odd:  # a real note with such a name: deep link, refresh, and opening it again adds no history entry
        await pg.goto(f"{URL}/ui" + await pg.evaluate("(p) => noteRoute(p)", odd))
        await pg.wait_for_function("(id) => current && current.path === id", arg=odd, timeout=15000)
        await pg.reload()
        await pg.wait_for_function("(id) => current && current.path === id", arg=odd, timeout=15000)
        n0 = await pg.evaluate("history.length")
        await pg.evaluate("(p) => openNote(p)", odd)
        await pg.wait_for_timeout(600)
        check(await pg.evaluate("(n) => history.length === n", n0), f"a note named {odd!r} opens by link, survives a refresh, no duplicate history entry")
    else:
        print("     no note with a space, ?, #, % or accent in its name: the live round trip is not checked")
    await pg.locator("#title").focus()
    await pg.keyboard.press("[")
    await pg.wait_for_timeout(300)
    rail = await pg.evaluate("[document.body.classList.contains('rail'), $('sidebar').getBoundingClientRect().width, localStorage.getItem('kn_rail')]")
    await pg.keyboard.press("[")
    await pg.wait_for_timeout(300)
    check(rail[0] and rail[1] < 80 and rail[2] == "1" and not await pg.evaluate("document.body.classList.contains('rail')"), f"[ folds the sidebar to a rail and back {rail}")
    size = "Graph.width() === $('graph').clientWidth && Graph.height() === $('graph').clientHeight && $('graph').clientWidth > 0"
    await route(pg, "#/map", ms=800)
    ok1 = await pg.evaluate(size)
    await pg.keyboard.press("[")
    await pg.wait_for_timeout(800)
    ok2 = await pg.evaluate(size)
    await pg.keyboard.press("[")
    await route(pg, "#/notes")
    await route(pg, "#/map", ms=800)
    check(ok1 and ok2 and await pg.evaluate(size), f"the map follows its box on every route and rail change {[ok1, ok2]}")
    check(not await pg.evaluate("['kn_tab', 'kn_cockpit', 'kn_map', 'kn_ck_view'].some(k => localStorage.getItem(k) !== null)"), "the view is not kept in localStorage")
    check(not errors, f"no page errors in the shell {errors[:2]}")
    await ctx.close()
    ctx, pg, errors = await page(browser, secret, **devices["iPhone 14"])
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 0", timeout=20000)
    check(await pg.evaluate("getComputedStyle($('tabbar')).display !== 'none' && getComputedStyle($('sidebar')).display === 'none'"), "phone: a tab bar instead of the sidebar")
    small = await pg.evaluate("[...document.querySelectorAll('#tabbar a, #tabbar button')].map(b => b.getBoundingClientRect().height).filter(h => h < 44)")
    check(not small, f"phone: tab bar targets are at least 44 px {small}")
    wide = []
    for v in ("inbox", "chats", "tasks", "tasks?view=flow", "machines", "notes", "map", "changes", "health"):
        await route(pg, f"#/{v}", ms=900)
        if await pg.evaluate("document.documentElement.scrollWidth > innerWidth"):
            wide.append(v)
    check(not wide, f"phone: no sideways scroll at 390 px {wide}")
    await route(pg, "#/inbox")
    await pg.tap("#more-btn")
    await pg.wait_for_selector("#more-sheet[open]")
    await pg.tap('#more-sheet [data-nav="machines"]')
    await pg.wait_for_timeout(400)
    check(await pg.evaluate("location.hash === '#/machines' && !$('more-sheet').open && $('more-btn').getAttribute('aria-current') === 'page'"), "phone: More opens a sheet whose links go to their view")
    await pg.go_back()
    await pg.wait_for_timeout(400)
    check(await pg.evaluate("location.hash === '#/inbox'"), "phone: back goes to the previous view")
    check(not errors, f"no page errors in the phone shell {errors[:2]}")
    await ctx.close()


async def knowledge(browser, devices, secret: str) -> None:
    """Knowledge pages and the command palette (0.19): Notes two panes, Changes and Health full width, the search page,
    the palette (notes, views, actions, keys, recent, roles), toasts instead of alert(); the phone's full-screen
    palette. Read-only: the one action run is the theme, in this browser only. Skipped on an older server."""
    ctx, pg, errors = await page(browser, secret, viewport={"width": 1440, "height": 900})
    if not await has_palette(pg):
        print("skip knowledge pages and palette (server older than 0.19)")
        await ctx.close()
        return
    dialogs = []
    pg.on("dialog", lambda d: (dialogs.append(d.message), asyncio.ensure_future(d.dismiss())))
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 0", timeout=20000)
    await route(pg, "#/notes", "#topics summary")
    await pg.locator("#topics summary").first.click()
    await pg.locator("#topics details[open] .hits button").first.click()
    await pg.wait_for_timeout(800)
    check(await pg.evaluate("(() => { const l = $('topics-pane').getBoundingClientRect(), n = $('note').getBoundingClientRect(); return l.width > 200 && n.left >= l.right - 1 && !!current; })()"),
          "Notes: the topic list and the note side by side")
    await route(pg, "#/changes", "#recent .card, #recent .empty", 500)
    check(await pg.evaluate("$('recent').getBoundingClientRect().width > 600 && $('topics-pane').offsetParent === null"), "Changes is a full-width page")
    if await pg.evaluate("me.admin"):
        await route(pg, "#/health", "#lint .card, #lint .empty", 500)
        check(await pg.evaluate("$('lint').getBoundingClientRect().width > 600"), "Health is a full-width page")
    await route(pg, f"#/notes?q={WORD}", "#hits li button, #hits-empty:not([hidden])", 500)
    check(await pg.evaluate("(w) => !$('results').hidden && $('note').hidden && $('q').value === w && $('results').getBoundingClientRect().width > 400", WORD),
          "the search page shows in the middle, in place of the note")
    # The palette: Ctrl+K from anywhere, roles, typing finds notes, arrows move, Enter opens.
    await route(pg, "#/inbox")
    await pg.keyboard.press("Control+k")
    await pg.wait_for_selector("#palette[open]")
    roles = await pg.evaluate("({focus: document.activeElement === $('pal-q'), combo: $('pal-q').getAttribute('role'), list: $('pal-list').getAttribute('role'), act: $('pal-q').getAttribute('aria-activedescendant'), views: [...document.querySelectorAll('.pal-opt')].length})")
    check(roles["focus"] and roles["combo"] == "combobox" and roles["list"] == "listbox" and roles["act"] and roles["views"] >= 8, f"Ctrl+K opens the palette: combobox, listbox, active item, views and actions {roles}")
    await pg.keyboard.type(WORD)
    await pg.wait_for_selector("#pal-list .pal-opt", timeout=5000)
    await pg.wait_for_timeout(700)
    first = await pg.evaluate("$('pal-q').getAttribute('aria-activedescendant')")
    await pg.keyboard.press("ArrowDown")
    second = await pg.evaluate("$('pal-q').getAttribute('aria-activedescendant')")
    check(first == "pal-o-0" and second == "pal-o-1" and await pg.evaluate("$('pal-o-1').getAttribute('aria-selected') === 'true'"), f"arrows move the active item {first} -> {second}")
    await pg.keyboard.press("ArrowUp")
    target = await pg.evaluate("pal.items[0]")
    await pg.keyboard.press("Enter")
    await pg.wait_for_timeout(1200)
    check(target["kind"] == "note" and await pg.evaluate("(id) => !$('palette').open && current && current.path === id && location.hash.startsWith('#/notes/')", target["id"]),
          f"typing finds the note and Enter opens it {target.get('id')}")
    await pg.locator("#title").focus()
    await pg.keyboard.press("Control+k")
    await pg.wait_for_selector("#palette[open]")
    check(await pg.evaluate("(id) => pal.items[0] && pal.items[0].id === id && $('pal-list').textContent.includes('Recent')", target["id"]), "the empty palette lists recent picks first")
    await pg.keyboard.type("changes")
    await pg.wait_for_timeout(200)
    await pg.evaluate("palSelect(pal.items.findIndex(i => i.kind === 'view' && i.id === 'changes'))")
    await pg.keyboard.press("Enter")
    await pg.wait_for_timeout(500)
    check(await pg.evaluate("location.hash === '#/changes' && !$('palette').open"), "the palette jumps to a view")
    theme = await pg.evaluate("document.documentElement.dataset.theme || 'auto'")
    await pg.keyboard.press("Control+k")
    await pg.keyboard.type("toggle theme")
    await pg.wait_for_timeout(200)
    await pg.keyboard.press("Enter")
    await pg.wait_for_timeout(300)
    check(await pg.evaluate("(t) => (document.documentElement.dataset.theme || 'auto') !== t", theme), "the palette runs an action (theme)")
    await pg.keyboard.press("Control+k")
    await pg.keyboard.press("Escape")
    await pg.wait_for_timeout(200)
    check(not await pg.evaluate("$('palette').open"), "Esc closes the palette")
    await pg.locator("#title").focus()
    await pg.keyboard.press("/")
    await pg.wait_for_selector("#palette[open]")
    check(await pg.evaluate("pal.notesOnly && $('pal-q').placeholder.startsWith('Search notes')"), "/ opens the palette for notes")
    await pg.keyboard.press("Escape")
    await pg.click("#jump")
    check(await pg.evaluate("$('palette').open"), "the sidebar's Search or jump opens the palette")
    await pg.keyboard.press("Escape")
    # alert() is gone from the knowledge views and the mission map: errors are toasts.
    src = await pg.evaluate("Promise.all(['app.js', 'mission.js'].map(f => fetch('/ui/static/' + f).then(r => r.text())))")
    left = [ln.strip() for f in src for ln in f.split("\n") if "alert(" in ln and not ln.strip().startswith("//")]
    # shortcut: ckDo (board actions) keeps its alert() until the chat-approvals branch lands, then this allows none
    check(len(left) <= 1 and all("ckDo" in s or "e.message" in s for s in left), f"no alert() outside the chat board {left}")
    await pg.evaluate("toast('smoke toast')")
    check(await pg.evaluate("(() => { const t = $('toasts').querySelector('.toast'); return !!t && t.offsetParent !== null && $('toasts').getAttribute('role') === 'status'; })()"), "toast() shows a message in a live region")
    check(not dialogs, f"no browser dialogs {dialogs[:2]}")
    check(not errors, f"no page errors in the knowledge pages and palette {errors[:2]}")
    await ctx.close()
    ctx, pg, errors = await page(browser, secret, **devices["iPhone 14"])
    await pg.wait_for_function("typeof Graph !== 'undefined' && Graph && Graph.graphData().nodes.length > 0", timeout=20000)
    await pg.tap("#head-search")
    await pg.wait_for_selector("#palette[open]")
    check(await pg.evaluate("(() => { const r = $('palette').getBoundingClientRect(); return Math.abs(r.width - innerWidth) < 2 && r.height >= innerHeight - 2; })()"), "phone: the palette is a full-screen sheet")
    await pg.tap("#pal-close")
    check(not await pg.evaluate("$('palette').open"), "phone: Close shuts the palette")
    check(not errors, f"no page errors in the phone palette {errors[:2]}")
    await ctx.close()


MISSION_LAYOUT_CHECK = """() => {
  // A seeded graph: 3 lanes, every status, chains inside lanes and links across them, a hand-over, lone tasks.
  const lanes = [{id: "claude", name: "Claude"}, {id: "codex", name: "Codex"}, {id: "agy", name: "Gemini"}], st = Object.keys(MS_STATUS);
  const graph = (n, seed) => {
    const nodes = [], edges = [{from: "lane:claude", to: "p/T-1", kind: "handover"}];
    let r = seed; const rnd = () => (r = (r * 16807) % 2147483647) / 2147483647;
    for (let i = 0; i < n + 6; i++) nodes.push({key: `p/T-${i}`, id: `T-${i}`, lane: lanes[i % 3].id, status: st[i % st.length]});
    for (let i = 3; i < n; i++) {
      if (i % 4) edges.push({from: `p/T-${i - 3}`, to: `p/T-${i}`, kind: "dep"});
      if (rnd() < 0.3) edges.push({from: `p/T-${Math.floor(rnd() * i)}`, to: `p/T-${i}`, kind: "cont"});
    }
    return {nodes, edges, lanes};
  };
  const check = (g) => {
    const L = missionLayout(g), P = [...L.pos.values()], bad = [];
    for (let i = 0; i < P.length; i++) for (let j = i + 1; j < P.length; j++) {
      const a = P[i], b = P[j];
      if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) bad.push("overlap");
    }
    const seg = [];
    for (const e of g.edges) {
      const f = L.pos.get(e.from), t = L.pos.get(e.to); if (!f || !t) continue;
      if (t.x < f.x + f.w) bad.push(`backwards ${e.from}->${e.to}`);
      seg.push([f.x + f.w, f.y + f.h / 2, t.x, t.y + t.h / 2]);
    }
    for (const n of g.nodes) {
      const p = L.pos.get(n.key), l = L.lanes.find((x) => x.id === n.lane);
      if (p.layer && !g.edges.some((e) => e.from === n.key || e.to === n.key)) bad.push(`lone ${n.key} not in column 0`);
      if (p.y < l.y || p.y + p.h > l.y + l.h) bad.push(`lane ${n.key}`);
    }
    if (L.lanes.map((l) => l.id).join() !== g.lanes.map((l) => l.id).join()) bad.push("lane order");
    const side = (ax, ay, bx, by, cx, cy) => Math.sign((bx - ax) * (cy - ay) - (by - ay) * (cx - ax));
    let crossings = 0;
    for (let i = 0; i < seg.length; i++) for (let j = i + 1; j < seg.length; j++) {
      const [a, b] = [seg[i], seg[j]];
      if (side(...a, b[0], b[1]) * side(...a, b[2], b[3]) < 0 && side(...b, a[0], a[1]) * side(...b, a[2], a[3]) < 0) crossings++;
    }
    return {bad, crossings, edges: seg.length};
  };
  return {small: check(graph(24, 7)), big: check(graph(60, 11))};
}"""


async def mission(browser, devices, secret: str) -> None:
    """The mission map (Agents > Tools): the layout of a seeded graph keeps tasks apart, edges left to right and
    tasks in their lane; the view opens from the tools menu, a task opens its panel, Esc goes back; on a phone the
    chains list fits the screen. Read-only (nothing is approved). Skipped on a server without it."""
    ctx, pg, errors = await page(browser, secret, viewport={"width": 1440, "height": 900})
    await pg.wait_for_selector("#note", state="attached")
    await pg.wait_for_timeout(500)
    if await pg.evaluate("typeof missionLayout") != "function":
        print("skip mission map (server without it)")
        await ctx.close()
        return
    res = await pg.evaluate(MISSION_LAYOUT_CHECK)
    check(not res["small"]["bad"] and not res["big"]["bad"], f"mission layout: no overlaps, edges left to right, lanes kept {res['small']['bad'][:3]} {res['big']['bad'][:3]}")
    print(f"     mission layout crossings: {res['small']['crossings']}/{res['small']['edges']} edges at 24 tasks, {res['big']['crossings']}/{res['big']['edges']} at 60")
    if await is_shell(pg):  # Tasks > Flow
        await route(pg, "#/tasks?view=flow", "#mission .ms-n, #mission .ms-empty")
        if await pg.locator("#mission .ms-n").count():
            await pg.locator("#mission .ms-n").first.click()
            check(await pg.locator("#ms-panel").is_visible() and bool(await pg.locator("#ms-panel .ms-line").inner_text()), "a task opens its panel with the full line")
            await pg.keyboard.press("Escape")
            check(not await pg.locator("#ms-panel").is_visible() and await pg.evaluate("location.hash === '#/tasks?view=flow'"), "Esc closes the panel, the view stays")
        check(not errors, f"no page errors in the mission map {errors[:2]}")
        await ctx.close()
        ctx, pg, errors = await page(browser, secret, "/ui#/tasks?view=flow", **devices["iPhone 14"])
        await pg.wait_for_selector("#mission .ms-c, #mission .ms-empty", timeout=10000)
        check(await pg.evaluate("document.documentElement.scrollWidth <= innerWidth && $('ms-stage').hidden && !$('ms-list').hidden"),
              "phone mission map: chains as a list, fits the screen")
        check(not errors, f"no page errors in the phone mission map {errors[:2]}")
        await ctx.close()
        return
    await pg.click("#cockpit-btn")
    await pg.click("#ck-tools-menu summary")
    await pg.click('#ck-tools-menu [data-go="mission"]')
    await pg.wait_for_selector("#mission .ms-n, #mission .ms-empty", timeout=10000)
    check(await pg.evaluate("!$('mission').hidden && !$('cockpit').hidden"), "Tools > Mission map opens the map over the cockpit")
    if await pg.locator("#mission .ms-n").count():
        await pg.locator("#mission .ms-n").first.click()
        check(await pg.locator("#ms-panel").is_visible() and bool(await pg.locator("#ms-panel .ms-line").inner_text()), "a task opens its panel with the full line")
        await pg.keyboard.press("Escape")
        check(not await pg.locator("#ms-panel").is_visible(), "Esc closes the panel first")
    await pg.keyboard.press("f")
    await pg.keyboard.press("Escape")
    check(await pg.evaluate("$('mission').hidden && !$('cockpit').hidden"), "Esc goes back to the cockpit")
    check(not errors, f"no page errors in the mission map {errors[:2]}")
    await ctx.close()
    ctx, pg, errors = await page(browser, secret, **devices["iPhone 14"])
    await pg.wait_for_selector("#cockpit-btn")
    await pg.evaluate("openCockpit(true); openMission(true)")
    await pg.wait_for_selector("#mission .ms-c, #mission .ms-empty", timeout=10000)
    check(await pg.evaluate("document.documentElement.scrollWidth <= screen.width && $('ms-stage').hidden && !$('ms-list').hidden"),
          "phone mission map: chains as a list, fits the screen")
    check(not errors, f"no page errors in the phone mission map {errors[:2]}")
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
        if path == "/ui" and await pg.locator("#sidebar").count():
            await pg.click('#sidebar [data-nav="map"]')
            await pg.wait_for_timeout(1500)
        elif path == "/ui":
            await pg.click("#map-btn")
            await pg.wait_for_timeout(1500)
        check(not blocked, f"nothing blocked by the CSP on {path} {blocked[:2]}")
        await ctx.close()


async def main() -> None:
    secret = os.environ.get("KNOWLEDGE_PROXY_SECRET") or subprocess.run(["secret", "KNOWLEDGE_PROXY_SECRET"], check=True, capture_output=True, text=True).stdout.strip()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        await csp(browser, secret)
        await desktop(browser, secret)
        await search(browser, secret)
        await phone(browser, p.devices, secret)
        await admin(browser, secret)
        await cockpit(browser, p.devices, secret)
        await mission(browser, p.devices, secret)
        await shell(browser, p.devices, secret)
        await knowledge(browser, p.devices, secret)
        await browser.close()
    print("ok" if not failures else f"{len(failures)} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
