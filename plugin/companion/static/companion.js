// Knowledge companion page: the state of this pc from the companion's local API (/local/api/*, companion.py).
"use strict";

// Set before first paint so the page never flashes the wrong theme (same key as the web UI).
try { const t = localStorage.getItem("kn_theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch { /* storage blocked: follow the system */ }

// The state of this pc as the companion's local API reports it; filled by load().
const EMPTY = { status: {}, agents: [], tasks: { ready: [], waiting: [], stalled: [] }, activity: [], settings: { token: {}, workersAllowed: false, maxWorkers: {} } };
let S = EMPTY;

// The key the local API wants for changes: the shortcut opens this page with #key=..., kept for the next visit.
const KEY = (() => {
  const k = new URLSearchParams(location.hash.slice(1)).get("key");
  try {
    if (k) { localStorage.setItem("kn_companion_key", k); history.replaceState(null, "", location.pathname); }
    return k || localStorage.getItem("kn_companion_key") || "";
  } catch { return k || ""; }
})();

async function api(path, body) {
  const headers = { "X-Companion-Key": KEY, ...(body === undefined ? {} : { "Content-Type": "application/json" }) };
  const r = await fetch(path, body === undefined ? { headers } : { method: "POST", headers, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

const clock = (at) => { const d = new Date(at * 1000); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
const ago = (at) => { const m = Math.round((Date.now() / 1000 - at) / 60); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`; };

async function load() {
  try { const r = await api("/local/api/state"); S = { ...EMPTY, ...r, status: { ...EMPTY.status, ...r.status } }; }
  catch (e) { toast(`Could not read the companion: ${e.message}`); return; }
  if (S.locked) toast("Open the companion from its shortcut to see tasks and change settings");
  renderChips(); renderAgents(); renderTasks(); renderActivity(); renderSettings();
}

const $ = (id) => document.getElementById(id);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...kids.flat().filter((k) => k != null && k !== false));
  return el;
};
const icon = (name) => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
};

let toastTimer;
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.add("cmp-toast--on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("cmp-toast--on"), 2600);
}

function renderChips() {
  const s = S.status;
  $("host").textContent = `This pc: ${s.host}`;
  const chips = [
    h("li", { class: "cmp-chip cmp-chip--live" }, h("span", { class: "cmp-dot" }), h("strong", {}, String(s.live)), " live"),
    s.workers ? h("li", { class: "cmp-chip cmp-chip--live" }, h("strong", {}, String(s.workers)), " working") : null,
    s.stalled ? h("li", { class: "cmp-chip cmp-chip--bad" }, h("strong", {}, String(s.stalled)), " stalled") : null,
    s.syncError ? h("li", { class: "cmp-chip cmp-chip--bad", title: s.syncError }, "server unreachable")
      : h("li", { class: "cmp-chip" }, "synced ", s.synced ? ago(s.synced) : "not yet"),
  ];
  $("chips").replaceChildren(...chips.filter(Boolean));
}

function state(ok, okText, badText, level = "warn") {
  return h("span", { class: `cmp-state cmp-state--${ok ? "ok" : level}` }, icon(ok ? "check" : "alert"), ok ? okText : badText);
}

function agentCard(a) {
  if (!a.installed) {
    return h("article", { class: "cmp-agent cmp-agent--missing", "aria-labelledby": `ag-${a.id}` },
      h("div", { class: "cmp-agent-head" }, h("span", { class: "cmp-agent-mark", "aria-hidden": "true" }, a.mark), h("h3", { id: `ag-${a.id}` }, a.name)),
      h("p", { class: "cmp-sessions-empty" }, `${a.name} is not installed on this pc.`));
  }
  const trustRow = a.hooksTrusted ? state(true, "Trusted") : [
    state(false, "", "Not trusted"),
    h("button", { class: "cmp-btn cmp-btn--primary cmp-btn--small", type: "button", onclick: () => trustHooks(a) }, icon("shield"), "Trust hooks"),
  ];
  const yoloId = `yolo-${a.id}`;
  return h("article", { class: "cmp-agent", "aria-labelledby": `ag-${a.id}` },
    h("div", { class: "cmp-agent-head" },
      h("span", { class: "cmp-agent-mark", "aria-hidden": "true" }, a.mark),
      h("h3", { id: `ag-${a.id}` }, a.name),
      a.yolo ? h("span", { class: "cmp-chip cmp-chip--bad", title: "Runs without asking" }, "YOLO") : null,
      h("span", { class: "cmp-ver" }, a.version ? `plugin ${a.version}` : "no plugin")),
    h("dl", { class: "cmp-facts" },
      h("dt", {}, "Installed"), h("dd", {}, state(true, "Yes")),
      h("dt", {}, "Hooks"), h("dd", {}, trustRow)),
    a.sessions.length
      ? h("ul", { class: "cmp-sessions", "aria-label": `${a.name} sessions` }, a.sessions.map((s) => h("li", {},
          h("span", { title: s.title }, s.title, h("br"), h("small", {}, `active ${ago(s.at)}${s.wakeable ? " · gets the next task" : ""}`)))))
      : h("p", { class: "cmp-sessions-empty" }, "No open sessions."),
    (a.workers || []).length
      ? h("ul", { class: "cmp-sessions", "aria-label": `${a.name} workers` }, a.workers.map((w) => h("li", {},
          h("span", { title: w.open || "" }, `${a.name} · ${w.id} · ${w.status === "running" ? `running ${ago(w.started).replace(" ago", "")}` : `${w.status} ${ago(w.ended || w.started)}`}`,
            h("br"), h("small", {}, w.branch ? `branch ${w.branch}${w.yolo ? " · YOLO" : ""}` : w.project)))))
      : h("p", { class: "cmp-sessions-empty" }, "No workers. A worker is a fresh session per task, when this pc allows workers (Settings)."),
    h("label", { class: "cmp-yolo", for: yoloId },
      h("input", { type: "checkbox", id: yoloId, checked: a.yolo, "aria-describedby": `${yoloId}-warn`, onchange: (e) => setYolo(a, e.target.checked) }),
      h("span", {}, "YOLO: run without asking (workers too)")),
    h("p", { class: "cmp-yolo-warn", id: `${yoloId}-warn` }, a.yoloPending != null
      ? `Turning YOLO ${a.yoloPending ? "on" : "off"} applies when ${a.name} is closed.`
      : "The agent may then edit files and run commands with no approval. Unchecking restores your previous settings."),
    a.lastError
      ? h("p", { class: "cmp-error" }, icon("alert"), h("span", {}, h("strong", {}, `Last hook error ${clock(a.lastError.at)}: `), a.lastError.text))
      : h("p", { class: "cmp-error cmp-error--none" }, "No hook errors."));
}

function renderAgents() { $("agents").replaceChildren(...S.agents.map(agentCard)); }

function renderTasks() {
  const lanes = [["ready", "Ready"], ["waiting", "Waiting"], ["stalled", "Stalled"]];
  const name = (id) => S.agents.find((a) => a.id === id)?.name || id;
  $("tasks").replaceChildren(...lanes.map(([key, label]) => {
    const items = S.tasks[key];
    return h("section", { class: `cmp-lane cmp-lane--${key}`, "aria-label": `${label} tasks` },
      h("h3", {}, h("span", { class: "cmp-dot" }), label, h("small", {}, String(items.length))),
      items.length
        ? h("ol", {}, items.map((t) => h("li", { class: "cmp-task" },
            h("span", { class: "cmp-task-id" }, `${t.id} for ${name(t.owner)} · ${t.project}`),
            h("p", { class: "cmp-task-text" }, t.text),
            t.why ? h("p", { class: "cmp-task-why" }, t.why) : null)))
        : h("p", { class: "cmp-lane-empty" }, "Nothing here."));
  }));
  const open = lanes.reduce((n, [k]) => n + S.tasks[k].length, 0);
  $("tasks-count").textContent = open ? String(open) : "";
}

function renderActivity() {
  const icons = { wake: "bolt", worker: "bolt", remote: "globe", trust: "shield", sync: "sync", error: "alert", risk: "alert", wait: "sync" };
  $("activity").replaceChildren(...S.activity.map((e) => h("li", {},
    h("time", { class: "cmp-log-time", datetime: new Date(e.at * 1000).toISOString() }, clock(e.at)),
    h("span", { class: `cmp-log-icon${e.kind === "error" ? " cmp-log-icon--bad" : ""}` }, icon(icons[e.kind] || "orb")),
    h("span", { class: "cmp-log-text" }, e.text, e.detail ? h("small", {}, e.detail) : null))));
}

function renderSettings() {
  const s = S.settings;
  const tok = s.token.present
    ? [state(true, "Token found (keyring or KNOWLEDGE_TOKEN)"), h("small", {}, "The token itself is never shown.")]
    : [state(false, "", "No token on this pc", "bad"), h("small", {}, "Add one with the plugin setup; it goes to the keyring, never to this page.")];
  $("settings").replaceChildren(
    h("div", { class: "cmp-field" },
      h("label", { for: "set-url" }, "Knowledge server URL"),
      h("input", { type: "url", id: "set-url", value: s.serverUrl, readonly: true, autocomplete: "off", spellcheck: "false" }),
      h("small", {}, "Set by the plugin setup (knowledge-agent.py install).")),
    h("div", { class: "cmp-field" }, h("span", { style: "font-weight:560" }, "Token"), h("div", { class: "cmp-token" }, tok)),
    h("fieldset", { class: "cmp-field" },
      h("legend", {}, "Workers"),
      h("small", {}, "A worker is a fresh session per ready task, in its own git worktree, started here. Off by default: turn it on only on a pc that may work on its own (not a work laptop or a shared family pc)." + (s.game ? ` Now: no workers while ${s.game}.` : "")),
      h("label", { class: "cmp-switch" }, h("input", { type: "checkbox", name: "workersAllowed", checked: !!s.workersAllowed }), "Workers allowed on this pc"),
      S.agents.filter((a) => a.installed).map((a) => h("label", { class: "cmp-switch" },
        h("input", { type: "number", name: `max-${a.id}`, min: 0, max: 8, value: s.maxWorkers[a.id] ?? 1, style: "width:4em" }), `at most this many ${a.name} workers at once`))),
    h("div", { class: "cmp-settings-foot" }, h("button", { class: "cmp-btn cmp-btn--primary", type: "submit" }, "Save settings")));
}

async function act(path, body, done) {
  try { const r = await api(path, body); if (done) toast(typeof done === "function" ? done(r) : done); }
  catch (e) { toast(e.message); }
  await load();
}
const trustHooks = (a) => act("/local/api/trust", { agent: a.id }, (r) => `Trusted the hooks of ${a.name}: ${r.trusted[a.id]}`);
const setYolo = (a, on) => act("/local/api/yolo", { agent: a.id, on }, (r) => r.yolo === "pending" ? `${a.name}: applies when it is closed` : `YOLO ${r.yolo} for ${a.name}`);

const TOOLS = [
  { icon: "shield", label: "Trust all hooks", hint: "For every agent on this pc", run: () => act("/local/api/trust", {}, "Trusted the knowledge hooks") },
  { icon: "bolt", label: "Start ready tasks now", hint: "Workers, instead of waiting for the next minute", run: () => act("/local/api/tick", {}, (r) => `Started ${r.started.length} worker(s)`) },
  { icon: "sync", label: "Update the plugin", hint: "Codex and agy; Claude Code updates itself", run: () => act("/local/api/update", {}, "Updating in the background; see Activity") },
  null,
  { icon: "globe", label: "Open the knowledge web UI", hint: "In your browser", run: () => window.open(`${S.settings.serverUrl}/ui`, "_blank", "noopener") },
  { icon: "copy", label: "Copy diagnostics", hint: "Versions and errors, no token", run: async () => {
    const d = { version: S.version, status: S.status, agents: S.agents.map(({ id, installed, version, hooksTrusted, yolo, lastError }) => ({ id, installed, version, hooksTrusted, yolo, lastError })) };
    try { await navigator.clipboard.writeText(JSON.stringify(d, null, 2)); toast("Copied diagnostics"); } catch { toast("The browser did not allow copying"); }
  } },
];

function setupMenu() {
  const btn = $("tools-btn"), menu = $("tools-menu");
  menu.replaceChildren(...TOOLS.map((t) => t
    ? h("button", { class: "cmp-menu-item", type: "button", role: "menuitem", tabindex: "-1", onclick: () => { close(true); t.run(); } },
        icon(t.icon), h("span", {}, t.label, h("small", {}, t.hint)))
    : h("div", { class: "cmp-menu-sep", role: "separator" })));
  const items = () => [...menu.querySelectorAll('[role="menuitem"]')];
  const open = (focusLast = false) => {
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    const list = items();
    (focusLast ? list[list.length - 1] : list[0]).focus();
  };
  function close(refocus) {
    if (menu.hidden) return;
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    if (refocus) btn.focus();
  }
  btn.addEventListener("click", () => (menu.hidden ? open() : close(true)));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); open(e.key === "ArrowUp"); }
  });
  menu.addEventListener("keydown", (e) => {
    const list = items(), i = list.indexOf(document.activeElement);
    const go = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: list.length - 1 }[e.key];
    if (go !== undefined) { e.preventDefault(); list[(go + list.length) % list.length].focus(); }
    else if (e.key === "Escape") { e.preventDefault(); close(true); }
    else if (e.key === "Tab") close(false);
  });
  document.addEventListener("pointerdown", (e) => { if (!menu.contains(e.target) && !btn.contains(e.target)) close(false); });
}

function setupTheme() {
  const btn = $("theme-btn");
  const dark = () => document.documentElement.dataset.theme === "dark"
    || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  const paint = () => {
    btn.querySelector("use").setAttribute("href", dark() ? "#i-sun" : "#i-moon");
    btn.setAttribute("aria-label", dark() ? "Switch to light theme" : "Switch to dark theme");
  };
  btn.addEventListener("click", () => {
    const next = dark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("kn_theme", next); } catch { /* storage blocked: this page only */ }
    paint();
  });
  paint();
}

function setupNav() {
  const links = [...document.querySelectorAll(".cmp-nav a")];
  const mark = (id) => links.forEach((l) => l.setAttribute("aria-current", l.hash === `#${id}` ? "true" : "false"));
  const io = new IntersectionObserver((entries) => {
    const seen = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (seen) mark(seen.target.id);
  }, { rootMargin: "-90px 0px -55% 0px" });
  links.forEach((l) => io.observe(document.querySelector(l.hash)));
  mark("sec-agents");
}

document.addEventListener("DOMContentLoaded", () => {
  setupMenu(); setupTheme(); setupNav();
  load();
  setInterval(load, 30000);
  $("refresh-btn").addEventListener("click", () => load().then(() => toast("Refreshed")));
  $("settings").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target.elements;
    const maxWorkers = Object.fromEntries(S.agents.filter((a) => f[`max-${a.id}`]).map((a) => [a.id, Number(f[`max-${a.id}`].value)]));
    act("/local/api/settings", { workersAllowed: !!f.workersAllowed?.checked, maxWorkers }, "Saved settings");
  });
});
