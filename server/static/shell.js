// App shell: the sidebar (desktop; [ folds it to an icon rail, below 1000 px it is the rail unless opened), the
// bottom tab bar and More sheet (phone), one page header per view, and a hash route per view: #/chats (Agents),
// #/inbox/<item key> (the same view, that item marked; #/inbox is an older name of #/chats), #/chats/<chat id>, #/chats/worker/<project>/<ID> (follow a worker), #/chats?view=board, #/tasks, #/tasks?view=flow, #/machines, #/notes, #/notes/<path>, #/notes?q=<search>, #/map, #/map/<path>,
// #/changes, #/health, #/thispc. The URL is the only record of the view (refresh, back and forward work); an old
// #<note path> link becomes #/notes/<path>. Paths in routes are encoded per part (encPath), decoded here. The views themselves are app.js's and mission.js's; this file shows one
// at a time and asks it to load. The sidebar, More sheet and theme are nav.js's. Uses app.js globals ($, el, store, decode, pushHash, ck, Graph, mapOn, current, …).
"use strict";

// view -> where it lives (element id), its section label and title
const VIEWS = {
  inbox: ["cockpit", "Agents", "Agents"], chats: ["cockpit", "Agents", "Agents"], tasks: ["tab-tasks", "Agents", "Tasks"],
  machines: ["tab-machines", "Agents", "Machines"], notes: ["kn", "Knowledge", "Notes"], map: ["kn", "Knowledge", "Map"],
  changes: ["tab-changes", "Knowledge", "Changes"], health: ["tab-lint", "Knowledge", "Health"], thispc: ["tab-thispc", "This pc", "This pc"],
  settings: ["tab-settings", "You", "Settings"],
};
const PHONE_TABS = ["chats", "tasks", "machines", "notes"]; // the rest sit behind More
let shellRoute = { view: "", arg: "", q: new URLSearchParams() };

// "#/notes/memory/x.md?a=b" -> {view: "notes", arg: "memory/x.md", q}; null for anything else.
function parseRoute(hash) {
  const m = hash.match(/^#\/([a-z]+)\/?([^?]*)(?:\?(.*))?$/);
  return m && VIEWS[m[1]] ? { view: m[1], arg: decode(m[2]), q: new URLSearchParams(m[3] || "") } : null;
}
const oldLink = (hash) => { const p = decode(hash.slice(1)); return p.includes("/") && !p.startsWith("/") ? p : ""; };
// The view the URL asks for, before it is shown (app.js decides from it what to load first).
const routeView = () => parseRoute(location.hash)?.view || (oldLink(location.hash) ? "notes" : "chats");

function go(hash, replace = false) {
  if (replace) history.replaceState(history.state, "", hash); else pushHash(hash);
  applyRoute();
}
// Back inside the app (a phone's back button does the same); the fallback when this page was the first.
function goBack(fallback) { if (history.state?.n > 0) history.back(); else go(fallback, true); }

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
  const put = () => { for (const [id, top] of Object.entries(at)) if (id === "@window") window.scrollTo(0, top); else if ($(id)) $(id).scrollTop = top; };
  requestAnimationFrame(put); setTimeout(put, 400); setTimeout(put, 1200); // lists render after their data comes
}
function applyRoute() {
  let r = parseRoute(location.hash);
  if (!r) { history.replaceState(history.state, "", oldLink(location.hash) ? noteRoute(oldLink(location.hash)) : "#/chats"); r = parseRoute(location.hash); }
  if (r.view === "inbox") { // the same view under its older name; a key marks that item once the board is in (ckFocus)
    if (r.arg) { ck.focus = r.arg; ck.focusAt = Date.now(); } else ck.hit = "";
    history.replaceState(history.state, "", "#/chats"); r = parseRoute(location.hash);
  }
  if (r.arg !== "new") { lastRoute[r.view] = location.hash; try { sessionStorage.setItem("kn_routes", JSON.stringify(lastRoute)); } catch { /* blocked */ } }
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
    ck.view = c ? ckKey(ckSubCard(c, subRoute[2]).meta) : "";
  } else if (r.view === "chats" && r.arg === "new") { // #/chats/new: the draft (kept with what is typed, unless a pc asks for a new one)
    const d = ck.cards.get(ckKey(CK_DRAFT));
    ck.view = ckKey(d && !ck.draftFor ? d.meta : ckDraft(ck.draftFor || "").meta); ck.draftFor = "";
  }
  else if (r.view === "chats") { const c = ck.chats.find((x) => x.id === r.arg || ckKey(x) === r.arg), hit = !c && ckFindWorker(r.arg); ck.view = c ? ckKey(c) : hit ? ckKey(ckWorkerCard(hit.m, hit.w).meta) : r.arg; }
  ckLayout();
  if (agents) {
    ckList();
    const card = ck.cards.get(ck.view);
    if (card && !touch) card.input.focus({ preventScroll: true }); // a phone would pop its keyboard up
  }
  ckRun(agents);
  // Tasks: List (the task lines) or Flow (the mission map).
  const flow = r.view === "tasks" && r.q.get("view") === "flow";
  $("tab-tasks").classList.toggle("flow", flow);
  for (const b of $("tasks-mode").querySelectorAll("button")) b.setAttribute("aria-pressed", String((b.dataset.mode === "flow") === flow));
  if (flow) openMission(true); else if (ms.root) openMission(false);
  if (r.view === "tasks" && !flow) loadTasks(true).catch((e) => $("tasks").replaceChildren(el("p", "error", e.message)));
  if (r.view === "machines") loadMachines().catch((e) => $("machines").replaceChildren(el("p", "error", `Machines: ${e.message}`)));
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
  if (r.view === "changes") loadRecent(true);
  if (r.view === "health") loadLint(true);
  if (r.view === "thispc") loadThisPc();
  if (r.view === "settings") loadSettings();
  // The map's canvas follows its box: the sidebar or the view may have changed its size.
  if (Graph && mapOn) requestAnimationFrame(() => Graph.width($("graph").clientWidth).height($("graph").clientHeight));
  if (changed && !first) restoreScroll(location.hash);
}
window.addEventListener("popstate", applyRoute);
// A plain click on a route link goes through go(), so its history entry counts for Back (a new tab still works).
document.addEventListener("click", (e) => {
  const a = e.target.closest('a[href^="#/"]');
  if (!a || e.defaultPrevented || e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  keepScroll();
  const view = a.dataset.nav, back = view && view !== shellRoute.view && lastRoute[view];
  go(back && a.getAttribute("href") === `#/${view}` ? back : a.getAttribute("href")); // the sidebar: where you were in that view
});

// One header for every view: section label and title, the view's own actions on the right.
function paintHead() {
  const r = shellRoute; if (!r.view) return;
  const [, sec, title] = VIEWS[r.view];
  $("crumb").textContent = sec === "Knowledge" ? `Knowledge · ${project === ALL ? "all projects" : shown(project)}` : sec;
  $("title").textContent = title;
  document.title = `${title} · Knowledge`;
  for (const a of $("head-actions").querySelectorAll("[data-views]")) a.hidden = !a.dataset.views.split(" ").includes(r.view);
  for (const a of document.querySelectorAll("[data-nav]")) {
    if (a.dataset.nav === r.view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  if (PHONE_TABS.includes(r.view)) $("more-btn").removeAttribute("aria-current"); else $("more-btn").setAttribute("aria-current", "page");
}
$("back").onclick = () => (shellRoute.view === "chats" && ck.view ? ckShow(null) : goBack(shellRoute.view === "notes" ? "#/notes" : "#/chats"));

// The Agents views follow the one project picker (sidebar, More sheet; "" = all projects); the Needs-you badge counts all.
function setAgentProject(p) {
  if (p === agentProject) return;
  agentProject = p;
  if (shellRoute.view !== "tasks") loadTasks(false).catch(() => {}); // its badge counts this project
  if (shellRoute.view === "chats") { ckList(); bdRender(); const d = ck.cards.get(ckKey(CK_DRAFT)); if (d) ckPoll(d); }
  else if (shellRoute.view === "tasks") {
    if ($("tab-tasks").classList.contains("flow")) { ms.project = p; ms.fitted = false; msLoad(); } else loadTasks(true);
  }
}
function inboxCount(items) {
  const n = items.filter((i) => i.col === "needs").length;
  for (const b of document.querySelectorAll(".inbox-count")) b.textContent = n || "";
  needsNotify(items);
}
// Browser notifications (Settings, per browser): each new Needs-you item while the tab is out of view; the first
// board after loading only sets what was there already.
const notifyOn = () => store.get("kn_notify") === "1" && window.Notification?.permission === "granted";
let notifySeen = null;
function needsNotify(items) {
  const needs = items.filter((i) => i.col === "needs"), sig = (i) => `${i.key}:${i.sig || ""}`;
  if (notifySeen && notifyOn() && document.hidden) for (const i of needs.filter((x) => !notifySeen.has(sig(x))).slice(0, 3)) {
    try {
      const n = new Notification(`Needs you: ${i.title || "an item"}`, { body: [i.label || i.project, i.line].filter(Boolean).join(" · ").slice(0, 180), tag: i.key });
      n.onclick = () => { window.focus(); go(i.kind === "chat" && i.id ? `#/chats/${encodeURIComponent(i.id)}` : `#/inbox/${encodeURIComponent(i.key)}`); n.close(); };
    } catch { /* a browser that only notifies from a service worker (Android): Matrix covers phones */ }
  }
  notifySeen = new Set(needs.map(sig));
}
// Off the Agents views the Needs-you badge and the live count still follow: a light refresh every 30 s (also out of view
// when notifications are on, which is when they matter).
setInterval(() => { if ($("cockpit").hidden && (!document.hidden || notifyOn()) && me.user) ckSummary().catch(() => {}); }, 30000);

for (const b of $("tasks-mode").querySelectorAll("button")) b.onclick = () => go(b.dataset.mode === "flow" ? "#/tasks?view=flow" : "#/tasks");

// Search or jump: the command palette (palette.js).
$("jump").onclick = () => openPalette();
$("head-search").onclick = () => openPalette();
