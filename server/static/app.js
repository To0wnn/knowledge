// Knowledge UI: the note in the middle (or the project map), topics/search/changes/lint on the left, connected notes
// on the right; editor, history, new memo and ingest. No build step, no external requests.
"use strict";

// Topic clusters reuse the group hues in a fixed order; clusters beyond these share the neutral doc colour.
const CLUSTER_HUES = ["project", "feedback", "reference", "user", "infra", "incident", "skill", "source",
  "sky", "brown", "lime", "magenta", "coral", "steel"];
let PROJECTS = ["homelab"]; // replaced by the projects this user may see (/ui/api/me)
const ROOT_PROJECT = { memory: "homelab", homelab: "homelab", anpitek: "anpitek", "anpitek-memory": "anpitek",
  trade: "trade", "trade-memory": "trade", claude: "shared" };
const MEMORY_ROOT = { homelab: "memory", anpitek: "anpitek-memory", trade: "trade-memory" };

const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
// Drawing reads the theme tokens per link and label each frame: cache them, cleared when the theme flips.
let tokens = {};
const tok = (v) => tokens[v] || (tokens[v] = css(v)); // an empty read (stylesheet not applied yet) is not cached
// The theme changed (system, or the viewer's pick in the user menu): re-read the tokens and recolour the map.
function themeChanged() {
  tokens = {}; glows.clear();
  if (!Graph) return;
  indexGraph(); renderTopics(); Graph.backgroundColor(tok("--canvas"));
  if (G3) { G3.backgroundColor(tok("--canvas")).nodeThreeObject(node3d); refresh3d(); } // sprites carry theme colours
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", themeChanged);
const decode = (s) => { try { return decodeURIComponent(s); } catch { return s; } }; // a literal % is not an escape
const $ = (id) => document.getElementById(id);
// A live refresh must not take away text someone is selecting: redraws wait while a selection touches their box,
// and text that did not change is not written again (a rewrite drops the selection in it, and costs a layout)
const selecting = (box) => { const s = getSelection(); return !!s && !s.isCollapsed && s.containsNode(box, true); };
const setText = (node, text) => { if (node.textContent !== text) node.textContent = text; };
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text instanceof Node) e.append(text); else if (text != null) e.textContent = text; return e; };
// One empty state and one loading state for every list; a toast instead of alert() (errors stay longer, a click
// dismisses), announced by the #toasts live region.
// An empty state is one muted line that says what the view is for and the next step, with an optional action button.
const actionButton = (text, onclick, cls = "btn small") => { const b = el("button", cls, text); b.type = "button"; b.onclick = onclick; return b; };
const emptyState = (text, ...acts) => { const p = el("p", "empty", text); for (const a of acts) p.append(" ", a); return p; };
const loadingState = () => el("p", "empty loading", "Loading…");
const errorState = (text, retry) => { const p = el("p", "empty error", text); p.setAttribute("role", "alert"); if (retry) p.append(" ", actionButton("Retry", retry)); return p; };
// A view's loader into its box: "Loading…" while the box is still empty, on an error the message and Retry.
function viewLoad(id, what, fn) {
  const box = $(id);
  if (!box.childNodes.length || box.querySelector(":scope > .empty.error")) box.replaceChildren(loadingState());
  return fn().catch((e) => box.replaceChildren(errorState(`${what} did not load: ${e.message}`, () => viewLoad(id, what, fn))));
}
function toast(text, bad = true) {
  const t = el("div", `toast${bad ? " bad" : ""}`, text);
  t.onclick = () => t.remove(); $("toasts").append(t);
  setTimeout(() => t.remove(), bad ? 8000 : 4000);
}
const projectOf = (path) => ROOT_PROJECT[path.split("/")[0]] || "homelab";
const api = (path, opts = {}) => fetch(path, { credentials: "same-origin", ...opts, headers: { "X-Knowledge-UI": "1", ...(opts.headers || {}) } })
  .then(async (r) => {
    if (r.status === 401) location.reload();
    const text = await r.text(), t = text.trim();
    let j; // a proxy error page is no JSON: its own short plain-text message, else the status
    try { j = JSON.parse(text); } catch { j = { error: t && t.length < 300 && !t.startsWith("<") ? `${r.status}: ${t}` : `${r.status} ${r.statusText || "no answer"}${r.status >= 502 || !r.status ? ": the server may be restarting" : ""}` }; }
    if (!r.ok) throw new Error(j.error || `${r.status} ${r.statusText}`); return j;
  });
// Note text can come from guests, write tokens and ingested sources: no forms, inputs, styles or ids (an id could
// shadow the app's own elements), and images only from this server.
const PURIFY = { FORBID_TAGS: ["style", "form", "input", "textarea", "select", "button", "dialog", "iframe", "object", "embed"],
  FORBID_ATTR: ["style", "id", "name", "form", "formaction"] };
DOMPurify.addHook("afterSanitizeAttributes", (n) => {
  if (n.tagName === "IMG" && !/^(\/(?!\/)|data:image\/)/.test(n.getAttribute("src") || "")) n.removeAttribute("src");
});
// A link to a file on the agent's pc (agy writes [`x`](file:///C:/repo/x)): the sanitizer drops file: URLs, so its
// path is kept aside as data-file (a chat turns it into a Files link, ckFileLinks).
DOMPurify.addHook("beforeSanitizeAttributes", (n) => {
  if (n.tagName === "A" && /^file:/i.test(n.getAttribute("href") || "")) n.setAttribute("data-file", n.getAttribute("href"));
});
const renderMd = (text) => DOMPurify.sanitize(marked.parse(text, { gfm: true }), PURIFY);
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* blocked */ } } };
// A new history entry for a route (shell.js); the count lets Back stay inside the app instead of leaving it, and `from`
// tells Back whether the entry before is the page it should go up to (goBack).
const pushHash = (h) => { if (location.hash !== h) history.pushState({ n: (history.state?.n || 0) + 1, from: location.hash }, "", h); };
// A note path inside a route or link: each part encoded, so "?", "#", "%", spaces and accents survive a refresh and
// match location.hash as the browser keeps it; parseRoute decodes it again.
const encPath = (p) => p.split("/").map(encodeURIComponent).join("/");
const noteRoute = (path, view = "notes") => `#/${view}/${encPath(path)}`;

let data = { nodes: [], links: [] };
let byId = new Map(), neighbors = new Map(), slugToId = new Map(), clusterColor = new Map();
let selected = null, hover = null, focusSet = new Set();
let machinesCache = []; // the last /ui/api/machines rows (declared early for loadTasks/loadMachines)
let project = navProject.get(), picks = navPicks(project); // picks: the picked projects, null = all of them
let labelMin = 0, current = null, editing = false, Graph, mapOn = false;

// "all": every project on one map, each project an island; otherwise one project and its topics.
// Several picked projects (or none) show as the All map does, with only those projects.
const ALL = "all", allMode = () => picks?.size !== 1, picked = (p) => !picks || picks.has(p);
const pickName = () => !picks ? "all projects" : [...picks].map(shown).join(", ") || "no project";
const inProject = (p) => picked(p) || p === "shared";
// A shared note (skill) shows only in the projects that link to it.
const nodeIn = (n) => picked(n.project) || (n.project === "shared" && (!picks || !n.projects || n.projects.some(picked)));
// What colours a note and makes its island: its project on the All map, its topic otherwise.
const groupOf = (n) => (allMode() ? n.project : n.cluster);
const lintProject = (item) => ROOT_PROJECT[item.split(/[/:]/)[0]] || "homelab"; // homelab memory items carry no root prefix
const nodeColor = (n) => clusterColor.get(groupOf(n)) || tok("--g-doc");
const linkId = (end) => end.id || end;
const linkW = (l) => l.w ?? 0.5; // an older server sends no weight
const fade = (color, f) => color.replace(/rgba\(([^)]+),\s*([\d.]+)\)/, (_, rgb, a) => `rgba(${rgb}, ${(a * f).toFixed(3)})`);
// "#rrggbb" + alpha 0..1 as "#rrggbbaa" (the topic colours are hex tokens; canvas and three.js both read the 8-digit form)
// On a light canvas the glow and nebulas are softer: colour on white turns muddy sooner than light on dark.
const darkCanvas = () => parseInt(tok("--canvas").slice(1, 3), 16) < 128;
const hexA = (hex, a) => (/^#[0-9a-f]{6}$/i.test(hex) ? hex + Math.round(a * 255).toString(16).padStart(2, "0") : hex);

// ---------------------------------------------------------------- graph data
function indexGraph() {
  byId = new Map(data.nodes.map((n) => [n.id, n]));
  neighbors = new Map(data.nodes.map((n) => [n.id, new Set()]));
  for (const l of data.links) { neighbors.get(linkId(l.source))?.add(linkId(l.target)); neighbors.get(linkId(l.target))?.add(linkId(l.source)); }
  slugToId = new Map();
  for (const n of data.nodes) {
    for (const k of [n.id.split("/").pop().replace(/\.md$/, ""), n.name].filter(Boolean)) { // file name and frontmatter name
      const key = k.replace(/-/g, "_");
      if (!slugToId.has(key) || inProject(n.project)) slugToId.set(key, n.id); // prefer the memo of this project
    }
  }
  // Cluster colours by topic name, not by size: a topic keeps its colour when notes are added or moved (by size,
  // a topic that shrank out of the top 8 turned grey and the others shifted). Past the hues it is grey.
  const groups = [...new Set(data.nodes.filter((n) => groupOf(n) && nodeIn(n)).map(groupOf))].sort((a, b) => a.localeCompare(b));
  clusterColor = new Map(groups.slice(0, CLUSTER_HUES.length).map((c, i) => [c, tok(`--g-${CLUSTER_HUES[i]}`)]));
}

function visibleData() {
  const nodes = data.nodes.filter(nodeIn);
  const keep = new Set(nodes.map((n) => n.id));
  const links = data.links.filter((l) => keep.has(linkId(l.source)) && keep.has(linkId(l.target)));
  // Every note keeps its strongest link in sight, so hiding weak links never makes a note look unlinked.
  const best = new Map();
  for (const l of links) for (const end of [linkId(l.source), linkId(l.target)]) if (!best.has(end) || linkW(l) > linkW(best.get(end))) best.set(end, l);
  for (const l of links) l.keep = best.get(linkId(l.source)) === l || best.get(linkId(l.target)) === l;
  // Labels at overview zoom: the ~15 best-connected notes in view.
  const degrees = nodes.map((n) => n.degree || 0).sort((a, b) => b - a);
  labelMin = Math.max(3, degrees[Math.min(14, degrees.length - 1)] || 0);
  return { nodes, links };
}

function applyView(refit = false) {
  if (refit) loadGraph.fitted = false;
  const v = mapData();
  Graph.graphData(v); settling = true; // new data reheats the layout
  set3dData();
}

async function refreshData() {
  await releaseFocus({ instant: true });
  // Reload after a change but keep the layout: known nodes get their positions and hold them while the new data reheats
  // the simulation (copied alone, its forces moved most of the map on every change); only new nodes look for a place.
  const old = new Map(data.nodes.map((n) => [n.id, n])), settled = !settling; // a first layout still has to finish
  data = await api("/ui/api/graph");
  for (const n of data.nodes) { const o = old.get(n.id); if (o) Object.assign(n, { x: o.x, y: o.y, vx: 0, vy: 0 }, settled || o.held ? { fx: o.x, fy: o.y, held: true } : {}); }
  indexGraph(); renderProjects(); renderTopics(); applyView();
  // a new note or one whose text changed pulses (size is the cheap tell; an edit of the same length is missed)
  if (old.size) for (const n of data.nodes) if (nodeIn(n) && (!old.has(n.id) || old.get(n.id).size !== n.size)) pulse(n.id);
  // the release above cut a click's gather short (its promise never ends): gather again, and zoom if that was still owed
  if (selected && mapOn && byId.get(selected)?.x != null) gather(selected, 1).then(() => { if (selected && zoomOwed) { zoomOwed = false; zoomToFocus(); } });
}

// ---------------------------------------------------------------- map rendering
const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
const touch = matchMedia("(hover: none)").matches; // phones: a tap selects, there is no hover
if (touch) $("q").placeholder = "Search titles, text and meaning…"; // no "/" key on a phone
const baseRadius = (n) => 3 + Math.sqrt(n.degree || 0) * 1.6;

// A soft glow behind every note: one pre-drawn sprite per colour (a canvas shadowBlur per note per frame is too slow
// for a thousand notes).
const glows = new Map();
function glowOf(color) {
  let g = glows.get(color);
  if (!g) {
    g = document.createElement("canvas"); g.width = g.height = 64;
    const c = g.getContext("2d"), grd = c.createRadialGradient(32, 32, 0, 32, 32, 32);
    const f = darkCanvas() ? 1 : 0.55;
    grd.addColorStop(0, hexA(color, 0.85 * f)); grd.addColorStop(0.3, hexA(color, 0.35 * f)); grd.addColorStop(1, hexA(color, 0));
    c.fillStyle = grd; c.fillRect(0, 0, 64, 64); glows.set(color, g);
  }
  return g;
}

// Live: a note that just changed (any agent, any device) sends out rings and its links flow for a while.
const PULSE_MS = 8000;
const pulses = new Map(); // id -> {t0, ms, flow}
function pulse(id, ms = PULSE_MS, flow = true) { // flow: its links flow too (not in the replay: a thousand notes)
  if (calm || !Graph) return;
  pulses.set(id, { t0: performance.now(), ms, flow }); pulse3d();
  if (!flow) return;
  wakeMap(ms);
  animateHover(ms);
  clearTimeout(pulse.timer); // the links stop flowing once the last pulse is over
  pulse.timer = setTimeout(() => { prunePulses(); if (Graph) { Graph.linkDirectionalParticles(Graph.linkDirectionalParticles()); wakeMap(1000); } refresh3d(); }, ms + 50);
}
const pulseLive = (p) => p && performance.now() - p.t0 < p.ms;
// drawPulse removes a pulse only when 2D draws its note; a hidden map or 3D draws nothing, so expired ones go here too
function prunePulses() { for (const [id, p] of pulses) if (!pulseLive(p)) pulses.delete(id); }
function drawPulse(node, ctx, scale, r) {
  const p = pulses.get(node.id);
  if (!p) return;
  const k = (performance.now() - p.t0) / p.ms;
  if (k >= 1) { pulses.delete(node.id); return; }
  ctx.strokeStyle = nodeColor(node); ctx.lineWidth = 2 / scale;
  for (const off of [0, 0.5]) { // two rings, half a beat apart
    const kk = (k * (p.ms / 2000) + off) % 1;
    ctx.globalAlpha = (1 - kk) * (1 - k * 0.6);
    ctx.beginPath(); ctx.arc(node.x, node.y, r * (1.3 + kk * 4), 0, 2 * Math.PI); ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

// Each topic as a soft nebula of its colour under its notes: the islands and the empty space between them read at a
// glance. Shared by the nebulas and the topic names: [{name, ns, cx, cy, spread}], biggest first.
function islands() {
  const groups = new Map();
  for (const n of Graph.graphData().nodes) if (groupOf(n) && n.x != null && shownAt(n)) (groups.get(groupOf(n)) || groups.set(groupOf(n), []).get(groupOf(n))).push(n);
  return [...groups].filter(([, ns]) => ns.length >= 2).sort((a, b) => b[1].length - a[1].length).map(([name, ns]) => {
    const cx = ns.reduce((s, n) => s + n.x, 0) / ns.length, cy = ns.reduce((s, n) => s + n.y, 0) / ns.length;
    return { name, ns, cx, cy, spread: ns.reduce((s, n) => s + Math.hypot(n.x - cx, n.y - cy), 0) / ns.length };
  });
}
function drawNebulas(ctx) {
  ctx.globalAlpha = focusSet.size ? 0.35 : 1;
  for (const { name, cx, cy, spread } of islands()) {
    const color = clusterColor.get(name) || tok("--g-doc"), R = spread * 2.2 + 50, f = darkCanvas() ? 1 : 0.6;
    if (!color) continue; // the stylesheet is not applied yet
    const grd = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
    grd.addColorStop(0, hexA(color, 0.34 * f)); grd.addColorStop(0.5, hexA(color, 0.13 * f)); grd.addColorStop(1, hexA(color, 0));
    ctx.fillStyle = grd; ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.fill();
  }
  ctx.globalAlpha = 1;
}

function drawNode(node, ctx, scale) {
  // Hover: the node eases up to 2.4x with a glow, its neighbours to 1.4x.
  const target = node.id === hover ? 2.4 : hover && focusSet.has(node.id) ? 1.4 : 1;
  node.__s = calm ? target : (node.__s || 1) + (target - (node.__s || 1)) * 0.25;
  const r = baseRadius(node) * node.__s;
  const lit = focusSet.size === 0 || focusSet.has(node.id);
  ctx.globalAlpha = node.gone ? 0.4 : 1; // a removed note in the replay: translucent
  if (lit && nodeColor(node)) { const g = r * 4.5; ctx.drawImage(glowOf(nodeColor(node)), node.x - g, node.y - g, 2 * g, 2 * g); }
  drawPulse(node, ctx, scale, r);
  ctx.globalAlpha = (lit ? 1 : 0.15) * (node.gone ? 0.4 : 1);
  ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
  ctx.fillStyle = nodeColor(node);
  if (node.id === hover) { ctx.shadowColor = ctx.fillStyle; ctx.shadowBlur = 20; }
  ctx.fill(); ctx.shadowBlur = 0;
  if (node.id === selected) { ctx.lineWidth = 2 / scale; ctx.strokeStyle = tok("--ink"); ctx.stroke(); }
  ctx.globalAlpha = 1;
}

// Labels after all nodes, most important first; a label that would overlap one already drawn is skipped.
function drawLabels(ctx, scale) {
  const overview = !focusSet.size && scale < 1.3;
  if (overview) drawTopics(ctx, scale);
  const rank = (n) => n.id === hover ? 3 : n.id === selected ? 2 : focusSet.has(n.id) ? 1 : 0;
  const wanted = Graph.graphData().nodes.filter((n) => n.x != null && shownAt(n) && (rank(n) ||
    (!focusSet.size && !overview && (scale > 2.2 || ((n.degree || 0) >= labelMin && scale > 0.6)))));
  wanted.sort((a, b) => rank(b) - rank(a) || (b.degree || 0) - (a.degree || 0));
  const taken = [];
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (const node of wanted) {
    const big = node.id === hover, size = Math.max((big ? 16 : 12.5) / scale, 3);
    const r = baseRadius(node) * (node.__s || 1), y = node.y + r + 2 / scale;
    ctx.font = `${big ? 600 : 400} ${size}px ${tok("--font")}`;
    const w = ctx.measureText(node.label).width + size, h = size * 1.5, box = [node.x - w / 2, y - size * 0.25, w, h];
    if (!big && node.id !== selected && taken.some((t) => box[0] < t[0] + t[2] && t[0] < box[0] + box[2] && box[1] < t[1] + t[3] && t[1] < box[1] + box[3])) continue;
    taken.push(box);
    if (big) { // a pill behind the hovered label
      ctx.fillStyle = tok("--panel"); ctx.strokeStyle = nodeColor(node); ctx.lineWidth = 1 / scale;
      ctx.beginPath(); ctx.roundRect(...box, h / 2); ctx.fill(); ctx.stroke();
    } else {
      ctx.lineWidth = 3 / scale; ctx.strokeStyle = tok("--canvas"); ctx.strokeText(node.label, node.x, y); // halo keeps labels readable over links
    }
    ctx.fillStyle = tok("--ink"); ctx.fillText(node.label, node.x, y);
  }
}

// Zoomed out: one name per topic in the topic colour, just above the island's core (mean distance from its
// centre, so a stray note does not push the name away); larger topics first, names that would overlap skipped.
function drawTopics(ctx, scale) {
  const size = 15 / scale, taken = [];
  ctx.font = `650 ${size}px ${tok("--font")}`; ctx.textAlign = "center"; ctx.textBaseline = "bottom";
  for (const { name, cx, cy, spread } of islands()) {
    const y = cy - spread - 6 / scale, w = ctx.measureText(name).width, box = [cx - w / 2, y - size, w, size];
    if (taken.some((t) => box[0] < t[0] + t[2] && t[0] < box[0] + box[2] && box[1] < t[1] + t[3] && t[1] < box[1] + box[3])) continue;
    taken.push(box);
    ctx.lineWidth = 4 / scale; ctx.strokeStyle = tok("--canvas"); ctx.strokeText(name, cx, y);
    ctx.fillStyle = clusterColor.get(name) || tok("--ink"); ctx.fillText(name, cx, y);
  }
}

// Keep redrawing while hover/selection animations run (link particles, easing); force-graph pauses
// redraws and its animation loop when idle to spare phone batteries, CPU and GPU.
let settleTimer, mapSleepTimer = 0;
function wakeMap(ms = 2000) {
  if (!Graph || !mapOn || map3d || document.hidden) return;
  Graph.resumeAnimation();
  clearTimeout(mapSleepTimer);
  if (!settling && !tweening && !replay?.playing && !hover && pulses.size === 0) {
    mapSleepTimer = setTimeout(() => {
      if (Graph && mapOn && !map3d && !settling && !tweening && !replay?.playing && !hover && pulses.size === 0) {
        Graph.pauseAnimation();
      }
    }, ms);
  }
}
function animateHover(ms = 400) {
  if (calm) { if (!tweening && !replay) Graph.autoPauseRedraw(true); return; }
  Graph.linkDirectionalParticles(Graph.linkDirectionalParticles()); // re-evaluate which links flow
  Graph.autoPauseRedraw(false); clearTimeout(settleTimer);
  wakeMap(ms + 1000);
  if (!hover) settleTimer = setTimeout(() => {
    if (!tweening && !replay) {
      Graph.autoPauseRedraw(true);
      wakeMap(1000);
    }
  }, ms);
}

// The overview fit: topic names are drawn at a fixed screen size and stick out past the outer notes, so pad by half
// the widest name (capped, so a phone keeps most of its width for the map).
function fitPad() {
  const ctx = document.createElement("canvas").getContext("2d"); ctx.font = `650 15px ${tok("--font")}`;
  const widest = Math.max(0, ...[...new Set(Graph.graphData().nodes.map(groupOf))].filter(Boolean).map((g) => ctx.measureText(g).width));
  return Math.round(Math.min(Math.max(40, widest / 2 + 16), $("graph").clientWidth / 4));
}
// Zoom to the selected note and everything it links to, capped so a lone note is not blown up.
let settling = true;
function zoomToFocus() {
  Graph.zoomToFit(600, 90, (m) => focusSet.has(m.id));
  setTimeout(() => { if (Graph.zoom() > 4) Graph.zoom(4, 300); }, 650);
}

// ---------------------------------------------------------------- focus mode
// Hover leans a note's neighbours towards it; a click gathers them in a ring around it (grouped by topic), a click
// beside the nodes sends everything home. Positions are animated, not simulated, so the island map comes back
// exactly as it was and the rest of the map does not shift.
const home = new Map(); // node -> {x, y} before focus mode moved it
let tweenId = 0, hoverTimer, tweening = false, homeTween = 0, zoomOwed = false; // homeTween: deselect's way home, which ends with the zoom out

function tween(moves, done) {
  const id = ++tweenId, t0 = performance.now(), ms = calm ? 0 : 450;
  const step = (now) => {
    if (id !== tweenId) return; // a newer move took over
    const k = ms ? Math.min(1, (now - t0) / ms) : 1, e = 1 - Math.pow(1 - k, 3); // ease-out cubic
    for (const m of moves) { m.n.x = m.n.fx = m.x0 + (m.x1 - m.x0) * e; m.n.y = m.n.fy = m.y0 + (m.y1 - m.y0) * e; m.n.vx = m.n.vy = 0; }
    if (k < 1) return requestAnimationFrame(step);
    tweening = false; animateHover(selected ? 4000 : 400); // redraw pauses again once idle
    if (done) done();
  };
  tweening = true; Graph.autoPauseRedraw(false); wakeMap(ms + 1500); requestAnimationFrame(step);
}

function gather(id, strength) {
  // strength 1 = ring around the note (click), below 1 = lean that fraction of the way (hover)
  const c = byId.get(id);
  if (!Graph || !c || c.x == null) return Promise.resolve();
  const hc = home.get(c) || { x: c.x, y: c.y };
  const nb = [...(neighbors.get(id) || [])].map((n) => byId.get(n)).filter((n) => n && n.x != null && n !== c)
    .sort((a, b) => (a.cluster || "").localeCompare(b.cluster || "") || a.label.localeCompare(b.label));
  const radius = Math.max(60, 40 + nb.length * 7), targets = new Map([[c, hc]]);
  nb.forEach((n, i) => {
    const h = home.get(n) || { x: n.x, y: n.y }, a = -Math.PI / 2 + (2 * Math.PI * i) / nb.length;
    targets.set(n, strength >= 1 ? { x: hc.x + radius * Math.cos(a), y: hc.y + radius * Math.sin(a) }
                                  : { x: h.x + (hc.x - h.x) * strength, y: h.y + (hc.y - h.y) * strength });
  });
  for (const [n, h] of home) if (!targets.has(n)) targets.set(n, h); // nodes of an earlier focus go home
  for (const n of targets.keys()) if (!home.has(n)) home.set(n, { x: n.x, y: n.y });
  return new Promise((resolve) => tween([...targets].map(([n, t]) => ({ n, x0: n.x, y0: n.y, x1: t.x, y1: t.y })), resolve));
}

function releaseFocus({ instant = false } = {}) {
  clearTimeout(hoverTimer);
  if (!home.size) return Promise.resolve();
  const moves = [...home].map(([n, h]) => ({ n, x0: n.x, y0: n.y, x1: h.x, y1: h.y }));
  const free = () => { for (const m of moves) m.n.fx = m.n.fy = undefined; home.clear(); };
  if (instant) { tweenId++; tweening = false; for (const m of moves) { m.n.x = m.x1; m.n.y = m.y1; } free(); return Promise.resolve(); }
  return new Promise((resolve) => tween(moves, () => { free(); resolve(); }));
}

function setFocus(id) {
  focusSet = id ? new Set([id, ...(neighbors.get(id) || [])]) : new Set();
  refresh3d();
}

const linkLit = (l) => focusSet.size && focusSet.has(linkId(l.source)) && focusSet.has(linkId(l.target));
// Links flow from a note in focus (flowing for a few seconds after selection then pausing to rest), and from a note that just changed.
let selectFlowUntil = 0;
const particles = (l) => (!calm && mapOn && (((hover || (selected && performance.now() < selectFlowUntil)) && linkLit(l)) || [l.source, l.target].some((e) => { const p = pulses.get(linkId(e)); return p?.flow && pulseLive(p); })) ? 2 : 0);

async function loadGraph() {
  data = await api("/ui/api/graph");
  indexGraph();
  Graph = ForceGraph()($("g2d"))
    .nodeId("id").nodeLabel(() => "") // the hover pill shows the name
    .nodeCanvasObject(drawNode)
    .nodePointerAreaPaint((n, c, ctx) => { ctx.fillStyle = c; ctx.beginPath(); ctx.arc(n.x, n.y, (baseRadius(n) + 2) * (n.__s || 1), 0, 2 * Math.PI); ctx.fill(); })
    // At rest: links inside a topic clearly visible, links between topics fainter so the islands still read (the
    // border colour --line was nearly the canvas colour; dark mode needs a lighter, stronger line: own tokens).
    // Highlighted when their note is in focus.
    // On the All map a link between two projects is the point of the view: drawn in the accent, not faded.
    // Weight (l.w, 0..1, graph.py weighted_links: link, shared sources and neighbours, type) sets thickness and
    // strength, so a strong relation reads at a glance. The weakest links between topics are hidden until their
    // note is in focus, except a note's strongest link (visibleData): less spaghetti between the islands, and no
    // note looks unlinked (24 Oct 2026: before this, 27 notes looked unlinked).
    .linkColor((l) => (linkLit(l) ? tok("--accent") : groupOf(l.source) === groupOf(l.target) ? (clusterColor.has(groupOf(l.source)) ? hexA(nodeColor(l.source), 0.3 + 0.4 * linkW(l)) : fade(tok("--link-in"), 0.55 + 0.45 * linkW(l)))
      : allMode() ? tok("--accent") + "99" : fade(tok("--link-cross"), 0.6 + 0.4 * linkW(l))))
    .linkWidth((l) => (linkLit(l) ? 1 + 1.6 * linkW(l) : allMode() && groupOf(l.source) !== groupOf(l.target) ? 1.2
      : groupOf(l.source) === groupOf(l.target) ? 0.5 + 1.6 * linkW(l) : 0.4 + 0.9 * linkW(l)))
    .nodeVisibility(shownAt) // the replay shows the notes in the order they were born
    .linkVisibility((l) => shownAt(l.source) && shownAt(l.target) && (linkLit(l) || l.keep || allMode() || groupOf(l.source) === groupOf(l.target) || linkW(l) >= 0.2))
    .linkDirectionalParticles(particles) // particles keep force-graph drawing every frame
    .linkDirectionalParticleWidth(2.2).linkDirectionalParticleSpeed(0.006)
    .linkDirectionalParticleColor(() => tok("--accent"))
    .backgroundColor(tok("--canvas"))
    .cooldownTicks(200) // the layout settles, then the simulation stops: no CPU while idle
    .onEngineStop(() => {
      settling = false;
      for (const n of data.nodes) if (n.held) { n.held = false; if (!home.has(n)) n.fx = n.fy = undefined; } // refreshData; a gathered node goes back with its focus
      if (zoomToFocus.again && selected) zoomToFocus();
      else if (!loadGraph.fitted) Graph.zoomToFit(mapOn ? 400 : 0, fitPad());
      zoomToFocus.again = false; loadGraph.fitted = true;
      // a hidden map, laid out and fitted: stop force-graph's frame loop (60 a second on every other view); setMap resumes it.
      // _animationCycle schedules requestAnimationFrame AFTER tickFrame/onEngineStop returns, so defer pauseAnimation past this frame.
      setTimeout(() => { if (!mapOn || map3d) Graph.pauseAnimation(); else wakeMap(2000); }, 20);
    })
    .onNodeHover((n) => {
      if (touch) return;
      // a note gliding home under a still mouse is no hover: its gather would take over the way home, and the zoom out never comes
      const goingHome = tweening && tweenId === homeTween;
      hover = n ? n.id : null; if (!selected && !goingHome) setFocus(hover);
      $("g2d").style.cursor = n ? "pointer" : ""; animateHover(selected ? 4000 : 400);
      clearTimeout(hoverTimer);
      if (!selected && !goingHome) hoverTimer = setTimeout(() => (hover ? gather(hover, 0.3) : releaseFocus()), hover ? 150 : 250);
    })
    .onNodeClick((n) => (n.gone ? ghostSays(n) : selected === n.id ? go(noteRoute(n.id)) : openNote(n.id))) // a second click reads it
    .onBackgroundClick(() => deselect())
    .onLinkClick(() => deselect()); // lines cross the empty space; a click on one is a click beside the nodes
  // More room than the force-graph defaults (-30 / 30) so labels do not pile up.
  // Links inside a topic hold its island together; links between topics barely pull, so islands stay apart.
  // Repulsion only acts nearby, so a loosely linked note is not flung to the edge.
  Graph.d3Force("charge").strength(-90).distanceMax(260);
  // A strong relation pulls its notes closer; between topics the pull stays weak so the islands keep apart.
  Graph.d3Force("link").distance((l) => 48 - 14 * linkW(l))
    .strength((l) => (groupOf(l.source) && groupOf(l.source) === groupOf(l.target) ? 0.3 + 0.4 * linkW(l) : 0.01 + 0.03 * linkW(l)));
  let simNodes = [];
  // Each topic gets its own island: an anchor point its notes are pulled to. The largest topic sits in the
  // middle, the rest on a golden-angle spiral around it. Notes without a topic drift to the centre.
  let anchors = new Map();
  const gravity = (alpha) => {
    for (const n of simNodes) {
      const a = anchors.get(groupOf(n)), k = (a ? 0.18 : 0.04) * alpha;
      n.vx += ((a ? a.x : 0) - n.x) * k; n.vy += ((a ? a.y : 0) - n.y) * k;
    }
  };
  gravity.initialize = (nodes) => {
    simNodes = nodes;
    const sizes = new Map();
    for (const n of nodes) if (groupOf(n)) sizes.set(groupOf(n), (sizes.get(groupOf(n)) || 0) + 1);
    const step = allMode() ? 520 : 230; // projects are bigger islands than topics
    anchors = new Map([...sizes].sort((a, b) => b[1] - a[1]).map(([c], i) =>
      [c, { x: step * Math.sqrt(i) * Math.cos(i * 2.4), y: step * Math.sqrt(i) * Math.sin(i * 2.4) }]));
  };
  Graph.d3Force("gravity", gravity);
  Graph.onRenderFramePre(drawNebulas).onRenderFramePost(drawLabels);
  for (const ev of ["pointerdown", "pointermove", "wheel", "touchstart"]) $("g2d").addEventListener(ev, () => wakeMap(), { passive: true });
  new ResizeObserver(() => { const w = $("graph").clientWidth, h = $("graph").clientHeight; if (w && h) { Graph.width(w).height(h); wakeMap(1000); } }).observe($("graph"));
  renderProjects(); renderTopics(); applyView(true);
}

// ---------------------------------------------------------------- replay
// Replay: a time bar along the bottom (as in Cosmograph): a histogram of when the notes were born (server: oldest kept
// history version, else the file time), a handle to drag through time, play/pause and a speed. Notes born up to the
// handle's moment are shown; the ones that just appeared pulse.
let replay = null; // {t0, t1, t, k, playing, id}
const shownAt = (n) => !replay || !n || ((n.born ?? 0) <= replay.t && !(n.gone && n.gone <= replay.t)); // !n: a link end force-graph has not resolved yet

// With removed notes: the replay also shows the notes that were archived, merged, renamed or deleted since (server
// ?history=1: their last text, born and gone), translucent, from their birth until their removal. Only while the
// replay is open; a click on one says when it went. The choice stays per browser (kn_replay_archive).
let ghostData = null; // {nodes, links} for this project, or null
function mapData() {
  const v = visibleData();
  if (replay && ghostData) { v.nodes = v.nodes.concat(ghostData.nodes); v.links = v.links.concat(ghostData.links); }
  return v;
}
async function loadGhosts() {
  const h = await api("/ui/api/graph?history=1"), cur = new Set(visibleData().nodes.map((n) => n.id));
  const ghosts = h.nodes.filter((n) => n.gone && nodeIn(n)), ids = new Set(ghosts.map((n) => n.id)), known = (id) => ids.has(id) || cur.has(id);
  const links = h.links.filter((l) => (ids.has(l.source) || ids.has(l.target)) && known(l.source) && known(l.target));
  for (const g of ghosts) { // start beside a linked note, not in the middle of the map
    const l = links.find((x) => (x.source === g.id && cur.has(x.target)) || (x.target === g.id && cur.has(x.source)));
    const n = l && byId.get(l.source === g.id ? l.target : l.source);
    if (n?.x != null) Object.assign(g, { x: n.x + (Math.random() - 0.5) * 40, y: n.y + (Math.random() - 0.5) * 40 });
  }
  ghostData = { nodes: ghosts, links };
}
const ghostSays = (n) => toast(`${n.label}: removed on ${fmtDay(n.gone)}. History or Trash still has it.`, false);
// keep the map where it is while notes come or go: the next layout only places the new ones (as refreshData does)
function holdMap() { for (const n of Graph.graphData().nodes) if (n.x != null && !home.has(n)) Object.assign(n, { fx: n.x, fy: n.y, held: true }); }
function replayRange() {
  const ts = Graph.graphData().nodes.flatMap((n) => (n.gone ? [n.born ?? 0, n.gone] : [n.born ?? 0]));
  replay.t0 = Math.min(...ts) - 1; replay.t1 = Math.max(...ts);
  replay.bins = replayBins();
}
const BINS = 72;
function replayBins() {
  const bins = new Array(BINS).fill(0), span = Math.max(1, replay.t1 - replay.t0);
  for (const n of Graph.graphData().nodes) bins[Math.min(BINS - 1, Math.floor(((n.born ?? replay.t0) - replay.t0) / span * BINS))]++;
  return bins;
}
function drawTimeBar() {
  const c = $("time-hist"), w = c.clientWidth, h = c.clientHeight, dpr = devicePixelRatio || 1;
  if (!w) return;
  if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
  const ctx = c.getContext("2d"), bins = replay.bins, top = Math.max(1, ...bins), bw = w / BINS;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
  for (let i = 0; i < BINS; i++) {
    if (!bins[i]) continue;
    const bh = Math.max(2, (h - 2) * Math.sqrt(bins[i] / top)); // sqrt: one big import day does not flatten the rest
    ctx.fillStyle = (i + 0.5) / BINS <= replay.k ? tok("--accent") : hexA(tok("--muted").startsWith("#") ? tok("--muted") : "#888888", 0.45);
    ctx.fillRect(i * bw + 1, h - bh, Math.max(1, bw - 2), bh);
  }
}
function replayAt(k) {
  const was = replay.t;
  replay.k = Math.max(0, Math.min(1, k));
  replay.t = replay.t0 + (replay.t1 - replay.t0) * replay.k;
  $("time-pos").value = String(Math.round(replay.k * 1000));
  $("map-date").textContent = fmtDay(replay.t);
  if (replay.t > was) for (const n of Graph.graphData().nodes) if ((n.born ?? 0) > was && (n.born ?? 0) <= replay.t) pulse(n.id, 1500, false);
  drawTimeBar();
  if (G3 && map3d) G3.nodeVisibility(G3.nodeVisibility()).linkVisibility(G3.linkVisibility());
}
function playReplay(on) {
  if (!replay) return;
  if (on && replay.k >= 1) replayAt(0); // at the end, play starts over
  replay.playing = on;
  $("time-play").textContent = on ? "❚❚" : "▶"; $("time-play").setAttribute("aria-label", on ? "Pause" : "Play");
  if (!on) return;
  const id = replay.id = {};
  let last = null, synced = 0;
  const step = (now) => {
    if (!replay?.playing || replay.id !== id) return;
    const dt = last == null ? 0 : Math.max(0, now - last); last = now; // a frame stamp can predate the click
    const k = replay.k + dt / (Number($("time-speed").value) * 1000);
    if (G3 && map3d && now - synced < 250 && k < 1) { // 3D rebuilds its objects on a visibility change: four times a second
      replay.k = k; replay.t = replay.t0 + (replay.t1 - replay.t0) * k; $("map-date").textContent = fmtDay(replay.t);
      $("time-pos").value = String(Math.round(k * 1000));
    } else { synced = now; replayAt(k); }
    if (k < 1) requestAnimationFrame(step); else playReplay(false);
  };
  requestAnimationFrame(step);
}
async function startReplay() {
  stopReplay();
  if (!Graph || !Graph.graphData().nodes.length) return; // the map data is still loading
  replay = { t0: 0, t1: 0, t: 0, k: 0, playing: false, id: null };
  if ($("time-archive").checked) await withGhosts(true);
  if (!replay) return; // closed meanwhile
  replayRange();
  $("map-time").hidden = false; $("map-replay").setAttribute("aria-pressed", "true");
  (map3d && G3 ? G3 : Graph).zoomToFit(400, fitPad());
  Graph.autoPauseRedraw(false);
  replayAt(0); playReplay(true);
}
function stopReplay() {
  if (!replay) return;
  replay = null;
  if (ghostData) { ghostData = null; holdMap(); applyView(); }
  $("map-time").hidden = true; $("map-replay").setAttribute("aria-pressed", "false");
  if (G3) G3.nodeVisibility(G3.nodeVisibility()).linkVisibility(G3.linkVisibility());
  prunePulses(); animateHover();
}
$("time-play").onclick = () => playReplay(!replay?.playing);
$("time-pos").oninput = () => { if (replay) { playReplay(false); replayAt(Number($("time-pos").value) / 1000); } };
$("time-close").onclick = () => stopReplay();
async function withGhosts(on) {
  if (on) { try { await loadGhosts(); } catch (e) { toast(`The removed notes did not load: ${e.message}`); $("time-archive").checked = false; ghostData = null; return; } }
  else ghostData = null;
  if (!replay) return;
  holdMap(); applyView();
}
$("time-archive").checked = localStorage.getItem("kn_replay_archive") === "1";
$("time-archive").onchange = async () => {
  localStorage.setItem("kn_replay_archive", $("time-archive").checked ? "1" : "0");
  if (!replay) return;
  await withGhosts($("time-archive").checked);
  if (replay) { replayRange(); replayAt(replay.k); }
};
$("time-speed").value = localStorage.getItem("kn_replay_speed") || "30";
$("time-speed").onchange = () => localStorage.setItem("kn_replay_speed", $("time-speed").value);

// ---------------------------------------------------------------- 3D
// The same notes in 3D (3d-force-graph, three.js): loaded on the first switch, so the 2D map stays light. Its own
// node copies ({id, src}), since both maps write positions into their nodes. The choice stays per browser (kn_map3d).
let G3 = null, map3d = false;
function loadScript(src) {
  return new Promise((ok, bad) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = () => bad(new Error("The 3D map did not load")); document.head.append(s); });
}
// Only the visible map runs its frame loop (a hidden one cost 60 frames a second on every view).
let g3SleepTimer = 0;
function wake3d(ms = 20000) {
  if (!G3 || !mapOn || !map3d || document.hidden) return;
  G3.resumeAnimation();
  clearTimeout(g3SleepTimer);
  g3SleepTimer = setTimeout(() => {
    if (!G3) return;
    const c = G3.controls?.();
    if (c) c.autoRotate = false;
    if (G3 && mapOn && map3d && !hover && pulses.size === 0 && !replay?.playing) {
      G3.pauseAnimation();
    }
  }, ms);
}
function syncAnim() {
  const active = mapOn && !document.hidden;
  if (Graph) active && !map3d ? wakeMap(2000) : Graph.pauseAnimation();
  if (G3) active && map3d ? wake3d(20000) : G3.pauseAnimation();
}
async function setMap3d(on) {
  map3d = on; localStorage.setItem("kn_map3d", on ? "1" : "0");
  $("map-3d").setAttribute("aria-pressed", String(on));
  $("g2d").hidden = on; $("g3d").hidden = !on;
  if (on) {
    try { await build3d(); } catch (e) { toast(e.message); return setMap3d(false); }
    refresh3d();
    if (selected) fly3d(selected); else G3.zoomToFit(400, 40);
    wake3d(20000);
  } else if (Graph && mapOn) {
    Graph.zoomToFit(400, fitPad());
    wakeMap(2000);
  }
  syncAnim();
}
function refresh3d() {
  if (!G3 || !map3d) return; // a hidden 3D map is restyled when it is switched on
  G3.nodeColor(G3.nodeColor()).linkColor(G3.linkColor()).linkWidth(G3.linkWidth()).linkDirectionalParticles(G3.linkDirectionalParticles());
  const dark = darkCanvas();
  for (const n of G3.graphData().nodes) if (n.__glow) n.__glow.material.opacity = (!focusSet.size || focusSet.has(n.id) ? 1 : 0.08) * (dark ? 0.9 : 0.45);
}
function set3dData() {
  if (!G3) return;
  const v = mapData(), old = new Map(G3.graphData().nodes.map((n) => [n.id, n]));
  const nodes = v.nodes.map((n) => Object.assign(old.get(n.id) || { id: n.id }, { src: n }));
  G3.graphData({ nodes, links: v.links.map((l) => ({ source: linkId(l.source), target: linkId(l.target), w: l.w, keep: l.keep })) });
}
function fly3d(id) {
  const n = G3?.graphData().nodes.find((x) => x.id === id);
  if (!n || n.x == null) return;
  const d = 90, k = 1 + d / Math.max(1, Math.hypot(n.x, n.y, n.z));
  G3.cameraPosition({ x: n.x * k, y: n.y * k, z: n.z * k }, n, 800);
}
async function build3d() {
  if (G3) return set3dData();
  if (!window.ForceGraph3D) {
    // three.js as a module first: the 3d-force-graph bundle then uses it (window.THREE) instead of its own copy, so
    // the glow and label sprites below are from the same three.js as the scene. Same revision as the bundle's (r186).
    window.THREE = await import("/ui/static/vendor/three-0.186.0/three.module.js").catch(() => { throw new Error("The 3D map did not load"); });
    await loadScript("/ui/static/vendor/3d-force-graph-1.80.2.js");
  }
  const box = $("g3d"), same = (l) => !!(l.source.src && l.target.src) && groupOf(l.source.src) === groupOf(l.target.src); // ends are ids until the first tick
  G3 = new ForceGraph3D(box, { controlType: "orbit" })
    .width($("graph").clientWidth).height($("graph").clientHeight)
    .backgroundColor(tok("--canvas")).showNavInfo(false)
    .nodeId("id").nodeLabel((n) => el("span", null, n.src.label)) // an element, not an HTML string: titles come from guests too
    .nodeVal((n) => 1 + (n.src.degree || 0)).nodeRelSize(2.4).nodeResolution(14).nodeOpacity(0.95)
    .nodeColor((n) => (!focusSet.size || focusSet.has(n.id) ? (n.src.gone ? hexA(nodeColor(n.src), 0.4) : nodeColor(n.src)) : hexA(nodeColor(n.src), 0.12)))
    .nodeVisibility((n) => shownAt(n.src))
    .linkVisibility((l) => shownAt(l.source.src || {}) && shownAt(l.target.src || {}) && (linkLit(l) || l.keep || same(l) || linkW(l) >= 0.2))
    .linkColor((l) => (linkLit(l) ? tok("--accent") : same(l) ? hexA(nodeColor(l.source.src), 0.45) : tok("--link-cross")))
    .linkWidth((l) => (linkLit(l) ? 1.2 : 0)).linkOpacity(0.5)
    .linkDirectionalParticles(particles).linkDirectionalParticleWidth(1.8).linkDirectionalParticleSpeed(0.006)
    .linkDirectionalParticleColor(() => tok("--accent"))
    .nodeThreeObjectExtend(true).nodeThreeObject(node3d)
    .cooldownTicks(200)
    .onEngineStop(() => { if (!fly3d.done) { fly3d.done = true; selected ? fly3d(selected) : G3.zoomToFit(600, 40); } }) // the first layout, then the view
    .onNodeHover((n) => {
      if (touch) return;
      hover = n ? n.id : null; if (!selected) setFocus(hover);
      box.style.cursor = n ? "pointer" : "";
    })
    .onNodeClick((n) => (n.src.gone ? ghostSays(n.src) : selected === n.id ? go(noteRoute(n.id)) : openNote(n.id)))
    .onBackgroundClick(() => deselect());
  G3.d3Force("charge").strength(-50);
  // a weak pull to the middle: without it loose groups drift off and the view never fits (2D has its island anchors)
  let g3n = [];
  G3.d3Force("middle", Object.assign((alpha) => { for (const n of g3n) { n.vx -= n.x * 0.03 * alpha; n.vy -= n.y * 0.03 * alpha; n.vz -= n.z * 0.03 * alpha; } },
    { initialize: (ns) => { g3n = ns; } }));
  G3.renderer().setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  new ResizeObserver(() => { const w = $("graph").clientWidth, h = $("graph").clientHeight; if (w && h) { G3.width(w).height(h); wake3d(1000); } }).observe($("graph"));
  // Idle orbit (as in brain-graph): after a few seconds without input the camera slowly circles; any input stops it.
  const controls = G3.controls(), idle = () => {
    clearTimeout(idle.t);
    controls.autoRotate = false;
    wake3d(20000);
    if (!calm) idle.t = setTimeout(() => {
      controls.autoRotate = true;
      wake3d(20000);
    }, 6000);
  };
  controls.autoRotateSpeed = 0.5;
  for (const ev of ["pointerdown", "wheel", "pointermove", "touchstart"]) box.addEventListener(ev, idle, { passive: true });
  idle();
  set3dData();
}

function dispose3d(obj) {
  if (!obj) return;
  obj.traverse((child) => {
    if (child.__dispose) child.__dispose();
    if (child.geometry) child.geometry.dispose();
    if (child.material) {
      if (child.material.map && child.material.map !== glowTex) child.material.map.dispose();
      child.material.dispose();
    }
  });
}

// 3D look: a soft glow sprite behind every note (one white texture, tinted per note) and the names of the best-connected
// notes as text sprites. Drawn on a canvas, so a title is never HTML.
let glowTex = null;
function glowTexture() {
  if (!glowTex) {
    const c = document.createElement("canvas"); c.width = c.height = 128;
    const g = c.getContext("2d"), grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    grd.addColorStop(0, "rgba(255,255,255,1)"); grd.addColorStop(0.2, "rgba(255,255,255,0.5)"); grd.addColorStop(0.5, "rgba(255,255,255,0.12)"); grd.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = grd; g.fillRect(0, 0, 128, 128);
    glowTex = new THREE.CanvasTexture(c);
  }
  return glowTex;
}
const radius3d = (n) => Math.cbrt(1 + (n.src.degree || 0)) * 2.4; // 3d-force-graph's own sphere radius: cbrt(val) * relSize
function node3d(n) {
  if (n.__group) dispose3d(n.__group);
  const group = new THREE.Group(), dark = darkCanvas();
  const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTexture(), color: nodeColor(n.src) || "#888888", transparent: true,
    depthWrite: false, blending: dark ? THREE.AdditiveBlending : THREE.NormalBlending, opacity: dark ? 0.9 : 0.45 }));
  n.__gs = radius3d(n) * 7; glow.scale.set(n.__gs, n.__gs, 1); n.__glow = glow; group.add(glow);
  if ((n.src.degree || 0) >= labelMin) group.add(label3d(n.src.label, radius3d(n)));
  n.__group = group;
  return group;
}
function label3d(text, r) {
  const c = document.createElement("canvas"), g = c.getContext("2d"), font = `600 36px ${tok("--font")}`;
  g.font = font; c.width = Math.ceil(g.measureText(text).width) + 20; c.height = 52;
  g.font = font; g.textBaseline = "middle"; g.lineJoin = "round";
  g.lineWidth = 8; g.strokeStyle = tok("--canvas"); g.strokeText(text, 10, 26);
  g.fillStyle = tok("--ink"); g.fillText(text, 10, 26);
  const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
  const sp = new THREE.Sprite(mat);
  sp.scale.set(c.width / 3.2, c.height / 3.2, 1); sp.position.y = -(r + 10); sp.renderOrder = 2;
  sp.__dispose = () => { tex.dispose(); mat.dispose(); };
  return sp;
}
// A pulse in 3D: the note's glow breathes while it lasts.
function pulse3d() {
  if (pulse3d.on || !G3 || !map3d) return;
  pulse3d.on = true;
  wake3d(20000);
  const step = () => {
    let any = false;
    for (const n of G3.graphData().nodes) {
      if (!n.__glow) continue;
      const p = pulses.get(n.id), live = pulseLive(p);
      const k = live ? 1 + 0.9 * Math.abs(Math.sin((performance.now() - p.t0) / 260)) * (1 - (performance.now() - p.t0) / p.ms) : 1;
      n.__glow.scale.set(n.__gs * k, n.__gs * k, 1); any ||= live;
    }
    if (any && map3d) requestAnimationFrame(step); else pulse3d.on = false;
  };
  requestAnimationFrame(step);
}
$("map-3d").onclick = () => setMap3d(!map3d);
// The topic list over the map, on request; the choice stays per browser.
function showTopics(on) {
  document.body.classList.toggle("map-topics", on); $("map-topics").setAttribute("aria-pressed", String(on));
  localStorage.setItem("kn_map_topics", on ? "1" : "0");
}
showTopics(localStorage.getItem("kn_map_topics") === "1");
$("map-topics").onclick = () => showTopics(!document.body.classList.contains("map-topics"));
$("map-replay").onclick = () => (replay ? stopReplay() : startReplay());

// ---------------------------------------------------------------- side panel controls
// The project of the knowledge views: a picker in the sidebar and in the phone's More sheet, with its note count.
// "Several…" opens a tick per project, with All, None and Only; a pick of several shows as one more option.
function renderProjects() {
  const several = picks && picks.size !== 1 ? [project] : [];
  for (const box of document.querySelectorAll(".project-pick")) {
    box.replaceChildren(...[...PROJECTS, ALL, ...several, PICK_SEVERAL].map((p) => {
      const n = data.nodes.filter((x) => p === ALL || (p === project ? picked(x.project) : x.project === p)).length;
      const o = el("option", null, p === PICK_SEVERAL ? "Several…" : `${p === ALL ? "All projects" : several[0] === p ? pickName() : shown(p)} · ${n}`);
      o.value = p; return o;
    }));
    box.value = project;
    box.onchange = () => { if (box.value === PICK_SEVERAL) { box.value = project; pickSeveral(); } else switchProject(box.value); };
  }
}
const PICK_SEVERAL = "\nseveral"; // no project name has a newline
// A list of projects as the stored pick: all of them is ALL, one its name, several "a,b", none ",".
const pickValue = (ps) => ps.length === PROJECTS.length ? ALL : ps.length === 1 ? ps[0] : ps.join(",") || ",";
function pickSeveral() {
  const d = el("dialog", "pick-several"), list = el("ul", "pick-list"), top = el("div", "actions"), bottom = el("div", "actions");
  d.setAttribute("aria-label", "Projects");
  const set = (ps) => { switchProject(pickValue(PROJECTS.filter((p) => ps.includes(p)))); paint(); };
  const paint = () => list.replaceChildren(...PROJECTS.map((p) => {
    const li = el("li"), lab = el("label"), box = el("input"), only = el("button", "btn small quiet", "Only");
    box.type = "checkbox"; box.checked = picked(p);
    box.onchange = () => set(PROJECTS.filter((q) => (q === p ? box.checked : picked(q))));
    only.type = "button"; only.title = `Only ${shown(p)}`; only.onclick = () => set([p]);
    lab.append(box, ` ${shown(p)} · ${data.nodes.filter((x) => x.project === p).length}`); li.append(lab, only); return li;
  }));
  const all = el("button", "btn small", "All"), none = el("button", "btn small", "None"), done = el("button", "btn primary", "Done");
  all.type = none.type = done.type = "button";
  all.onclick = () => set(PROJECTS); none.onclick = () => set([]); done.onclick = () => d.close();
  d.onclose = () => d.remove();
  paint(); top.append(all, none); bottom.append(done);
  d.append(el("h2", null, "Projects"), top, list, bottom);
  document.body.append(d); d.showModal();
}

function switchProject(p, refit = true) {
  releaseFocus({ instant: true }); stopReplay();
  if (p === project || (p !== ALL && ![...navPicks(p)].every((x) => PROJECTS.includes(x)))) return;
  project = p; picks = navPicks(p); navProject.set(p);
  setAgentProject(allMode() ? "" : p); // one project picker for every view
  $("new-memo").hidden = !me.writable.includes(p);
  $("add-menu").hidden = $("new-memo").hidden && $("ingest-open").hidden;
  selected = null; setFocus(null);
  indexGraph(); renderProjects(); renderTopics(); applyView(refit);
  // a note of the old project would stay in the URL, so a refresh would jump back to it: show the new project's home
  if (current && !allMode() && projectOf(current.path) !== p) deselect();
  else if (!current) homeView();
  if (!$("results").hidden) runSearch();
  loadLint(!$("tab-lint").hidden);
  loadRecent();
  paintHead();
  filesProject(); // Machines > Files lists the new project's folders
}

// Every topic of the project with its notes, biggest first; the topic of the open note is unfolded. On the map,
// unfolding a topic highlights its island.
// The project's topics, biggest first: [[topic, notes]]. Shared by the topic list and the overview on the home view.
function topicGroups() {
  const groups = new Map(), ns = data.nodes.filter(nodeIn);
  const own = new Set(ns.filter((n) => !n.id.startsWith("claude/")).map((n) => n.cluster)); // this project's topics
  for (const n of ns) {
    // A shared skill whose topic is another project's heading (a heading of a guest project) goes under Skills.
    const t = allMode() ? n.project : n.id.startsWith("claude/") && !own.has(n.cluster) ? "Skills" : n.cluster || (n.group === "doc" ? "Docs" : "Other");
    (groups.get(t) || groups.set(t, []).get(t)).push(n);
  }
  return [...groups].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
}

function renderTopics() {
  const open = new Set([...$("topics").querySelectorAll("details[open]")].map((d) => d.dataset.topic));
  const sel = byId.get(selected);
  $("topics").replaceChildren(...topicGroups().map(([topic, ns]) => {
    const d = el("details", "topic"); d.dataset.topic = topic;
    d.open = open.has(topic) || (sel && (sel.cluster || "") === topic);
    const s = el("summary"), dot = el("i"); dot.style.background = clusterColor.get(topic) || tok("--g-doc");
    s.append(dot, topic, el("span", "n", String(ns.length)));
    const list = el("ol", "hits");
    for (const n of ns.sort((a, b) => a.label.localeCompare(b.label))) {
      const li = hitItem(noteName(n.id).title, "", () => openNote(n.id));
      li.firstChild.dataset.id = n.id;
      if (n.id === selected) li.firstChild.classList.add("active");
      list.append(li);
    }
    s.onclick = () => { if (!d.open && mapOn) highlight(ns.map((n) => n.id)); }; // fires before the toggle: about to open
    d.append(s, list); return d;
  }));
  if (!$("topics").childNodes.length) $("topics").append(emptyState("No notes in this project yet: agents write memos here as they work.",
    ...(me.writable?.includes(project) ? [actionButton("New memo", () => $("new-memo").click())] : [])));
}

// The middle column shows the note, or the map of the project instead.
// The Map view (#/map) and the Notes view share the lists and the right column; shell.js switches between them.
function setMap(on) {
  mapOn = on;
  $("graph").hidden = !on; $("note").hidden = on;
  if (current) renderSide();
  if (Graph) Graph.linkDirectionalParticles(Graph.linkDirectionalParticles());
  syncAnim();
  if (!on) { stopReplay(); return; }
  if ((localStorage.getItem("kn_map3d") === "1") !== map3d) setMap3d(!map3d); // the last pick of this browser
  requestAnimationFrame(() => { // once the map has its size
    if (!Graph) return; // the map data is still loading: loadGraph sizes it
    Graph.width($("graph").clientWidth).height($("graph").clientHeight);
    wakeMap(2500);
    if (selected) { zoomOwed = true; gather(selected, 1).then(() => { zoomOwed = false; zoomToFocus(); }); if (settling) zoomToFocus.again = true; } else Graph.zoomToFit(400, fitPad());
  });
}
addEventListener("kn-theme", themeChanged); // the viewer switched the theme (nav.js)

// ---------------------------------------------------------------- note preview, deep links and editor
function splitFront(text) {
  const m = text.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!m) return { meta: {}, body: text };
  const meta = {};
  for (const line of m[1].split("\n")) { const k = line.match(/^\s*(name|description|type|title|source):\s*(.*)$/); if (k) meta[k[1]] = k[2].replace(/^"|"$/g, ""); }
  return { meta, body: text.slice(m[0].length) };
}

// One date format for every view: relative under a day ("21 h ago"), else "1 Nov 2026" (no day/month order to
// guess); a time of day in the browser's own locale.
const fmtDay = (sec) => new Date(sec * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
const fmtClock = (sec) => new Date(sec * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
const fmtTime = (sec) => `${fmtDay(sec)}, ${fmtClock(sec)}`;
const fmtIso = (iso) => (/^\d{4}-\d\d-\d\d$/.test(iso || "") ? fmtDay(Date.parse(`${iso}T12:00`) / 1000) : iso); // a due date "2026-10-21"
const age = (sec) => { const d = Date.now() / 1000 - sec; return d < 90 ? "" : d < 3600 ? `${Math.round(d / 60)} min` : d < 86400 ? `${Math.round(d / 3600)} h` : null; };
const ago = (sec) => { const a = age(sec); return a === "" ? "just now" : a ? `${a} ago` : fmtDay(sec); };
const offlineFor = (sec) => { const a = age(sec); return a ? `offline ${a}` : a === "" ? "offline just now" : `offline since ${fmtDay(sec)}`; };

// A note's name for lists and headings: its title, never a slug or file name; skills and archive files are marked.
function noteName(path) {
  const n = byId.get(path), file = path.split("/").pop(), skill = path.startsWith("claude/skills/");
  const archive = /(^|\/)(archive|archief|old|history)\//.test(path) || /_archi(ve|ef)\.md$/.test(file);
  const words = (t) => { t = t.replace(/[_-]+/g, " ").trim(); return t.charAt(0).toUpperCase() + t.slice(1); };
  const title = file === "MEMORY.md" ? `Index of ${shown(projectOf(path))}` : n && !skill ? n.label
    : words(skill ? path.split("/")[2] : file.replace(/\.md$/, "").replace(/_archi(ve|ef)$/, "").replace(/^(project|feedback|reference|user|infra|incident)_/, ""));
  return { title, file, kind: skill ? "skill" : archive ? "archive" : "" };
}
// The name as a list line: title, a chip for a skill or archive file, the file name small and secondary.
function nameEl(path, cls = "hit-path") {
  const { title, file, kind } = noteName(path), s = el("span", cls, title);
  if (kind) s.append(" ", el("span", "chip name-kind", kind));
  if (kind !== "skill" && !title.toLowerCase().includes(file.replace(/\.md$/, "").toLowerCase())) s.append(" ", el("small", "name-file", file));
  return s;
}
// Memo text in a list line: `code` as <code> (text nodes only, never innerHTML), **bold** markers dropped.
function inlineCode(text, cls = "hit-text") {
  const span = el("span", cls);
  String(text).replace(/\*\*/g, "").split(/(`[^`\n]+`)/).forEach((part, i) => span.append(i % 2 ? el("code", null, part.slice(1, -1)) : part));
  return span;
}

// Clicking empty canvas clears the selection and the note in the URL, so a refresh opens nothing.
function deselect({ push = true } = {}) {
  if (editing) return; // keep unsaved edits on screen
  selected = null; current = null; selectFlowUntil = 0; setFocus(null); animateHover();
  // other views have no note in their route; go() so a phone's note page and the search page give way to the list
  if (push && (shellRoute.view === "notes" || shellRoute.view === "map") && location.hash !== (mapOn ? "#/map" : "#/notes")) go(mapOn ? "#/map" : "#/notes");
  $("note").replaceChildren(el("div", "note-empty muted", "Pick a note from the list or search."));
  $("side").replaceChildren(); markActive();
  homeView();
  const back = releaseFocus(); homeTween = tweenId;
  back.then(() => { if (!selected && mapOn) (map3d && G3 ? G3 : Graph).zoomToFit(600, fitPad()); }); // home, then the overview
}

// With nothing selected the middle shows the project's MEMORY.md: the user's own table of contents.
let homeNote = null;
async function homeView() {
  const p = project, path = `${MEMORY_ROOT[p]}/MEMORY.md`;
  let note = { text: "" }; // All: no one index, the cards come from the projects themselves
  if (!allMode()) try { note = await api(`/ui/api/note?path=${encodeURIComponent(path)}`); } catch { return; } // no index yet
  if (current || editing || p !== project) return; // the user opened something or switched meanwhile
  homeNote = note;
  if (!data.nodes.length) return; // the map data is still loading: its arrival draws the home view
  const body = splitFront(note.text).body, groups = topicGroups();
  if (!allMode() && (!/^## /m.test(body) || !groups.length)) { // no headings yet: the index as it is
    const md = el("div", "md"); md.innerHTML = renderMd(body);
    document.body.classList.remove("home"); // this overview keeps its right column
    homeRecent($("side"));
    $("note").replaceChildren(el("div", "note-empty muted", "Pick a note from the list or search. Index of this project:"), md);
    return;
  }
  // An overview: one card per topic with its best-connected notes and their index hook, the rest one click away.
  const hooks = new Map([...body.matchAll(/^- \[[^\]]+\]\(([^)]+\.md)\)\s*[—–-]\s*(.+)$/gm)].map((m) => [`${MEMORY_ROOT[p]}/${m[1]}`, m[2]]));
  const notes = groups.reduce((a, [, ns]) => a + ns.length, 0);
  const head = el("header", "home-head");
  head.append(el("h2", null, allMode() ? pickName().replace(/^./, (c) => c.toUpperCase()) : p), el("p", "muted", allMode()
    ? `${notes} notes in ${groups.length} projects. The map (M) shows how they link to each other.`
    : `${notes} notes in ${groups.length} topics. Pick a note, search with /, or open the map with M.`));
  const grid = el("div", "home-grid");
  for (const [topic, ns] of groups) {
    const card = el("section", "home-card"), h = el("h3"), dot = el("i");
    dot.style.background = clusterColor.get(topic) || tok("--g-doc");
    h.append(dot, topic, el("span", "n", String(ns.length)));
    const list = el("ol", "hits"), ranked = [...ns].sort((a, b) => (b.degree || 0) - (a.degree || 0) || a.label.localeCompare(b.label));
    const show = (k) => list.replaceChildren(...ranked.slice(0, k).map((n) => hitItem(noteName(n.id).title, inlineCode(hooks.get(n.id) || n.desc || ""), () => openNote(n.id))));
    show(5); card.append(h, list);
    if (ns.length > 5) {
      const more = el("button", "btn small", `All ${ns.length}`);
      more.onclick = () => { show(ns.length); more.remove(); }; // unfold in place: works on a phone too, where the list is hidden
      card.append(more);
    }
    grid.append(card);
  }
  const recent = el("section", "home-recent"); homeRecent(recent);
  $("note").replaceChildren(head, recent, grid);
  document.body.classList.add("home"); // phone: the cards replace the topic list
}

// On the home view, above the topics: what changed lately, with a way to what lint asks for.
async function homeRecent(target) {
  const p = project;
  let res; try { res = await api(`/ui/api/recent?project=${p}`); } catch { return; }
  if (current || p !== project) return;
  const list = el("ol", "hits"), top = el("div", "home-recent-head");
  for (const f of res.files.slice(0, 6)) {
    list.append(hitItem(nameEl(f.path), `${ago(f.mtime)} · ${f.session ? f.session.device : f.by === "VM" ? "on the VM" : writer(f.by)}`, () => openNote(f.path)));
  }
  if (!res.files.length) list.append(el("li", null, emptyState("No changes yet: edits to memos and docs show here.")));
  const all = el("button", "btn small", "All changes"); all.onclick = () => go("#/changes");
  top.append(el("h3", null, "Recently changed"), all);
  const lint = $("lint-count").textContent;
  if (lint) {
    const b = el("button", "btn small", `${lint} health finding${lint === "1" ? "" : "s"}`); b.onclick = () => go("#/health");
    top.append(b);
  }
  target.replaceChildren(top, list);
}

// The open note is marked in the topic list, and its topic unfolded.
function markActive() {
  for (const b of $("topics").querySelectorAll("button[data-id]")) {
    b.classList.toggle("active", b.dataset.id === selected);
    if (b.dataset.id === selected) b.closest("details").open = true;
  }
}

// Unsaved: the editor's text differs from the note it started from.
const dirty = () => editing && $("note").querySelector("textarea.editor")?.value !== current?.text;
const keepEdits = () => dirty() && !confirm("Discard your unsaved changes?");

let openNoteSeq = 0;
// Returns false when nothing was opened (the user kept their edits, or the note could not be read).
async function openNote(id, { push = true, find = null } = {}) {
  if (keepEdits()) return false;
  editing = false; historyOpen = false;
  const seq = ++openNoteSeq;
  let note;
  try { note = await api(`/ui/api/note?path=${encodeURIComponent(id)}`); } catch (e) {
    if (seq !== openNoteSeq) return false;
    $("note").textContent = e.message; return false;
  }
  if (seq !== openNoteSeq) return false;
  const p = projectOf(note.path);
  if (p !== "shared" && p !== project && !allMode()) switchProject(p, false); // the All map stays the All map
  current = note; document.body.classList.remove("home");
  if (push) { // its route; from another view (a task, a change, a lint finding) this shows the Notes view
    pushHash(noteRoute(note.path, mapOn ? "map" : "notes"));
    applyRoute(); // also within Notes: a phone swaps the list for the note
  }
  selected = byId.has(note.path) ? note.path : null;
  if (selected) selectFlowUntil = performance.now() + 4000;
  setFocus(selected); markActive();
  if (Graph) { animateHover(selected ? 4000 : 400); wakeMap(4500); }
  if (map3d && mapOn && selected) fly3d(selected);
  const n = byId.get(note.path);
  if (mapOn && n && n.x != null) {
    loadGraph.fitted = true; // the reader chose a view: no overview fit after the layout settles
    clearTimeout(hoverTimer);
    zoomOwed = true;
    gather(n.id, 1).then(() => { if (selected === n.id) { zoomOwed = false; zoomToFocus(); } });
    if (settling) zoomToFocus.again = true; // nodes still move: fit once more when they stop
  } else if (Graph) releaseFocus({ instant: !mapOn }); // no ring of the previous note
  renderNote();
  if (find) markInNote(find);
  else if (matchMedia("(max-width: 699.98px)").matches && push) window.scrollTo(0, 0); // phone: the note replaced the list
  return true;
}

// Scroll the note to the first place where a search word occurs and mark it, so a hit lands on its line.
function markInNote(words) {
  const md = $("note").querySelector(".md"); if (!md) return;
  const walk = document.createTreeWalker(md, NodeFilter.SHOW_TEXT);
  for (let t = walk.nextNode(); t; t = walk.nextNode()) {
    const low = fold(t.data), w = words.find((x) => low.includes(x));
    if (!w) continue;
    const at = low.indexOf(w), range = document.createRange();
    range.setStart(t, at); range.setEnd(t, at + w.length);
    const m = document.createElement("mark"); range.surroundContents(m);
    m.scrollIntoView({ block: "center", behavior: calm ? "auto" : "smooth" });
    return;
  }
}

let historyOpen = false; // the History view replaced the note: live updates leave it alone
function renderNote() {
  historyOpen = false;
  const note = current, n = byId.get(note.path);
  const { meta, body } = splitFront(note.text);
  // [[slug]] -> internal link to that memo, when it exists
  const linked = body.replace(/\[\[([a-z0-9][a-z0-9_-]*)\]\]/g, (all, slug) => {
    const target = slugToId.get(slug.replace(/-/g, "_"));
    return target ? `[${slug}](#${encPath(target)})` : all;
  });
  const pane = $("note"); pane.replaceChildren();
  const head = el("div", "note-head");
  head.append(el("h2", null, noteName(note.path).title));
  if (noteName(note.path).kind) head.append(el("span", "chip name-kind", noteName(note.path).kind));
  if (n) { const pill = el("span", "pill topic", n.cluster || n.group); pill.style.setProperty("--c", nodeColor(n)); head.append(pill); }
  const tools = el("div", "note-tools");
  const copy = el("button", "btn small", "Copy link");
  copy.onclick = () => navigator.clipboard?.writeText(`${location.origin}/ui${noteRoute(note.path)}`).then(() => { copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy link"), 1500); });
  tools.append(copy);
  if (note.writable) {
    const edit = el("button", "btn small", touch ? "Edit" : "Edit ({key})"); edit.onclick = startEdit; tools.append(edit);
    if (!touch) { edit.dataset.shortcut = "edit"; keyLabels(tools); }
  }
  const hist = el("button", "btn small", "History"); hist.onclick = () => showHistory(); tools.append(hist);
  pane.append(head, tools, el("div", "note-path", `${note.path} · version ${note.version}`));
  if (meta.description) pane.append(el("p", "note-desc", meta.description));
  const md = el("div", "md"); md.innerHTML = renderMd(linked); pane.append(md);
  pane.scrollTop = 0;
  renderSide();
}

// Right column: what the open note connects to, one list. Related notes (the relevance model: links, shared sources,
// shared neighbours, meaning) come first with the reason; linked notes the model did not rank follow. On the map the
// column also carries the note's title and a button to read it.
function renderSide() {
  const note = current, n = byId.get(note.path), side = $("side");
  side.replaceChildren();
  if (mapOn) {
    const card = el("div", "map-card");
    card.append(el("h2", null, noteName(note.path).title));
    const desc = splitFront(note.text).meta.description; if (desc) card.append(el("p", "note-desc", desc));
    const read = el("button", "btn small primary", "Read note"); read.onclick = () => go(noteRoute(note.path)); card.append(read);
    side.append(card);
  }
  const box = el("div", "connected"); side.append(box);
  const draw = (related) => {
    if (current !== note) return;
    const seen = new Set(related.map((x) => x.id));
    const linked = [...(neighbors.get(note.path) || [])].filter((x) => !seen.has(x)).map((x) => byId.get(x)).filter(Boolean)
      .sort((a, b) => a.label.localeCompare(b.label)).map((b) => ({ id: b.id, label: b.label, project: b.project, why: "linked" }));
    const all = [...related, ...linked];
    box.replaceChildren(el("h3", null, all.length ? `Connected (${all.length})` : "Not connected to other notes yet"));
    for (const x of all) {
      const a = el("a"); a.href = `#${encPath(x.id)}`;
      const why = x.why.replace("shared neighbours", "neighbours").replace(/similar meaning \(([\d.]+)\)/, "similar").replace(/, /g, " · ");
      a.append(noteName(x.id).title, el("span", "why", `${x.project !== project && x.project !== "shared" ? x.project + " · " : ""}${why}`));
      box.append(a);
    }
  };
  if (note.related) return draw(note.related);
  draw([]); // links at once, the ranked list when it arrives
  api(`/ui/api/related?path=${encodeURIComponent(note.path)}`).then((r) => { note.related = r.related; draw(r.related); }).catch(() => {});
}
$("side").addEventListener("click", (e) => {
  const a = e.target.closest("a[href^='#']"); if (!a) return;
  e.preventDefault(); openNote(decode(a.getAttribute("href").slice(1)));
});

// The VM's Claude Code sessions reach the server with the "vm-sessions" token (also the session-log token), so their
// edits carry that name; say who it is.
const writer = (name) => (name === "vm-sessions" ? "Claude on the VM" : name);

// Words that differ between two lines, by longest common subsequence over words and spaces: [[text, changed], ...]
// for each side. Long lines skip it (quadratic), and a pair that shares almost nothing stays a plain whole-line change.
function wordDiff(a, b) {
  const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
  if (x.length * y.length > 250000) return null;
  const L = Array.from({ length: x.length + 1 }, () => new Uint16Array(y.length + 1));
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--)
    L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  if (L[0][0] * 2 < Math.min(x.length, y.length)) return null;
  const left = [], right = [], push = (side, t, ch) => { const last = side[side.length - 1];
    if (last && last[1] === ch) last[0] += t; else side.push([t, ch]); };
  let i = 0, j = 0;
  while (i < x.length || j < y.length) {
    if (i < x.length && j < y.length && x[i] === y[j]) { push(left, x[i], false); push(right, y[j], false); i++; j++; }
    else if (j < y.length && (i >= x.length || L[i][j + 1] >= L[i + 1][j])) push(right, y[j++], true);
    else push(left, x[i++], true);
  }
  return [left, right];
}

// A unified diff as a <pre>: removed and added lines as rows; where a block of removed lines is followed by as many
// added ones, the pairs show only the words that changed (an appended note no longer looks like a rewritten line).
// With `comment(at, n, span)` each line inside a hunk is focusable and calls it on a click or Enter.
function diffView(text, { comment } = {}) {
  const pre = el("pre", "diff"), lines = text.split("\n");
  const kind = (l) => (l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : null);
  const row = (cls, parts) => { const span = el("span", cls); // .add/.del are blocks: their own line break would add a row
    for (const [t, ch] of parts) span.append(ch ? el("mark", null, t) : document.createTextNode(t));
    return span; };
  for (let i = 0; i < lines.length;) {
    if (kind(lines[i]) !== "del") { const k = kind(lines[i]); pre.append(el("span", k, k ? lines[i] : lines[i] + "\n")); i++; continue; }
    let d = i; while (d < lines.length && kind(lines[d]) === "del") d++;
    let a = d; while (a < lines.length && kind(lines[a]) === "add") a++;
    const dels = lines.slice(i, d), adds = lines.slice(d, a), pairs = dels.length === adds.length ? dels.map((l, n) => wordDiff(l.slice(1), adds[n].slice(1))) : [];
    dels.forEach((l, n) => pre.append(pairs[n] ? row("del", [["-", false], ...pairs[n][0]]) : el("span", "del", l)));
    adds.forEach((l, n) => pre.append(pairs[n] ? row("add", [["+", false], ...pairs[n][1]]) : el("span", "add", l)));
    i = a;
  }
  if (comment) diffLines(lines).forEach((at, n) => { // one span per line: child n is line n
    if (!at) return;
    const span = pre.children[n];
    span.tabIndex = 0; span.classList.add("diff-at"); span.dataset.at = at; span.title = `${at} · comment on this line`;
    span.onclick = () => comment(at, n, span);
    span.onkeydown = (e) => { if (e.key === "Enter" && e.target === span) { e.preventDefault(); comment(at, n, span); } };
  });
  return pre;
}
// Where each line of a unified diff sits: "path:12" on the new side, "path:old line 7" for a removed line, null outside
// a hunk (the stat, headers, untracked names, the cut note).
function diffLines(lines) {
  let path = "", old = 0, now = 0, hunk = false;
  return lines.map((l) => {
    const h = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(l);
    if (h) { [old, now, hunk] = [+h[1], +h[2], !!path]; return null; }
    if (hunk && l[0] === "+") return `${path}:${now++}`;
    if (hunk && l[0] === "-") return `${path}:old line ${old++}`;
    if (hunk && l[0] === " ") { old++; return `${path}:${now++}`; }
    if (hunk && l[0] === "\\") return null; // "\ No newline at end of file"
    hunk = false;
    if (l.startsWith("diff --git ")) path = "";
    else if (l.startsWith("--- ") && l !== "--- /dev/null") path = l.slice(4).replace(/^a\//, "");
    else if (l.startsWith("+++ ") && l !== "+++ /dev/null") path = l.slice(4).replace(/^b\//, ""); // a deleted file keeps its --- name
    return null;
  });
}

async function showHistory(open = null) {
  // Every version of this note (who, when), its diff against the version before, and restore. `open` = a version id
  // to show at once (a click in Changes).
  const note = current, pane = $("note");
  const res = await api(`/ui/api/history?path=${encodeURIComponent(note.path)}`);
  if (current !== note || editing) return;
  historyOpen = true;
  pane.replaceChildren();
  const head = el("div", "note-head"); head.append(el("h2", null, `History of ${noteName(note.path).title}`));
  const back = el("button", "btn small", "Back to the note"); back.onclick = renderNote;
  pane.append(head, el("div", "note-tools"), el("div", "note-path", note.path));
  pane.querySelector(".note-tools").append(back);
  if (!res.versions.length) { pane.append(el("p", "muted", "No earlier versions yet: history starts at the first change.")); return; }
  const list = el("ol", "hits"), view = el("div", "version");
  pane.append(view, list); // the chosen diff above the list, so it is in sight
  res.versions.forEach((v, i) => {
    const when = fmtTime(v.time);
    const li = hitItem(`${when}${i === 0 ? " (current)" : ""}`, `${v.by.endsWith("-removed") ? `removed (${v.by === "VM-removed" ? "on the VM" : `via ${v.by.slice(0, -8)}`}); the version below holds its last text` : v.by === "VM" ? "edited on the VM" : v.by === "before" ? (v.size ? "baseline" : "did not exist yet") : `via ${writer(v.by)}`} · ${v.size} bytes`, async () => {
      for (const b of list.querySelectorAll("button")) b.classList.remove("active");
      li.firstChild.classList.add("active");
      const ver = await api(`/ui/api/history?path=${encodeURIComponent(note.path)}&version=${encodeURIComponent(v.id)}`);
      view.replaceChildren(el("h3", "sub", "Changes in this version"));
      view.append(diffView(ver.diff || "(first version)"));
      view.scrollIntoView({ block: "start", behavior: calm ? "auto" : "smooth" });
      if (note.writable && i > 0) {
        const restore = el("button", "btn small", "Restore this version");
        restore.onclick = async () => {
          if (!confirm("Replace the current note with this version? The current text stays in the history.")) return;
          try {
            await api("/ui/api/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: note.path, text: ver.text, base_version: note.version }) });
            await openNote(note.path, { push: false }); refreshData();
          } catch (e) { toast(e.message); }
        };
        view.append(restore);
      }
    });
    list.append(li);
    if (v.id === open || (open && i === 0 && !res.versions.some((x) => x.id === open))) li.firstChild.click();
  });
}

function startEdit() {
  if (!current?.writable || editing) return; // a second E must not rebuild the editor over unsaved text
  editing = true;
  const pane = $("note"); pane.replaceChildren();
  const head = el("div", "note-head"); head.append(el("h2", null, `Editing ${noteName(current.path).title}`));
  const area = el("textarea", "editor"); area.value = current.text; area.spellcheck = false;
  area.setAttribute("aria-label", `Text of ${current.path}`);
  const err = el("p", "error"); err.hidden = true; err.setAttribute("role", "alert");
  const save = el("button", "btn primary", "Save ({key})"), cancel = el("button", "btn", "Cancel");
  save.dataset.shortcut = "save";
  const actions = el("div", "actions"); actions.append(cancel, save); keyLabels(actions);
  pane.append(head, el("div", "note-path", `${current.path} · based on version ${current.version}`), area, err, actions);
  area.focus(); area.setSelectionRange(0, 0); area.scrollTop = 0;
  cancel.onclick = () => { if (keepEdits()) return; editing = false; renderNote(); };
  save.onclick = async () => {
    save.disabled = true;
    try {
      await api("/ui/api/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: current.path, text: area.value, base_version: current.version }) });
      editing = false; await openNote(current.path, { push: false }); refreshData();
    } catch (e) {
      err.hidden = false; save.disabled = false;
      err.textContent = `${e.message}. Copy your text, reopen the note and apply your change again.`;
    }
  };
  area.addEventListener("keydown", (e) => {
    if (keyHit(e, "save")) { e.preventDefault(); save.click(); }
    if (e.key === "Escape" && !e.isComposing) { e.preventDefault(); e.stopPropagation(); cancel.click(); } // not on to the page's Escape
  });
}

// internal links in the preview: #<path>, [[slug]] links and relative x.md links
$("note").addEventListener("click", (e) => {
  const a = e.target.closest("a"); if (!a) return;
  const href = a.getAttribute("href") || "";
  if (href.startsWith("#")) { // a note path; an in-page anchor (#keuzes) has no target here, ids are stripped
    e.preventDefault();
    if (/^#[^#]+\/[^#]+$/.test(href)) openNote(decode(href.slice(1)));
    return;
  }
  const base = current || homeNote; // links in a note, or in the index shown when nothing is selected
  if (/^(?![a-z][\w+.-]*:)[^?#]+\.md(#.*)?$/i.test(href) && base) { // relative: x.md, archief/x.md, ../homelab/x.md
    e.preventDefault();
    openNote(decode(new URL(href.split("#")[0], `http://x/${base.path}`).pathname.slice(1)));
    return;
  }
  if (/^https?:/.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
});
window.addEventListener("beforeunload", (e) => { if (editing) e.preventDefault(); });

// ---------------------------------------------------------------- new memo
$("new-memo").onclick = async () => {
  $("add-menu").open = false;
  $("new-where").textContent = `In ${MEMORY_ROOT[project]}/ (project ${shown(project)}), with a line in its MEMORY.md under the heading you pick.`;
  $("new-error").hidden = true; $("new-form").reset();
  let headings = [];
  try { headings = (await api(`/ui/api/note?path=${encodeURIComponent(MEMORY_ROOT[project] + "/MEMORY.md")}`)).text.split("\n").filter((l) => l.startsWith("## ")).map((l) => l.slice(3).trim()); } catch { /* no index yet */ }
  $("new-heading").replaceChildren(...headings.map((h) => el("option", null, h)), el("option", null, "(end of the index)"));
  $("new-dialog").showModal(); $("new-title").focus();
};
$("new-cancel").onclick = () => $("new-dialog").close();
$("new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const type = $("new-type").value, title = $("new-title").value.trim(), desc = $("new-desc").value.trim();
  const slug = title.toLowerCase().normalize("NFKD").replace(/\p{M}/gu, "").replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 50);
  const file = `${type}_${slug}.md`, path = `${MEMORY_ROOT[project]}/${file}`;
  const fail = (msg) => { $("new-error").hidden = false; $("new-error").textContent = msg; };
  if (!slug) return fail("The title needs letters or digits: they make the file name.");
  if (byId.has(path)) return fail(`${file} already exists: open it and update it instead, or pick another title.`);
  const text = `---\nname: ${type}_${slug}\ndescription: ${JSON.stringify(desc)}\nmetadata:\n  type: ${type}\n---\n\n${desc}\n\n${(me.sections || ["## Status", "## Decisions"]).join("\n\n")}\n`; // the house-rules layout, in the server's language
  const heading = $("new-heading").selectedIndex < $("new-heading").options.length - 1 ? $("new-heading").value : "";
  try {
    const res = await api("/ui/api/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, text, base_version: "", index_line: `- [${title}](${file}) — ${desc}`, heading }) });
    $("new-dialog").close(); await refreshData();
    if (await openNote(path)) {
      startEdit();
      if (res.hint) { const w = el("p", "error live-warn", res.hint); w.setAttribute("role", "status"); $("note").querySelector(".actions")?.before(w); }
    }
  } catch (err) { fail(err.message); }
});

// ---------------------------------------------------------------- search and shortcuts
// The search page (#/notes?q=…, shell.js shows it in the middle): note titles of every project (instant, from the
// graph), then full text and meaning in this project. The palette (palette.js) searches the same way.
let searchTimer, searchSeq = 0;
async function runSearch() {
  const q = $("q").value.trim(), seq = ++searchSeq;
  if (!q) { $("hits").replaceChildren(); $("sem").replaceChildren(); $("titles").replaceChildren(); $("hits-empty").hidden = true; return; }
  $("sem").replaceChildren(); $("sem-head").hidden = true;
  if (!$("hits").childNodes.length) $("hits").replaceChildren(el("li", null, loadingState()));
  const words = fold(q).split(/\s+/).filter(Boolean);
  const titles = data.nodes.filter((n) => words.every((w) => fold(`${n.label} ${n.id}`).includes(w)))
    .sort((a, b) => (inProject(b.project) - inProject(a.project)) || (b.degree - a.degree)).slice(0, 6);
  $("titles-head").hidden = !titles.length;
  $("titles").replaceChildren(...titles.map((n) => hitItem(noteName(n.id).title, `${shown(n.project)} · ${n.id}`, () => openNote(n.id))));
  let res; try { res = await api(`/ui/api/search?q=${encodeURIComponent(q)}&project=${project}`); } catch (e) { $("hits").textContent = e.message; return; }
  if (seq !== searchSeq) return; // a newer search is on its way
  $("hits-empty").hidden = res.notes.length > 0;
  if (!res.notes.length) $("hits-empty").textContent = "No note contains all of these words.";
  $("hits").replaceChildren(...res.notes.map((h) => {
    const li = el("li"), b = el("button", "row-link");
    const top = nameEl(h.path); if (h.count > h.hits.length) top.append(`  +${h.count - h.hits.length}`); b.append(top);
    b.append(el("span", "hit-sub muted", h.path));
    for (const x of h.hits) b.append(marked_(x.text, words));
    b.onclick = () => openNote(h.path, { find: words });
    li.append(b); return li;
  }));
  // Meaning-based results next to the exact matches: finds notes that use other words.
  let sem; try { sem = await api(`/ui/api/semantic?q=${encodeURIComponent(q)}&project=${project}`); } catch { return; }
  if (seq !== searchSeq) return;
  const seen = new Set(res.notes.map((h) => h.path));
  const extra = sem.hits.filter((h) => !seen.has(h.path) && seen.add(h.path)); // one row per note
  $("sem-head").hidden = false;
  $("sem-head").textContent = sem.hits.length ? "By meaning" : `By meaning: ${sem.status}`;
  $("sem").replaceChildren(...extra.map((h) => hitItem(nameEl(h.path), `${h.path}:${h.line} — ${h.text}`, () => openNote(h.path))));
}
// Lowercase without accents, one char for one char (é -> e), so "geinstalleerd" finds "geïnstalleerd" and indexes
// into the folded text still point into the original; the server folds the same way (server.py fold).
const fold = (s) => s.toLowerCase().replace(/[\u00C0-\u024F]/g, (c) => c.normalize("NFD")[0]);
// A search line as text with the query words in <mark>, built from text nodes (never innerHTML).
function marked_(text, words) {
  const span = el("span", "hit-text"), low = fold(text);
  let i = 0;
  while (i < text.length) {
    let at = -1, len = 0;
    for (const w of words) { const j = low.indexOf(w, i); if (j >= 0 && (at < 0 || j < at)) { at = j; len = w.length; } }
    if (at < 0) { span.append(text.slice(i)); break; }
    span.append(text.slice(i, at), el("mark", null, text.slice(at, at + len))); i = at + len;
  }
  return span;
}
function hitItem(top, sub, onclick) {
  const li = el("li"), b = el("button", "row-link");
  b.append(typeof top === "string" ? el("span", "hit-path", top) : top); if (sub) b.append(typeof sub === "string" ? el("span", "hit-text", sub) : sub);
  b.onclick = onclick; li.append(b); return li;
}
$("q").addEventListener("input", () => { // the query lives in the route, so a refresh or Back keeps it
  history.replaceState(history.state, "", `#/notes?q=${encodeURIComponent($("q").value)}`);
  clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 150);
});
$("q").addEventListener("keydown", (e) => { // Enter opens the first result
  if (e.key === "Enter") { e.preventDefault(); clearTimeout(searchTimer); runSearch().then(() => $("results").querySelector("button")?.click()); }
});

// Keys (KEY_ACTIONS, Settings): the palette from anywhere; Ctrl or Alt plus a key jumps (appKey). Without them: in a note Esc closes it, in a chat Esc goes back, and in the
// chats a key is text for the box, arrows on the grid go from card to card.
function appKey(e) {
  if (solo()) return; // a chat alone in its tab has no views to jump to
  if (bdOn() && (bdPartKey(e) || (keyHit(e, "arrows") && bdArrow(e)))) { e.preventDefault(); return; } // on the grid the digits are its parts, not the projects
  const kn = shellRoute.view === "notes" || shellRoute.view === "map", at = current ? `/${encPath(current.path)}` : "";
  const digit = keyHit(e, "digits") && keyCombo(e).at(-1);
  const act = keyHit(e, "rail") ? toggleRail
    : keyHit(e, "notes_search") || (e.key === "/" && e.altKey && keyOf("notes_search") === "Alt+/") ? () => openPalette(true) // some layouts need Shift for /
    : keyHit(e, "chats") ? () => go("#/chats")
    : keyHit(e, "new_chat") && !agentsOff() ? () => $("ck-new").onclick()
    : digit === "0" ? () => switchProject(ALL)
    : digit && PROJECTS[Number(digit) - 1] ? () => switchProject(PROJECTS[Number(digit) - 1])
    : keyHit(e, "map") ? () => go(mapOn ? `#/notes${at}` : `#/map${at}`)
    : keyHit(e, "edit") && kn && current?.writable && !editing ? startEdit : null;
  if (act) { e.preventDefault(); act(); }
}
document.addEventListener("keydown", (e) => {
  const on = document.activeElement, dialog = document.querySelector("dialog[open]"), typing = /input|textarea|select/i.test(on.tagName) || dialog;
  if (keyHit(e, "palette")) { e.preventDefault(); if (!document.querySelector("dialog[open]:not(#palette)")) openPalette(); return; }
  // A chat's box keeps what is typed; anywhere else (a note being edited) a jump could lose it.
  // On a Mac Option types characters (ñ, å, and [ or { on some layouts), so there the box keeps them.
  if (e.altKey || e.ctrlKey || e.metaKey) { if (!typing || (!dialog && on.matches(".ck-form textarea") && !(e.altKey && /Mac/.test(navigator.platform)))) appKey(e); return; }
  if (typing) return;
  const menu = document.querySelector("details.menu[open]"); // not any details: a topic stays unfolded in a hidden view
  if (e.key === "Escape") {
    if (shellRoute.view === "chats" && ck.view && !menu) { if (!solo()) store.set("kn_last_chat", ""); ckShow(null); } // an open menu takes Esc for itself; as its ×
    else if (shellRoute.view === "notes" || shellRoute.view === "map") deselect();
    return;
  }
  if (menu) return;
  if (bdOn() && !e.shiftKey && (on === document.body || on.closest("#bd")) && bdArrow(e)) { e.preventDefault(); return; }
  if (e.key.length !== 1 || (e.key === " " && on.closest("button, a, summary"))) return;
  // A key is text: moving the focus now makes the browser type this very key in the box.
  const chat = !bdOn() && shellRoute.view === "chats" && ck.cards.get(ck.view)?.root;
  const room = bdOn() ? bdCardNow() : chat && (on === document.body || chat.contains(on)) ? chat : null, box = room?.querySelector(".ck-form textarea");
  if (box?.offsetParent && !box.disabled) box.focus();
});

// ---------------------------------------------------------------- views: changes, lint, tasks, machines, ingest

// This pc: the knowledge companion on the viewer's own machine (plugin/hooks/companion.py). The tab shows only when it
// answers on 127.0.0.1; the page's CSP allows exactly that origin, and the companion allows this page's origin.
const COMPANION = "http://127.0.0.1:47321";
async function companion(path, body) {
  const r = await fetch(COMPANION + path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `companion: HTTP ${r.status}`);
  return data;
}
let localMachine = ""; // SHA-256 of the companion's machine id on this pc (never the id itself): its row says "This pc"
async function probeCompanion() {
  try { localMachine = (await companion("/local/api/hello")).machineHash || ""; } catch { return false; } // no companion on this pc: no tab
  for (const a of document.querySelectorAll(".nav-thispc")) a.hidden = false;
  return true;
}
async function loadThisPc() {
  const box = $("thispc-agents");
  let s;
  try { s = await companion("/local/api/state"); }
  catch (e) { box.replaceChildren(el("p", "error", `The companion did not answer: ${e.message}`)); return; }
  const state = (ok, text) => el("span", `cmp-state cmp-state--${ok ? "ok" : "warn"}`, text);
  box.replaceChildren(...s.agents.filter((a) => a.installed).map((a) => {
    const card = el("article", "cmp-agent"), head = el("div", "cmp-agent-head");
    head.append(el("span", "cmp-agent-mark", a.mark), el("h3", null, a.name), el("span", "cmp-ver", a.version ? `plugin ${a.version}` : "no plugin"));
    const facts = el("dl", "cmp-facts"), hooks = el("dd");
    hooks.append(state(a.hooksTrusted, a.hooksTrusted ? "Trusted" : "Not trusted"));
    if (!a.hooksTrusted) {
      const b = el("button", "cmp-btn cmp-btn--primary cmp-btn--small", "Trust hooks");
      b.type = "button";
      b.onclick = async () => { try { await companion("/local/api/trust", { agent: a.id }); } catch (e) { toast(e.message); } loadThisPc(); };
      hooks.append(" ", b);
    }
    facts.append(el("dt", null, "Hooks"), hooks, el("dt", null, "YOLO"), el("dd", null, a.yolo ? "on: woken tasks run without asking" : "off"));
    const list = el("ul", "cmp-sessions");
    for (const x of a.sessions) list.append(el("li", null, x.title));
    card.append(head, facts, a.sessions.length ? list : el("p", "cmp-sessions-empty", "No open sessions."),
      a.lastError ? el("p", "cmp-error", `Last hook error: ${a.lastError.text}`) : el("p", "cmp-error cmp-error--none", "No hook errors."));
    return card;
  }));
}

// Changes: newest first, per day; lines added/removed, who, and the question of the session that made the change.
// A click opens the diff. Changes since the last look at this tab are marked; the badge counts them meanwhile.
async function loadRecent(look = false) {
  if (look && !$("recent").childNodes.length) $("recent").replaceChildren(loadingState());
  const p = project, res = await api(`/ui/api/recent?project=${p}`);
  if (p !== project) return; // switched meanwhile: that project's own call follows
  const key = `kn_seen_${p}`, seen = Number(store.get(key)) || Date.now() / 1000;
  const fresh = res.files.filter((f) => f.mtime > seen).length;
  $("changes-count").textContent = fresh && !look ? fresh : "";
  if (!look && $("tab-changes").hidden) { if (!store.get(key)) store.set(key, String(seen)); return; }
  const box = $("recent"); box.replaceChildren();
  let day = "", list;
  for (const f of res.files) {
    const d = new Date(f.mtime * 1000).toDateString();
    if (d !== day) { // one card per day
      day = d;
      const today = new Date().toDateString(), yesterday = new Date(Date.now() - 864e5).toDateString();
      const card = el("section", "card"); list = el("ol", "hits");
      card.append(el("h3", "sub", d === today ? "Today" : d === yesterday ? "Yesterday" : fmtDay(f.mtime)), list);
      box.append(card);
    }
    const li = el("li"), b = el("button", "row-link"), top = nameEl(f.path);
    if (f.mtime > seen) top.append(el("span", "new", "new"));
    const meta = el("span", "hit-text");
    if (f.added != null) meta.append(el("span", "plus", `+${f.added}`), " ", el("span", "minus", `−${f.removed}`), " · ");
    meta.append(`${age(f.mtime) == null ? fmtClock(f.mtime) : ago(f.mtime)} · ${f.session ? f.session.device : f.by === "VM" ? "on the VM" : writer(f.by)}`);
    b.append(top, meta);
    if (f.session?.asked) b.append(el("span", "hit-text why", `“${f.session.asked}”`));
    b.title = f.path;
    b.onclick = async () => { if (await openNote(f.path) && f.version) { setMap(false); showHistory(f.version); } };
    li.append(b); list.append(li);
  }
  if (!res.files.length) box.append(emptyState(`No changes in ${p === ALL ? "any project" : pickName()} yet: every edit to a memo or doc shows here, newest first.`));
  store.set(key, String(Math.max(seen, ...res.files.map((f) => f.mtime))));
  loadTrash(p).catch((e) => box.append(el("p", "error", `Deleted: ${e.message}`)));
}
// Deleted: the copies knowledge_delete keeps, newest first; Restore puts one back where it was (never over a file
// that is there again: the server answers 409 with the reason).
let trashOpen = false;
async function loadTrash(p) {
  const res = await api("/ui/api/trash");
  if (p !== project || $("tab-changes").hidden) return;
  const items = res.items.filter((i) => picked(i.project)), card = el("details", "card trash");
  card.id = "trash"; card.hidden = !items.length; card.open = trashOpen; card.ontoggle = () => { trashOpen = card.open; };
  const sum = el("summary", "sub", "Deleted"); sum.append(el("span", "badge", String(items.length)));
  const list = el("ol", "hits");
  for (const i of items.slice(0, 100)) {
    const li = el("li", "task"), info = el("span", "trash-info");
    info.append(nameEl(i.path), el("span", "hit-text", `${i.path} · deleted ${ago(i.deleted)} · ${(i.size / 1024).toFixed(1)} kB`));
    const b = el("button", "btn small", "Restore"); b.type = "button";
    b.disabled = !i.can_restore;
    b.title = i.can_restore ? `Put ${i.path} back as it was` : i.exists ? "A file is at that path again: open both and merge by hand" : "Only the project's owner or an admin may restore a file";
    b.onclick = async () => {
      b.disabled = true;
      try { await post("/ui/api/trash/restore", { name: i.name }); toast(`Restored ${i.path}`, false); refreshData(); loadRecent(true); }
      catch (e) { toast(e.message); b.disabled = false; }
    };
    li.append(info, b); list.append(li);
  }
  card.append(sum, items.length ? list : emptyState("Nothing deleted."));
  $("trash")?.remove(); $("recent").prepend(card); // first and folded: found without scrolling past every day
}

function highlight(ids) {
  // Show a set of notes in the graph: dim the rest and zoom to them.
  focusSet = new Set(ids); refresh3d();
  const pts = ids.map((i) => byId.get(i)).filter((n) => n && n.x != null);
  if (pts.length) { animateHover(); Graph.zoomToFit(600, 80, (n) => focusSet.has(n.id)); }
}

async function loadLint(render = true) {
  if (!me.admin) { if (render) $("lint").replaceChildren(emptyState("Health lists what lint finds in every project: an admin's view.")); return; } // lint spans every project
  if (render && !$("lint").childNodes.length) $("lint").replaceChildren(loadingState());
  const res = await api("/ui/api/lint?v=2"), full = res.report || res, hints = res.hints || {};
  // checks in a group (token usage) are context, not problems: folded at the end and left out of the badge
  const grouped = new Map((res.groups || []).flatMap((g) => g.checks.map((c) => [c, g])));
  const report = Object.fromEntries(Object.entries(full).map(([k, v]) => [k, v.filter((i) => picked(lintProject(i)))]));
  const count = (r) => Object.entries(r).reduce((a, [c, v]) => a + (grouped.has(c) ? 0 : v.length), 0);
  const here = count(report), all = count(full);
  // Badge and tab show this project's findings; the tab points to the rest so findings elsewhere are not missed.
  $("lint-count").textContent = here || "";
  $("lint-count").title = `${here} in ${project}, ${all} in all projects`;
  if (!render) return;
  const box = $("lint"); box.replaceChildren();
  const folds = new Map(), rank = (c) => { const i = LINT_URGENT.indexOf(c); return i < 0 ? LINT_URGENT.length : i; };
  for (const [check, items] of Object.entries(report).sort((a, b) => rank(a[0]) - rank(b[0]) || b[1].length - a[1].length)) {
    if (!items.length) continue;
    const [title, fix] = hints[check] || LINT_INFO[check] || [check.replace(/_/g, " "), ""];
    const ul = el("ol", "hits");
    for (const item of items) { // "file.md: detail" -> the note's title, the detail under it
      const m = item.match(/^([\w./-]+\.md)(?::\s*|\s+)?(.*)$/);
      if (!m) { ul.append(el("li", "hit-text", item)); continue; }
      const id = m[1].includes("/") ? m[1] : `memory/${m[1]}`;
      const li = hitItem(nameEl(id), inlineCode(m[2].replace(/\bdue (\d{4}-\d\d-\d\d)/, (_, d) => `due ${fmtIso(d)}`)), () => openNote(id));
      li.firstChild.title = item; ul.append(li);
    }
    const card = el("section", "card"), h = el("h3", "sub", title);
    h.append(el("span", "badge", String(items.length)));
    card.append(h, ...(fix ? [el("p", "lint-fix muted", fix)] : []), ul);
    const g = grouped.get(check);
    if (!g) { box.append(card); continue; }
    if (!folds.has(g.id)) { const d = el("details", "card lint-group"); d.open = !g.collapsed; d.append(el("summary", null, g.title)); folds.set(g.id, d); }
    folds.get(g.id).append(card);
  }
  box.append(...folds.values());
  if (!here) box.append(el("p", "empty ok", `All clear: no findings in ${project === ALL ? "any project" : pickName()}.`));
  if (all > here) {
    const b = el("button", "btn small", `${all - here} in other projects`); b.onclick = () => switchProject(ALL);
    box.append(b);
  }
}
// Tasks: the task lines between agents in every project (or the one picked in the header), overdue first, then by due
// date, with where each runs and a "Start on <pc>" per pc that may take it; results handed back. Cancel turns an open
// line into a result for its requester; Remove takes a read result away (history keeps both). The badge counts the open lines
// of the project picked in the sidebar, the same number as the list's Open heading.
let agentProject = allMode() ? "" : project; // the Agents views' project: the sidebar's, "" = all (shell.js setAgentProject)
async function loadTasks(render = true) {
  const res = await api(render ? "/ui/api/tasks?hosts=1" : "/ui/api/tasks"), all = res.tasks.filter((t) => !t.done);
  // the badge follows the project picker, as the list does (the Health badge too); its title has both counts
  const inProject = res.tasks.filter((t) => picked(t.project)), spill = !!picks && !inProject.length && res.tasks.length > 0; // nothing here: show the work elsewhere
  const here = all.filter((t) => picked(t.project)), counted = spill ? all : here;
  $("tasks-count").textContent = counted.length || "";
  $("tasks-count").className = counted.some((t) => t.overdue) ? "badge warn" : "badge";
  $("tasks-count").title = picks ? `${here.length} open in ${pickName()}, ${all.length} in all projects` : `${all.length} open in all projects`;
  const tasks = spill ? res.tasks : inProject, open = tasks.filter((t) => !t.done), back = tasks.filter((t) => t.done);
  if (!render) return;
  const box = $("tasks"); box.replaceChildren();
  const elsewhere = all.length - open.length; // the badge counts every project: say what the picker hides
  const showAll = () => actionButton(`${elsewhere} in other projects: show`, () => switchProject(ALL), "btn small ck-elsewhere");
  // the state first: nothing here (and how much elsewhere), or the lists with a pointer to the other projects
  if (!tasks.length) box.append(emptyState(`No tasks${picks ? ` in ${pickName()}` : ""}: when an agent hands work to another in a memo, it shows here.`));
  else if (spill) box.append(emptyState(`No tasks in ${pickName()}. In your other projects:`));
  else if (elsewhere) box.append(showAll());
  // the workers of these tasks, from the Machines list: progress and Stop/Restart live with the task, not the pc
  const machines = await api("/ui/api/machines").then((r) => (Array.isArray(r) ? r : r.machines || [])).catch(() => []);
  machinesCache = machines;
  const workersOf = (t) => machines.flatMap((m) => Object.values(m.agents || {}).flatMap((ag) => (ag.workers || []).map((w) => ({ w, m }))))
    .filter(({ w }) => w.project === t.project && (w.id === t.id || w.id === t.id.replace(/-(cont|fix)$/i, "")));
  const devices = [...new Set(machines.filter((m) => m.own).flatMap((m) => m.tokens || []))].sort();
  const row = (t) => {
    const li = hitItem(`${t.id} · ${t.owner}`, inlineCode(t.text), () => openNote(t.path, { find: [t.id.toLowerCase()] }));
    const meta = el("span", "hit-text", [t.done ? "" : `${t.from || "?"} → ${t.owner}`, t.done ? "" : `start ${t.start}`,
      t.due && `due ${fmtIso(t.due)}`, t.done && t.since && `handed back ${ago(t.since)}`, t.claimed && "claimed", t.waiting_reason, t.runs, noteName(t.path).title,
      agentProject && !spill ? "" : shown(t.project)].filter(Boolean).join(" · "));
    if (t.legacy) meta.prepend(el("span", "badge", "legacy"), " ");
    if (t.stalled) meta.prepend(el("span", "new", "stalled"), " ");
    if (t.overdue) meta.prepend(el("span", "chip bad", "overdue"), " ");
    li.className = t.waiting_reason ? "task waiting" : "task"; li.firstChild.append(meta);
    li.firstChild.title = t.line + (t.legacy ? "\nLegacy line: no from/start, so it may start now." : "") +
      (t.author ? `\nWritten by ${t.author}` : "") + (t.stalled ? "\nWoken, but nobody claimed it within 30 minutes." : "");
    const acts = el("div", "task-acts"), act = (text, title, fn, cls = "btn small") => { const b = el("button", cls, text); b.type = "button"; b.title = title; b.onclick = fn; acts.append(b); return b; };
    const run = (fn) => async () => { try { await fn(); loadTasks(true); } catch (e) { toast(e.message); } };
    const approve = run(() => post("/ui/api/task/approve", { project: t.project, id: t.id }));
    if (t.can_approve) act("Approve", "Let this task start (it waits for the project owner's or an admin's approval)" + (t.approve_warning ? `\n\n${t.approve_warning}` : ""),
      () => (!t.approve_warning || confirm(`${t.approve_warning}\n\nApprove ${t.id} anyway?`)) && approve());
    if (!(t.hosts || []).length && t.no_host) act("Start", `No pc can start it: ${t.no_host}`, null).disabled = true;
    for (const h of t.hosts || []) act(`Start on ${h.label}`, "Start a worker for this task there now",
      run(async () => { await post("/ui/api/machines/action", { machine: h.machine, action: "start_worker", args: { project: t.project, id: t.id } }); toast(`Asked ${h.label} to start ${t.id}`, false); }));
    for (const { w, m } of workersOf(t)) { // its worker: where, how far, and Stop / Restart right here
      const p = w.progress || {}, line = el("div", "task-worker");
      line.append(el("span", "chip", `worker on ${m.label}: ${m.online ? "" : "pc offline · "}${w.status}`),
        " ", [p.commit && `last commit: ${p.commit}`, p.commits && `${p.commits} commit${p.commits > 1 ? "s" : ""}`, p.changed && `${p.changed} changed`, w.why]
          .filter(Boolean).join(" · "));
      if ((p.log || []).length) { const d = el("details"); d.append(el("summary", null, "last output"), el("pre", "mono", p.log.join("\n"))); line.append(d); }
      if (m.own) line.append(" ", ckFollowButton(w));
      if (m.own && m.online && w.status === "running") line.append(" ", machineButton("Stop", "Stop this worker", () => confirm(`Stop the worker on ${t.id}?`) && machineAction(m, "stop_worker", { project: w.project, id: w.id })));
      if (m.own && m.online && w.status === "failed") line.append(" ", machineButton("Restart", "Start this worker again now (past the 6 h hold)", () => machineAction(m, "restart", { project: w.project, id: w.id }).then(() => loadTasks(true))));
      li.append(line);
    }
    if (!t.done && t.can_cancel !== undefined) { // Assign: another agent or person, optionally pinned to one pc
      const form = el("span", "task-assign"); form.hidden = true;
      const who = el("select", "small"), where = el("select", "small"), save = el("button", "btn small primary", "Save"); save.type = "button";
      for (const o of ["Claude", "Codex", "Gemini", "Someone else…"]) { const x = el("option", null, o); x.selected = o === "Gemini" ? /gemini|agy|antigravity/i.test(t.owner) : t.owner.toLowerCase().startsWith(o.toLowerCase()); who.append(x); }
      where.append(el("option", null, "any pc"));
      for (const d of devices) { const x = el("option", null, d); x.value = d; where.append(x); }
      where.firstChild.value = "";
      save.onclick = run(async () => {
        const owner = who.value === "Someone else…" ? (prompt("Who gets it? A person's name, as task lines write it:", "") || "").trim() : who.value;
        if (!owner) return;
        await post("/ui/api/task/assign", { project: t.project, id: t.id, owner, device: where.value }); toast(`${t.id} → ${owner}${where.value ? "@" + where.value : ""}`, false); });
      const now = el("button", "btn small", "Start chat now"); now.type = "button"; // assign and start in one go: a new chat of that agent takes it
      now.title = "A new chat of this agent (on the picked pc, else any of yours with a folder of this project) takes it right away";
      const agentOf = { Claude: "claude", Codex: "codex", Gemini: "agy" };
      who.onchange = () => { now.disabled = !agentOf[who.value]; }; who.onchange();
      now.onclick = () => ckTaskChat(t.project, t.id, agentOf[who.value], where.value);
      form.append(who, where, save, now);
      const b = act("Assign…", "Give this task to another agent or person, optionally on one pc", () => { form.hidden = !form.hidden; }, "btn small quiet");
      if (!t.can_cancel) { b.disabled = true; b.title = "Only the project's owner or an admin, and not while it is being worked on"; }
      acts.append(form);
    }
    if (!t.done) {
      const held = t.claimed || /^(running in|taken by)/.test(t.runs || "");
      const b = act("Cancel", "Turn this line into a result for its requester: they hear it, and a task that starts after it waits", run(async () => {
        const why = prompt(`Cancel ${t.id}? Its requester (${t.from || t.owner}) gets it back as a result. Why?`, "");
        if (why === null) return;
        await post("/ui/api/task/cancel", { project: t.project, id: t.id, reason: why });
        toast(`${t.id} cancelled`, false);
      }), "btn small quiet risky");
      if (!t.can_cancel) { b.disabled = true; b.title = held ? "Being worked on: stop its worker first (here, under the task), then cancel" : "Only the project's owner or an admin may cancel a task"; }
    } else if (t.done && me.writable?.includes(t.project)) act("Remove", "Read it? Remove the line from the memo (its history keeps it)", run(async () => {
      if (confirm(`Remove ${t.id} from ${t.path}?`)) await post("/ui/api/task/done", { path: t.path, line: t.line });
    }));
    if (acts.childNodes.length) li.append(acts);
    return li;
  };
  const section = (title, items) => {
    if (!items.length) return;
    const ol = el("ol", "hits"); items.forEach((t) => ol.append(row(t)));
    box.append(el("h3", null, `${title} (${items.length})`), ol);
  };
  open.sort((a, b) => (b.overdue - a.overdue) || (a.due || "9").localeCompare(b.due || "9"));
  section("Open", open);
  for (const who of [...new Set(back.map((t) => t.owner))].sort()) section(`Results waiting to be read by ${who}`, back.filter((t) => t.owner === who));
}

// Machines: every pc of the viewer's tokens (an admin sees all) with its agents, workers and recent sessions. The
// owner steers a pc with fixed actions only; the server queues them and the pc's companion fetches them.
const AGENT_NAME = { claude: "Claude Code", codex: "Codex", agy: "agy" };
// An agent's usage windows that have not reset, soonest reset first: Claude and Codex 5h and 7d, agy one per quota pool
// ("Gemini", "Claude/GPT-OSS"); [percent, name, resets_at]. An older companion sends only its fullest window.
// A window's model group ("Gemini 7d" -> "Gemini"; Claude's and Codex's plain "7d" -> "") and its length.
const usageGroup = (n) => n.replace(/(^| )(5h|7d|30d)$/, "");
const usageSpan = (n) => (n.match(/(?:^| )(5h|7d|30d)$/) || [, ""])[1];
// Live windows, each model group together (fullest group first), the longest window first: "Gemini 7d · 5h".
function usageWindows(ag) {
  const wins = (ag.windows || []).filter((w) => !w[2] || w[2] > Date.now() / 1000), top = {};
  for (const [p, n] of wins) top[usageGroup(n)] = Math.max(top[usageGroup(n)] ?? -1, p);
  const rank = { "30d": 0, "7d": 1, "5h": 2 };
  return wins.sort((a, b) => top[usageGroup(b[1])] - top[usageGroup(a[1])] || usageGroup(a[1]).localeCompare(usageGroup(b[1]))
    || (rank[usageSpan(a[1])] ?? 3) - (rank[usageSpan(b[1])] ?? 3));
}
// "7d 82% · 5h 12%"; agy names each group once: "Gemini 7d 25% · 5h 57% · Claude/GPT 7d 0% · 5h 0%"
const usageText = (ag) => usageWindows(ag).map(([p, n], i, all) =>
  `${i && usageGroup(all[i - 1][1]) === usageGroup(n) ? usageSpan(n) || n : n} ${Math.round(p)}%`).join(" · ") || `${Math.round(ag.limit)}%`;
// Usage of a Claude account (Machines, the model chip): a subscription per usage window ("5h 38% · resets 17:40"),
// an API key what it spent this month and its budget (the API has no balance a key can read).
const leftWhen = (t) => new Date(t * 1000).toLocaleString("en-GB", t - Date.now() / 1000 > 86400 ? { weekday: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" }).replace(",", "");
function accountTiming(a) {
  if (a.pc) return "";
  const parts = [];
  const now = Date.now() / 1000;
  if (a.expires && a.expires > 0) {
    parts.push(a.expires <= now ? "expired" : `expires ${fmtDay(a.expires)}`);
  } else if (a.kind === "apikey") {
    parts.push("no expiry");
  }
  return parts.join(" · ");
}
// Signed in: an email or a sign-in kind; a signed-out account's usage windows are from before, so they are not shown.
const accountSignedIn = (a) => !!(a.email || a.kind);
// An account's name: on someone else's pc its own sign-in is named after the account it really runs on (their main).
const accountName = (a) => a.id || a.name || "own";
// The order people pick from: their main account first, signed-out ones last, otherwise as the pc lists them.
const accountOrder = (list, main) => [...list].sort((a, b) => (b.id === main) - (a.id === main) || accountSignedIn(b) - accountSignedIn(a));
// The pc's own sign-in after a sign-out: an empty slot, hidden unless it is main or a sign-in for it is under way.
const accountGone = (a, main) => a.id === "" && !a.pc && !accountSignedIn(a) && main !== "" && !a.login && !a.error;
function accountUsage(a) {
  if (!accountSignedIn(a)) return "";
  if (a.kind === "apikey") return a.budget ? `$${(a.spent || 0).toFixed(2)} of $${a.budget} (${Math.round(((a.spent || 0) / a.budget) * 100)}%)` : `$${(a.spent || 0).toFixed(2)} spent this month`;
  return usageWindows(a).map(([p, n, r]) => `${usageSpan(n) || n} ${Math.round(p)}%${r ? ` · resets ${leftWhen(r)}` : ""}`).join(" · ");
}
const accountLeft = accountUsage;
// The same as small bars of what is used (0% empty -> 100% full), one per window or for the budget.
function accountBars(a) {
  const box = el("span", "acc-bars"), bar = (pct, label) => {
    const t = el("span", "mx-track acc-bar"), fill = el("i");
    fill.style.width = `${Math.min(100, Math.max(0, pct))}%`; if (pct >= 80) fill.className = pct >= 95 ? "bad" : "warn";
    t.append(fill); t.setAttribute("role", "img"); t.setAttribute("aria-label", label); t.title = label; box.append(t);
  };
  if (a.kind === "apikey") { if (a.budget) bar(Math.min(100, Math.max(0, ((a.spent || 0) / a.budget) * 100)), accountUsage(a)); }
  else for (const [p, n, r] of usageWindows(a)) bar(p, `${usageSpan(n) || n}: ${Math.round(p)}% used${r ? `, resets ${leftWhen(r)}` : ""}`);
  const text = accountUsage(a);
  if (text) box.append(el("small", "muted", text));
  return box;
}
// A running worker's progress from its companion: newest commit, commits, changed files, the last log lines.
function workerProgress(p) {
  const box = el("span", "progress");
  box.append(el("span", "progress-sum", [p.commits ? `${p.commits} commit${p.commits > 1 ? "s" : ""}` : "no commits yet", `${p.changed} changed`, p.commit && `last: ${p.commit}`].filter(Boolean).join(" · ")));
  if ((p.log || []).length) box.append(el("code", "progress-log", p.log.join("\n")));
  return box;
}
// Health lists these first (something to do now or a risk), the rest by how many findings they have.
const LINT_URGENT = ["open_overdue", "possible_secret_in_memo", "possible_secret_in_skill", "stale_handback", "offline_conflict",
  "history_failed", "duplicate_check_failed", "skill_broken", "worker_unmerged", "branch_unlanded", "plugin_behind"];
// Lint checks in plain words, with what to do about them (unknown checks fall back to their name).
const LINT_INFO = {
  usage_outlier: ["Session that used far more tokens than usual", "Look at that session's log: a loop, a huge file read or a runaway subagent? Nothing to fix when the work was simply big."],
  worker_unmerged: ["Worker branch not merged after a week", "A worker finished its task on a branch nobody merged: review the branch on that pc and merge it (the companion then removes the worktree), or delete the branch and worktree by hand."],
  branch_unlanded: ["Branch with work that never reached main", "A branch in a linked folder has commits not in main and no worktree uses it: often work an agent reported done. Merge or deploy it on that pc, or delete the branch when it is not wanted."],
  plugin_behind: ["Device with an old plugin", "Its auto-update did not fetch the newest version within two days: update it by hand on that device (the session start tells the agent how)."],
  skill_broken: ["Skill Claude cannot use", "Give SKILL.md frontmatter with name and a one-line description; without it the skill is never picked."],
  skill_name_mismatch: ["Skill name differs from its folder", "Set name: to the folder name (lowercase, digits, hyphens)."],
  skill_too_long: ["Skill longer than 500 lines", "Keep SKILL.md short; move details to a file next to it and link it."],
  skill_dead_link: ["Skill points to something missing", "Fix the memo or file name, or remove the reference."],
  possible_secret_in_skill: ["Possible secret in a skill", "Replace the value with ‹secret:NAME› and fetch it with the secret tool."],
  index_points_to_missing_file: ["Index line without a file", "Remove the line from MEMORY.md, or restore the memo."],
  memo_not_in_index: ["Memo missing from MEMORY.md", "Add one index line under its heading."],
  dead_wikilink: ["Link to a memo that does not exist", "Fix the [[name]], or write that memo."],
  missing_frontmatter: ["Frontmatter incomplete", "Add name, description and metadata.type."],
  big_memo_without_archive: ["Big memo without archive", "Move the history to archief/<name>_archief.md."],
  long_index_line: ["Index line too long", "Keep it under about 150 characters."],
  orphan_memo: ["Nothing points to this memo", "Index it, or link it from a related memo."],
  possibly_stale_open_item: ["Open item that may be stale", "Check it; update or close it."],
  possible_secret_in_memo: ["Possible secret", "Replace the value with ‹secret:NAME›."],
  unprocessed_source: ["Source not turned into memos yet", "Ask Claude to process it."],
  index_line_without_heading: ["Index line outside a heading", "Move it under a ## heading."],
  index_near_load_limit: ["MEMORY.md near Claude Code's load limit", "Shorten or merge index lines."],
  context_near_limit: ["Start context near what Claude Code loads", "Past 6 parts of 9000 characters the rest is dropped: shorten the index, the CLAUDE.md files or the open items."],
  relative_date: ["Relative date", "Write the absolute date."],
  missing_structure: ["Missing ## Stand / ## Keuzes", "Add the two sections."],
  doc_without_topic: ["Doc without topic", "Add topic: (a MEMORY.md heading) to its frontmatter."],
  decision_without_date: ["Decision without date", "Add the date when you know it; leave it when you don't."],
  stale_unchecked_fact: ["Fact unchecked for long", "Check it on the live system, then mark it (gecontroleerd <date>)."],
  open_overdue: ["Open item overdue", "An item marked 'open tot <date>' passed its date: do it, or move the date with a reason."],
  review_due: ["Due for a check", "IPs or versions not checked for a while: check them on the live system, then mark (gecontroleerd <date>)."],
  stale_handback: ["Result nobody read", "An agent handed a result back over a week ago and its requester never read it: read it, act on it, then remove the line."],
  isolated_note: ["Memo without links", "Link it to the memos it belongs with."],
  possible_duplicate: ["Two memos that may cover the same subject", "A reranker judged them alike: merge them if they really are one subject, or link them and sharpen their descriptions if they are kept apart on purpose."],
  duplicate_check_failed: ["Duplicate check failed", "The background reranker stopped with this error: see the admin page and the server log (docker logs knowledge)."],
  sparse_topic: ["Topic whose notes barely link", "Link its notes, or move them to a better topic."],
  history_failed: ["History not saved", "See the server log."],
  offline_conflict: ["Written offline while it changed here", "Merge it into the memo, then delete it."],
};

$("ingest-open").onclick = () => {
  $("add-menu").open = false; $("ingest-form").reset();
  $("ingest-result").className = "muted"; $("ingest-result").textContent = "Sources are stored unchanged in homelab/sources/. Ask Claude to turn a new source into memos; lint lists unprocessed sources.";
  $("ingest-dialog").showModal();
};
$("ingest-cancel").onclick = () => $("ingest-dialog").close();
$("ingest-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = $("ingest-result"), title = $("in-title").value.trim();
  out.className = "muted"; out.textContent = "Working…";
  try {
    let res;
    const file = $("in-file").files[0];
    if (file) {
      res = await api("/ui/api/ingest", { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name), "X-Title": encodeURIComponent(title) } });
    } else {
      const body = $("in-url").value.trim() ? { url: $("in-url").value.trim(), title } : { text: $("in-text").value, title };
      res = await api("/ui/api/ingest", { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
    }
    out.replaceChildren(el("p", null, `Saved ${res.path} (${res.chars} characters): ${res.title}`));
    const link = (label, path) => { const a = el("a", null, label); a.href = `#${encPath(path)}`; a.onclick = (ev) => { ev.preventDefault(); $("ingest-dialog").close(); openNote(path); }; return a; };
    out.append(link("Open the source", res.path));
    if (res.related.length) {
      out.append(el("p", null, "Possibly related memos:"));
      for (const rel of res.related) { const a = link(rel, rel); a.style.display = "block"; out.append(a); }
    }
    out.append(el("p", "muted", "Next: ask Claude to integrate this source into the memos."));
    e.target.reset();
    refreshData();
  } catch (err) { out.className = "error"; out.textContent = err.message; }
});

// ---------------------------------------------------------------- live updates (Server-Sent Events)
let liveTimer, liveBusy = 0, liveAgain = false;
// One refresh at a time: changes during a refresh make one more after it, so a slow server never gets a growing pile of
// requests. A background tab asks nothing until it is looked at. After a minute a refresh counts as hung (a request can
// stay open on a half-dead connection after sleep), which must not end the live updates for good.
async function onChanged() {
  if (Date.now() - liveBusy < 60000 || document.hidden) { liveAgain = true; return; }
  liveBusy = Date.now();
  try { await liveRefresh(); } finally {
    liveBusy = 0;
    if (liveAgain) { liveAgain = false; onChanged(); }
  }
}
document.addEventListener("visibilitychange", () => {
  syncAnim();
  if (!document.hidden && liveAgain) { liveAgain = false; onChanged(); }
});
async function liveRefresh() {
  await refreshData(); // first: the Changes list names notes from the graph
  const done = await Promise.allSettled([loadRecent(), loadLint(!$("tab-lint").hidden), loadTasks(!$("tab-tasks").hidden), liveNote()]);
  for (const r of done) if (r.reason) console.warn("live refresh:", r.reason);
}
async function liveNote() {
  const note = current;
  if (!note || historyOpen) return;
  let fresh; try { fresh = await api(`/ui/api/note?path=${encodeURIComponent(note.path)}`); } catch { return; }
  if (current !== note || fresh.version === note.version) return; // another note opened meanwhile
  if (editing) { // never overwrite what the user is typing: warn instead
    const warn = document.querySelector("#note .live-warn") || el("p", "error live-warn");
    warn.textContent = `This note was just changed elsewhere (now version ${fresh.version}). Saving will be refused; copy your text first.`;
    $("note").querySelector(".actions")?.before(warn);
    return;
  }
  const top = $("note").scrollTop;
  current = fresh; renderNote(); $("note").scrollTop = top;
}
let liveHellos = 0, liveRetry = 3000;
function connectLive() {
  const es = new EventSource("/ui/api/events");
  const soon = () => { clearTimeout(liveTimer); liveTimer = setTimeout(onChanged, 700); };
  es.addEventListener("changed", soon);
  // A session log says who changed what: only a page showing that needs it, and the graph and lint answers stay cached.
  es.addEventListener("sessions", () => { if (!$("tab-changes").hidden || current?.path.includes("/sessions/")) soon(); });
  es.addEventListener("hello", (e) => {
    $("live").classList.add("on"); liveRetry = 3000;
    // A new release was deployed while this tab was open (the stream reconnects after the restart): reload, unless
    // the user is typing or has a dialog open, then say so instead.
    if (e.data && e.data !== document.documentElement.dataset.release) {
      if (!editing && !document.querySelector("dialog[open]")) location.reload();
      else $("live").title = "A new version is available: save your work, then reload";
    } else if (liveHellos++) onChanged(); // reconnected: catch up on what changed while the stream was down
  });
  es.onerror = () => {
    $("live").classList.remove("on");
    // The browser retries by itself, but gives up for good after a non-200 answer (Traefik 502 during a deploy).
    if (es.readyState === EventSource.CLOSED) {
      es.close();
      setTimeout(connectLive, liveRetry);
      liveRetry = Math.min(liveRetry * 2, 60000);
    }
  };
}

let me = { admin: false, writable: [], labels: {} };
const shown = (p) => (me.labels || {})[p] || p; // the display name the admin gave a project
async function loadMe() {
  // Projects created in the admin page have roots <name> (docs) and <name>-memory, like the built-in ones.
  me = await api("/ui/api/me");
  PROJECTS = me.projects;
  for (const p of PROJECTS) if (!MEMORY_ROOT[p]) { MEMORY_ROOT[p] = `${p}-memory`; ROOT_PROJECT[p] = p; ROOT_PROJECT[`${p}-memory`] = p; }
  if (picks) { // a project no longer yours drops out of the pick; when it was the only one, the first project
    const ok = PROJECTS.filter((p) => picks.has(p));
    project = ok.length || project.includes(",") ? pickValue(ok) : PROJECTS[0]; picks = navPicks(project);
  }
  agentProject = allMode() ? "" : project;
  navMe(me);
  if (!me.admin) $("ingest-open").hidden = true;
  $("new-memo").hidden = !me.writable.includes(project);
  $("add-menu").hidden = $("new-memo").hidden && $("ingest-open").hidden;
}

// The URL says which view shows (shell.js). The Agents views need no map data, so they show as soon as the user is
// known; the knowledge views once the map data is in.
let routedEarly = false;
// Also wait for the scripts after this one (shell.js, ...): a fast /ui/api/me answer can come before they ran.
Promise.all([loadMe(), new Promise((ready) => { if (document.readyState === "loading") addEventListener("DOMContentLoaded", ready); else ready(); })]).then(() => {
  if (!["notes", "map", "changes", "health"].includes(routeView())) { applyRoute(); routedEarly = true; }
  return loadGraph();
}).catch((e) => {
  $("content").prepend(el("p", "error load-error", `Could not load the knowledge base: ${e.message}. Reload the page; if it stays, the server may be restarting.`));
  throw e;
}).then(() => {
  connectLive();
  loadLint(false); loadTasks(false); loadRecent();
  if (!routedEarly) applyRoute();
  if ($("cockpit").hidden && !agentsOff()) ckSummary().catch(() => {}); // the Needs-you badge, on every view
  probeCompanion();
});
