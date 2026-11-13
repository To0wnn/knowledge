// The navigation every signed-in page shares (nav.html): the sidebar and its icon rail, the phone's More sheet, the
// theme switch and who is signed in. The app (index.html) routes its views itself (shell.js); the admin and help pages
// are pages of their own, so here they mark their own entry and fetch who is signed in. Globals: toggleRail,
// closeSide, navMe (app.js, shell.js and palette.js call them), setTheme (Settings).
"use strict";
let navProjects = null; // the signed-in person's projects (/ui/api/me): the lint badge counts as the app does

const navEl = (id) => document.getElementById(id);
const navStore = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* blocked: not remembered */ } } };
// The picked project belongs to this window, so a refresh or a deploy's reload never takes over another window's pick;
// the last one picked anywhere is where a new window starts.
const navProject = {
  get() { try { const p = sessionStorage.getItem("kn_project") || localStorage.getItem("kn_project") || "homelab"; sessionStorage.setItem("kn_project", p); return p; } catch { return "homelab"; } },
  set(p) { try { sessionStorage.setItem("kn_project", p); localStorage.setItem("kn_project", p); } catch { /* blocked: this window still shows it */ } },
};
// The stored pick as a set of projects: null = all, "a" one, "a,b" several, "," none.
const navPicks = (v) => (v === "all" ? null : new Set(v.split(",").filter(Boolean)));

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

// Keys per person (Settings; /ui/api/me/prefs "keys" holds what differs from these). "Ctrl" is also Cmd, "#" any digit, "Arrow" any arrow.
// The last field: only with the agent views. Fixed: Esc, Enter, arrows without Alt, the image viewer's keys: they do what such a key does everywhere.
const KEY_ACTIONS = [
  ["palette", "Ctrl+K", "Search notes, chats and tasks"],
  ["notes_search", "Alt+/", "Search notes only"],
  ["chats", "Alt+A", "Agents view", true],
  ["new_chat", "Alt+N", "New chat", true],
  ["map", "Alt+M", "Map or notes"],
  ["rail", "Alt+[", "Fold the sidebar"],
  ["digits", "Alt+#", "Pick a project; on the grid show a part (0 = all)"],
  ["digits_move", "Alt+Shift+#", "On the grid: move a card to a part", true],
  ["arrows", "Alt+Arrow", "On the grid: to the next card that way", true],
  ["queue", "Alt+Enter", "Queue a message after this turn", true],
  ["force", "Ctrl+Shift+Enter", "Force: stop the step, then this message", true],
  ["speak", "Ctrl+Space", "Speak", true],
  ["jump", "Ctrl+End", "Jump to the end of a chat", true],
  ["find", "Ctrl+F", "Find in a chat (elsewhere the browser's own find)", true],
  ["edit", "Alt+E", "Edit a note"],
  ["save", "Ctrl+S", "Save a note"],
  ["fit", "Alt+F", "Fit the task map"],
];
let keyPrefs = {};
const keyDefault = (action) => KEY_ACTIONS.find((a) => a[0] === action)[1];
const keyOf = (action) => keyPrefs[action] || keyDefault(action);
const keyShow = (action) => keyOf(action).replace(/#$/, "1-9").replace(/Arrow$/, "arrows");
// A letter as the layout has it, else by position: Alt+N on a Mac is a dead key, Shift changes the character of a digit.
function keyCombo(e) {
  const c = e.code, k = e.key || ""; // a browser's autofill sends a keydown without a key
  const name = /^[a-z]$/i.test(k) ? k.toUpperCase() : /^Key[A-Z]$/.test(c) ? c[3] : /^(?:Digit|Numpad)\d$/.test(c) ? c.at(-1)
    : c === "BracketLeft" ? "[" : c === "Slash" ? "/" : k === " " ? "Space" : k.length === 1 ? k.toUpperCase() : k;
  return `${e.ctrlKey || e.metaKey ? "Ctrl+" : ""}${e.altKey ? "Alt+" : ""}${e.shiftKey ? "Shift+" : ""}${name}`;
}
const keyFamily = (combo) => combo.replace(/\+\d$/, "+#").replace(/Arrow\w+$/, "Arrow");
const keyHit = (e, action) => keyFamily(keyCombo(e)) === keyOf(action);
// An element with data-shortcut: {key} in its title, or else its text, is the action's key now.
function keyLabels(root = document) {
  for (const n of root.querySelectorAll("[data-shortcut]")) {
    const prop = n.title ? "title" : "textContent";
    n.dataset.shortcutText ||= n[prop];
    n[prop] = n.dataset.shortcutText.replace("{key}", keyShow(n.dataset.shortcut));
  }
}
keyLabels();

// Who is signed in (/ui/api/me): the user menu, and what an admin sees that others do not.
function navMe(me) {
  keyPrefs = me.prefs?.keys || {}; keyLabels();
  document.body.classList.toggle("no-agents", me.agents === false); // no token with remote control on: the knowledge views only
  for (const a of document.querySelectorAll(".admin-link")) { // non-admins manage their own tokens there
    (a.querySelector(".label") || a).textContent = me.admin ? "Admin" : "My tokens & projects"; a.title = me.admin ? "Admin" : "My tokens & projects";
  }
  const avatar = document.createElement("span"), name = document.createElement("span");
  avatar.className = "avatar"; avatar.textContent = (me.user || "?")[0].toUpperCase(); name.className = "label"; name.textContent = me.user;
  navEl("who").replaceChildren(avatar, name);
  navEl("who").title = me.impersonator ? `Viewing as ${me.user} (impersonated by ${me.impersonator})` : `Signed in as ${me.user}`;
  navEl("who-more").textContent = me.user;
  if (!me.admin) for (const a of document.querySelectorAll(".nav-health")) a.hidden = true;
  document.body.classList.toggle("is-impersonating", Boolean(me.impersonator));
  let bar = document.getElementById("impersonate-bar");
  if (me.impersonator) {
    if (!bar) {
      bar = document.createElement("aside");
      bar.id = "impersonate-bar";
      bar.className = "impersonate-bar";
      const target = document.querySelector(".main, .page-body") || document.body;
      target.prepend(bar);
    }
    bar.replaceChildren();
    const txt = document.createElement("span");
    txt.className = "impersonate-text";
    txt.append("Viewing as ");
    const strong = document.createElement("strong");
    strong.textContent = me.user;
    txt.append(strong, ` (impersonated by ${me.impersonator})`);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn small";
    btn.textContent = "Stop";
    btn.onclick = async () => {
      try {
        await fetch("/ui/api/admin/impersonate", {
          method: "POST", credentials: "same-origin",
          headers: { "X-Knowledge-UI": "1", "Content-Type": "application/json" },
          body: JSON.stringify({ stop: true })
        });
        location.href = "/ui/admin";
      } catch {
        location.href = "/ui/impersonate/stop";
      }
    };
    bar.append(txt, btn);
    bar.hidden = false;
  } else if (bar) {
    bar.hidden = true;
  }
  let stopLink = document.getElementById("impersonate-stop-link");
  if (me.impersonator) {
    if (!stopLink) {
      stopLink = document.createElement("a");
      stopLink.id = "impersonate-stop-link";
      stopLink.href = "/ui/impersonate/stop";
      const menuList = document.querySelector("#user-menu .menu-list");
      if (menuList) menuList.prepend(stopLink);
    }
    stopLink.textContent = `Stop viewing as ${me.user}`;
    stopLink.hidden = false;
  } else if (stopLink) {
    stopLink.hidden = true;
  }
  if (me.projects && me.projects.length) {
    for (const s of document.querySelectorAll(".project-pick")) {
      const cur = navProject.get(), picks = navPicks(cur), several = picks && picks.size !== 1 ? [cur] : []; // picked in the app
      s.replaceChildren(...[...me.projects, "all", ...several].map((p) => {
        const opt = document.createElement("option");
        opt.value = p; opt.textContent = p === "all" ? "All projects" : p === several[0] ? [...picks].map((q) => me.labels?.[q] || q).join(", ") || "No project" : me.labels?.[p] || p;
        return opt;
      }));
      s.value = navProject.get();
      s.onchange = () => { navProject.set(s.value); location.href = "/ui#/notes"; }; // the app starts from the picked project (app.js renderProjects takes over there)
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
    if (keyHit(e, "palette") && typeof openPalette !== "function") { e.preventDefault(); location.href = "/ui#/notes?q="; return; }
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (typeof openPalette !== "function" && keyHit(e, "rail")) { e.preventDefault(); toggleRail(); } // the app has appKey
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
    const ps = navPicks(navProject.get()), mine = (t) => !ps || ps.has(t.project), el = navEl("tasks-count"), openAll = (ts.tasks || []).filter((t) => !t.done), inProject = (ts.tasks || []).some(mine), open = inProject ? openAll.filter(mine) : openAll; // nothing here: the list shows the work elsewhere
    if (el) { el.textContent = open.length || ""; el.className = open.some((t) => t.overdue) ? "badge warn" : "badge"; }
  }).catch(() => {});
  meReady.then(() => get("/ui/api/lint?v=2")).then((res) => { // this project's findings, as the app's Health badge (project from the app's picker)
    const el = navEl("lint-count"), ps = navPicks(navProject.get()), full = res.report || res;
    const grouped = new Set((res.groups || []).flatMap((g) => g.checks));
    const of = (item) => { // as app.js lintProject: a project's root or its -memory root, claude/ shared, the rest homelab
      const r = item.split(/[/:]/)[0], p = r.replace(/-memory$/, "");
      return r === "claude" ? "shared" : (navProjects || []).includes(p) ? p : "homelab";
    };
    const n = Object.entries(full).reduce((a, [c, v]) => a + (grouped.has(c) ? 0 : v.filter((i) => !ps || ps.has(of(i))).length), 0);
    if (el) el.textContent = n || "";
  }).catch(() => {});
}
