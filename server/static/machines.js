// The Machines view, split out of app.js: it shares app.js's global scope ($, el, api, go, me, ago ...) and loads
// right after it (index.html).
// Pcs (#/machines): your own online pcs side by side, one column each, so a value that differs shows at a glance; then
// other people's pcs (each its own column: their accounts never mix with yours) and the pcs that are offline.
// Folders (#/machines?view=folders): per project the folders linked on each pc. Workers live in Agents, not here.
// Files (#/files, its own sidebar item): browse, read, edit and play the files in those folders, also on a pc whose
// owner shared a project with you for use (only that project's folders); the project picker scopes it.

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
// the server's word (GitHub's newest); only an old server without it falls back to the highest a pc reports
const newestPlugin = () => machinesRes?.newest || machinesCache.flatMap((pc) => [pc.companion?.version, ...Object.values(pc.agents || {}).map((ag) => ag.plugin)].filter(Boolean)).reduce((a, v) => (versionNewer(v, a) ? v : a), "0");
function pluginBehind(m) { // an agent that waits for itself to close (agy open on Windows) updates by itself: no button for it
  const newest = newestPlugin(), later = m.companion?.self?.later || [];
  return [m.companion?.version, ...Object.entries(m.agents || {}).filter(([a]) => !later.includes(a)).map(([, ag]) => ag.plugin)].some((v) => v && versionNewer(newest, v)) ? newest : "";
}
// The agent program itself (Claude Code 2.1.319): the newest of its own update channel, "" while the server does not know it.
const agentNewest = (m, a) => { const t = machinesRes?.newestAgents?.[a] || {}; return t[m.agents?.[a]?.channel] || t.latest || ""; };
const agentBehind = (m, a) => { const v = m.agents?.[a]?.cli, n = agentNewest(m, a); return v && n && versionNewer(n, v) ? n : ""; };
const agentsBehind = (m) => AGENT_ORDER.filter((a) => agentBehind(m, a));
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
  max_agents: (a) => (a.n ? `At most ${a.n} agents at once` : "No limit on agents at once"),
  yolo: (a) => `YOLO ${a.on ? "on" : "off"} for ${AGENT_NAME[a.agent] || a.agent}`, admin: (a) => `Admin rights ${a.on ? "on" : "off"}`, trust_hooks: (a) => `Trust the hooks of ${AGENT_NAME[a.agent] || a.agent}`,
  link_folder: (a) => `Link ${a.path} to ${shown(a.project)}`, unlink_folder: (a) => `Unlink ${a.path}`,
  install_tool: (a) => `Install ${a.tool}`, remove_tool: (a) => `Remove ${a.tool}`, tool_auto: (a) => `Daily update of ${a.tool} ${a.on ? "on" : "off"}`,
  start_worker: (a) => `Start a worker for ${a.id}`, stop_worker: (a) => `Stop the worker of ${a.id}`, restart: (a) => `Restart the worker of ${a.id}`,
  rollback: (a) => (a.undo ? `Undo the roll back of chat ${a.id}` : `Roll back ${a.chat ? "chat " : ""}${a.id}`), archive_worker: (a) => `Archive the worker of ${a.id}`, merge: (a) => `Merge ${a.id}`, takeover: (a) => `Take over ${a.id}`,
};
const actionWords = (x) => (ACTION_WORDS[x.action] || ((a) => `${x.action} ${Object.values(a).join(" ")}`.trim()))(x.args || {});
const ACTION_STATE = { waiting: "waiting for the pc", running: "running", done: "done", failed: "failed" };
// A worker that waits for its owner (Agents shows them; here only how many).
const workerNeedsYou = (m, w) => (w.status === "failed" && !w.owner_stopped) || w.review?.ahead > 0 || (w.status === "running" && !m.online);
const allWorkers = (m) => Object.entries(m.agents || {}).flatMap(([a, ag]) => (ag.workers || []).map((w) => ({ ...w, agent: w.agent || a })));

// ---------------------------------------------------------------- the folder chooser of a pc of your own
// One level at a time, asked of its companion the same way as a diff.
let chooseAsk = 0, chooseMachine = null, choosePath = "";
// pcs: the pcs to choose from in the dialog; switching one starts its folder list over, with its own projects
function chooseFolder(m, project = "", pcs = [m]) {
  const pick = $("choose-pc"), use = (x) => {
    chooseMachine = x;
    $("choose-project").replaceChildren(...x.companion.projects.map((p) => { const o = el("option", null, shown(p)); o.value = p; return o; }));
    if (project && x.companion.projects.includes(project)) $("choose-project").value = project;
  };
  pick.replaceChildren(...pcs.map((x) => { const o = el("option", null, x.label); o.value = x.id; return o; }));
  pick.value = m.id;
  pick.onchange = () => { use(pcs.find((x) => x.id === pick.value)); chooseGo(""); };
  use(m);
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
let machinesMode = "pcs"; // or "folders" or "files" (shell.js sets it from the route)
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
// Why a pc's companion is not (yet) on the newest version, in words, from what it reports about itself ("" when it is).
function selfNote(m) {
  const c = m.companion, me = c?.self || {}, newest = newestPlugin(), last = lastUpdate(m);
  if (me.waits) return `Knowledge ${me.disk} is installed; the companion restarts on it after ${me.waits}.`;
  if ((me.later || []).includes("agy")) return `agy is open on ${m.label}: its plugin updates by itself once agy is closed.`;
  if (!c?.version || !versionNewer(newest, c.version)) return "";
  if (me.from === "copy") return `Its companion runs from a plugin copy, which Update does not move: run the installer once on ${m.label}.`;
  if (last?.status === "done" && !updating(m) && Date.now() / 1000 - last.at < 86400) {
    if (last.args?.target) {
      if (!versionNewer(last.args.target, c.version)) return "";
    } else if (last.result && last.result.includes(c.version)) {
      return "";
    }
    return `The update ${ago(last.at)} said done, yet the companion still runs ${c.version}${me.from ? "" : " (its plugin is too old to say why)"}.`;
  }
  return "";
}
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
  const wins = usageWindows({ windows: Object.values(by) });
  const box = el("div", "mx-meters");
  if (!wins.length) { box.append(val("Usage not known yet: it shows after a session or within 15 minutes.", "muted")); return box; }
  // agy names a window after its model group: "Gemini 5h", "Claude/GPT 7d"
  const name = (n) => ({ "5h": "5 hours", "7d": "This week", "30d": "This month" }[n] || n.replace(/ 5h$/, " · 5 hours").replace(/ 7d$/, " · this week"));
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

// A pc's load (the companion's sysstats) as a stat tile: the value now, a meter, a chart that is always there (hover
// for a moment's value) and min/avg/max of the zoomed window. While Machines is open the value and meter go live.
const STAT_NAME = { cpu: "CPU", gpu: "GPU", ram: "RAM", vram: "VRAM" };
let STAT_ZOOM = "24h"; // one zoom for every chart: comparing pcs side by side
const STAT_ZOOMS = { "1h": "1 h", "6h": "6 h", "24h": "24 h" };
const statGb = (x) => (x / 1024).toFixed(x < 10240 ? 1 : 0);
function statCell(m, k) {
  const st = m.companion?.stats?.[k], total = m.companion?.stats?.total?.[k];
  if (!st) return null;
  const fmt = total ? (x) => `${statGb(x)} GB` : (x) => `${Math.round(x)}%`;
  // a companion before zooming sends only 24 h: its chart stays at 24 h
  const z = STAT_ZOOM !== "24h" && st.hour?.length ? STAT_ZOOM : "24h", [lo, hi, avg] = st.windows?.[z] || [st.min, st.max, st.avg];
  const series = z === "1h" ? st.hour : z === "6h" ? st.series.slice(-36) : st.series;
  const tile = el("div", "mx-stat"), head = el("div", "mx-stat-head"), v = el("b", "mx-stat-v"), of = el("span", "muted");
  const track = el("div", "mx-track mx-stat-meter"), fill = el("i");
  track.append(fill); track.setAttribute("aria-hidden", "true");
  head.append(v, of); tile.dataset.live = `${m.id}:${k}`;
  tile.append(head, track, sparkline(series, total || 100, z === "1h" ? 60 : m.companion.stats.bucket || 600, fmt,
    `${STAT_NAME[k]} on ${m.label}, last ${STAT_ZOOMS[z]}: min ${fmt(lo)}, average ${fmt(avg)}, max ${fmt(hi)}`),
    val(`min ${fmt(lo)} · avg ${fmt(avg)} · max ${fmt(hi)}`, "mx-stat-range"));
  statValue(tile, total ? [st.now, total] : st.now);
  statLiveShow(tile);
  return tile;
}
function statValue(tile, x) { // x: a % or [used, total] MB
  const [v, of] = tile.querySelector(".mx-stat-head").children, fill = tile.querySelector(".mx-stat-meter i");
  const pct = Array.isArray(x) ? 100 * x[0] / x[1] : x;
  setText(v, Array.isArray(x) ? statGb(x[0]) : `${Math.round(x)}%`);
  setText(of, Array.isArray(x) ? ` / ${statGb(x[1])} GB` : "");
  fill.style.width = `${Math.min(100, Math.max(0, pct))}%`; fill.className = pct >= 95 ? "bad" : pct >= 80 ? "warn" : "";
}
// Live load: every few seconds while Machines is open (the companions send it only while someone asks)
let statLive = {}, statLiveTimer = 0;
function statLiveShow(tile) {
  const [id, k] = tile.dataset.live.split(":"), x = statLive[id]?.[k];
  if (x != null) statValue(tile, x);
}
function statLivePill(p) {
  const on = !!statLive[p.dataset.pc];
  p.className = `pill mx-livepill${on ? " on" : ""}`; setText(p, on ? "Live" : "Every minute");
  p.title = on ? "The values update every few seconds" : "No live values from this pc (its companion is older, or it is away): the values are from its last minute report";
}
async function statLiveTick() {
  clearTimeout(statLiveTimer);
  const ids = [...new Set([...document.querySelectorAll("#machines [data-live]")].map((b) => b.dataset.live.split(":")[0]))];
  if ($("tab-machines").hidden) { statLiveTimer = 0; return; } // loadMachines starts it again
  if (ids.length && !document.hidden) {
    try { statLive = (await api(`/ui/api/machines/live?ids=${ids.join(",")}`)).live || {}; } catch { statLive = {}; }
    document.querySelectorAll("#machines [data-live]").forEach(statLiveShow);
    document.querySelectorAll("#machines .mx-livepill").forEach(statLivePill);
  }
  statLiveTimer = setTimeout(statLiveTick, 3000);
}
function statZoom() {
  const zoom = el("div", "mx-zoom");
  for (const [w, label] of Object.entries(STAT_ZOOMS)) {
    const b = el("button", `btn small${w === STAT_ZOOM ? " on" : " quiet"}`, label);
    b.type = "button"; b.setAttribute("aria-pressed", String(w === STAT_ZOOM));
    b.onclick = () => { STAT_ZOOM = w; drawMachines(); };
    zoom.append(b);
  }
  return zoom;
}
// An area chart: a 2px line over a light wash, a gap where the pc was off; hover shows the time and value there.
// A round is "every minute" plus its own work, so now and then a minute holds no sample: a gap of one or two buckets
// between samples is drawn through (the average of its neighbours); a longer one is the pc being off.
const statBridge = (series) => series.map((v, i) => {
  if (v != null) return v;
  let l = i, r = i;
  while (l >= 0 && series[l] == null) l--;
  while (r < series.length && series[r] == null) r++;
  return l >= 0 && r < series.length && r - l - 1 <= 2 ? (series[l] + series[r]) / 2 : null;
});
function sparkline(series, top, step, fmt, label) {
  series = statBridge(series);
  const ns = "http://www.w3.org/2000/svg", w = 240, h = 44, svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`); svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "mx-spark"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label);
  const dx = w / Math.max(1, series.length - 1), y = (v) => (h - 1 - (h - 4) * Math.min(1, v / top)).toFixed(1);
  let line = "", area = "", run = [];
  const flush = () => {
    if (run.length === 1) run.push([run[0][0] + 1, run[0][1]]); // a lone point stays visible
    if (run.length) {
      line += `M${run.map((p) => p.join(",")).join("L")}`;
      area += `M${run[0][0]},${h}L${run.map((p) => p.join(",")).join("L")}L${run[run.length - 1][0]},${h}Z`;
    }
    run = [];
  };
  series.forEach((v, i) => { if (v == null) flush(); else run.push([(i * dx).toFixed(1), y(v)]); });
  flush();
  const path = (d, cls) => { const p = document.createElementNS(ns, "path"); p.setAttribute("d", d); p.setAttribute("class", cls); return p; };
  const cross = path(`M0,0L0,${h}`, "mx-spark-x");
  svg.append(path(area, "mx-spark-area"), path(line, "mx-spark-line"), cross);
  const wrap = el("div", "mx-spark-box"), tip = el("span", "mx-spark-tip");
  svg.onpointermove = (e) => {
    const r = svg.getBoundingClientRect(), i = Math.round((e.clientX - r.left) / r.width * (series.length - 1));
    const at = new Date(Date.now() - (series.length - 1 - i) * step * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    cross.setAttribute("transform", `translate(${(i * dx).toFixed(1)},0)`); wrap.classList.add("hover");
    tip.textContent = `${at} · ${series[i] == null ? "off" : fmt(series[i])}`;
  };
  svg.onpointerleave = () => wrap.classList.remove("hover");
  wrap.append(svg, tip);
  return wrap;
}

// The head of a pc's column: its name, its Knowledge version, what acts on the whole pc, and whether workers may start.
function pcHead(m, here) {
  const c = m.companion, box = el("div", "mx-pc"), name = el("div", "mx-pc-name");
  const dot = el("span", `machine-dot${m.online ? " on" : ""}`); dot.setAttribute("aria-hidden", "true");
  name.append(dot, el("h3", null, m.label));
  if (here && m.id === here) name.append(el("span", "pill this", "This pc"));
  if (m.mine && !m.own) { const p = el("span", "pill", "remote control off"); p.title = "Your pc, but its token does not allow steering it from here (Admin › Tokens)"; name.append(p); }
  else if (!m.own && Object.keys(m.shared || {}).length) { // its owner shared it with you for some projects
    const sh = Object.entries(m.shared), p = el("span", "pill", `shared: ${sh.map(([k, v]) => `${shown(k)} (${v})`).join(", ")}`);
    p.title = "Its owner shared it with you: view shows its chats in that project, use also lets you start your own chats there (Chats)"; name.append(p);
  }
  else if (!m.own) { const p = el("span", "pill", "view only"); p.title = "Someone else's pc, or one not marked as your own device: you see it, you cannot steer it"; name.append(p); }
  const behind = pluginBehind(m), meta = el("div", "mx-pc-meta");
  meta.append(behind ? differs(`Knowledge ${c?.version || "?"}`) : val(`Knowledge ${c?.version || "?"}`));
  if (c?.game) meta.append(" · ", val(c.game));
  box.append(name, meta);
  const note = selfNote(m);
  if (note) box.append(el("p", c.self?.waits || c.self?.later?.length ? "mx-pc-meta" : "machine-error", note));
  const acts = el("div", "mx-pc-acts");
  if (behind && steers(m)) acts.append(machineButton(`Update to ${behind}`, "Update the knowledge plugin of every agent there and its companion (it restarts by itself)", () => machineAction(m, "update_plugin", { target: behind }), updating(m), "btn small primary"));
  // an admin may update the plugin on someone else's pc, nothing else there (the server checks its remote-control switch)
  if (behind && me.admin && !m.own && !m.old && m.online && c) acts.append(machineButton(`Update to ${behind} (admin)`, "Update the knowledge plugin there; its owner's settings stay as they are", () => machineAction(m, "update_plugin", { target: behind }), updating(m), "btn small primary"));
  const old = agentsBehind(m);
  if (old.length && steers(m)) acts.append(machineButton("Update agents", `Update ${old.map((a) => AGENT_NAME[a] || a).join(", ")} with each one's own update command (a running session keeps its version until it restarts)`,
    () => machineAction(m, "update_agents"), pendingAction(m, "update_agents"), "btn small primary"));
  const sharedUse = !m.own && m.online && (c?.folders?.linked || []).some((f) => usesShare(m.shared?.[f.project]));
  if ((steers(m) || sharedUse) && (c.agents || []).length && (c.folders?.linked || []).length) acts.append(machineButton("Chat", "Talk to an agent on this pc (Chats)", async () => { go("#/chats"); if (!ck.machines.length) await ckSummary().catch(() => {}); ckNewChat(m.id); }));
  if (m.mine) acts.append(pcMore(m));
  if (acts.childNodes.length) box.append(acts);
  if (c) box.append(steers(m) ? machineSwitch("Runs workers", c.workersAllowed, pendingAction(m, "workers_allowed"), `Workers on ${m.label}`, (on) => {
    if (on && !confirm(`${WORKERS_WARNING}\n\n${m.label}`)) return false;
    machineAction(m, "workers_allowed", { on }); return true;
  }) : val(c.workersAllowed ? "Runs workers" : "Runs no workers", "mx-pc-meta"));
  if (c?.admin) box.append(adminLine(m, c.admin));
  if (c?.maxAgents != null) { // chat turns and workers at once, all agents together (a companion before the setting reports none)
    const line = el("div", "mx-pc-meta");
    line.title = "Chats and workers running at once on this pc, all agents together";
    line.append(steers(m) ? limitControl(c.maxAgents, pendingAction(m, "max_agents"), `Agents at once on ${m.label}`, (n) => machineAction(m, "max_agents", { n }))
      : val(c.maxAgents ? `At most ${c.maxAgents} agents at once` : "No limit on agents at once"));
    box.append(line);
  }
  return box;
}
// Admin rights for the pc's chats: asked once at the pc itself (UAC, the sudo or admin password), kept until switched off.
const ADMIN_WARNING = "Admin rights for this pc's chats: their agents can then install software, change services and system settings. "
  + "You approve it once on the pc itself (Windows: the UAC prompt; Linux: the sudo password; Mac: the admin password); nobody approving within 2 minutes leaves it off. "
  + "While on, every program you run there can become administrator too, not only the agents. A guest's chat starts as administrator only when shared with \"YOLO and admin\", but it runs as you: one that can run commands can still reach it while this is on.";
function adminLine(m, a) {
  const line = el("div", "mx-pc-meta");
  const words = a.asking ? "Admin: approve on the pc" : a.on ? "Admin rights on" : "Admin rights off";
  if (steers(m)) line.append(machineSwitch(words, a.on, a.asking || pendingAction(m, "admin"), `Admin rights on ${m.label}`, (on) => {
    if (on && prompt(`${ADMIN_WARNING}\n\nType ADMIN to ask for it on ${m.label}.`) !== "ADMIN") return false;
    machineAction(m, "admin", on ? { on: true, confirm: "ADMIN" } : { on: false }); return true;
  }));
  else line.append(val(words));
  if (a.last && !a.on) line.append(" ", el("small", "muted", a.last.startsWith("terminal: ") ? `Run in a terminal on that pc: ${a.last.slice(10)}` : `Last ask: ${a.last}`));
  return line;
}
// The rarer actions on a pc, behind a More button: update its agents, what was asked of it, forget it.
function pcMore(m) {
  const menu = el("details", "menu machine-more"), sum = el("summary", "btn small quiet", "More"), list = el("div", "menu-list");
  sum.setAttribute("aria-label", `More for ${m.label}`);
  const item = (text, fn) => list.append(actionButton(text, () => { menu.open = false; fn(); }, ""));
  if (steers(m)) item("Update agents", () => machineAction(m, "update_agents"));
  item("Activity…", () => pcActivity(m));
  if (!m.old) item("Share…", () => pcShare(m));
  item("Forget this pc…", () => forgetPc(m));
  menu.append(sum, list);
  return menu;
}
async function forgetPc(m) {
  if (!confirm(`Forget ${m.label}? It leaves this list and comes back when it reports again; its actions, history and chats on the server go.`)) return;
  try { await api("/ui/api/machines/forget", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: m.id }) }); } catch (e) { toast(e.message); }
  loadMachines();
}
// Share this pc per project with its other members: View shows them its chats in that project, Use also lets them start
// chats there (never YOLO, every tool call asks them, on your agent subscription). Off takes it back at once.
const SHARE_WORDS = { "": "Off", view: "View chats", use: "View and use", yolo: "Use with YOLO", admin: "YOLO and admin" };
const usesShare = (x) => x === "use" || x === "yolo" || x === "admin";
async function pcShare(m) {
  const d = el("dialog", "mx-dialog"), h = el("h3", null, `Share ${m.label}`), body = el("div");
  const close = el("button", "btn", "Close"); close.type = "button"; close.onclick = () => d.close();
  d.append(h, el("p", "muted", "Per project linked on this pc, with the project's other members. View: they see this pc's chats in that project. "
    + "Use: they can also start chats here in that project; each tool call asks them first. Use with YOLO: their chats follow this pc's YOLO switch, as yours do. "
    + "YOLO and admin: also this pc's admin rights when you switched them on (on a Mac every chat of yours there can use sudo anyway). "
    + "Their chats run as you on this pc, on your agent subscription, in a worktree of their own; you see those chats and can stop them. Off takes it back at once."), body, close);
  d.onclose = () => { d.remove(); loadMachines(); };
  document.body.append(d); d.showModal();
  let res;
  try { res = await api(`/ui/api/machine/share?machine=${encodeURIComponent(m.id)}`); } catch (e) { body.append(el("p", "machine-error", e.message)); return; }
  const projects = (res.projects || []).filter((p) => p.members.length);
  if (!projects.length) { body.append(el("p", "muted", "No project linked on this pc has other members: an admin adds people to a project on the admin page.")); return; }
  for (const p of projects) {
    const sec = el("section"), ul = el("ul", "machine-list");
    sec.append(el("h4", null, shown(p.project)));
    for (const u of p.members) {
      const li = el("li"), sel = el("select", "small");
      sel.setAttribute("aria-label", `${u.user} on ${shown(p.project)}`);
      for (const [v, t] of Object.entries(SHARE_WORDS)) sel.append(new Option(t, v));
      sel.value = u.mode || "";
      sel.onchange = async () => {
        if (sel.value === "use" && !confirm(`Let ${u.user} run agents on ${m.label} in ${shown(p.project)}?\n\nTheir agents run as you on this pc (files, network, your agent subscription). Every tool call asks them first, and they answer it themselves: give this only to someone you would give a shell.`)) { sel.value = u.mode || ""; return; }
        if (sel.value === "admin" && prompt(`${u.user}'s chats on ${m.label} (${shown(p.project)}) then run with YOLO and, when this pc's admin rights are on, as administrator: they can change anything on this pc. Type ADMIN to allow it.`) !== "ADMIN") { sel.value = u.mode || ""; return; }
        if (sel.value === "yolo" && prompt(`${u.user}'s chats on ${m.label} (${shown(p.project)}) then run with this pc's YOLO switch: no tool call asked, as you on this pc, on your agent subscription; risky actions are flagged, never stopped. Type YOLO to allow it.`) !== "YOLO") { sel.value = u.mode || ""; return; }
        try {
          await api("/ui/api/machine/share", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: m.id, project: p.project, user: u.user, mode: sel.value }) });
          u.mode = sel.value; toast(`${u.user}: ${SHARE_WORDS[sel.value]} on ${m.label} (${shown(p.project)})`, false);
        } catch (e) { sel.value = u.mode || ""; toast(e.message); }
      };
      li.append(el("span", null, u.user), " ", sel); ul.append(li);
    }
    sec.append(ul); body.append(sec);
  }
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
function versionCell(m, a) {
  const ag = m.agents?.[a];
  if (!ag) return null;
  if (!ag.cli) return val("Not reported", "muted"); // a companion before this report, or an agent that does not say
  const to = agentBehind(m, a);
  if (to) { const v = differs(`${ag.cli} → ${to}`); v.title = `${ag.cli} is installed; ${to} is the newest${ag.channel === "stable" ? " on the stable channel" : ""}`; return v; }
  return val(ag.cli, agentNewest(m, a) ? "ok" : "");
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
  const running = (ag.workers || []).filter((w) => w.status === "running").length;
  return val(running ? `${running} running` : "none running", "muted");
}
// Claude accounts on one pc: the pc's own sign-in and each extra one (accounts.py), signed in by browser, long-lived
// token or API key; what is left of each; a main one per person (their new chats there start on it, on their own pc
// its workers run under it). Each person sees only their own accounts; the pc's owner sees who added the others.
const usesPc = (m) => Object.values(m.shared || {}).some((x) => ["use", "yolo", "admin"].includes(x));
const accountSteer = (m) => !!m.online && !!m.companion && !m.old && (steersAgent(m, "claude") || !m.own && usesPc(m));
async function accountMain(m, account) {
  try { await api("/ui/api/machines/claude-main", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: m.id, account }) }); }
  catch (e) { toast(e.message); }
  await loadMachines();
}
function accountsCell(m) {
  const ag = m.agents?.claude;
  if (!ag) return null;
  if (!ag.accounts) return val(steersAgent(m, "claude") ? "Update the plugin to add accounts" : "—", "muted");
  const box = el("div", "mx-accounts"), steer = accountSteer(m), main = m.claudeMain || "";
  for (const x of ag.accounts) {
    const line = el("div", "mx-account"), mine = x.mine !== false;
    // the pc's own on a token or key: chats and workers use that, the terminal keeps the sign-in shown beside it
    const kindLabel = x.kind === "token" ? (x.preview ? `long-lived token (${x.preview})` : "long-lived token")
      : x.kind === "apikey" ? (x.preview ? `API key (${x.preview})` : "API key")
      : x.kind === "login" ? "browser sign-in" : "";
    const who = x.pc ? "this pc's own sign-in"
      : !x.id && x.kind ? `${kindLabel} for chats and workers${x.email ? ` (terminal: ${x.email})` : ""}`
      : x.email ? `${x.email}${kindLabel && x.kind !== "login" ? ` (${kindLabel})` : ""}`
      : kindLabel || "not signed in";
    line.append(el("b", null, x.id || "own"), " ", val(who, x.pc || x.email || x.kind ? "" : "mx-diff"));
    if (x.plan) line.append(" ", val(x.plan, "muted"));
    if (mine && x.id === main) line.append(el("span", "pill", "main"));
    if (!mine) line.append(" ", val(`added by ${x.by || "?"}`, "muted"));
    const timing = accountTiming(x);
    if (timing) line.append(" ", val(timing, "muted"));
    if (!x.pc && (usageWindows(x).length || x.kind === "apikey")) line.append(accountBars(x));
    if (x.error) line.append(el("p", "machine-error", x.error));
    const acts = [];
    if (steer && mine && x.id !== main) acts.push(actionButton("Make main", () => accountMain(m, x.id), "btn small quiet"));
    const busy = pendingAction(m, "claude_login", (y) => y.account === x.id) || pendingAction(m, "claude_code", (y) => y.account === x.id);
    if (steer && mine && x.login) {
      const a = el("a", "btn small primary", "Open sign-in"); a.href = x.login; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.title = "Sign in with the account in a new tab; claude.com then shows a code to paste here";

      const inp = el("input"); inp.placeholder = "code or token"; inp.autocomplete = "off";
      inp.className = "input small"; inp.style.minWidth = "170px"; inp.setAttribute("aria-label", "Code or token from sign-in");

      const confirmBtn = machineButton("Confirm", "Save this code or token", () => {
        const val = inp.value.trim();
        if (!val) return;
        if (/^sk-ant-oat\d\d-[\w-]{20,400}$/.test(val)) return machineAction(m, "claude_token", { account: x.id, token: val });
        if (/^sk-ant-api\d\d-[\w-]{20,400}$/.test(val)) return machineAction(m, "claude_key", { account: x.id, key: val });
        return machineAction(m, "claude_code", { account: x.id, code: val });
      }, busy);

      inp.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); confirmBtn.click(); } };

      const restartBtn = machineButton(x.setup ? "Switch to long-lived token…" : "Sign in again", "Restart the sign-in procedure",
        () => machineAction(m, "claude_login", { account: x.id, setup: !!x.setup }), busy, "btn small quiet");

      acts.push(a, inp, confirmBtn, restartBtn);
    } else if (steer && (mine || (x.id === "" && m.own))) {
      const isLogin = !x.kind || x.kind === "login";
      const isSigned = x.email || x.kind;
      if (isLogin && isSigned) {
        acts.push(machineButton("Switch to long-lived token…", `Start long-lived token setup for ${x.id || "this pc's own"} (valid 1 year)`,
          () => machineAction(m, "claude_login", { account: x.id, setup: true }),
          busy || pendingAction(m, "claude_token", (y) => y.account === x.id) || pendingAction(m, "claude_key", (y) => y.account === x.id)));
        acts.push(machineButton("Change sign-in…", `Change sign-in: token, API key or new browser sign-in`,
          () => accountAdd(m, x.id), busy, "btn small quiet"));
      } else if (x.kind === "token") {
        acts.push(machineButton("Switch to browser sign-in…", `Switch ${x.id || "this pc's own"} back to standard browser sign-in`,
          () => machineAction(m, "claude_login", { account: x.id, setup: false }),
          busy || pendingAction(m, "claude_token", (y) => y.account === x.id) || pendingAction(m, "claude_key", (y) => y.account === x.id)));
        acts.push(machineButton("Change sign-in…", `Change sign-in: new token or API key`,
          () => accountAdd(m, x.id), busy, "btn small quiet"));
      } else if (!isSigned) {
        acts.push(machineButton("Sign in…", `Sign ${x.id || "this pc's own"} in: browser, long-lived token or API key`,
          () => accountAdd(m, x.id), busy));
      } else {
        acts.push(machineButton("Change sign-in…", `Change sign-in for ${x.id || "this pc's own"}`,
          () => accountAdd(m, x.id), busy));
      }

      if (x.id === "" && x.email) {
        acts.push(machineButton("Sign out…", "Sign this pc's own Claude out", () => {
          const word = prompt(`Sign ${m.label}'s own Claude out? Its terminal sessions, workers and chats on it stop until it signs in again (Sign in here). Type SIGN OUT to go on.`);
          return word === "SIGN OUT" ? machineAction(m, "claude_logout", { account: "", confirm: word }) : null;
        }, pendingAction(m, "claude_logout"), "btn small quiet"));
      }
    }
    if (steer && mine && x.kind === "apikey") acts.push(machineButton("Budget…", "A monthly budget: the page shows what is left of it", () => {
      const v = prompt(`Monthly budget for ${x.id || "this pc's own"} in dollars (0: none)`, x.budget || "");
      return v === null || !Number.isFinite(Number(v)) || Number(v) < 0 ? null : machineAction(m, "claude_budget", { account: x.id, usd: Number(v) });
    }, pendingAction(m, "claude_budget", (y) => y.account === x.id), "btn small quiet"));
    if (steer && x.id && (mine || m.own)) acts.push(machineButton("Remove", `Sign ${x.id} out on ${m.label} and remove it`, () => confirm(`Remove the Claude account ${x.id} from ${m.label}? Chats that use it go back to the pc's own sign-in.`) ? machineAction(m, "claude_remove", { account: x.id }) : null,
      pendingAction(m, "claude_remove", (y) => y.account === x.id), "btn small quiet"));
    if (acts.length) { const row = el("div", "mx-pop-acts"); row.append(...acts); line.append(row); }
    box.append(line);
  }
  if (steer) {
    const addRow = el("div", "mx-accounts-add");
    addRow.append(machineButton("+ Add account…", "Another Claude account on this pc, for your chats to pick", () => accountAdd(m), false, "btn small quiet"));
    box.append(addRow);
  }
  return box;
}
// A small form in a dialog (it is not redrawn with the list, so what is typed stays); done once closed or sent.
function accountForm(title, intro, fields, go, send) {
  let sent = false, done;
  const closed = new Promise((r) => (done = r));
  const d = el("dialog", "mx-dialog"), form = el("form", "mx-ask-form"), acts = el("div", "mx-pop-acts");
  const ok = el("button", "btn primary", go), cancel = el("button", "btn quiet", "Cancel");
  ok.type = "submit"; cancel.type = "button"; cancel.onclick = () => d.close(); acts.append(cancel, ok);
  form.append(el("h3", null, title), ...intro.map((t) => (typeof t === "string" ? el("p", "muted", t) : t)));
  for (const [label, input] of fields) { const l = el("label", null, label); l.append(input); form.append(l); }
  form.append(acts);
  form.onsubmit = (e) => { e.preventDefault(); sent = true; d.close(); Promise.resolve(send()).finally(done); };
  d.append(form); d.onclose = () => { d.remove(); if (!sent) done(); };
  document.body.append(d); d.showModal(); fields[0]?.[1].focus();
  return closed;
}
const ACCOUNT_KIND = { token: "long-lived token", apikey: "API key", login: "browser sign-in" };
// How an account signs in: [value, button, what it is, what to paste (none: the browser), its form]
const ACCOUNT_WAYS = [
  ["login", "Browser", "Sign in with the account in your browser; claude.com then shows a code to paste here. Standard subscription sign-in.", "", ""],
  ["token", "Long-lived token", "Run claude setup-token in a terminal on any pc and paste the token it prints. It holds for a year and uses the account's subscription.", "sk-ant-oat01-…", "sk-ant-oat\\d\\d-[\\w\\-]{20,400}"],
  ["apikey", "API key", "An API key from the Anthropic Console (console.anthropic.com > API keys). Billed per use, not the subscription.", "sk-ant-api03-…", "sk-ant-api\\d\\d-[\\w\\-]{20,400}"]];
function accountAdd(m, account = null) { // null: a new account; "": the pc's own; a name: that account
  const name = el("input"), secret = el("input"), ways = el("div", "seg"), about = el("p", "muted");
  name.required = true; name.pattern = "[a-z0-9][a-z0-9\\-]{0,30}"; name.placeholder = "e.g. work"; name.title = "a-z, 0-9 and -";
  if (account !== null) { name.value = account; name.readOnly = true; }
  secret.type = "password"; secret.autocomplete = "off";
  ways.setAttribute("role", "group"); ways.setAttribute("aria-label", "How it signs in");
  let way = ACCOUNT_WAYS[0];
  const pick = (w) => {
    way = w; about.textContent = `${w[2]} A token or key goes only to this pc; the server does not keep it.`;
    for (const b of ways.children) b.setAttribute("aria-pressed", String(b.dataset.way === w[0]));
    const lab = secret.closest("label"); if (lab) { lab.hidden = !w[3]; lab.firstChild.textContent = w[1]; }
    secret.required = !!w[3]; secret.placeholder = w[3]; secret.pattern = w[4] || ".*"; secret.value = "";
  };
  for (const w of ACCOUNT_WAYS) { const b = el("button", null, w[1]); b.type = "button"; b.dataset.way = w[0]; b.onclick = () => pick(w); ways.append(b); }
  const intro = account === null ? ["Chats on this pc can then run under it (the model chip). Settings, plugins and skills stay the pc's own."]
    : account === "" ? ["Browser signs ~/.claude itself in again (the terminal too). A long-lived token or API key runs this pc's chats and workers on it instead; the terminal keeps its own sign-in. Browser again drops the token or key."] : [];
  const shown = accountForm(account === "" ? `This pc's own Claude on ${m.label}` : account ? `Sign ${account} in on ${m.label}` : `Add a Claude account on ${m.label}`,
    [...intro, ways, about], [...(account === null ? [["Name", name]] : []), ["Token", secret]], account === null ? "Add" : "Sign in", () => (way[0] === "login"
      ? machineAction(m, "claude_login", { account: name.value, setup: false })
      : machineAction(m, way[0] === "token" ? "claude_token" : "claude_key", { account: name.value, [way[0] === "token" ? "token" : "key"]: secret.value.trim() })));
  pick(way);
  return shown;
}
function accountCode(m, account) {
  const code = el("input");
  code.required = true; code.autocomplete = "off"; code.placeholder = "the code from the sign-in page";
  return accountForm(`Sign ${account} in on ${m.label}`, ["Paste the code claude.com showed after you signed in, or an sk-ant-oat... / sk-ant-api... secret."], [["Code", code]], "Sign in",
    () => {
      const val = code.value.trim();
      if (/^sk-ant-oat\d\d-[\w-]{20,400}$/.test(val)) return machineAction(m, "claude_token", { account, token: val });
      if (/^sk-ant-api\d\d-[\w-]{20,400}$/.test(val)) return machineAction(m, "claude_key", { account, key: val });
      return machineAction(m, "claude_code", { account, code: val });
    });
}
// A limit: a switch (off: no limit, sent as 0) and, when on, a number from 1 up (how many is the owner's choice).
function limitControl(most, busy, aria, send) {
  const box = el("span", "mx-limit"), num = el("input", "small");
  num.type = "number"; num.min = 1; num.value = most || 4; num.hidden = !most; num.disabled = busy;
  num.setAttribute("aria-label", aria);
  num.onchange = () => { const n = Math.round(Number(num.value)); if (n >= 1) send(n); else num.value = most || 4; };
  const sw = machineSwitch(most ? "Limit" : "No limit", !!most, busy, `Limit for ${aria}`, (on) => { send(on ? Number(num.value) || 4 : 0); return true; });
  box.append(sw, num);
  return box;
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
  const stats = ["cpu", "gpu", "ram", "vram"].filter((k) => pcs.some((m) => m.companion?.stats?.[k]));
  if (stats.length) {
    const lab = el("div", "mx-load-lab"); lab.append(el("span", null, "Load"), statZoom());
    grid.append(mxRow("mx-sec", lab, pcs.map((m) => {
      if (!m.companion?.stats?.cpu) return null;
      const p = el("span"); p.dataset.pc = m.id; statLivePill(p); return p;
    })));
  }
  for (const k of stats) grid.append(row("", STAT_NAME[k], pcs.map((m) => statCell(m, k) || val("—", "muted"))));
  const agents = AGENT_ORDER.filter((a) => pcs.some((m) => m.agents?.[a]));
  for (const a of agents) {
    const name = el("div", "mx-agent-name"), mark = el("span", "mx-mark", AGENT_MARK[a] || a[0].toUpperCase());
    mark.setAttribute("aria-hidden", "true");
    name.append(mark, el("span", null, AGENT_NAME[a] || a));
    const guest = pcs.every((m) => !m.own && !(m.tokens || []).length); // a pc shared with you: only your own accounts there
    if (!mine && !guest) name.append(el("small", "muted", "their account"));
    const head = mxRow("mx-agent", name, []);
    if (!guest) head.insertBefore(meters(pcs, a), head.lastChild);
    grid.append(head);
    if (guest) { grid.append(row("", "Accounts", pcs.map((m) => accountsCell(m)))); continue; }
    grid.append(row("", "Version", pcs.map((m) => versionCell(m, a))));
    grid.append(row("", "Plugin", pcs.map((m) => pluginCell(m, a))));
    grid.append(row("", "Hooks", pcs.map((m) => hooksCell(m, a))));
    grid.append(row("", "YOLO", pcs.map((m) => yoloCell(m, a))));
    grid.append(row("", "Workers", pcs.map((m) => workersCell(m, a))));
    if (a === "claude") grid.append(row("", "Accounts", pcs.map((m) => accountsCell(m))));
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
  if (mine) { // what is not in the list yet: an admin adds it, anyone else asks
    const ask = el("div", "mx-ask");
    if (me.admin) { const a = el("a", "btn small", "Add a tool or skill"); a.href = "/ui/admin#external"; ask.append(a, val("in Admin › Tools & skills: it reaches every pc.", "muted")); }
    else ask.append(actionButton("Ask for a tool or skill…", () => askTool(), "btn small"), val("An admin decides; once it is added you install it here.", "muted"));
    grid.append(mxRow("mx-askrow", ids.length ? null : "Tools", [ask], null));
    grid.lastChild.children[1].style.gridColumn = "2 / -1";
  }
  return wrap;
}
// Ask the admins for a tool or skill, and see what became of earlier requests.
async function askTool() {
  const d = el("dialog", "mx-dialog"), form = el("form", "mx-ask-form"), what = el("input"), why = el("textarea"), list = el("ul", "mx-ask-list");
  what.required = true; what.minLength = 3; what.maxLength = 300; what.placeholder = "Name or link, e.g. https://github.com/owner/tool";
  why.maxLength = 300; why.rows = 3; why.placeholder = "What do you want to use it for?";
  const lab = (text, field) => { const l = el("label", null, text); l.append(field); return l; };
  const send = el("button", "btn primary", "Send request"), cancel = el("button", "btn quiet", "Cancel"), acts = el("div", "mx-pop-acts");
  send.type = "submit"; cancel.type = "button"; cancel.onclick = () => d.close(); acts.append(cancel, send);
  form.append(el("h3", null, "Ask for a tool or skill"), el("p", "muted", "An admin decides whether it is added. Once it is, you install it per pc on this page."),
    lab("Name or link", what), lab("What do you want to use it for?", why), acts);
  const STATE = { waiting: "Waiting for an admin", drafting: "Being prepared", added: "Added: install it on Machines", declined: "Declined" };
  const fill = async () => {
    try {
      const res = await api("/ui/api/tools/requests");
      list.replaceChildren(...res.requests.map((r) => { const li = el("li"); li.append(el("span", null, r.what), el("span", "muted", STATE[r.status] + (r.status === "declined" && r.reason ? `: "${r.reason}"` : ""))); return li; }));
    } catch (e) { list.replaceChildren(el("li", "error", e.message)); }
  };
  form.onsubmit = async (e) => {
    e.preventDefault(); send.disabled = true;
    try { await api("/ui/api/tools/request", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ what: what.value, why: why.value }) }); toast("Sent: an admin decides", false); form.reset(); await fill(); }
    catch (err) { toast(err.message); } finally { send.disabled = false; }
  };
  d.append(form, el("h4", null, "Your requests"), list);
  d.onclose = () => d.remove();
  document.body.append(d); d.showModal(); what.focus();
  fill();
}

// What needs a person on your own pcs, one line each, the action right there.
function machinesStatus(res, own, online) {
  const box = el("section", "mx-status"), text = el("div"), items = [];
  const lead = el("p", "mx-lead", own.length ? `${online.length} of your ${own.length} pc${own.length === 1 ? " is" : "s are"} online.` : "None of your pcs reported yet.");
  const behind = online.filter((m) => steers(m) && pluginBehind(m));
  for (const m of behind) {
    const note = selfNote(m);
    if (note && !m.companion.self?.waits) { items.push([`${m.label}: ${note} `, actionButton("see Activity", () => pcActivity(m), "btn small quiet")]); continue; }
    items.push([versionNewer(newestPlugin(), m.companion.version || "0") ? `${m.label} runs an older Knowledge (${m.companion.version}).`
    : `The plugin of ${Object.entries(m.agents || {}).filter(([, ag]) => ag.plugin && versionNewer(newestPlugin(), ag.plugin)).map(([a]) => AGENT_NAME[a] || a).join(" and ")} on ${m.label} is older than its Knowledge.`]);
  }
  for (const m of online.filter(steers)) {
    const old = agentsBehind(m);
    if (old.length) items.push([`On ${m.label} an agent can update: ${old.map((a) => `${AGENT_NAME[a] || a} ${m.agents[a].cli} → ${agentBehind(m, a)}`).join(", ")}. `,
      machineButton("Update agents", `Update the agents on ${m.label}`, () => machineAction(m, "update_agents"), pendingAction(m, "update_agents"), "btn small")]);
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
  for (const s of res.stuck || []) items.push([`${s.tasks} task${s.tasks > 1 ? "s" : ""} of ${shown(s.project)} cannot start by itself: none of your pcs has a folder linked to that project.`]);
  text.append(lead);
  if (items.length) { const ul = el("ul", "mx-items"); for (const parts of items) { const li = el("li"); li.append(...parts); ul.append(li); } text.append(ul); }
  else if (online.length) text.append(el("p", "muted", "Everything is up to date."));
  box.append(text);
  if (behind.length > 1 || (behind.length === 1 && online.length > 1)) box.append(machineButton("Update all my pcs", `Update Knowledge on ${behind.map((m) => m.label).join(", ")}`,
    () => Promise.all(behind.map((m) => machineAction(m, "update_plugin", { target: pluginBehind(m) }))), behind.some((m) => pendingAction(m, "update_plugin")), "btn primary"));
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
  for (const s of res.stuck || []) parts.push(el("p", "machine-stuck", `${s.tasks} task${s.tasks > 1 ? "s" : ""} of ${shown(s.project)} cannot start by itself: none of your pcs has a folder linked to that project.`));
  // an admin links and unlinks on anyone's online pc too (the server checks its token allows remote control)
  const theirs = res.machines.filter((m) => !m.mine && !m.old && m.companion), reach = (m) => m.own || (me.admin && !m.mine && m.online && m.remote !== false);
  const lede = el("div", "mx-proj-head mx-link-bar"), hosts = [...own.filter((m) => steers(m)), ...theirs.filter((m) => me.admin && m.online && m.remote !== false)]
    .filter((m) => (m.companion.projects || []).length);
  lede.append(el("p", "muted", "Which folder on which pc belongs to which project. An agent started in a linked folder gets that project's memory and docs, and workers of that project may start there."));
  // one way to link: the dialog asks for the pc, a folder on it, then the project
  if (hosts.length) lede.append(machineButton("Link a folder", "Choose a pc, a folder on it, then its project", () => chooseFolder(hosts.find((m) => m.id === machinesHere) || hosts[0], "", hosts)));
  parts.push(lede);
  // other people's pcs this person sees (an admin: all; a member: the ones shared with them): their links (read-only for
  // a member), so a project linked only on someone else's pc does not look unlinked
  const projects = [...new Set([...own.flatMap((m) => [...(m.companion.projects || []), ...(m.companion.folders?.linked || []).map((f) => f.project)]),
    ...theirs.flatMap((m) => (m.companion.folders?.linked || []).map((f) => f.project))])].sort();
  for (const p of projects) {
    const box = el("section", "mx-proj"), head = el("div", "mx-proj-head");
    head.append(el("h3", null, shown(p)));
    box.append(head);
    const rows = [...own, ...theirs].flatMap((m) => (m.companion.folders?.linked || []).filter((f) => f.project === p).map((f) => [m, f]));
    if (!rows.length) box.append(el("p", "muted mx-empty", `No folder linked yet. Link one to give agents in that folder the memory of ${shown(p)}.`));
    for (const [m, f] of rows) {
      const r = el("div", "mx-frow");
      const who = el("span", null, m.label);
      if (!m.mine) { const pill = el("span", "pill", reach(m) ? "their pc" : "view only"); pill.title = reach(m) ? "Someone else's pc: as admin you may link and unlink its folders" : "Someone else's pc: its owner links and unlinks its folders"; who.append(" ", pill); }
      r.append(who, el("code", null, f.path));
      r.append(m.online && reach(m) ? machineButton("Unlink", "Remove its .knowledge-project file (nothing else)", () => confirm(`Unlink ${f.path} on ${m.label}?`) && machineAction(m, "unlink_folder", { path: f.path }),
        pendingAction(m, "unlink_folder", (x) => x.path === f.path), "btn small quiet") : el("span", "muted", m.online ? "" : "pc offline"));
      box.append(r);
    }
    parts.push(box);
  }
  if (!projects.length) parts.push(emptyState("No pc of yours lists a project yet: once a pc runs the knowledge setup, its projects show here."));
  return parts;
}

// ---------------------------------------------------------------- Files: the folders linked on your pcs, in the browser
// Each click asks the pc (its companion answers at once on its long poll); media stream from the pc with Range
// requests, so a <video> seeks. Every folder and file has its own URL (#/files?m=<pc>&p=<folder>&f=<file>), so Back,
// reload and a link work. A desktop shows the list beside the open file; a phone one page at a time (the Back arrow).
// The box lives across the page's 20 s refreshes: a redraw would stop what plays.
const filesBox = el("div", "fm");
let filesKey = "", filesList = null; // the route drawn; {mid, p, res, el} of the folder listed
const FILE_KIND = { image: /\.(png|jpe?g|gif|webp|avif|bmp|ico)$/i, audio: /\.(mp3|m4a|aac|flac|ogg|oga|opus|wav)$/i, video: /\.(mp4|m4v|webm|ogv|mov|mkv)$/i };
const fileKind = (name) => Object.keys(FILE_KIND).find((k) => FILE_KIND[k].test(name)) || "";
const fileSize = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : n < 1073741824 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1073741824).toFixed(2)} GB`;
const filesApi = (m, body) => api("/ui/api/files", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: m.id, ...body }) });
const FILE_ICON = { // 24 px line icons in the sidebar's style; a folder in the accent, the rest muted
  dir: '<path d="M3.5 6.5h6l2 2h9v10h-17z"/>',
  image: '<rect x="4" y="4.5" width="16" height="15" rx="2"/><circle cx="9" cy="9.5" r="1.5"/><path d="M20 16l-5-5-8.5 8.5"/>',
  audio: '<path d="M9 17.5V6l10-2v11.5"/><circle cx="6.5" cy="17.5" r="2.5"/><circle cx="16.5" cy="15.5" r="2.5"/>',
  video: '<rect x="3.5" y="6" width="12" height="12" rx="2"/><path d="M15.5 10.5l5-3v9l-5-3z"/>',
  file: '<path d="M6 3.5h8l4 4v13H6z"/><path d="M14 3.5v4h4"/>',
};
function fileIcon(kind) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", `i fm-ic ${kind}`); s.setAttribute("aria-hidden", "true");
  s.innerHTML = FILE_ICON[kind];
  return s;
}
const filesUrl = (mid, p, f) => `#/files?${new URLSearchParams({ m: mid, p, ...(f ? { f } : {}) })}`;
const filesMachine = (mid) => (machinesRes?.machines || []).find((m) => m.id === mid) || { id: mid, label: "the pc" };
const baseName = (path, sep) => path.split(sep || (path.includes("\\") ? "\\" : "/")).filter(Boolean).pop() || path;
const filesDirty = () => { const a = filesBox.querySelector("textarea.editor"); return a && a.value !== a.dataset.start; };
function filesGo(url) { if (filesDirty() && !confirm("Leave this file? Your unsaved edits are lost.")) return; go(url); }
// A link that opens in the app (a middle click still opens a new tab).
function filesLink(cls, url, ...kids) {
  const a = el("a", cls); a.href = url; a.append(...kids);
  a.onclick = (e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); filesGo(url); };
  return a;
}
// Where Back goes: from a file to its folder, from a folder to its parent, from a linked folder to the list.
function filesUp() {
  const q = shellRoute.q, mid = q.get("m"), p = q.get("p");
  if (q.get("f")) return filesUrl(mid, p);
  const res = filesList?.mid === mid && filesList.p === p ? filesList.res : null;
  return res?.parent ? filesUrl(mid, res.parent) : "#/files";
}

// Draws what the URL asks for; a refresh of the page with the same URL leaves it alone.
async function filesRoute(q = shellRoute.q) {
  const mid = q.get("m") || "", p = q.get("p") || "", f = q.get("f") || "", key = `${mid}\n${p}\n${f}`;
  if (key === filesKey && filesBox.childNodes.length) return;
  filesKey = key;
  if (!machinesRes) return; // drawn once the pc list is in
  if (!p) return filesRoots();
  const m = filesMachine(mid);
  if (filesList?.mid !== mid || filesList.p !== p) {
    filesBox.replaceChildren(el("p", "empty loading", `Asking ${m.label}…`));
    let res;
    try { res = await filesApi(m, { op: "list", path: p }); } catch (e) { if (filesKey === key) filesFail(m, e); return; }
    if (filesKey !== key) return; // the person went on meanwhile
    filesList = { mid, p, res, el: null };
  }
  filesDraw(m, f);
}
function filesProject() { if (machinesMode === "files" && !$("tab-machines").hidden && !shellRoute.q.get("p")) { filesKey = ""; filesRoute(); } }
// A path that is not there (a link from a chat to a file renamed since, or one only in a chat's own worktree) in words.
const filesWhy = (m, e) => /No such file|cannot find the (file|path)|WinError [23]\]/i.test(e.message) // WinError: any Windows language
  ? `This is not on ${m.label}: it may have been moved or deleted, or exist only on a chat's own branch (worktree) that is not merged yet.` : e.message;
function filesFail(m, e) {
  filesBox.replaceChildren(errorState(`${m.label}: ${filesWhy(m, e)}`, () => { filesKey = ""; filesList = null; filesRoute(); }));
}

// The start: the linked folders, grouped by pc.
function filesRoots() {
  const usable = (m, f) => m.own || ["use", "yolo", "admin"].includes(m.shared?.[f.project]); // a pc shared with you: that project's folders
  const pcs = (machinesRes?.machines || []).filter((m) => m.online && !m.old && m.companion && (steers(m) || m.shared))
    .sort((a, b) => (b.id === machinesHere) - (a.id === machinesHere) || b.own - a.own || a.label.localeCompare(b.label));
  const parts = [el("p", "muted fm-lede", "The folders linked on your pcs, and those of a project someone shared with you on theirs. git's own files, .env files and keys stay hidden.")];
  for (const m of pcs) {
    const folders = (m.companion.folders?.linked || []).filter((f) => usable(m, f) && picked(f.project));
    if (!folders.length) continue;
    const sec = el("section", "fm-group"), h = el("h2", "mx-h", m.label);
    if (m.id === machinesHere) h.append(el("span", "chip", "this pc"));
    if (!m.own) h.append(el("span", "chip", "shared with you"));
    const ul = el("ul", "fm-rows");
    for (const f of folders) {
      const main = el("span", "fm-main");
      main.append(el("span", "fm-name", baseName(f.path)), el("span", "fm-sub", f.path));
      const pill = el("span", "pill topic", shown(f.project));
      const li = el("li"); li.append(filesLink("fm-row", filesUrl(m.id, f.path), fileIcon("dir"), main, pill));
      ul.append(li);
    }
    sec.append(h, ul);
    parts.push(sec);
  }
  if (parts.length === 1) parts.push(emptyState(pcs.length ? `No folder${allMode() ? "" : ` of ${shown(project)}`} is linked on a pc of yours, or one shared with you, that is online. Link one under Machines, Folders.` : "No pc of yours, or shared with you, is online with remote control on."));
  filesList = null;
  filesBox.replaceChildren(...parts);
}

// A folder: its path, then the list; with a file open, the file beside it (desktop) or instead of it (phone).
function filesDraw(m, f) {
  const { res } = filesList;
  if (!filesList.el) filesList.el = filesFolder(m, res);
  for (const r of filesList.el.querySelectorAll(".fm-row[data-path]")) {
    if (r.dataset.path === f) r.setAttribute("aria-current", "true"); else r.removeAttribute("aria-current");
  }
  const split = el("div", "fm-split" + (f ? " has-file" : "") + (fileKind(f) === "audio" ? " audio" : "")); // audio: a phone keeps the list
  split.append(filesList.el);
  if (f) {
    const pane = el("article", "fm-file");
    split.append(pane);
    filesShow(m, res, f, pane);
  }
  filesBox.replaceChildren(split);
}
function filesFolder(m, res) {
  const box = el("section", "fm-folder"), head = el("div", "fm-head"), title = el("div", "fm-title");
  title.append(el("h2", null, baseName(res.path, res.sep)), filesCrumbs(m, res));
  const reload = el("button", "btn small quiet icon", "↻"); reload.type = "button"; reload.title = "Read this folder again"; reload.setAttribute("aria-label", "Reload");
  reload.onclick = () => { filesList = null; filesKey = ""; filesRoute(); };
  head.append(title, el("span", "muted fm-count", `${res.entries.length + res.more} item${res.entries.length + res.more === 1 ? "" : "s"}`), reload);
  const cols = el("div", "fm-cols"); cols.setAttribute("aria-hidden", "true");
  cols.append(el("span"), el("span", null, "Name"), el("span", null, "Modified"), el("span", null, "Size"));
  const ul = el("ul", "fm-rows");
  for (const e of res.entries) {
    const kind = e.dir ? "dir" : fileKind(e.name) || "file", when = ago(e.mtime);
    const main = el("span", "fm-main");
    main.append(el("span", "fm-name", e.name), el("span", "fm-sub", e.dir ? when : `${fileSize(e.size)} · ${when}`));
    const a = filesLink("fm-row" + (e.name.startsWith(".") ? " dot" : ""), e.dir ? filesUrl(m.id, e.path) : filesUrl(m.id, res.path, e.path),
      fileIcon(kind), main, el("span", "fm-date", when), el("span", "fm-size", e.dir ? "" : fileSize(e.size)));
    a.title = e.name;
    if (!e.dir) a.dataset.path = e.path;
    const li = el("li"); li.append(a); ul.append(li);
  }
  box.append(head, cols, ul);
  if (!res.entries.length) box.append(emptyState("This folder is empty."));
  if (res.more) box.append(emptyState(`${res.more} more not shown (a folder shows its first ${res.entries.length}).`));
  return box;
}
// Files › pc: linked folder › … › parent; the folder itself is the title above it.
function filesCrumbs(m, res) {
  const nav = el("nav", "fm-crumbs"); nav.setAttribute("aria-label", "Path");
  const rel = res.path.slice(res.root.length).split(res.sep).filter(Boolean);
  const steps = [[baseName(res.root, res.sep), res.root]];
  let at = res.root;
  for (const part of rel) { at = at.endsWith(res.sep) ? at + part : at + res.sep + part; steps.push([part, at]); }
  steps.pop(); // the folder itself
  const shown = steps.length > 4 ? [steps[0], ["…", steps[steps.length - 3][1], steps.slice(1, -2).map((s) => s[0]).join(res.sep)], ...steps.slice(-2)] : steps;
  nav.append(filesLink("fm-crumb", "#/files", "Files"), el("span", "fm-sep", "›"), el("span", "fm-pc", `${m.label}:`));
  shown.forEach(([name, path, hidden], i) => {
    if (i) nav.append(el("span", "fm-sep", "›"));
    const a = filesLink("fm-crumb", filesUrl(m.id, path), name);
    if (hidden) a.title = hidden;
    nav.append(a);
  });
  return nav;
}

// One file: media play from the pc, everything else opens as text with line numbers, editable unless a secret was masked.
async function filesShow(m, res, path, pane) {
  const e = res.entries.find((x) => x.path === path) || { name: baseName(path, res.sep), path, size: 0, mtime: 0 };
  const kind = fileKind(e.name), head = el("div", "note-head fm-file-head"), tools = el("div", "note-tools");
  head.append(el("h2", null, e.name));
  const meta = el("p", "muted fm-meta", [e.size ? fileSize(e.size) : "", e.mtime ? `changed ${ago(e.mtime)}` : "", m.label].filter(Boolean).join(" · "));
  const close = filesLink("btn small quiet fm-close", filesUrl(m.id, res.path), "Close"); close.title = "Back to the folder";
  pane.append(head, meta, tools);
  if (kind === "audio") { // it plays in the player under the list, so browsing on does not stop it
    filesPlay(m, res, path);
    const open = el("a", "btn small", "Open in a new tab"); open.href = filesRaw(m.id, path); open.target = "_blank"; open.rel = "noopener";
    tools.append(open, close);
    pane.append(el("p", "muted", "Playing in the player below; the next file in this folder follows when it ends."));
    return;
  }
  if (kind) {
    const src = filesRaw(m.id, path);
    const media = el(kind === "image" ? "img" : kind, "fm-media");
    media.src = src;
    if (kind === "image") media.alt = e.name;
    else { // opened by a click or the one before it ended: it plays at once, and the next video follows
      media.controls = true; media.preload = "metadata"; media.setAttribute("playsinline", "");
      if (fp.audio.src) filesPlayerClose(); // one thing plays at a time
      media.play().catch(() => {}); // a reload has no click: the browser refuses, the controls stay
      media.onended = () => { const n = filesSibling(res, path, 1); if (n && shellRoute.q.get("f") === path) go(filesUrl(m.id, res.path, n), true); };
    }
    const stage = el("div", `fm-stage ${kind}`), note = el("p", "muted");
    media.onerror = () => { note.textContent = kind === "image" ? "This image could not be shown." : "This browser cannot play this file (its format or codec: MKV, HEVC and AC3 often fail), or the pc stopped answering."; };
    const open = el("a", "btn small", "Open in a new tab"); open.href = src; open.target = "_blank"; open.rel = "noopener";
    tools.append(open, close);
    stage.append(media);
    pane.append(stage, note);
    return;
  }
  const wait = el("p", "empty loading", `Asking ${m.label}…`);
  tools.append(close);
  pane.append(wait);
  let txt;
  try { txt = await filesApi(m, { op: "read", path }); } catch (err) { return wait.replaceWith(errorState(filesWhy(m, err), () => { pane.replaceChildren(); filesShow(m, res, path, pane); })); }
  if (!pane.isConnected) return;
  const code = filesCode(txt.text);
  wait.replaceWith(code);
  if (/\.html?$/i.test(e.name)) { // a page: its preview first, the code one click away
    const frame = el("iframe", "fm-preview"), flip = el("button", "btn small", "Code"), again = el("button", "btn small quiet icon", "↻");
    const src = `/ui/api/files/preview?${new URLSearchParams({ machine: m.id, path })}`;
    frame.setAttribute("sandbox", "allow-scripts allow-modals"); // opaque origin: no cookies or API of this site (the server's CSP sandboxes it too)
    frame.title = `Preview of ${e.name}`; frame.src = src;
    flip.type = "button"; flip.title = "Switch between the page and its code";
    flip.onclick = () => { code.hidden = !code.hidden; frame.hidden = !frame.hidden; flip.textContent = frame.hidden ? "Preview" : "Code"; };
    again.type = "button"; again.title = "Draw the page again (after an edit on the pc)"; again.setAttribute("aria-label", "Reload the preview");
    again.onclick = () => { frame.src = "about:blank"; setTimeout(() => { frame.src = src; }); };
    const open = el("a", "btn small", "Open in a new tab"); open.href = src; open.target = "_blank"; open.rel = "noopener";
    code.hidden = true; code.after(frame); tools.prepend(flip, again, open);
  }
  if (txt.masked) pane.insertBefore(el("p", "muted", "A known secret is masked here, so this file is read-only: edit it on the pc itself."), code);
  else {
    const edit = el("button", "btn small", "Edit"); edit.type = "button";
    edit.onclick = () => filesEdit(m, txt, tools, code);
    tools.prepend(edit);
  }
}
// The audio player: one <audio> under the folder list that keeps playing while you browse (on a phone the list stays
// in view and this is its mini player); when a track ends the next audio file of its folder plays. It lives in the
// Files view only: leaving it takes the bar out of the page, which pauses it.
const filesRaw = (mid, path) => `/ui/api/files/raw?${new URLSearchParams({ machine: mid, path })}`;
const filesSibling = (res, path, by) => { // the next (1) or previous (-1) file of the same kind in the folder listed
  const kind = fileKind(path), list = res.entries.filter((x) => !x.dir && fileKind(x.name) === kind).map((x) => x.path);
  return list[list.indexOf(path) + by] || "";
};
const fp = { bar: el("div", "fm-player"), audio: el("audio"), name: el("a", "fm-pname"), prev: el("button", "btn small quiet icon", "⏮"),
  next: el("button", "btn small quiet icon", "⏭"), close: el("button", "btn small quiet icon", "×"), track: null }; // track: {m, res, path}
fp.bar.hidden = true; fp.audio.controls = true; fp.audio.preload = "metadata";
for (const [b, label] of [[fp.prev, "Previous file"], [fp.next, "Next file"], [fp.close, "Stop and close the player"]]) { b.type = "button"; b.title = label; b.setAttribute("aria-label", label); }
fp.bar.setAttribute("role", "region"); fp.bar.setAttribute("aria-label", "Player");
fp.bar.append(fp.name, fp.prev, fp.next, fp.close, fp.audio);
fp.name.onclick = (e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return; e.preventDefault(); filesGo(fp.name.getAttribute("href")); };
function filesPlay(m, res, path) {
  const t = fp.track, same = t && t.m.id === m.id && t.path === path;
  fp.track = { m, res, path };
  fp.name.textContent = baseName(path, res.sep); fp.name.href = filesUrl(m.id, res.path, path); fp.name.title = path;
  fp.prev.disabled = !filesSibling(res, path, -1); fp.next.disabled = !filesSibling(res, path, 1);
  fp.bar.hidden = false;
  if (!fp.bar.isConnected) filesBox.after(fp.bar);
  if (same) return; // the same file drawn again (a refresh): it plays on
  fp.audio.src = filesRaw(m.id, path);
  fp.audio.play().catch(() => {}); // a reload has no click: the browser refuses, the play button stays
}
// To the next or previous track: the list marks it when it shows the track that played, else it only plays.
function filesStep(by) {
  const t = fp.track, n = t && filesSibling(t.res, t.path, by);
  if (!n) return;
  if (shellRoute.view === "files" && shellRoute.q.get("f") === t.path) go(filesUrl(t.m.id, t.res.path, n), true);
  else filesPlay(t.m, t.res, n);
}
function filesPlayerClose() { fp.audio.pause(); fp.audio.removeAttribute("src"); fp.audio.load(); fp.track = null; fp.bar.hidden = true; }
fp.prev.onclick = () => filesStep(-1);
fp.next.onclick = () => filesStep(1);
fp.close.onclick = filesPlayerClose;
fp.audio.onended = () => filesStep(1);

// Text with a line-number gutter; long lines scroll sideways, as in a code view, so the numbers stay beside their lines.
function filesCode(text) {
  const box = el("div", "fm-code"), lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const gutter = el("pre", "fm-gutter", lines.map((_, i) => i + 1).join("\n")); gutter.setAttribute("aria-hidden", "true");
  box.append(gutter, el("pre", "fm-lines", lines.join("\n")));
  return box;
}
function filesEdit(m, txt, tools, code) {
  const area = el("textarea", "editor fm-editor"); area.value = txt.text; area.spellcheck = false; area.setAttribute("aria-label", "Edit the file");
  area.dataset.start = area.value; // the textarea's own line ends (\n): a CRLF file compares equal until edited
  const save = el("button", "btn small primary", "Save"); save.type = "button"; save.disabled = true;
  const cancel = el("button", "btn small quiet", "Cancel"); cancel.type = "button";
  area.oninput = () => { save.disabled = area.value === area.dataset.start; };
  const done = () => { filesKey = ""; filesRoute(); }; // read the file again
  cancel.onclick = () => { if (!filesDirty() || confirm("Discard your unsaved changes?")) { area.dataset.start = area.value; done(); } };
  save.onclick = async () => {
    save.disabled = true;
    try {
      await filesApi(m, { op: "write", path: txt.path, text: area.value, base: { mtime: txt.mtime, size: txt.size } });
      area.dataset.start = area.value;
      toast(`Saved on ${m.label} (the old version is in its backups folder, under files)`, false);
      if (filesList) filesList.el = null; // its size and date changed
      filesList = null; done();
    } catch (err) { toast(err.message); save.disabled = false; }
  };
  for (const b of tools.querySelectorAll("button")) b.remove();
  tools.prepend(save, cancel);
  code.parentNode.querySelector(".fm-preview")?.remove();
  code.replaceWith(area);
  area.focus();
}

// ---------------------------------------------------------------- the page
let machinesTimer = 0, machinesRes = null, machinesHere = "";
const MACHINE_REFRESH = 20000, MACHINE_REFRESH_UPDATING = 5000; // a pc reports about every 10 s; an update shows sooner
// Redraws while the view is open; never under a person's hands (a field being typed in, an open menu, panel or dialog).
function machinesBusy() {
  const box = $("machines"), a = document.activeElement;
  const typing = box.contains(a) && (/^(SELECT|TEXTAREA)$/.test(a.tagName) || (a.tagName === "INPUT" && !/^(checkbox|radio)$/.test(a.type)));
  return typing || box.querySelector(".menu[open]") || document.querySelector("dialog[open]") || selecting(box);
}
function machinesSoon() {
  clearTimeout(machinesTimer);
  machinesTimer = setTimeout(() => {
    if ($("tab-machines").hidden) return;
    if (document.hidden || machinesBusy()) return machinesSoon();
    loadMachines().catch(() => machinesSoon());
  }, (machinesCache || []).some(updating) ? MACHINE_REFRESH_UPDATING : MACHINE_REFRESH);
}
async function loadMachines() {
  const res = await api("/ui/api/machines");
  machinesCache = Array.isArray(res) ? res : res.machines || [];
  machinesRes = { ...res, machines: machinesCache };
  if (machinesBusy() && $("machines").childNodes.length) return machinesSoon(); // machineAction still reads machinesCache
  await markHere();
  drawMachines();
  machinesSoon();
  if (!statLiveTimer) statLiveTick();
  // never before the first draw: without a companion, a refused 127.0.0.1 takes ~2 s on Windows
  if (!localMachine) probeCompanion().then(async (ok) => { if (ok && !machinesBusy()) { await markHere(); drawMachines(); } });
}
async function markHere() { // which row is the pc this page runs on (its companion's machine hash)
  const sha = async (t) => [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(t)))].map((b) => b.toString(16).padStart(2, "0")).join("");
  machinesHere = localMachine && crypto.subtle ? (await Promise.all(machinesCache.map(async (m) => (await sha(m.id)) === localMachine ? m.id : ""))).find(Boolean) || "" : "";
}
function drawMachines() {
  const res = machinesRes, here = machinesHere, box = $("machines"), parts = [];
  if (!res) return;
  if (machinesMode === "files") { // drawn from its URL; a refresh leaves an open folder and what plays alone
    if (!filesBox.isConnected) box.replaceChildren(filesBox, ...(fp.track ? [fp.bar] : []));
    return filesRoute();
  }
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
      if (behind.length > 1) h.append(machineButton("Update all their pcs (admin)", `Update Knowledge on ${behind.map((m) => m.label).join(", ")}`, () => Promise.all(behind.map((m) => machineAction(m, "update_plugin", { target: pluginBehind(m) })))));
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
