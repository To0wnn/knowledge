// App shell: the sidebar (desktop; [ folds it to an icon rail, below 1000 px it is the rail unless opened), the
// bottom tab bar and More sheet (phone), one page header per view, and a hash route per view: #/inbox, #/inbox/<item key>, #/chats,
// #/chats/<chat id>, #/chats?view=board, #/tasks, #/tasks?view=flow, #/machines, #/notes, #/notes/<path>, #/notes?q=<search>, #/map, #/map/<path>,
// #/changes, #/health, #/thispc. The URL is the only record of the view (refresh, back and forward work); an old
// #<note path> link becomes #/notes/<path>. Paths in routes are encoded per part (encPath), decoded here. The views themselves are app.js's and mission.js's; this file shows one
// at a time and asks it to load. The sidebar, More sheet and theme are nav.js's. Uses app.js globals ($, el, store, decode, pushHash, ck, Graph, mapOn, current, …).
"use strict";

// view -> where it lives (element id), its section label and title
const VIEWS = {
  inbox: ["cockpit", "Agents", "Inbox"], chats: ["cockpit", "Agents", "Chats"], tasks: ["tab-tasks", "Agents", "Tasks"],
  machines: ["tab-machines", "Agents", "Machines"], notes: ["kn", "Knowledge", "Notes"], map: ["kn", "Knowledge", "Map"],
  changes: ["tab-changes", "Knowledge", "Changes"], health: ["tab-lint", "Knowledge", "Health"], thispc: ["tab-thispc", "This pc", "This pc"],
};
const PHONE_TABS = ["inbox", "chats", "tasks", "notes"]; // the rest sit behind More
let shellRoute = { view: "", arg: "", q: new URLSearchParams() };

// "#/notes/memory/x.md?a=b" -> {view: "notes", arg: "memory/x.md", q}; null for anything else.
function parseRoute(hash) {
  const m = hash.match(/^#\/([a-z]+)\/?([^?]*)(?:\?(.*))?$/);
  return m && VIEWS[m[1]] ? { view: m[1], arg: decode(m[2]), q: new URLSearchParams(m[3] || "") } : null;
}
const oldLink = (hash) => { const p = decode(hash.slice(1)); return p.includes("/") && !p.startsWith("/") ? p : ""; };
// The view the URL asks for, before it is shown (app.js decides from it what to load first).
const routeView = () => parseRoute(location.hash)?.view || (oldLink(location.hash) ? "notes" : "inbox");

function go(hash, replace = false) {
  if (replace) history.replaceState(history.state, "", hash); else pushHash(hash);
  applyRoute();
}
// Back inside the app (a phone's back button does the same); the fallback when this page was the first.
function goBack(fallback) { if (history.state?.n > 0) history.back(); else go(fallback, true); }

function applyRoute() {
  let r = parseRoute(location.hash);
  if (!r) { history.replaceState(history.state, "", oldLink(location.hash) ? noteRoute(oldLink(location.hash)) : "#/inbox"); r = parseRoute(location.hash); }
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
  // Agents: Inbox and Chats share the cockpit; polling runs only while one of them shows.
  const agents = home === "cockpit";
  $("cockpit").dataset.mode = r.view;
  // a chat route carries the chat id; the card is keyed by pc and id (ckSummary resolves an id it does not know yet)
  if (r.view === "chats") { const c = ck.chats.find((x) => x.id === r.arg || ckKey(x) === r.arg); ck.view = c ? ckKey(c) : r.arg; } else if (r.view === "inbox") { ck.view = ""; ck.focus = r.arg; ck.focusAt = Date.now(); if (!r.arg) ck.hit = ""; }
  ckLayout();
  if (agents) {
    if (r.view === "inbox") ckBoard(); else ckHistory();
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
  // The map's canvas follows its box: the sidebar or the view may have changed its size.
  if (Graph && mapOn) requestAnimationFrame(() => Graph.width($("graph").clientWidth).height($("graph").clientHeight));
}
window.addEventListener("popstate", applyRoute);
// A plain click on a route link goes through go(), so its history entry counts for Back (a new tab still works).
document.addEventListener("click", (e) => {
  const a = e.target.closest('a[href^="#/"]');
  if (!a || e.defaultPrevented || e.button || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  e.preventDefault(); go(a.getAttribute("href"));
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
  const pick = $("agent-project");
  if (pick.options.length !== PROJECTS.length + 1) pick.replaceChildren(new Option("All projects", ""), ...PROJECTS.map((p) => new Option(shown(p), p)));
  pick.value = agentProject;
}
$("back").onclick = () => (shellRoute.view === "chats" && ck.view ? ckShow(null) : goBack(shellRoute.view === "notes" ? "#/notes" : "#/inbox"));

// The Agents views' project filter: all projects unless one is picked; the Inbox badge always counts all.
function setAgentProject(p) {
  agentProject = p; $("agent-project").value = p;
  if (shellRoute.view === "inbox") ckBoard();
  else if (shellRoute.view === "chats") { ckHistory(); bdRender(); }
  else if (shellRoute.view === "tasks") {
    if ($("tab-tasks").classList.contains("flow")) { ms.project = p; ms.fitted = false; msLoad(); } else loadTasks(true);
  }
}
$("agent-project").onchange = () => setAgentProject($("agent-project").value);
function inboxCount(items) {
  const n = items.filter((i) => i.col === "needs").length;
  for (const b of document.querySelectorAll(".inbox-count")) b.textContent = n || "";
}
// Off the Agents views the Inbox badge and the live count still follow: a light refresh every 30 s.
setInterval(() => { if ($("cockpit").hidden && !document.hidden && me.user) ckSummary().catch(() => {}); }, 30000);

for (const b of $("tasks-mode").querySelectorAll("button")) b.onclick = () => go(b.dataset.mode === "flow" ? "#/tasks?view=flow" : "#/tasks");

// Search or jump: the command palette (palette.js).
$("jump").onclick = () => openPalette();
$("head-search").onclick = () => openPalette();
