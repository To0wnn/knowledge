// App shell: the sidebar (desktop; [ folds it to an icon rail, below 1000 px it is the rail unless opened), the
// bottom tab bar and More sheet (phone), one page header per view, and a hash route per view: #/chats (Agents),
// #/inbox/<item key> (the same view, that item marked; #/inbox is an older name of #/chats), #/chats/<chat id>, #/chats/worker/<project>/<ID> (follow a worker), #/chats?view=list (the attention list a desktop otherwise shows beside an open chat), #/tasks, #/tasks?view=flow, #/machines, #/machines?view=folders, #/files, #/notes, #/notes/<path>, #/notes?q=<search>, #/map, #/map/<path>,
// #/changes, #/health, #/thispc. #/chats/<id>?solo=1 is one chat alone, in a tab of its own (a grid card's ↗): no sidebar, no list, no header; its logo goes home, × closes the tab. The URL is the only record of the view (refresh, back and forward work); an old
// #<note path> link becomes #/notes/<path>. Paths in routes are encoded per part (encPath), decoded here. The views themselves are app.js's, agents.js's and mission.js's; this file shows one
// at a time and asks it to load. The sidebar, More sheet and theme are nav.js's. Uses app.js globals ($, el, store, decode, pushHash, ck, Graph, mapOn, current, …).
"use strict";

// view -> where it lives (element id), its section label and title
const VIEWS = {
  inbox: ["cockpit", "Agents", "Agents"], chats: ["cockpit", "Agents", "Agents"], tasks: ["tab-tasks", "Agents", "Tasks"],
  machines: ["tab-machines", "Agents", "Machines"], files: ["tab-machines", "Agents", "Files"], notes: ["kn", "Knowledge", "Notes"], map: ["kn", "Knowledge", "Map"],
  changes: ["tab-changes", "Knowledge", "Changes"], health: ["tab-lint", "Knowledge", "Health"], thispc: ["tab-thispc", "This pc", "This pc"],
  settings: ["tab-settings", "You", "Settings"],
};
const PHONE_TABS = ["chats", "machines", "notes", "map"]; // the rest sit behind More
// Without a token whose pc may be steered from here (me.agents false) the page is the knowledge part only: the agent
// views go to Notes, and Map and Changes take their places in the tab bar (nav.html ag-on / ag-off).
const AGENT_VIEWS = ["inbox", "chats", "machines", "files"];
const agentsOff = () => me.agents === false;
const phoneTabs = () => (agentsOff() ? ["tasks", "notes", "map", "changes"] : PHONE_TABS);
const startRoute = () => { const v = me.prefs?.start_page || "chats"; return agentsOff() && AGENT_VIEWS.includes(v) ? "#/notes" : `#/${v}`; }; // Settings
const solo = () => document.body.classList.contains("solo");
let shellRoute = { view: "", arg: "", q: new URLSearchParams() };

// "#/notes/memory/x.md?a=b" -> {view: "notes", arg: "memory/x.md", q}; null for anything else.
function parseRoute(hash) {
  const m = hash.match(/^#\/([a-z]+)\/?([^?]*)(?:\?(.*))?$/);
  return m && VIEWS[m[1]] ? { view: m[1], arg: decode(m[2]), q: new URLSearchParams(m[3] || "") } : null;
}
const oldLink = (hash) => { const p = decode(hash.slice(1)); return p.includes("/") && !p.startsWith("/") ? p : ""; };
// The view the URL asks for, before it is shown (app.js decides from it what to load first).
const routeView = () => { const v = parseRoute(location.hash)?.view || (oldLink(location.hash) ? "notes" : startRoute().slice(2)); return agentsOff() && AGENT_VIEWS.includes(v) ? "notes" : v; };

function go(hash, replace = false) {
  if (replace) history.replaceState(history.state, "", hash); else pushHash(hash);
  applyRoute();
}
// The Back arrow goes up to `up` (a chat to the chat list, a file to its folder), never to whatever came before: the
// browser's own Back when the entry before is that page (`ok` also accepts another URL of it), else a step to it.
function goBack(up, ok = (h) => h === up) { if (history.state?.n > 0 && ok(history.state.from || "")) history.back(); else go(up); }

// Where you were per view (its last route, and how far its lists were scrolled), for this tab: the sidebar goes back
// there, not to the view's start. Clicking the view you are on goes to its start.
const lastRoute = (() => { try { return JSON.parse(sessionStorage.getItem("kn_routes") || "{}"); } catch { return {}; } })();
const scrolled = new Map(); // route -> {element id: scrollTop}
function keepScroll() {
  const r = shellRoute; if (!r.view) return;
  const home = $(VIEWS[r.view][0]), at = {};
  for (const e of [home, ...home.querySelectorAll("[id]")]) if (e.scrollTop > 0) at[e.id] = e.scrollTop;
  if (window.scrollY > 0) at["@window"] = window.scrollY; // a phone scrolls the page
  scrolled.set(location.hash, at);
}
function restoreScroll(hash) {
  const at = scrolled.get(hash); if (!at) return;
  const you = new AbortController(); // once you scroll or click yourself, a retry would throw you back
  for (const ev of ["wheel", "touchstart", "pointerdown", "keydown"]) addEventListener(ev, () => you.abort(), { capture: true, passive: true, signal: you.signal });
  const put = () => { if (!you.signal.aborted) for (const [id, top] of Object.entries(at)) if (id === "@window") window.scrollTo(0, top); else if ($(id)) $(id).scrollTop = top; };
  requestAnimationFrame(put); setTimeout(put, 400); setTimeout(() => { put(); you.abort(); }, 1200); // lists render after their data comes
}
function applyRoute() {
  let r = parseRoute(location.hash);
  if (!r || (agentsOff() && AGENT_VIEWS.includes(r.view))) {
    history.replaceState(history.state, "", r || !oldLink(location.hash) ? startRoute() : noteRoute(oldLink(location.hash))); r = parseRoute(location.hash);
  }
  // Tasks is the task list for a person without Agents; with Agents the tasks are there (Needs you, Queued)
  if (r.view === "tasks" && !agentsOff()) { history.replaceState(history.state, "", "#/chats?view=list"); r = parseRoute(location.hash); }
  if (r.view === "inbox") { // the same view under its older name; a key marks that item once the board is in (ckFocus)
    if (r.arg) { ck.focus = r.arg; ck.focusAt = Date.now(); } else ck.hit = "";
    history.replaceState(history.state, "", "#/chats?view=list"); r = parseRoute(location.hash); // the item is in the list, not the grid
  }
  // a chat alone in its tab is no place to come back to and leaves the other tabs' last chat alone
  document.body.classList.toggle("solo", r.view === "chats" && !!r.arg && r.q.has("solo"));
  if (r.arg !== "new" && !solo()) { lastRoute[r.view] = location.hash; try { sessionStorage.setItem("kn_routes", JSON.stringify(lastRoute)); } catch { /* blocked */ } }
  const [home] = VIEWS[r.view], changed = r.view !== shellRoute.view, first = !shellRoute.view;
  shellRoute = r;
  document.body.dataset.view = r.view;
  for (const id of new Set(Object.values(VIEWS).map((v) => v[0]))) $(id).hidden = id !== home;
  closeSide();
  paintHead();
  if (changed && !first) {
    window.scrollTo(0, 0); // a phone scrolls the page, not the view
    if (!$("page-head").contains(document.activeElement)) $("title").focus({ preventScroll: true }); // a screen reader hears the new page
  }
  // Agents: polling runs only while the cockpit shows.
  const agents = home === "cockpit";
  $("cockpit").dataset.mode = r.view;
  // a chat route carries the chat id; the card is keyed by pc and id (ckSummary resolves an id it does not know yet)
  const subRoute = r.view === "chats" && /^([0-9a-f]{12})\/sub\/(.+)$/.exec(r.arg || "");
  if (subRoute) { // #/chats/<chat>/sub/<id>: a subagent as a chat of its own
    const c = ck.chats.find((x) => x.id === subRoute[1]);
    ck.view = c ? ckKey(ckSubCard(c, subRoute[2]).meta) : r.arg; // a reload: ckSummary resolves it once the chats are known
  } else if (r.view === "chats" && r.arg === "new") { // #/chats/new: the draft (kept with what is typed, unless a pc asks for a new one)
    const d = ck.cards.get(ckKey(CK_DRAFT));
    ck.view = ckKey(d && !ck.draftFor ? d.meta : ckDraft(ck.draftFor || "").meta); ck.draftFor = "";
  }
  else if (r.view === "chats") {
    const c = ck.chats.find((x) => x.id === r.arg || ckKey(x) === r.arg), hit = !c && ckFindWorker(r.arg);
    ck.view = c ? ckKey(c) : hit ? ckKey(ckWorkerCard(hit.m, hit.w).meta) : r.arg;
    if (/^[0-9a-f]{12}$/.test(r.arg || "") && !solo()) store.set("kn_last_chat", r.arg); // the list opens it again next time
  }
  ckLayout();
  if (agents) {
    ckList();
    const card = ck.cards.get(ck.view);
    if (card && !touch) card.input.focus({ preventScroll: true }); // a phone would pop its keyboard up
  }
  ckRun(agents);
  if (r.view === "tasks") viewLoad("tasks", "Tasks", () => loadTasks(true));
  // Machines: Pcs (the comparison) or Folders (per project); one load, two drawings of it
  // Files (#/files) is the same view's third drawing, with its own place in the sidebar
  if (r.view !== "files") fp.audio.pause(); // the player shows in Files only: nothing plays out of sight
  machinesMode = r.view === "files" ? "files" : r.view === "machines" && r.q.get("view") === "folders" ? "folders" : "pcs";
  for (const b of $("machines-mode").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.mode === machinesMode));
  if (r.view === "files" && !changed && machinesRes) drawMachines(); // a step inside Files: the pc list stays
  else if (r.view === "machines" || r.view === "files") viewLoad("machines", VIEWS[r.view][2], loadMachines);
  document.body.classList.toggle("fm-detail", r.view === "files" && r.q.has("p")); // a phone shows the Back arrow
  // Notes and Map: the topic list and right column, the note, the search results or the map in the middle. On a
  // phone the list and the note are two pages: a note or a search hides the list and shows the Back arrow.
  const q = r.view === "notes" ? r.q.get("q") : null; // null: no search; "" an empty search page
  document.body.classList.toggle("kn-detail", r.view === "notes" && (!!r.arg || q !== null));
  document.body.classList.toggle("kn-search", q !== null);
  if (home === "kn") {
    if ((r.view === "map") !== mapOn) setMap(r.view === "map");
    $("results").hidden = q === null; if (!mapOn) $("note").hidden = q !== null;
    if (r.arg) { if (current?.path !== r.arg) openNote(r.arg, { push: false }); }
    else if (current) deselect({ push: false });
    else if (changed) homeView();
    if (q !== null && $("q").value !== q) { $("q").value = q; runSearch(); }
  } else if (mapOn) setMap(false);
  if (r.view === "changes") viewLoad("recent", "Changes", () => loadRecent(true));
  if (r.view === "health") viewLoad("lint", "Health", () => loadLint(true));
  if (r.view === "thispc") loadThisPc();
  if (r.view === "settings") loadSettings();
  // The map's canvas follows its box: the sidebar or the view may have changed its size.
  if (Graph && mapOn) requestAnimationFrame(() => Graph.width($("graph").clientWidth).height($("graph").clientHeight));
  if (changed && !first) restoreScroll(location.hash);
}
window.addEventListener("popstate", applyRoute);
// A plain click on a route link goes through go(), so its history entry counts for Back (a new tab still works).
document.addEventListener("click", (e) => {
  const a = e.target.closest('a[href^="#/"]:not([target])');
  if (!a || e.defaultPrevented || e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  keepScroll();
  const view = a.dataset.nav, back = view && view !== shellRoute.view && lastRoute[view];
  go(back && a.getAttribute("href") === `#/${view}` ? back : a.getAttribute("href")); // the sidebar: where you were in that view
});

// The browser tab: the open chat's name or the view, "(n)" in front for what needs you (as Gmail), so tabs tell apart.
let needsN = 0;
function paintTitle() {
  if (!shellRoute.view) return;
  const card = document.body.classList.contains("in-chat") && ck.cards.get(ck.view);
  const n = card ? +(ckState(card.meta) === "needs") : needsN;
  document.title = `${n ? `(${n}) ` : ""}${card ? ckTitle(card.meta, card) : VIEWS[shellRoute.view][2]} · Knowledge`;
}
// One header for every view: section label and title, the view's own actions on the right.
function paintHead() {
  const r = shellRoute; if (!r.view) return;
  const [, sec, title] = VIEWS[r.view];
  $("crumb").textContent = sec === "Knowledge" ? `Knowledge · ${pickName()}` : sec;
  $("crumb").hidden = sec === title; // "Agents › Agents" says nothing twice
  $("title").textContent = title;
  paintTitle();
  for (const a of $("head-actions").querySelectorAll("[data-views]")) a.hidden = !a.dataset.views.split(" ").includes(r.view);
  for (const a of document.querySelectorAll("[data-nav]")) {
    if (a.dataset.nav === r.view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  if (phoneTabs().includes(r.view)) $("more-btn").removeAttribute("aria-current"); else $("more-btn").setAttribute("aria-current", "page");
}
$("back").onclick = () => (shellRoute.view === "chats" && ck.view ? ckShow(null) : shellRoute.view === "files" ? (!filesDirty() || confirm("Leave this file? Your unsaved edits are lost.")) && goBack(filesUp())
  : shellRoute.view === "notes" ? goBack("#/notes", (h) => { const r = parseRoute(h); return r?.view === "notes" && !r.arg; }) // the list, or the search you came from
  : goBack(startRoute()));

// The Agents views follow the one project picker (sidebar, More sheet; "" = all projects); the Needs-you badge counts all.
function setAgentProject(p) {
  agentProject = p; // also when it stays "": another pick of several
  if (shellRoute.view !== "tasks") loadTasks(false).catch(() => {}); // its badge counts this project
  if (shellRoute.view === "chats") { ckList(); bdRender(); const d = ck.cards.get(ckKey(CK_DRAFT)); if (d) ckPoll(d); }
  else if (shellRoute.view === "tasks") {
    viewLoad("tasks", "Tasks", () => loadTasks(true));
  }
}
function inboxCount(items) {
  const n = items.filter((i) => i.col === "needs").length;
  for (const b of document.querySelectorAll(".inbox-count")) b.textContent = n || "";
  needsN = n; paintTitle();
  needsNotify(items);
}
// Browser notifications (Settings, per browser): each new Needs-you item while the tab is out of view (three, then one
// "and N more"); the first board after loading only sets what was there already. What was seen survives a reload.
const notifyOn = () => store.get("kn_notify") === "1" && window.Notification?.permission === "granted";
let notifySeen = (() => { try { const v = JSON.parse(sessionStorage.getItem("kn_notify_seen")); return Array.isArray(v) ? new Set(v) : null; } catch { return null; } })();
function needsNotify(items) {
  const needs = items.filter((i) => i.col === "needs"), sig = (i) => `${i.key}:${i.sig || ""}`;
  const fresh = notifySeen && notifyOn() && document.hidden ? needs.filter((x) => !notifySeen.has(sig(x))) : [];
  const say = (title, body, tag, to) => {
    try {
      const n = new Notification(title, { body: body.slice(0, 180), tag });
      n.onclick = () => { window.focus(); if (!solo() || to !== `#/chats/${encodeURIComponent(shellRoute.arg)}`) go(to); n.close(); }; // a chat alone in its tab stays alone
    } catch { /* a browser that only notifies from a service worker (Android): Matrix covers phones */ }
  };
  for (const i of fresh.slice(0, 3)) say(`Needs you: ${i.title || "an item"}`, [i.label || shown(i.project), i.line].filter(Boolean).join(" · "), i.key,
    i.kind === "chat" && i.id ? `#/chats/${encodeURIComponent(i.id)}` : `#/inbox/${encodeURIComponent(i.key)}`);
  if (fresh.length > 3) say(`Needs you: and ${fresh.length - 3} more`, fresh.slice(3, 8).map((i) => i.title).filter(Boolean).join(" · "), "kn-needs-more", "#/chats");
  notifySeen = new Set(needs.map(sig));
  try { sessionStorage.setItem("kn_notify_seen", JSON.stringify([...notifySeen])); } catch { /* blocked */ }
}
// Off the Agents views the Needs-you badge and the live count still follow: a light refresh every 30 s (also out of view
// when notifications are on, which is when they matter).
setInterval(() => { if ($("cockpit").hidden && (!document.hidden || notifyOn()) && me.user && !agentsOff()) ckSummary().catch(() => {}); }, 30000);

for (const b of $("machines-mode").querySelectorAll("button")) b.onclick = () => go(b.dataset.mode === "folders" ? "#/machines?view=folders" : "#/machines");

// Search or jump: the command palette (palette.js).
$("jump").onclick = () => openPalette();
$("head-search").onclick = () => openPalette();
