// Knowledge companion page. Renders the state of this pc; MOCK stands in for the local API until the backend exists.
"use strict";

// Set before first paint so the page never flashes the wrong theme (same key as the web UI).
try { const t = localStorage.getItem("kn_theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch { /* storage blocked: follow the system */ }

// shortcut: static sample data; the backend replaces each field with fetch("/local/api/<name>") (status, agents, tasks, activity, settings).
const MOCK = {
  status: { host: "workstation", live: 3, stalled: 1, resets: "2:39pm", synced: "1 min ago" },
  agents: [
    {
      id: "claude", name: "Claude Code", mark: "C", installed: true, version: "0.9.17", hooksTrusted: true, yolo: false,
      sessions: [
        { title: "homelab: traefik route for netbox", age: "idle 4 min", wakeable: true },
        { title: "knowledge: companion backend", age: "working", wakeable: false },
      ],
      lastError: null,
    },
    {
      id: "codex", name: "Codex", mark: "X", installed: true, version: "0.9.17", hooksTrusted: false, yolo: false,
      sessions: [{ title: "trade: backtest window fix", age: "idle 22 min", wakeable: true }],
      lastError: { at: "10:42", text: "SessionStart: hook not trusted, run Trust hooks" },
    },
    {
      id: "agy", name: "agy", mark: "A", installed: true, version: "0.9.16", hooksTrusted: true, yolo: true,
      sessions: [],
      lastError: { at: "09:15", text: "PostInvocation: timed out after 10 s reaching the knowledge server" },
    },
  ],
  tasks: {
    ready: [
      { id: "KN-41", owner: "claude", text: "Add the companion API routes under /local/api" },
      { id: "HL-12", owner: "codex", text: "Rotate the backup token on the offsite target" },
    ],
    waiting: [
      { id: "TR-7", owner: "codex", text: "Rerun the backtest on the October data", why: "Waiting for KN-41 to finish" },
    ],
    stalled: [
      { id: "AN-3", owner: "agy", text: "Draft the invoice template in English", why: "Woken twice, no answer for 3 h" },
    ],
  },
  activity: [
    { at: "11:02", kind: "wake", text: "Woke Claude Code for KN-41", detail: "Session homelab: traefik route for netbox" },
    { at: "10:48", kind: "trust", text: "Trusted the hooks of agy", detail: "Plugin 0.9.16" },
    { at: "10:42", kind: "error", text: "Codex hook failed", detail: "SessionStart: hook not trusted" },
    { at: "09:30", kind: "sync", text: "Plugin updated to 0.9.17", detail: "Claude Code and Codex" },
    { at: "08:01", kind: "wake", text: "Started a Codex session for HL-12", detail: "No session was open" },
  ],
  settings: {
    serverUrl: "https://knowledge.example.org",
    token: { present: true, source: "system keyring", checked: "today 08:00" },
    startWhenNone: { claude: true, codex: false, agy: false },
    autostart: true,
  },
};

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

function log(kind, text, detail) {
  const now = new Date();
  MOCK.activity.unshift({ at: `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`, kind, text, detail });
  renderActivity();
}

function renderChips() {
  const s = MOCK.status;
  $("host").textContent = `This pc: ${s.host}`;
  const chips = [
    h("li", { class: "cmp-chip cmp-chip--live" }, h("span", { class: "cmp-dot" }), h("strong", {}, String(s.live)), " live"),
    s.stalled ? h("li", { class: "cmp-chip cmp-chip--bad" }, h("strong", {}, String(s.stalled)), " stalled") : null,
    h("li", { class: "cmp-chip" }, "resets ", h("strong", {}, s.resets)),
    h("li", { class: "cmp-chip" }, "synced ", s.synced),
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
      h("span", { class: "cmp-ver" }, `plugin ${a.version}`)),
    h("dl", { class: "cmp-facts" },
      h("dt", {}, "Installed"), h("dd", {}, state(true, "Yes")),
      h("dt", {}, "Hooks"), h("dd", {}, trustRow)),
    a.sessions.length
      ? h("ul", { class: "cmp-sessions", "aria-label": `${a.name} sessions` }, a.sessions.map((s) => h("li", {},
          h("span", { title: s.title }, s.title, h("br"), h("small", {}, s.age)),
          s.wakeable ? h("button", { class: "cmp-btn cmp-btn--small", type: "button", "aria-label": `Wake ${s.title}`, onclick: () => wake(a, s) }, icon("play"), "Wake") : null)))
      : h("p", { class: "cmp-sessions-empty" }, "No open sessions. A task starts one only when allowed in Settings."),
    h("label", { class: "cmp-yolo", for: yoloId },
      h("input", { type: "checkbox", id: yoloId, checked: a.yolo, "aria-describedby": `${yoloId}-warn`, onchange: (e) => setYolo(a, e.target.checked) }),
      h("span", {}, "YOLO: run woken tasks without asking")),
    h("p", { class: "cmp-yolo-warn", id: `${yoloId}-warn` }, "The agent may then edit files and run commands with no approval."),
    a.lastError
      ? h("p", { class: "cmp-error" }, icon("alert"), h("span", {}, h("strong", {}, `Last hook error ${a.lastError.at}: `), a.lastError.text))
      : h("p", { class: "cmp-error cmp-error--none" }, "No hook errors."));
}

function renderAgents() { $("agents").replaceChildren(...MOCK.agents.map(agentCard)); }

function renderTasks() {
  const lanes = [["ready", "Ready"], ["waiting", "Waiting"], ["stalled", "Stalled"]];
  const name = (id) => MOCK.agents.find((a) => a.id === id)?.name || id;
  $("tasks").replaceChildren(...lanes.map(([key, label]) => {
    const items = MOCK.tasks[key];
    return h("section", { class: `cmp-lane cmp-lane--${key}`, "aria-label": `${label} tasks` },
      h("h3", {}, h("span", { class: "cmp-dot" }), label, h("small", {}, String(items.length))),
      items.length
        ? h("ol", {}, items.map((t) => h("li", { class: "cmp-task" },
            h("span", { class: "cmp-task-id" }, `${t.id} for ${name(t.owner)}`),
            h("p", { class: "cmp-task-text" }, t.text),
            t.why ? h("p", { class: "cmp-task-why" }, t.why) : null)))
        : h("p", { class: "cmp-lane-empty" }, "Nothing here."));
  }));
  const open = lanes.reduce((n, [k]) => n + MOCK.tasks[k].length, 0);
  $("tasks-count").textContent = open ? String(open) : "";
}

function renderActivity() {
  const icons = { wake: "bolt", trust: "shield", sync: "sync", error: "alert" };
  $("activity").replaceChildren(...MOCK.activity.map((e) => h("li", {},
    h("time", { class: "cmp-log-time" }, e.at),
    h("span", { class: `cmp-log-icon${e.kind === "error" ? " cmp-log-icon--bad" : ""}` }, icon(icons[e.kind] || "orb")),
    h("span", { class: "cmp-log-text" }, e.text, e.detail ? h("small", {}, e.detail) : null))));
}

function renderSettings() {
  const s = MOCK.settings;
  const tok = s.token.present
    ? [state(true, `Token stored in the ${s.token.source}`), h("small", {}, `Checked ${s.token.checked}. The token itself is never shown.`)]
    : [state(false, "", "No token on this pc", "bad"), h("small", {}, "Add one with the plugin setup; it goes to the keyring, never to this page.")];
  $("settings").replaceChildren(
    h("div", { class: "cmp-field" },
      h("label", { for: "set-url" }, "Knowledge server URL"),
      h("input", { type: "url", id: "set-url", value: s.serverUrl, required: true, autocomplete: "off", spellcheck: "false" })),
    h("div", { class: "cmp-field" }, h("span", { style: "font-weight:560" }, "Token"), h("div", { class: "cmp-token" }, tok)),
    h("fieldset", { class: "cmp-field" },
      h("legend", {}, "Start a session when none is open"),
      h("small", {}, "When a task arrives for an agent with no session, open one."),
      MOCK.agents.filter((a) => a.installed).map((a) => h("label", { class: "cmp-switch" },
        h("input", { type: "checkbox", name: `start-${a.id}`, checked: !!s.startWhenNone[a.id] }), a.name))),
    h("div", { class: "cmp-field" },
      h("label", { class: "cmp-switch" }, h("input", { type: "checkbox", name: "autostart", checked: s.autostart }), "Start the companion when I log in")),
    h("div", { class: "cmp-settings-foot" }, h("button", { class: "cmp-btn cmp-btn--primary", type: "submit" }, "Save settings")));
}

function trustHooks(a) {
  a.hooksTrusted = true;
  if (a.lastError && /trust/i.test(a.lastError.text)) a.lastError = null;
  log("trust", `Trusted the hooks of ${a.name}`, `Plugin ${a.version}`);
  renderAgents();
  toast(`Trusted the hooks of ${a.name}`);
}
function wake(a, s) {
  log("wake", `Woke ${a.name}`, `Session ${s.title}`);
  toast(`Woke ${a.name}`);
}
function setYolo(a, on) {
  a.yolo = on;
  log("trust", `YOLO ${on ? "on" : "off"} for ${a.name}`, on ? "Woken tasks run without asking" : "Woken tasks ask first");
}

const TOOLS = [
  { icon: "shield", label: "Trust all hooks", hint: "For every agent on this pc", run: () => MOCK.agents.filter((a) => a.installed && !a.hooksTrusted).forEach(trustHooks) },
  { icon: "bolt", label: "Wake the next ready task", hint: "Sends it to its owner's session", run: () => toast("Woke the next ready task") },
  { icon: "sync", label: "Update the plugin", hint: "All agents, then restart them", run: () => toast("Checked for plugin updates") },
  null,
  { icon: "globe", label: "Open the knowledge web UI", hint: "In your browser", run: () => toast("Opening the web UI") },
  { icon: "copy", label: "Copy diagnostics", hint: "Versions and errors, no token", run: () => toast("Copied diagnostics") },
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
  renderChips(); renderAgents(); renderTasks(); renderActivity(); renderSettings();
  setupMenu(); setupTheme(); setupNav();
  $("refresh-btn").addEventListener("click", () => toast("Refreshed"));
  $("settings").addEventListener("submit", (e) => { e.preventDefault(); toast("Saved settings"); });
});
