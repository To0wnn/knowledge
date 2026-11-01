// The navigation every signed-in page shares (nav.html): the sidebar and its icon rail, the phone's More sheet, the
// theme switch and who is signed in. The app (index.html) routes its views itself (shell.js); the admin and help pages
// are pages of their own, so here they mark their own entry and fetch who is signed in. Globals: toggleRail,
// closeSide, navMe (app.js, shell.js and palette.js call them).
"use strict";

const navEl = (id) => document.getElementById(id);
const navStore = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* blocked: not remembered */ } } };

// Theme: Auto (the system's), Light or Dark; theme.js applies the stored pick before the page paints. The sidebar has
// it as an icon, the More sheet as a labelled button. The app listens for "kn-theme" to recolour the map.
const NAV_THEMES = ["auto", "light", "dark"];
function showTheme() {
  const name = `Theme: ${{ auto: "Auto", light: "Light", dark: "Dark" }[document.documentElement.dataset.theme || "auto"]}`;
  for (const b of document.querySelectorAll(".theme-btn")) { b.title = name; b.setAttribute("aria-label", name); if (!b.querySelector("svg")) b.textContent = name; }
}
for (const b of document.querySelectorAll(".theme-btn")) b.onclick = () => {
  const next = NAV_THEMES[(NAV_THEMES.indexOf(document.documentElement.dataset.theme || "auto") + 1) % NAV_THEMES.length];
  if (next === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
  navStore.set("kn_theme", next); showTheme(); dispatchEvent(new Event("kn-theme"));
};
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
paintRail();

// Phone: More opens a sheet with the other views, the project, theme and sign out.
navEl("more-btn").onclick = () => navEl("more-sheet").showModal();
navEl("more-sheet").addEventListener("click", (e) => { if (e.target === navEl("more-sheet") || e.target.closest("a")) navEl("more-sheet").close(); });

// Who is signed in (/ui/api/me): the user menu, and what an admin sees that others do not.
function navMe(me) {
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
    if (e.target.closest("input, textarea, select, [contenteditable]") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "[") toggleRail();
  });
  fetch("/ui/api/me", { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
    .then(navMe).catch(() => { navEl("who").textContent = "Signed in"; });
  fetch("/ui/api/board", { credentials: "same-origin" }).then((r) => r.ok && r.json()).then((bd) => {
    const n = (bd.items || []).filter((i) => i.col === "needs").length;
    for (const b of document.querySelectorAll(".inbox-count")) b.textContent = n || "";
  }).catch(() => {});
  fetch("/ui/api/chats", { credentials: "same-origin" }).then((r) => r.ok && r.json()).then((cs) => {
    const live = (cs.chats || []).filter((c) => ["queued", "running"].includes(c.status)).length;
    const elLive = navEl("ck-live"), elN = navEl("ck-live-n");
    if (elN) elN.textContent = live;
    if (elLive) elLive.classList.toggle("on", live > 0);
  }).catch(() => {});
  fetch("/ui/api/tasks", { credentials: "same-origin" }).then((r) => r.ok && r.json()).then((ts) => {
    const el = navEl("tasks-count");
    if (el && (ts.tasks || []).length) el.textContent = ts.tasks.length;
  }).catch(() => {});
  fetch("/ui/api/lint", { credentials: "same-origin" }).then((r) => r.ok && r.json()).then((lt) => {
    const el = navEl("lint-count");
    if (el && (lt.findings || []).length) el.textContent = lt.findings.length;
  }).catch(() => {});
}
