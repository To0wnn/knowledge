// The Machines view, split out of app.js: it shares app.js's global scope ($, el, api, go, me, ago ...) and loads
// right after it (index.html).
// Pcs (#/machines): your own online pcs side by side, one column each, so a value that differs shows at a glance; then
// other people's pcs (each its own column: their accounts never mix with yours) and the pcs that are offline.
// Folders (#/machines?view=folders): per project the folders linked on each pc. Workers live in Agents, not here.

const YOLO_WARNING = "YOLO changes this agent's own global setting on that pc (Claude Code: bypassPermissions, Codex: approval never + no sandbox, agy: always-proceed), so EVERY session of it there runs every command and edits every file without asking: the owner's own sessions too, not only workers and chats, also for tasks other people wrote. Risky actions are flagged, never stopped. Turning it off puts the previous settings back.";
const WORKERS_WARNING = "Allow workers on this pc? For every ready task in a linked project it may then start a fresh agent session on its own, in a git worktree of its own, without anyone watching (at most the max per agent). Only for a pc that may work by itself: not a work laptop or a shared family pc.";

// ---------------------------------------------------------------- actions on a pc (the server queues, the pc runs)
async function machineAction(m, action, args = {}) {
  let sent = {};
  try {
    sent = await api("/ui/api/machines/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: m.id, action, args }) });
  } catch (e) { toast(e.message); return loadMachines(); }
  // the pc fetches the action within ~10 s and reports its new state right after: refresh until nothing waits
  for (let i = 0; i < 60; i++) { // up to 3 min: updating the plugins of three agents takes a while
    await loadMachines();
    const row = (machinesCache || []).find((x) => x.id === m.id);
    const mine = row && (row.actions || []).find((x) => x.id === sent.id);
    if (mine?.status === "failed") { toast(`${actionWords(mine)} failed on ${m.label}: ${mine.result || "no reason given"}`); break; } // never only in the log
    if (!row || !(row.actions || []).some((x) => x.status === "waiting" || x.status === "running")) break;
    await new Promise((r) => setTimeout(r, 3000));
  }
  setTimeout(loadMachines, 4000); // the report that follows the action
}
// A button for an action a pc carries out a moment later: a spinner and "Working…" until it reports back. `busy`: the
// list says this action still waits or runs (the page is redrawn on every refresh, so the button cannot remember it).
function machineButton(text, title, onclick, busy = false, cls = "btn small") {
  const b = el("button", cls, text); b.type = "button"; b.title = title;
  const on = () => { b.disabled = true; b.classList.add("busy"); b.textContent = "Working…"; b.setAttribute("aria-busy", "true"); };
  if (busy) on();
  b.onclick = async () => {
    const run = onclick();
    if (!run || typeof run.then !== "function") return; // cancelled (a prompt answered no): nothing to wait for
    on();
    try { await run; } finally { if (b.isConnected) { b.disabled = false; b.classList.remove("busy"); b.removeAttribute("aria-busy"); b.textContent = text; } }
  };
  return b;
}
const pendingAction = (m, action, match = () => true) => (m.actions || []).some((x) => (x.status === "waiting" || x.status === "running") && x.action === action && match(x.args || {}));
const versionParts = (v) => String(v).split(".").map(Number);
const versionNewer = (a, b) => { const x = versionParts(a), y = versionParts(b); for (let i = 0; i < 3; i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); return false; };
// The newest plugin any pc reports, and whether a pc has an older one anywhere ("" when it is current).
const newestPlugin = () => machinesCache.flatMap((pc) => [pc.companion?.version, ...Object.values(pc.agents || {}).map((ag) => ag.plugin)].filter(Boolean)).reduce((a, v) => (versionNewer(v, a) ? v : a), "0");
function pluginBehind(m) {
  const newest = newestPlugin();
  return [m.companion?.version, ...Object.values(m.agents || {}).map((ag) => ag.plugin)].some((v) => v && versionNewer(newest, v)) ? newest : "";
}
// A switch that sends an action: it shows the state the pc reported, and waits while the change is on its way.
function machineSwitch(label, on, busy, aria, onchange) {
  const l = el("label", "check machine-switch"), c = el("input"); c.type = "checkbox"; c.setAttribute("role", "switch"); c.checked = !!on;
  c.setAttribute("aria-label", aria); c.disabled = busy;
  if (busy) l.setAttribute("aria-busy", "true");
  c.onchange = async () => {
    const want = c.checked;
    if (!(await onchange(want))) { c.checked = !want; return; }
    c.disabled = true; l.setAttribute("aria-busy", "true"); // machineAction redraws the page once the pc reported
  };
  l.append(c, " ", label);
  return l;
}
// YOLO per agent: a switch with the state in words. On asks to type YOLO.
function yoloSwitch(m, a, ag) {
  const name = AGENT_NAME[a] || a;
  const l = machineSwitch(ag.yolo ? "YOLO on" : "Off", ag.yolo, pendingAction(m, "yolo", (x) => x.agent === a), `YOLO for ${name} on ${m.label}`, (on) => {
    if (on && prompt(`${YOLO_WARNING}\n\nType YOLO to turn it on for ${name} on ${m.label}.`) !== "YOLO") return false;
    machineAction(m, "yolo", on ? { agent: a, on: true, confirm: "YOLO" } : { agent: a, on: false });
    return true;
  });
  l.classList.add("yolo-switch"); l.classList.toggle("on", !!ag.yolo); l.title = "Run without asking: the agent's global setting on that pc";
  return l;
}
// Remote actions in words: a pc's activity reads as what happened, not as the API's names.
const ACTION_WORDS = {
  update_plugin: () => "Update Knowledge", update_agents: () => "Update agents", apply_models: () => "Apply models",
  workers_allowed: (a) => (a.on ? "Allow workers" : "Turn workers off"),
  max_workers: (a) => `${AGENT_NAME[a.agent] || a.agent}: ${a.n || "no limit on"} workers at a time`,
  yolo: (a) => `YOLO ${a.on ? "on" : "off"} for ${AGENT_NAME[a.agent] || a.agent}`, trust_hooks: (a) => `Trust the hooks of ${AGENT_NAME[a.agent] || a.agent}`,
  link_folder: (a) => `Link ${a.path} to ${a.project}`, unlink_folder: (a) => `Unlink ${a.path}`,
  install_tool: (a) => `Install ${a.tool}`, remove_tool: (a) => `Remove ${a.tool}`, tool_auto: (a) => `Daily update of ${a.tool} ${a.on ? "on" : "off"}`,
  start_worker: (a) => `Start a worker for ${a.id}`, stop_worker: (a) => `Stop the worker of ${a.id}`, restart: (a) => `Restart the worker of ${a.id}`,
  rollback: (a) => `Roll back ${a.id}`, archive_worker: (a) => `Archive the worker of ${a.id}`, merge: (a) => `Merge ${a.id}`, takeover: (a) => `Take over ${a.id}`,
};
const actionWords = (x) => (ACTION_WORDS[x.action] || ((a) => `${x.action} ${Object.values(a).join(" ")}`.trim()))(x.args || {});
const ACTION_STATE = { waiting: "waiting for the pc", running: "running", done: "done", failed: "failed" };
// A worker that waits for its owner (Agents shows them; here only how many).
const workerNeedsYou = (m, w) => (w.status === "failed" && !w.owner_stopped) || w.review?.ahead > 0 || (w.status === "running" && !m.online);
const allWorkers = (m) => Object.entries(m.agents || {}).flatMap(([a, ag]) => (ag.workers || []).map((w) => ({ ...w, agent: w.agent || a })));

// ---------------------------------------------------------------- the folder chooser of a pc of your own
// One level at a time, asked of its companion the same way as a diff.
let chooseAsk = 0, chooseMachine = null, choosePath = "";
function chooseFolder(m, project = "") {
  chooseMachine = m;
  $("choose-project").replaceChildren(...m.companion.projects.map((p) => { const o = el("option", null, p); o.value = p; return o; }));
  if (project) $("choose-project").value = project;
  $("choose-dialog").showModal();
  chooseGo("");
}
async function chooseGo(path) {
  const ask = ++chooseAsk, m = chooseMachine, note = $("choose-note"), live = () => ask === chooseAsk && $("choose-dialog").open;
  $("choose-ok").disabled = true;
  note.textContent = `Asking ${m.label}…`;
  try {
    const post = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: m.id, dirs: path }) };
    const { id } = await api("/ui/api/git/diff", post);
    for (let n = 0; n < 40 && live(); n++) {
      await new Promise((r) => setTimeout(r, n ? 1000 : 300));
      const a = await api(`/ui/api/git/diff?machine=${m.id}&id=${id}`);
      if (a.status !== "done") continue;
      if (!live()) return;
      // a companion before 0.38.6 takes this for a diff of an unknown folder
      if (a.error) { note.textContent = /not a folder linked/.test(a.error) ? `${m.label} needs plugin 0.38.6 or newer for this: update it first.` : a.error; return; }
      const d = JSON.parse(a.text), row = (label, to) => { const li = el("li"), b = el("button", "btn small", label); b.type = "button"; b.onclick = () => chooseGo(to); li.append(b); return li; };
      choosePath = d.path;
      $("choose-where").textContent = d.path || m.label;
      note.textContent = d.path && !d.linkable ? "A drive root, the home folder or a system folder cannot be linked: open a folder in it." : d.dirs.length ? "" : "No folders in here.";
      $("choose-ok").disabled = !d.linkable;
      $("choose-list").replaceChildren(...(d.parent == null ? [] : [row("↑ Up", d.parent)]), ...d.dirs.map((x) => row(x.name, x.path)));
      return;
    }
    if (live()) note.textContent = `${m.label} did not answer: it may need plugin 0.38.6 or newer, or it is busy.`;
  } catch (e) { if (live()) note.textContent = e.message; }
}
$("choose-close").onclick = () => $("choose-dialog").close();
$("choose-ok").onclick = () => {
  $("choose-dialog").close();
  machineAction(chooseMachine, "link_folder", { path: choosePath, project: $("choose-project").value, chosen: true });
};

// ---------------------------------------------------------------- Pcs: the comparison
let machinesMode = "pcs"; // or "folders" (shell.js sets it from the route)
const machinesOpen = new Set(); // the tool panels a person opened: the page is redrawn every refresh
const AGENT_ORDER = ["claude", "codex", "agy"];
const TOOL_KIND = { skill: ["skill", "Instructions an agent loads when a task fits"], program: ["program", "Software installed on this pc that agents run, such as an MCP server"] };
const recentError = (ag) => ag.error && Date.now() / 1000 - (ag.error.at || 0) < 86400;
const steers = (m) => m.own && !m.old && m.online && !!m.companion;
// An update of the plugin that the pc ran in the last 5 minutes: it still restarts on the new version. Read from the
// server's list, so it shows the same after a switch to another page and back.
const UPDATE_SETTLE = 300;
const lastUpdate = (m) => (m.actions || []).filter((x) => x.action === "update_plugin").pop();
const updating = (m) => pendingAction(m, "update_plugin") || (lastUpdate(m)?.status === "done" && Date.now() / 1000 - lastUpdate(m).at < UPDATE_SETTLE);
// An agent this pc's companion knows: only then do its switches and buttons act (a hook alone may report an agent).
const steersAgent = (m, a) => steers(m) && (m.companion.agents || []).includes(a);

// One row of the grid: its name, one cell per pc, and the column at the end for an action on the whole row.
// `pcs`: the column names, which a phone shows in front of each cell (its rows are blocks, the pcs under each other).
function mxRow(cls, label, cells, end, pcs = []) {
  const r = el("div", `mx-row ${cls}`), k = el("div", "mx-k");
  if (label != null) k.append(label);
  r.append(k, ...cells.map((c, i) => { const d = el("div", "mx-c"); if (pcs[i]) d.dataset.pc = pcs[i]; if (c != null) d.append(...[].concat(c)); return d; }));
  const e = el("div", "mx-end"); if (end) e.append(end); r.append(e);
  return r;
}
const val = (text, cls = "") => el("span", cls, text);
const differs = (text) => val(text, "mx-diff"); // a value that is not like the others: the reason to look

// Usage of one account: per window the fullest reading of these pcs. Usage belongs to the account, not to a pc.
function meters(pcs, a) {
  const by = {};
  for (const m of pcs) for (const w of usageWindows(m.agents?.[a] || {})) if (!by[w[1]] || w[0] > by[w[1]][0]) by[w[1]] = w;
  const wins = Object.values(by).sort((x, y) => (x[2] || Infinity) - (y[2] || Infinity));
  const box = el("div", "mx-meters");
  if (!wins.length) { box.append(val("Usage not known yet: it shows after a session or within 15 minutes.", "muted")); return box; }
  const name = (n) => ({ "5h": "5 hours", "7d": "This week", "30d": "This month" }[n] || n);
  for (const [pct, n, resets] of wins) {
    const mt = el("div", "mx-meter"), lab = el("div", "mx-meter-lab"), track = el("div", "mx-track"), fill = el("i");
    lab.append(el("span", null, name(n)), el("b", null, `${Math.round(pct)}%`));
    fill.style.width = `${Math.min(100, Math.max(0, pct))}%`; if (pct >= 80) fill.className = pct >= 95 ? "bad" : "warn";
    track.append(fill); track.setAttribute("role", "img"); track.setAttribute("aria-label", `${name(n)}: ${Math.round(pct)}% used`);
    mt.append(lab, track);
    if (resets) mt.append(el("div", "mx-meter-lab", `resets ${new Date(resets * 1000).toLocaleString("en-GB", resets - Date.now() / 1000 > 86400 ? { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" }).replace(",", "")}`));
    box.append(mt);
  }
  return box;
}

// The head of a pc's column: its name, its Knowledge version, what acts on the whole pc, and whether workers may start.
function pcHead(m, here) {
  const c = m.companion, box = el("div", "mx-pc"), name = el("div", "mx-pc-name");
  const dot = el("span", `machine-dot${m.online ? " on" : ""}`); dot.setAttribute("aria-hidden", "true");
  name.append(dot, el("h3", null, m.label));
  if (here && m.id === here) name.append(el("span", "pill this", "This pc"));
  if (m.mine && !m.own) { const p = el("span", "pill", "remote control off"); p.title = "Your pc, but its token does not allow steering it from here (Admin › Tokens)"; name.append(p); }
  else if (!m.own) { const p = el("span", "pill", "view only"); p.title = "Someone else's pc, or one not marked as your own device: you see it, you cannot steer it"; name.append(p); }
  const behind = pluginBehind(m), meta = el("div", "mx-pc-meta");
  meta.append(behind ? differs(`Knowledge ${c?.version || "?"}`) : val(`Knowledge ${c?.version || "?"}`));
  if (c?.game) meta.append(" · ", val(c.game));
  box.append(name, meta);
  const last = lastUpdate(m);
  if (behind && last?.status === "done" && !updating(m) && Date.now() / 1000 - last.at < 86400)
    box.append(el("p", "machine-error", `The update ${ago(last.at)} said done, yet it still runs ${c?.version || "an older version"}.`));
  const acts = el("div", "mx-pc-acts");
  if (behind && steers(m)) acts.append(machineButton(`Update to ${behind}`, "Update the knowledge plugin of every agent there and its companion (it restarts by itself)", () => machineAction(m, "update_plugin"), updating(m), "btn small primary"));
  // an admin may update the plugin on someone else's pc, nothing else there (the server checks its remote-control switch)
  if (behind && me.admin && !m.own && !m.old && m.online && c) acts.append(machineButton(`Update to ${behind} (admin)`, "Update the knowledge plugin there; its owner's settings stay as they are", () => machineAction(m, "update_plugin"), updating(m), "btn small primary"));
  if (steers(m) && (c.agents || []).length && (c.folders?.linked || []).length) acts.append(machineButton("Chat", "Talk to an agent on this pc (Chats)", async () => { go("#/chats"); if (!ck.machines.length) await ckSummary().catch(() => {}); ckNewChat(m.id); }));
  if (m.mine) acts.append(pcMore(m));
  if (acts.childNodes.length) box.append(acts);
  if (c) box.append(steers(m) ? machineSwitch("Runs workers", c.workersAllowed, pendingAction(m, "workers_allowed"), `Workers on ${m.label}`, (on) => {
    if (on && !confirm(`${WORKERS_WARNING}\n\n${m.label}`)) return false;
    machineAction(m, "workers_allowed", { on }); return true;
  }) : val(c.workersAllowed ? "Runs workers" : "Runs no workers", "mx-pc-meta"));
  return box;
}
// The rarer actions on a pc, behind a More button: update its agents, what was asked of it, forget it.
function pcMore(m) {
  const menu = el("details", "menu machine-more"), sum = el("summary", "btn small quiet", "More"), list = el("div", "menu-list");
  sum.setAttribute("aria-label", `More for ${m.label}`);
  const item = (text, fn) => list.append(actionButton(text, () => { menu.open = false; fn(); }, ""));
  if (steers(m)) item("Update agents", () => machineAction(m, "update_agents"));
  item("Activity…", () => pcActivity(m));
  item("Forget this pc…", () => forgetPc(m));
  menu.append(sum, list);
  return menu;
}
async function forgetPc(m) {
  if (!confirm(`Forget ${m.label}? It leaves this list and comes back when it reports again; its actions, history and chats on the server go.`)) return;
  try { await api("/ui/api/machines/forget", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: m.id }) }); } catch (e) { toast(e.message); }
  loadMachines();
}
// What was asked of a pc and how it went, in a dialog: it is looked at rarely.
function pcActivity(m) {
  const d = el("dialog", "mx-dialog"), h = el("h3", null, `${m.label}: what was asked of it`), ul = el("ul", "machine-list activity");
  for (const x of (m.actions || []).slice().reverse()) {
    const li = el("li", x.status === "failed" ? "failed" : null);
    li.append(el("span", "activity-what", actionWords(x)), el("span", `pill ${x.status === "failed" ? "warn" : x.status === "done" ? "ok" : ""}`, ACTION_STATE[x.status] || x.status),
      el("span", "muted", `${x.by || "?"} · ${ago(x.at)}`));
    if (x.result) li.append(el("span", x.status === "failed" ? "machine-error activity-result" : "worker-note activity-result", x.result));
    ul.append(li);
  }
  const close = el("button", "btn", "Close"); close.type = "button"; close.onclick = () => d.close();
  d.append(h, ul.childNodes.length ? ul : el("p", "muted", "Nothing was asked of this pc lately."), close);
  d.onclose = () => d.remove();
  document.body.append(d); d.showModal();
}

// One agent on one pc, per row.
function pluginCell(m, a) {
  const ag = m.agents?.[a];
  if (!ag || !(m.companion?.agents || Object.keys(m.agents || {})).includes(a)) return val("Not installed", "muted");
  if (!ag.plugin) return val("No plugin", "mx-diff");
  return versionNewer(newestPlugin(), ag.plugin) ? differs(ag.plugin) : val(ag.plugin, "ok");
}
function hooksCell(m, a) {
  const ag = m.agents?.[a];
  if (!ag) return null;
  const out = [];
  if (ag.trusted === false) {
    out.push(differs("Not trusted"));
    if (steersAgent(m, a)) out.push(" ", machineButton("Trust", "Trust the knowledge hooks the agent's own way", () => machineAction(m, "trust_hooks", { agent: a }), pendingAction(m, "trust_hooks", (x) => x.agent === a)));
  } else out.push(val(ag.trusted ? "Trusted" : "Unknown", ag.trusted ? "ok" : "muted"));
  if (recentError(ag)) {
    const e = ag.error, p = el("p", "machine-error", `Hook ${e.hook || "?"} failed ${ago(e.at)}: ${(e.stderr || e.text || "").trim().split("\n").pop().slice(0, 160)}`);
    p.title = e.stderr || ""; out.push(p);
  }
  return out;
}
function yoloCell(m, a) {
  const ag = m.agents?.[a];
  if (!ag) return null;
  if (steersAgent(m, a)) return yoloSwitch(m, a, ag);
  return ag.yolo ? el("span", "pill yolo", "YOLO on") : val("Off", "muted");
}
function workersCell(m, a) {
  const ag = m.agents?.[a], c = m.companion;
  if (!ag || !c) return null; // without a companion nothing starts workers: no number to show
  const running = (ag.workers || []).filter((w) => w.status === "running").length, most = (c?.maxWorkers || {})[a] ?? 1;
  const now = val(running ? `${running} running` : "none running", "muted");
  if (!steersAgent(m, a)) return [val(most ? `at most ${most}` : "no limit"), " · ", now];
  const pick = el("select", "small"); pick.setAttribute("aria-label", `Workers of ${AGENT_NAME[a] || a} at a time on ${m.label}`);
  for (const n of [...new Set([1, 2, 3, 4, 5, most || 1])].sort((x, y) => x - y).concat(0)) { const o = el("option", null, n ? String(n) : "No limit"); o.value = n; pick.append(o); }
  pick.value = String(most); pick.disabled = pendingAction(m, "max_workers", (x) => x.agent === a);
  pick.onchange = () => machineAction(m, "max_workers", { agent: a, n: Number(pick.value) });
  return [pick, " ", now];
}

// A tool on one pc: what it is there, and once installed a panel with its version, the daily update, Update now, Remove.
function toolCell(m, id, cellKey) {
  const t = m.companion?.tools?.[id];
  if (!t) return val("—", "muted");
  if (t.busy) { const s = el("span", "mx-busy", { remove: "Removing…", update: "Updating…" }[t.busy] || "Installing…"); s.setAttribute("role", "status"); return s; }
  const own = steers(m), out = [];
  if (!t.installed) {
    if (own) out.push(machineButton("Install", `Install ${t.name} for every agent on ${m.label}`, () => machineAction(m, "install_tool", { tool: id }), pendingAction(m, "install_tool", (x) => x.tool === id)));
    else out.push(val("Not installed", "muted"));
    if (t.error) out.push(el("p", "machine-error", t.error));
    return out;
  }
  const bad = t.error || t.ok === false, open = machinesOpen.has(cellKey);
  const b = el("button", `mx-cellbtn${bad ? " mx-diff" : " ok"}`, `${bad ? "Needs attention" : "Installed"}${t.version ? ` ${t.version}` : ""} ▾`);
  b.type = "button"; b.setAttribute("aria-expanded", String(open)); b.dataset.key = cellKey;
  b.onclick = () => { if (machinesOpen.has(cellKey)) machinesOpen.delete(cellKey); else { machinesOpen.clear(); machinesOpen.add(cellKey); } drawMachines(); };
  out.push(b);
  if (open) {
    const pop = el("div", "mx-pop"); pop.setAttribute("role", "group"); pop.setAttribute("aria-label", `${t.name} on ${m.label}`);
    pop.append(el("h4", null, `${t.name} on ${m.label}`));
    if (t.description) pop.append(el("p", "muted", t.description));
    const fact = (k, v) => { const r = el("div", "mx-pop-row"); r.append(el("span", null, k), v); pop.append(r); };
    fact("Version", val(t.version || "not reported"));
    if (t.at) fact("Last installed or updated", val(ago(t.at)));
    if (own) fact("Update every day", machineSwitch(t.auto === false ? "Off" : "On", t.auto !== false, pendingAction(m, "tool_auto", (x) => x.tool === id), `Daily update of ${t.name} on ${m.label}`,
      (on) => { machineAction(m, "tool_auto", { tool: id, on }); return true; }));
    else fact("Update every day", val(t.auto === false ? "Off" : "On"));
    if (t.error || t.detail) pop.append(el("p", t.error ? "machine-error" : "worker-note", t.error || t.detail));
    const acts = el("div", "mx-pop-acts");
    if (own) {
      acts.append(machineButton("Update now", "Install the newest version again for every agent here", () => machineAction(m, "install_tool", { tool: id }), pendingAction(m, "install_tool", (x) => x.tool === id)));
      acts.append(machineButton("Remove…", `Remove ${t.name} from every agent on ${m.label}`, () => confirm(`Remove ${t.name} from ${m.label}?`) && (machinesOpen.delete(cellKey), machineAction(m, "remove_tool", { tool: id })), pendingAction(m, "remove_tool", (x) => x.tool === id), "btn small danger"));
    }
    if (t.homepage) { const a = el("a", "tool-source", "Source"); a.href = t.homepage; a.target = "_blank"; a.rel = "noopener"; acts.append(a); }
    pop.append(acts);
    out.push(pop);
  }
  return out;
}

// The comparison of some pcs. `mine`: your own (their usage is one account, rows can be made the same); else one other pc.
function compareTable(pcs, here, mine) {
  const wrap = el("div", "mx-wrap"), grid = el("div", "mx-grid");
  grid.style.setProperty("--n", pcs.length);
  wrap.append(grid);
  const names = pcs.length > 1 ? pcs.map((m) => m.label) : [], row = (cls, label, cells, end) => mxRow(cls, label, cells, end, names);
  grid.append(mxRow("mx-head", null, pcs.map((m) => pcHead(m, here))));
  const agents = AGENT_ORDER.filter((a) => pcs.some((m) => m.agents?.[a]));
  for (const a of agents) {
    const name = el("div", "mx-agent-name"), mark = el("span", "mx-mark", AGENT_MARK[a] || a[0].toUpperCase());
    mark.setAttribute("aria-hidden", "true");
    name.append(mark, el("span", null, AGENT_NAME[a] || a));
    if (!mine) name.append(el("small", "muted", "their account"));
    const head = mxRow("mx-agent", name, []);
    head.insertBefore(meters(pcs, a), head.lastChild);
    grid.append(head);
    grid.append(row("", "Plugin", pcs.map((m) => pluginCell(m, a))));
    grid.append(row("", "Hooks", pcs.map((m) => hooksCell(m, a))));
    grid.append(row("", "YOLO", pcs.map((m) => yoloCell(m, a))));
    grid.append(row("", "Workers at a time", pcs.map((m) => workersCell(m, a))));
  }
  const ids = [...new Set(pcs.flatMap((m) => Object.keys(m.companion?.tools || {})))].sort((x, y) => {
    const name = (id) => pcs.map((m) => m.companion?.tools?.[id]?.name).find(Boolean) || id;
    return name(x).localeCompare(name(y));
  });
  if (ids.length) {
    grid.append(mxRow("mx-sec", "Tools", pcs.map(() => null)));
    for (const id of ids) {
      const t = pcs.map((m) => m.companion?.tools?.[id]).find(Boolean), label = el("span", "mx-tool");
      label.append(el("span", null, t.name));
      const kind = TOOL_KIND[t.kind]; if (kind) { const k = el("small", "muted", kind[0]); k.title = kind[1]; label.append(" ", k); }
      const lacking = mine ? pcs.filter((m) => steers(m) && m.companion?.tools?.[id] && !m.companion.tools[id].installed && !m.companion.tools[id].busy) : [];
      const end = mine && lacking.length > 1 ? machineButton("Install on all my pcs", `Install ${t.name} on ${lacking.map((m) => m.label).join(", ")}`,
        () => Promise.all(lacking.map((m) => machineAction(m, "install_tool", { tool: id })))) : null;
      grid.append(row("", label, pcs.map((m) => toolCell(m, id, `${m.id}:${id}`)), end));
    }
  }
  return wrap;
}

// What needs a person on your own pcs, one line each, the action right there.
function machinesStatus(res, own, online) {
  const box = el("section", "mx-status"), text = el("div"), items = [];
  const lead = el("p", "mx-lead", own.length ? `${online.length} of your ${own.length} pc${own.length === 1 ? " is" : "s are"} online.` : "None of your pcs reported yet.");
  const behind = online.filter((m) => steers(m) && pluginBehind(m));
  for (const m of behind) {
    const last = lastUpdate(m);
    if (last?.status === "done" && !updating(m) && Date.now() / 1000 - last.at < 86400) { // it said done, yet it is still behind
      items.push([`The update on ${m.label} ${ago(last.at)} did not bring it to ${newestPlugin()} (it runs ${m.companion.version}): `, actionButton("see Activity", () => pcActivity(m), "btn small quiet")]);
      continue;
    }
    items.push([versionNewer(newestPlugin(), m.companion.version || "0") ? `${m.label} runs an older Knowledge (${m.companion.version}).`
    : `The plugin of ${Object.entries(m.agents || {}).filter(([, ag]) => ag.plugin && versionNewer(newestPlugin(), ag.plugin)).map(([a]) => AGENT_NAME[a] || a).join(" and ")} on ${m.label} is older than its Knowledge.`]);
  }
  for (const m of online) for (const [a, ag] of Object.entries(m.agents || {})) {
    if (ag.trusted === false) items.push([`${AGENT_NAME[a] || a} on ${m.label} does not trust the knowledge hooks yet.`]);
    if (recentError(ag)) items.push([`A hook of ${AGENT_NAME[a] || a} on ${m.label} failed ${ago(ag.error.at)}.`]);
  }
  for (const m of online) {
    const bad = Object.values(m.companion?.tools || {}).filter((t) => t.error || t.ok === false);
    if (bad.length) items.push([`${bad.map((t) => t.name).join(", ")} on ${m.label} ${bad.length > 1 ? "need" : "needs"} attention.`]);
    const day = Date.now() / 1000 - 86400, failed = (m.actions || []).filter((x) => x.status === "failed" && x.at > day);
    if (failed.length) { const link = actionButton("see Activity", () => pcActivity(m), "btn small quiet"); items.push([`${actionWords(failed[failed.length - 1])} failed on ${m.label} ${ago(failed[failed.length - 1].at)}: `, link]); }
    if (m.companion && (m.companion.agents || []).length && !(m.companion.folders?.linked || []).length) {
      const link = el("a", null, "link one"); link.href = "#/machines?view=folders"; items.push([`${m.label} has no linked folder yet: `, link, "."]);
    }
  }
  const waiting = own.reduce((n, m) => n + allWorkers(m).filter((w) => workerNeedsYou(m, w)).length, 0);
  if (waiting) { const link = el("a", null, "Agents"); link.href = "#/chats"; items.push([`${waiting} worker${waiting > 1 ? "s need" : " needs"} you, in `, link, "."]); }
  for (const s of res.stuck || []) items.push([`${s.tasks} task${s.tasks > 1 ? "s" : ""} of ${s.project} cannot start by itself: none of your pcs has a folder linked to that project.`]);
  text.append(lead);
  if (items.length) { const ul = el("ul", "mx-items"); for (const parts of items) { const li = el("li"); li.append(...parts); ul.append(li); } text.append(ul); }
  else if (online.length) text.append(el("p", "muted", "Everything is up to date."));
  box.append(text);
  if (behind.length > 1 || (behind.length === 1 && online.length > 1)) box.append(machineButton("Update all my pcs", `Update Knowledge on ${behind.map((m) => m.label).join(", ")}`,
    () => Promise.all(behind.map((m) => machineAction(m, "update_plugin"))), behind.some((m) => pendingAction(m, "update_plugin")), "btn primary"));
  return box;
}

// Offline pcs: one line each, with Forget, and Revoke when its token is its own (revoking cuts off every pc using it).
function offlineSection(title, pcs, res) {
  const sec = el("section", "mx-offline"), ul = el("ul", "mx-lines"), groups = new Map();
  // a pc set up again reports under a new id each time: one line per name and token, its reports counted
  for (const m of pcs) { const k = `${m.label.toLowerCase()}|${[...m.tokens].sort().join(",")}`; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(m); }
  sec.append(el("h2", "mx-h", `${title} (${groups.size})`));
  for (const ms of groups.values()) {
    const m = ms[0], ids = new Set(ms.map((x) => x.id)), li = el("li", "mx-line"), name = el("strong"), info = el("div");
    const dot = el("span", "machine-dot"); dot.setAttribute("aria-hidden", "true");
    name.append(dot, " ", m.label);
    if (ms.length > 1) name.append(" ", el("span", "pill", `${ms.length} reports`));
    if (!m.mine) name.append(" ", el("span", "pill", "view only"));
    const others = res.machines.filter((o) => !ids.has(o.id) && o.tokens.some((t) => m.tokens.includes(t)));
    info.append(el("span", null, [m.old ? "old plugin" : `last seen ${ago(m.at)}`, m.companion?.version && `Knowledge ${m.companion.version}`, m.tokens.length && `token ${m.tokens.join(", ")}`].filter(Boolean).join(" · ")));
    if (others.length) info.append(el("small", "muted", `Its token is also used by ${[...new Set(others.map((o) => o.label))].join(", ")}: revoking it would cut those off too.`));
    const acts = el("div", "mx-line-acts"), mine = ms.filter((x) => x.mine);
    if (mine.length) acts.append(actionButton(mine.length > 1 ? `Forget all ${mine.length}` : "Forget", async () => {
      if (mine.length === 1) return forgetPc(m);
      if (!confirm(`Forget the ${mine.length} reports of ${m.label}? Their actions, history and chats on the server go; a pc that reports again comes back.`)) return;
      for (const x of mine) await api("/ui/api/machines/forget", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: x.id }) }).catch((e) => toast(e.message));
      loadMachines();
    }, "btn small"));
    if (me.admin && !ms.some((x) => x.online) && m.tokens.length && !others.length) acts.append(actionButton("Revoke its token…", async () => {
      if (!confirm(`Revoke token ${m.tokens.join(", ")} of ${m.label}? It can no longer reach the server; a new token is needed to set it up again.`)) return;
      try { for (const t of m.tokens) await api("/ui/api/admin/token", { method: "POST", headers: { "Content-Type": "application/json", "X-Knowledge-UI": "1" }, body: JSON.stringify({ name: t, revoke: true }) }); } catch (e) { toast(e.message); }
      loadMachines();
    }, "btn small danger"));
    li.append(name, info, acts);
    ul.append(li);
  }
  sec.append(ul);
  return sec;
}

// ---------------------------------------------------------------- Folders: per project, the folders on each pc
function foldersView(res) {
  const own = res.machines.filter((m) => m.mine && !m.old && m.companion), parts = [];
  for (const s of res.stuck || []) parts.push(el("p", "machine-stuck", `${s.tasks} task${s.tasks > 1 ? "s" : ""} of ${s.project} cannot start by itself: none of your pcs has a folder linked to that project.`));
  parts.push(el("p", "muted mx-lede", "Which folder on which pc belongs to which project. An agent started in a linked folder gets that project's memory and docs, and workers of that project may start there."));
  const projects = [...new Set(own.flatMap((m) => [...(m.companion.projects || []), ...(m.companion.folders?.linked || []).map((f) => f.project)]))].sort();
  for (const p of projects) {
    const box = el("section", "mx-proj"), head = el("div", "mx-proj-head");
    head.append(el("h3", null, p));
    const hosts = own.filter((m) => steers(m) && (m.companion.projects || []).includes(p));
    if (hosts.length === 1) head.append(machineButton("Link a folder", `Choose a folder on ${hosts[0].label}`, () => chooseFolder(hosts[0], p)));
    else if (hosts.length > 1) {
      const pick = el("select", "small"); pick.setAttribute("aria-label", `Pc to link a folder of ${p} on`);
      for (const m of hosts) { const o = el("option", null, m.label); o.value = m.id; pick.append(o); }
      head.append(pick, machineButton("Link a folder", "Choose a folder on that pc", () => chooseFolder(hosts.find((m) => m.id === pick.value), p)));
    }
    box.append(head);
    const rows = own.flatMap((m) => (m.companion.folders?.linked || []).filter((f) => f.project === p).map((f) => [m, f]));
    if (!rows.length) box.append(el("p", "muted mx-empty", `No folder linked yet. Link one to give agents in that folder the memory of ${p}.`));
    for (const [m, f] of rows) {
      const r = el("div", "mx-frow");
      r.append(el("span", null, m.label), el("code", null, f.path));
      r.append(m.online && m.own ? machineButton("Unlink", "Remove its .knowledge-project file (nothing else)", () => confirm(`Unlink ${f.path} on ${m.label}?`) && machineAction(m, "unlink_folder", { path: f.path }),
        pendingAction(m, "unlink_folder", (x) => x.path === f.path), "btn small quiet") : el("span", "muted", m.online ? "" : "pc offline"));
      box.append(r);
    }
    parts.push(box);
  }
  const cand = own.filter((m) => steers(m)).flatMap((m) => (m.companion.folders?.candidates || []).map((f) => [m, f]));
  if (cand.length) {
    const box = el("section", "mx-proj"), head = el("div", "mx-proj-head");
    head.append(el("h3", null, "Not linked, but looks like a project"));
    box.append(head);
    for (const [m, f] of cand) {
      const r = el("div", "mx-frow"), proj = el("select", "small");
      proj.setAttribute("aria-label", `Project for ${f.path}`);
      for (const p of m.companion.projects || []) { const o = el("option", null, p); o.value = p; proj.append(o); }
      r.append(el("span", null, m.label), el("code", null, f.path), proj, machineButton("Link", "Write .knowledge-project in that folder", () => machineAction(m, "link_folder", { path: f.path, project: proj.value }), pendingAction(m, "link_folder", (x) => x.path === f.path)));
      box.append(r);
    }
    parts.push(box);
  }
  if (!projects.length) parts.push(emptyState("No pc of yours lists a project yet: once a pc runs the knowledge setup, its projects show here."));
  return parts;
}

// ---------------------------------------------------------------- the page
let machinesTimer = 0, machinesRes = null, machinesHere = "";
const MACHINE_REFRESH = 20000; // a pc reports about every 10 s
// Redraws while the view is open; never under a person's hands (a field being typed in, an open menu, panel or dialog).
function machinesBusy() {
  const box = $("machines"), a = document.activeElement;
  const typing = box.contains(a) && (/^(SELECT|TEXTAREA)$/.test(a.tagName) || (a.tagName === "INPUT" && !/^(checkbox|radio)$/.test(a.type)));
  return typing || box.querySelector(".menu[open]") || document.querySelector("dialog[open]");
}
function machinesSoon() {
  clearTimeout(machinesTimer);
  machinesTimer = setTimeout(() => {
    if ($("tab-machines").hidden) return;
    if (document.hidden || machinesBusy()) return machinesSoon();
    loadMachines().catch(() => machinesSoon());
  }, MACHINE_REFRESH);
}
async function loadMachines() {
  const res = await api("/ui/api/machines");
  machinesCache = Array.isArray(res) ? res : res.machines || [];
  machinesRes = { ...res, machines: machinesCache };
  if (machinesBusy() && $("machines").childNodes.length) return machinesSoon(); // machineAction still reads machinesCache
  if (!localMachine) await probeCompanion();
  const sha = async (t) => [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(t)))].map((b) => b.toString(16).padStart(2, "0")).join("");
  machinesHere = localMachine && crypto.subtle ? (await Promise.all(machinesCache.map(async (m) => (await sha(m.id)) === localMachine ? m.id : ""))).find(Boolean) || "" : "";
  drawMachines();
  machinesSoon();
}
function drawMachines() {
  const res = machinesRes, here = machinesHere, box = $("machines"), parts = [];
  if (!res) return;
  if (machinesMode === "folders") parts.push(...foldersView(res));
  else if (!res.machines.length) {
    const how = el("a", "btn small", "How to install"); how.href = "/ui/help#install";
    parts.push(emptyState("No pcs yet: a pc shows up here, with its agents and tools, once it runs the knowledge setup.", how));
  } else {
    // yours by owner (mine), not by "may steer" (own): a pc of yours with remote control off is still yours, read-only
    const own = res.machines.filter((m) => m.mine), online = own.filter((m) => m.online && !m.old).sort((a, b) => (b.id === here) - (a.id === here));
    parts.push(machinesStatus(res, own, online));
    if (online.length) parts.push(compareTable(online, here, true));
    const offline = own.filter((m) => !m.online || m.old).sort((a, b) => (b.at || 0) - (a.at || 0));
    if (offline.length) parts.push(offlineSection("Offline", offline, res));
    const theirs = res.machines.filter((m) => !m.mine);
    if (theirs.length) {
      const sec = el("section", "mx-theirs"), h = el("h2", "mx-h", "Other people's pcs"), live = theirs.filter((m) => m.online && !m.old);
      const behind = me.admin ? live.filter((m) => m.companion && pluginBehind(m)) : [];
      if (behind.length > 1) h.append(machineButton("Update all their pcs (admin)", `Update Knowledge on ${behind.map((m) => m.label).join(", ")}`, () => Promise.all(behind.map((m) => machineAction(m, "update_plugin")))));
      sec.append(h, el("p", "muted mx-lede", "Each pc on its owner's accounts: never counted with yours. You see them; their owner steers them."));
      for (const m of live) sec.append(compareTable([m], here, false));
      const gone = theirs.filter((m) => !m.online || m.old);
      if (gone.length) sec.append(offlineSection("Offline", gone, res));
      parts.push(sec);
    }
  }
  const y = box.closest(".view")?.scrollTop, a = document.activeElement, was = box.contains(a) && a !== box && { key: a.dataset.key, text: a.textContent, tag: a.tagName, label: a.getAttribute("aria-label") };
  box.replaceChildren(...parts);
  if (y != null) box.closest(".view").scrollTop = y; // a refresh must not move the page under the reader
  if (was) { // nor take the keyboard focus away
    const again = (was.key && box.querySelector(`[data-key="${CSS.escape(was.key)}"]`)) || [...box.querySelectorAll(was.tag)].find((e) => (was.label && e.getAttribute("aria-label") === was.label) || e.textContent === was.text);
    again?.focus({ preventScroll: true });
  }
}
// An open tool panel closes with Escape or a click outside it.
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && machinesOpen.size && !$("tab-machines").hidden) { machinesOpen.clear(); drawMachines(); } });
// No redraw on that click: it would replace what was clicked (a More menu, a list, a button) before it acts.
document.addEventListener("click", (e) => {
  if (!machinesOpen.size || $("tab-machines").hidden || e.target.closest(".mx-pop, .mx-cellbtn, dialog")) return;
  machinesOpen.clear();
  for (const p of $("machines").querySelectorAll(".mx-pop")) p.remove();
  for (const b of $("machines").querySelectorAll('.mx-cellbtn[aria-expanded="true"]')) b.setAttribute("aria-expanded", "false");
});
