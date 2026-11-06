// The navigation every signed-in page shares (nav.html): the sidebar and its icon rail, the phone's More sheet, the
// theme switch and who is signed in. The app (index.html) routes its views itself (shell.js); the admin and help pages
// are pages of their own, so here they mark their own entry and fetch who is signed in. Globals: toggleRail,
// closeSide, navMe (app.js, shell.js and palette.js call them), setTheme (Settings).
"use strict";
let navProjects = null; // the signed-in person's projects (/ui/api/me): the lint badge counts as the app does

const navEl = (id) => document.getElementById(id);
const navStore = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* blocked: not remembered */ } } };

// Theme: Auto (the system's), Light or Dark; theme.js applies the stored pick before the page paints. The sidebar has
// it as an icon, the More sheet as a labelled button. The app listens for "kn-theme" to recolour the map.
const NAV_THEMES = ["auto", "light", "dark"];
function setTheme(next) {
  if (next === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
  navStore.set("kn_theme", next); showTheme(); dispatchEvent(new Event("kn-theme"));
}
function showTheme() {
  const name = `Theme: ${{ auto: "Auto", light: "Light", dark: "Dark" }[document.documentElement.dataset.theme || "auto"]}`;
  const pick = navEl("set-theme"); if (pick) pick.value = document.documentElement.dataset.theme || "auto"; // Settings
  for (const b of document.querySelectorAll(".theme-btn")) { b.title = name; b.setAttribute("aria-label", name); if (!b.querySelector("svg")) b.textContent = name; }
}
for (const b of document.querySelectorAll(".theme-btn")) b.onclick = () => setTheme(NAV_THEMES[(NAV_THEMES.indexOf(document.documentElement.dataset.theme || "auto") + 1) % NAV_THEMES.length]);
showTheme();

// The sidebar: wide screens keep the viewer's choice (stored); narrow ones show the rail and open it over the page.
const wide = matchMedia("(min-width: 1000px)");
let sideOpen = false;
function paintRail() {
  const rail = wide.matches ? navStore.get("kn_rail") === "1" : !sideOpen;
  document.body.classList.toggle("rail", rail);
  navEl("collapse").setAttribute("aria-expanded", String(!rail));
  navEl("collapse").setAttribute("aria-label", rail ? "Expand the sidebar" : "Collapse the sidebar");
  navEl("collapse").title = `${rail ? "Expand" : "Collapse"} the sidebar ( [ )`;
}
function toggleRail() {
  if (wide.matches) navStore.set("kn_rail", document.body.classList.contains("rail") ? "0" : "1"); else sideOpen = !sideOpen;
  paintRail();
}
// A new view or page: the sidebar opened over it and the More sheet close.
function closeSide() {
  if (sideOpen) { sideOpen = false; paintRail(); }
  if (navEl("more-sheet").open) navEl("more-sheet").close();
}
navEl("collapse").onclick = toggleRail;
wide.addEventListener("change", () => { sideOpen = false; paintRail(); });
document.addEventListener("click", (e) => { if (sideOpen && !navEl("sidebar").contains(e.target)) { sideOpen = false; paintRail(); } });
// Every open menu (user, + Add, a row's ⋯, the model chip) closes on a click outside it or on one of its own actions.
document.addEventListener("click", (e) => {
  for (const d of document.querySelectorAll("details.menu[open]")) if (!d.contains(e.target) || e.target.closest(".menu-list a, .menu-list button")) d.open = false;
});
paintRail();

// Sign out: what was typed but not sent stays with the person, not with the browser (agents.js ckDrafts).
for (const a of document.querySelectorAll('a[href="/ui/logout"]')) a.addEventListener("click", () => { try { localStorage.removeItem("kn_drafts"); } catch { /* blocked: nothing kept either */ } });

// Phone: More opens a sheet with the other views, the project, theme and sign out.
navEl("more-btn").onclick = () => navEl("more-sheet").showModal();
navEl("more-sheet").addEventListener("click", (e) => { if (e.target === navEl("more-sheet") || e.target.closest("a")) navEl("more-sheet").close(); });

// Who is signed in (/ui/api/me): the user menu, and what an admin sees that others do not.
function navMe(me) {
  document.body.classList.toggle("no-agents", me.agents === false); // no token with remote control on: the knowledge views only
  for (const a of document.querySelectorAll(".admin-link")) { // non-admins manage their own tokens there
    (a.querySelector(".label") || a).textContent = me.admin ? "Admin" : "My tokens & projects"; a.title = me.admin ? "Admin" : "My tokens & projects";
  }
  const avatar = document.createElement("span"), name = document.createElement("span");
  avatar.className = "avatar"; avatar.textContent = (me.user || "?")[0].toUpperCase(); name.className = "label"; name.textContent = me.user;
  navEl("who").replaceChildren(avatar, name);
  navEl("who").title = `Signed in as ${me.user}`; navEl("who-more").textContent = me.user;
  if (!me.admin) for (const a of document.querySelectorAll(".nav-health")) a.hidden = true;
  if (me.projects && me.projects.length) {
    for (const s of document.querySelectorAll(".project-pick")) {
      s.replaceChildren(...me.projects.map((p) => {
        const opt = document.createElement("option");
        opt.value = p; opt.textContent = me.labels?.[p] || p;
        return opt;
      }));
      s.onchange = () => { location.href = `/ui#/notes?project=${encodeURIComponent(s.value)}`; };
    }
  }
}

// The admin and help pages: their own entry is the current one (on a phone it sits behind More); search and the
// views are the app's.
const navPage = document.body.dataset.page;
if (navPage) {
  for (const a of document.querySelectorAll(`[data-nav="${navPage}"]`)) a.setAttribute("aria-current", "page");
  navEl("more-btn").setAttribute("aria-current", "page");
  navEl("jump").onclick = () => { location.href = "/ui#/notes?q="; };
  navEl("live")?.classList.add("on");
  document.addEventListener("keydown", (e) => {
    // on admin/help (no palette there) Ctrl/Cmd+K goes to the app's search, as the button says
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && typeof openPalette !== "function") { e.preventDefault(); location.href = "/ui#/notes?q="; return; }
    if (e.target.closest("input, textarea, select, [contenteditable]") || !e.altKey || e.ctrlKey || e.metaKey) return;
    if (typeof openPalette !== "function" && e.code === "BracketLeft") { e.preventDefault(); toggleRail(); } // the app has appKey
  });
  const meReady = fetch("/ui/api/me", { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
    .then((me) => { navProjects = me.projects; return navMe(me); }).catch(() => { navEl("who").textContent = "Signed in"; });
  fetch("/ui/api/board", { credentials: "same-origin" }).then((r) => r.ok && r.json()).then((bd) => {
    const n = (bd.items || []).filter((i) => i.col === "needs").length;
    for (const b of document.querySelectorAll(".inbox-count")) b.textContent = n || "";
  }).catch(() => {});
  // the badges count as the app does (agents.js ckSummary, app.js loadTasks, loadLint), so they do not change on this page
  const get = (u) => fetch(u, { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))));
  meReady.then(() => document.body.classList.contains("no-agents") ? [{}, {}] : Promise.all([get("/ui/api/chats"), get("/ui/api/machines")])).then(([cs, ms]) => {
    const now = Date.now() / 1000;
    let live = (cs.chats || []).filter((c) => ["queued", "running"].includes(c.status)).length;
    for (const m of (ms.machines || []).filter((x) => x.own && !x.old)) for (const ag of Object.values(m.agents || {}))
      live += (ag.workers || []).filter((w) => w.status === "running").length + (ag.sessions || []).filter((s) => !s.worker && now - s.at < 600).length;
    const elLive = navEl("ck-live"), elN = navEl("ck-live-n");
    if (elN) elN.textContent = live;
    if (elLive) elLive.classList.toggle("on", live > 0);
  }).catch(() => {});
  get("/ui/api/tasks").then((ts) => {
    // the picked project's open lines, as the app's Tasks badge ("all" = every project)
    const p = localStorage.getItem("kn_project") || "homelab", el = navEl("tasks-count"), openAll = (ts.tasks || []).filter((t) => !t.done), inProject = (ts.tasks || []).some((t) => p === "all" || t.project === p), open = inProject ? openAll.filter((t) => p === "all" || t.project === p) : openAll; // nothing here: the list shows the work elsewhere
    if (el) { el.textContent = open.length || ""; el.className = open.some((t) => t.overdue) ? "badge warn" : "badge"; }
  }).catch(() => {});
  meReady.then(() => get("/ui/api/lint?v=2")).then((res) => { // this project's findings, as the app's Health badge (project from the app's picker)
    const el = navEl("lint-count"), project = localStorage.getItem("kn_project") || "homelab", full = res.report || res;
    const grouped = new Set((res.groups || []).flatMap((g) => g.checks));
    const of = (item) => { // as app.js lintProject: a project's root or its -memory root, claude/ shared, the rest homelab
      const r = item.split(/[/:]/)[0], p = r.replace(/-memory$/, "");
      return r === "claude" ? "shared" : (navProjects || []).includes(p) ? p : "homelab";
    };
    const n = Object.entries(full).reduce((a, [c, v]) => a + (grouped.has(c) ? 0 : v.filter((i) => project === "all" || of(i) === project).length), 0);
    if (el) el.textContent = n || "";
  }).catch(() => {});
}
