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
  tokens = {};
  if (!Graph) return;
  indexGraph(); renderTopics(); Graph.backgroundColor(tok("--canvas"));
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", themeChanged);
const decode = (s) => { try { return decodeURIComponent(s); } catch { return s; } }; // a literal % is not an escape
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
// One empty state and one loading state for every list; a toast instead of alert() (errors stay longer, a click
// dismisses), announced by the #toasts live region.
const emptyState = (text) => el("p", "empty", text);
const loadingState = () => el("p", "empty loading", "Loading…");
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
const renderMd = (text) => DOMPurify.sanitize(marked.parse(text, { gfm: true }), PURIFY);
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* blocked */ } } };
// A new history entry for a route (shell.js); the count lets Back stay inside the app instead of leaving it.
const pushHash = (h) => { if (location.hash !== h) history.pushState({ n: (history.state?.n || 0) + 1 }, "", h); };
// A note path inside a route or link: each part encoded, so "?", "#", "%", spaces and accents survive a refresh and
// match location.hash as the browser keeps it; parseRoute decodes it again.
const encPath = (p) => p.split("/").map(encodeURIComponent).join("/");
const noteRoute = (path, view = "notes") => `#/${view}/${encPath(path)}`;

let data = { nodes: [], links: [] };
let byId = new Map(), neighbors = new Map(), slugToId = new Map(), clusterColor = new Map();
let selected = null, hover = null, focusSet = new Set();
let machinesCache = []; // the last /ui/api/machines rows (declared early for loadTasks/loadMachines)
let project = store.get("kn_project") || "homelab";
let labelMin = 0, current = null, editing = false, Graph, mapOn = false;

// "all": every project on one map, each project an island; otherwise one project and its topics.
const ALL = "all", allMode = () => project === ALL;
const inProject = (p) => allMode() || p === project || p === "shared";
// A shared note (skill) shows only in the projects that link to it.
const nodeIn = (n) => allMode() || n.project === project || (n.project === "shared" && (!n.projects || n.projects.includes(project)));
// What colours a note and makes its island: its project on the All map, its topic otherwise.
const groupOf = (n) => (allMode() ? n.project : n.cluster);
const lintProject = (item) => ROOT_PROJECT[item.split(/[/:]/)[0]] || "homelab"; // homelab memory items carry no root prefix
const nodeColor = (n) => clusterColor.get(groupOf(n)) || tok("--g-doc");
const linkId = (end) => end.id || end;
const linkW = (l) => l.w ?? 0.5; // an older server sends no weight
const fade = (color, f) => color.replace(/rgba\(([^)]+),\s*([\d.]+)\)/, (_, rgb, a) => `rgba(${rgb}, ${(a * f).toFixed(3)})`);

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
  const v = visibleData();
  Graph.graphData(v); settling = true; // new data reheats the layout
}

async function refreshData() {
  await releaseFocus({ instant: true });
  // Reload after a change but keep the layout: copy positions of known nodes onto the new data.
  const old = new Map(data.nodes.map((n) => [n.id, n]));
  data = await api("/ui/api/graph");
  for (const n of data.nodes) { const o = old.get(n.id); if (o) Object.assign(n, { x: o.x, y: o.y, vx: 0, vy: 0 }); }
  indexGraph(); renderProjects(); renderTopics(); applyView();
}

// ---------------------------------------------------------------- map rendering
const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
const touch = matchMedia("(hover: none)").matches; // phones: a tap selects, there is no hover
if (touch) $("q").placeholder = "Search titles, text and meaning…"; // no "/" key on a phone
const baseRadius = (n) => 3 + Math.sqrt(n.degree || 0) * 1.6;

function drawNode(node, ctx, scale) {
  // Hover: the node eases up to 2.4x with a glow, its neighbours to 1.4x.
  const target = node.id === hover ? 2.4 : hover && focusSet.has(node.id) ? 1.4 : 1;
  node.__s = calm ? target : (node.__s || 1) + (target - (node.__s || 1)) * 0.25;
  const r = baseRadius(node) * node.__s;
  const lit = focusSet.size === 0 || focusSet.has(node.id);
  ctx.globalAlpha = lit ? 1 : 0.15;
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
  const wanted = Graph.graphData().nodes.filter((n) => n.x != null && (rank(n) ||
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
  const groups = new Map();
  for (const n of Graph.graphData().nodes) if (groupOf(n) && n.x != null) (groups.get(groupOf(n)) || groups.set(groupOf(n), []).get(groupOf(n))).push(n);
  const size = 15 / scale, taken = [];
  ctx.font = `650 ${size}px ${tok("--font")}`; ctx.textAlign = "center"; ctx.textBaseline = "bottom";
  for (const [name, ns] of [...groups].sort((a, b) => b[1].length - a[1].length)) {
    if (ns.length < 2) continue;
    const cx = ns.reduce((s, n) => s + n.x, 0) / ns.length, cy = ns.reduce((s, n) => s + n.y, 0) / ns.length;
    const spread = ns.reduce((s, n) => s + Math.hypot(n.x - cx, n.y - cy), 0) / ns.length;
    const y = cy - spread - 6 / scale, w = ctx.measureText(name).width, box = [cx - w / 2, y - size, w, size];
    if (taken.some((t) => box[0] < t[0] + t[2] && t[0] < box[0] + box[2] && box[1] < t[1] + t[3] && t[1] < box[1] + box[3])) continue;
    taken.push(box);
    ctx.lineWidth = 4 / scale; ctx.strokeStyle = tok("--canvas"); ctx.strokeText(name, cx, y);
    ctx.fillStyle = clusterColor.get(name) || tok("--ink"); ctx.fillText(name, cx, y);
  }
}

// Keep redrawing while hover/selection animations run (link particles, easing); force-graph pauses
// redraws when idle. A selection flows for a few seconds, then pauses to spare phone batteries.
let settleTimer;
function animateHover(ms = 400) {
  if (calm) { if (!tweening) Graph.autoPauseRedraw(true); return; }
  Graph.linkDirectionalParticles(Graph.linkDirectionalParticles()); // re-evaluate which links flow
  Graph.autoPauseRedraw(false); clearTimeout(settleTimer);
  if (!hover) settleTimer = setTimeout(() => { if (!tweening) Graph.autoPauseRedraw(true); }, ms);
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
let tweenId = 0, hoverTimer, tweening = false;

function tween(moves, done) {
  const id = ++tweenId, t0 = performance.now(), ms = calm ? 0 : 450;
  const step = (now) => {
    if (id !== tweenId) return; // a newer move took over
    const k = ms ? Math.min(1, (now - t0) / ms) : 1, e = 1 - Math.pow(1 - k, 3); // ease-out cubic
    for (const m of moves) { m.n.x = m.n.fx = m.x0 + (m.x1 - m.x0) * e; m.n.y = m.n.fy = m.y0 + (m.y1 - m.y0) * e; m.n.vx = m.n.vy = 0; }
    if (k < 1) return requestAnimationFrame(step);
    tweening = false; animateHover(selected ? 6000 : 400); // redraw pauses again once idle
    if (done) done();
  };
  tweening = true; Graph.autoPauseRedraw(false); requestAnimationFrame(step);
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
}

const linkLit = (l) => focusSet.size && focusSet.has(linkId(l.source)) && focusSet.has(linkId(l.target));

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
    .linkColor((l) => (linkLit(l) ? tok("--accent") : groupOf(l.source) === groupOf(l.target) ? fade(tok("--link-in"), 0.55 + 0.45 * linkW(l))
      : allMode() ? tok("--accent") + "99" : fade(tok("--link-cross"), 0.6 + 0.4 * linkW(l))))
    .linkWidth((l) => (linkLit(l) ? 1 + 1.6 * linkW(l) : allMode() && groupOf(l.source) !== groupOf(l.target) ? 1.2
      : groupOf(l.source) === groupOf(l.target) ? 0.5 + 1.6 * linkW(l) : 0.4 + 0.9 * linkW(l)))
    .linkVisibility((l) => linkLit(l) || l.keep || allMode() || groupOf(l.source) === groupOf(l.target) || linkW(l) >= 0.2)
    .linkDirectionalParticles((l) => (!calm && mapOn && (hover || selected) && linkLit(l) ? 2 : 0)) // particles keep force-graph drawing every frame
    .linkDirectionalParticleWidth(2.2).linkDirectionalParticleSpeed(0.006)
    .linkDirectionalParticleColor(() => tok("--accent"))
    .backgroundColor(tok("--canvas"))
    .cooldownTicks(250) // the layout settles, then the simulation stops: no CPU while idle
    .onEngineStop(() => {
      settling = false;
      if (zoomToFocus.again && selected) zoomToFocus();
      else if (!loadGraph.fitted) Graph.zoomToFit(400, 40);
      zoomToFocus.again = false; loadGraph.fitted = true;
    })
    .onNodeHover((n) => {
      if (touch) return;
      hover = n ? n.id : null; if (!selected) setFocus(hover);
      $("g2d").style.cursor = n ? "pointer" : ""; animateHover(selected ? 6000 : 400);
      clearTimeout(hoverTimer);
      if (!selected) hoverTimer = setTimeout(() => (hover ? gather(hover, 0.3) : releaseFocus()), hover ? 150 : 250);
    })
    .onNodeClick((n) => (selected === n.id ? go(noteRoute(n.id)) : openNote(n.id))) // a second click reads it
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
  Graph.onRenderFramePost(drawLabels);
  new ResizeObserver(() => { const w = $("graph").clientWidth, h = $("graph").clientHeight; if (w && h) Graph.width(w).height(h); }).observe($("graph"));
  renderProjects(); renderTopics(); applyView(true);
}

// ---------------------------------------------------------------- side panel controls
// The project of the knowledge views: a picker in the sidebar and in the phone's More sheet, with its note count.
function renderProjects() {
  for (const box of document.querySelectorAll(".project-pick")) {
    box.replaceChildren(...[...PROJECTS, ALL].map((p) => {
      const o = el("option", null, `${p === ALL ? "All projects" : shown(p)} · ${p === ALL ? data.nodes.length : data.nodes.filter((x) => x.project === p).length}`);
      o.value = p; return o;
    }));
    box.value = project;
    box.onchange = () => switchProject(box.value);
  }
}

function switchProject(p, refit = true) {
  releaseFocus({ instant: true });
  if (p === project || (p !== ALL && !PROJECTS.includes(p))) return;
  project = p; store.set("kn_project", p);
  setAgentProject(p === ALL ? "" : p); // one project picker for every view
  $("new-memo").hidden = !me.writable.includes(p);
  $("add-menu").hidden = $("new-memo").hidden && $("ingest-open").hidden;
  selected = null; setFocus(null);
  indexGraph(); renderProjects(); renderTopics(); applyView(refit);
  // a note of the old project would stay in the URL, so a refresh would jump back to it: show the new project's home
  if (current && p !== ALL && projectOf(current.path) !== p) deselect();
  else if (!current) homeView();
  if (!$("results").hidden) runSearch();
  loadLint(!$("tab-lint").hidden);
  loadRecent();
  paintHead();
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
      const li = hitItem(n.label, "", () => openNote(n.id));
      li.firstChild.dataset.id = n.id;
      if (n.id === selected) li.firstChild.classList.add("active");
      list.append(li);
    }
    s.onclick = () => { if (!d.open && mapOn) highlight(ns.map((n) => n.id)); }; // fires before the toggle: about to open
    d.append(s, list); return d;
  }));
  if (!$("topics").childNodes.length) $("topics").append(emptyState("No notes in this project yet."));
}

// The middle column shows the note, or the map of the project instead.
// The Map view (#/map) and the Notes view share the lists and the right column; shell.js switches between them.
function setMap(on) {
  mapOn = on;
  $("graph").hidden = !on; $("note").hidden = on;
  if (current) renderSide();
  if (Graph) { Graph.linkDirectionalParticles(Graph.linkDirectionalParticles()); on ? Graph.resumeAnimation() : Graph.pauseAnimation(); }
  if (!on) return;
  requestAnimationFrame(() => { // once the map has its size
    if (!Graph) return; // the map data is still loading: loadGraph sizes it
    Graph.width($("graph").clientWidth).height($("graph").clientHeight);
    if (selected) { gather(selected, 1).then(zoomToFocus); if (settling) zoomToFocus.again = true; } else Graph.zoomToFit(400, 40);
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

const ago = (sec) => { const d = Date.now() / 1000 - sec; return d < 90 ? "just now" : d < 3600 ? `${Math.round(d / 60)} min ago` : d < 86400 ? `${Math.round(d / 3600)} h ago` : new Date(sec * 1000).toLocaleDateString(); };

// Clicking empty canvas clears the selection and the note in the URL, so a refresh opens nothing.
function deselect({ push = true } = {}) {
  if (editing) return; // keep unsaved edits on screen
  selected = null; current = null; setFocus(null); animateHover();
  // other views have no note in their route; go() so a phone's note page and the search page give way to the list
  if (push && (shellRoute.view === "notes" || shellRoute.view === "map") && location.hash !== (mapOn ? "#/map" : "#/notes")) go(mapOn ? "#/map" : "#/notes");
  $("note").replaceChildren(el("div", "note-empty muted", "Pick a note from the list or search."));
  $("side").replaceChildren(); markActive();
  homeView();
  releaseFocus().then(() => { if (!selected && mapOn) Graph.zoomToFit(600, 40); }); // home, then the overview
}

// With nothing selected the middle shows the project's MEMORY.md: the user's own table of contents.
let homeNote = null;
async function homeView() {
  const p = project, path = `${MEMORY_ROOT[p]}/MEMORY.md`;
  let note = { text: "" }; // All: no one index, the cards come from the projects themselves
  if (p !== ALL) try { note = await api(`/ui/api/note?path=${encodeURIComponent(path)}`); } catch { return; } // no index yet
  if (current || editing || p !== project) return; // the user opened something or switched meanwhile
  homeNote = note;
  if (!data.nodes.length) return; // the map data is still loading: its arrival draws the home view
  homeSide();
  const body = splitFront(note.text).body, groups = topicGroups();
  if (p !== ALL && (!/^## /m.test(body) || !groups.length)) { // no headings yet: the index as it is
    const md = el("div", "md"); md.innerHTML = renderMd(body);
    $("note").replaceChildren(el("div", "note-empty muted", "Pick a note from the list or search. Index of this project:"), md);
    return;
  }
  // An overview: one card per topic with its best-connected notes and their index hook, the rest one click away.
  const hooks = new Map([...body.matchAll(/^- \[[^\]]+\]\(([^)]+\.md)\)\s*[—–-]\s*(.+)$/gm)].map((m) => [`${MEMORY_ROOT[p]}/${m[1]}`, m[2]]));
  const notes = groups.reduce((a, [, ns]) => a + ns.length, 0);
  const head = el("header", "home-head");
  head.append(el("h2", null, p === ALL ? "All projects" : p), el("p", "muted", p === ALL
    ? `${notes} notes in ${groups.length} projects. The map (M) shows how they link to each other.`
    : `${notes} notes in ${groups.length} topics. Pick a note, search with /, or open the map with M.`));
  const grid = el("div", "home-grid");
  for (const [topic, ns] of groups) {
    const card = el("section", "home-card"), h = el("h3"), dot = el("i");
    dot.style.background = clusterColor.get(topic) || tok("--g-doc");
    h.append(dot, topic, el("span", "n", String(ns.length)));
    const list = el("ol", "hits"), ranked = [...ns].sort((a, b) => (b.degree || 0) - (a.degree || 0) || a.label.localeCompare(b.label));
    const show = (k) => list.replaceChildren(...ranked.slice(0, k).map((n) => hitItem(n.label, hooks.get(n.id) || n.desc || "", () => openNote(n.id))));
    show(5); card.append(h, list);
    if (ns.length > 5) {
      const more = el("button", "btn small", `All ${ns.length}`);
      more.onclick = () => { show(ns.length); more.remove(); }; // unfold in place: works on a phone too, where the list is hidden
      card.append(more);
    }
    grid.append(card);
  }
  $("note").replaceChildren(head, grid);
  document.body.classList.add("home"); // phone: the cards replace the topic list
}

// The right column on the home view: what changed lately and what lint asks for, instead of an empty panel.
async function homeSide() {
  const p = project;
  let res; try { res = await api(`/ui/api/recent?project=${p}`); } catch { return; }
  if (current || p !== project) return;
  const box = el("div", "connected"), list = el("ol", "hits");
  for (const f of res.files.slice(0, 8)) {
    const n = byId.get(f.path);
    list.append(hitItem(n ? n.label : f.path.split("/").pop(), `${ago(f.mtime)} · ${f.session ? f.session.device : f.by === "VM" ? "on the VM" : writer(f.by)}`, () => openNote(f.path)));
  }
  if (!res.files.length) list.append(el("li", null, emptyState("No changes yet.")));
  const all = el("button", "btn small", "All changes"); all.onclick = () => go("#/changes");
  box.append(el("h3", null, "Recently changed"), list, all);
  const lint = $("lint-count").textContent;
  if (lint) {
    const b = el("button", "btn small", `${lint} lint findings`); b.onclick = () => go("#/health");
    box.append(el("h3", "home-lint", "Housekeeping"), b);
  }
  $("side").replaceChildren(box);
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
  selected = byId.has(note.path) ? note.path : null; setFocus(selected); markActive(); if (Graph) animateHover(6000);
  const n = byId.get(note.path);
  if (mapOn && n && n.x != null) {
    loadGraph.fitted = true; // the reader chose a view: no overview fit after the layout settles
    clearTimeout(hoverTimer);
    gather(n.id, 1).then(() => { if (selected === n.id) zoomToFocus(); });
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
    const low = t.data.toLowerCase(), w = words.find((x) => low.includes(x));
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
  head.append(el("h2", null, n ? n.label : note.path.split("/").pop()));
  if (n) { const pill = el("span", "pill topic", n.cluster || n.group); pill.style.setProperty("--c", nodeColor(n)); head.append(pill); }
  const tools = el("div", "note-tools");
  const copy = el("button", "btn small", "Copy link");
  copy.onclick = () => navigator.clipboard?.writeText(`${location.origin}/ui${noteRoute(note.path)}`).then(() => { copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy link"), 1500); });
  tools.append(copy);
  if (note.writable) { const edit = el("button", "btn small", touch ? "Edit" : "Edit (E)"); edit.onclick = startEdit; tools.append(edit); }
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
    card.append(el("h2", null, n ? n.label : note.path.split("/").pop()));
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
      const why = x.why.replace("shared neighbours", "neighbours").replace(/similar meaning \(([\d.]+)\)/, "meaning $1").replace(/, /g, " · ");
      a.append(x.label, el("span", "why", `${x.project !== project && x.project !== "shared" ? x.project + " · " : ""}${why}`));
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
function diffView(text) {
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
  return pre;
}

async function showHistory(open = null) {
  // Every version of this note (who, when), its diff against the version before, and restore. `open` = a version id
  // to show at once (a click in Changes).
  const note = current, pane = $("note");
  const res = await api(`/ui/api/history?path=${encodeURIComponent(note.path)}`);
  if (current !== note || editing) return;
  historyOpen = true;
  pane.replaceChildren();
  const head = el("div", "note-head"); head.append(el("h2", null, `History of ${note.path.split("/").pop()}`));
  const back = el("button", "btn small", "Back to the note"); back.onclick = renderNote;
  pane.append(head, el("div", "note-tools"), el("div", "note-path", note.path));
  pane.querySelector(".note-tools").append(back);
  if (!res.versions.length) { pane.append(el("p", "muted", "No earlier versions yet: history starts at the first change.")); return; }
  const list = el("ol", "hits"), view = el("div", "version");
  pane.append(view, list); // the chosen diff above the list, so it is in sight
  res.versions.forEach((v, i) => {
    const when = new Date(v.time * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
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
  const head = el("div", "note-head"); head.append(el("h2", null, `Editing ${current.path.split("/").pop()}`));
  const area = el("textarea", "editor"); area.value = current.text; area.spellcheck = false;
  area.setAttribute("aria-label", `Text of ${current.path}`);
  const err = el("p", "error"); err.hidden = true; err.setAttribute("role", "alert");
  const save = el("button", "btn primary", "Save (Ctrl+S)"), cancel = el("button", "btn", "Cancel");
  const actions = el("div", "actions"); actions.append(cancel, save);
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
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save.click(); }
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
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const titles = data.nodes.filter((n) => words.every((w) => `${n.label} ${n.id}`.toLowerCase().includes(w)))
    .sort((a, b) => (inProject(b.project) - inProject(a.project)) || (b.degree - a.degree)).slice(0, 6);
  $("titles-head").hidden = !titles.length;
  $("titles").replaceChildren(...titles.map((n) => hitItem(n.label, `${shown(n.project)} · ${n.id}`, () => openNote(n.id))));
  let res; try { res = await api(`/ui/api/search?q=${encodeURIComponent(q)}&project=${project}`); } catch (e) { $("hits").textContent = e.message; return; }
  if (seq !== searchSeq) return; // a newer search is on its way
  $("hits-empty").hidden = res.notes.length > 0;
  if (!res.notes.length) $("hits-empty").textContent = "No note contains all of these words.";
  $("hits").replaceChildren(...res.notes.map((h) => {
    const li = el("li"), b = el("button", "row-link");
    b.append(el("span", "hit-path", `${(byId.get(h.path) || {}).label || h.path.split("/").pop()}${h.count > h.hits.length ? `  +${h.count - h.hits.length}` : ""}`));
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
  $("sem").replaceChildren(...extra.map((h) => hitItem(`${(byId.get(h.path) || {}).label || h.path} · ${Math.round(h.score * 100)}%`, `${h.path}:${h.line} — ${h.text}`, () => openNote(h.path))));
}
// A search line as text with the query words in <mark>, built from text nodes (never innerHTML).
function marked_(text, words) {
  const span = el("span", "hit-text"), low = text.toLowerCase();
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
  b.append(el("span", "hit-path", top)); if (sub) b.append(el("span", "hit-text", sub));
  b.onclick = onclick; li.append(b); return li;
}
$("q").addEventListener("input", () => { // the query lives in the route, so a refresh or Back keeps it
  history.replaceState(history.state, "", `#/notes?q=${encodeURIComponent($("q").value)}`);
  clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 150);
});
$("q").addEventListener("keydown", (e) => { // Enter opens the first result
  if (e.key === "Enter") { e.preventDefault(); clearTimeout(searchTimer); runSearch().then(() => $("results").querySelector("button")?.click()); }
});

// Keys: Ctrl/Cmd+K the palette (/ the same, for notes only), [ folds the sidebar, A the Inbox, M map or notes, 1-9/0 the project; in a note E
// edits and Esc closes it; in a chat Esc goes back.
document.addEventListener("keydown", (e) => {
  const typing = /input|textarea|select/i.test(document.activeElement.tagName) || document.querySelector("dialog[open]");
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); if (!document.querySelector("dialog[open]:not(#palette)")) openPalette(); return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  const kn = shellRoute.view === "notes" || shellRoute.view === "map", at = current ? `/${encPath(current.path)}` : "";
  if (e.key === "[") toggleRail();
  else if (e.key === "/") { e.preventDefault(); openPalette(true); }
  else if (e.key.toLowerCase() === "a") go("#/inbox");
  else if (/^[1-9]$/.test(e.key) && PROJECTS[Number(e.key) - 1]) switchProject(PROJECTS[Number(e.key) - 1]);
  else if (e.key === "0") switchProject(ALL);
  else if (e.key.toLowerCase() === "m") go(mapOn ? `#/notes${at}` : `#/map${at}`);
  else if (e.key === "Escape" && shellRoute.view === "chats" && ck.view) ckShow(null);
  else if (!kn) return;
  else if (e.key.toLowerCase() === "e" && current?.writable) { e.preventDefault(); startEdit(); }
  else if (e.key === "Escape") deselect();
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
      card.append(el("h3", "sub", d === today ? "Today" : d === yesterday ? "Yesterday" : new Date(f.mtime * 1000).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })), list);
      box.append(card);
    }
    const n = byId.get(f.path), li = el("li"), b = el("button", "row-link");
    const top = el("span", "hit-path", n ? n.label : f.path.split("/").pop());
    if (f.mtime > seen) top.append(el("span", "new", "new"));
    const meta = el("span", "hit-text");
    if (f.added != null) meta.append(el("span", "plus", `+${f.added}`), " ", el("span", "minus", `−${f.removed}`), " · ");
    meta.append(`${ago(f.mtime)} · ${f.session ? f.session.device : f.by === "VM" ? "on the VM" : writer(f.by)}`);
    b.append(top, meta);
    if (f.session?.asked) b.append(el("span", "hit-text why", `“${f.session.asked}”`));
    b.title = f.path;
    b.onclick = async () => { if (await openNote(f.path) && f.version) { setMap(false); showHistory(f.version); } };
    li.append(b); list.append(li);
  }
  if (!res.files.length) box.append(emptyState("No changes yet."));
  store.set(key, String(Math.max(seen, ...res.files.map((f) => f.mtime))));
  loadTrash(p).catch((e) => box.append(el("p", "error", `Deleted: ${e.message}`)));
}
// Deleted: the copies knowledge_delete keeps, newest first; Restore puts one back where it was (never over a file
// that is there again: the server answers 409 with the reason).
let trashOpen = false;
async function loadTrash(p) {
  const res = await api("/ui/api/trash");
  if (p !== project || $("tab-changes").hidden) return;
  const items = res.items.filter((i) => allMode() || i.project === p), card = el("details", "card trash");
  card.id = "trash"; card.open = trashOpen; card.ontoggle = () => { trashOpen = card.open; };
  const sum = el("summary", "sub", "Deleted"); sum.append(el("span", "badge", String(items.length)));
  const list = el("ol", "hits");
  for (const i of items.slice(0, 100)) {
    const li = el("li", "task"), info = el("span", "trash-info");
    info.append(el("span", "hit-path", i.path.split("/").pop()), el("span", "hit-text", `${i.path} · deleted ${ago(i.deleted)} · ${(i.size / 1024).toFixed(1)} kB`));
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
  focusSet = new Set(ids);
  const pts = ids.map((i) => byId.get(i)).filter((n) => n && n.x != null);
  if (pts.length) { animateHover(); Graph.zoomToFit(600, 80, (n) => focusSet.has(n.id)); }
}

async function loadLint(render = true) {
  if (!me.admin) return; // lint spans every project: admins only
  if (render && !$("lint").childNodes.length) $("lint").replaceChildren(loadingState());
  const res = await api("/ui/api/lint?v=2"), full = res.report || res, hints = res.hints || {};
  // checks in a group (token usage) are context, not problems: folded at the end and left out of the badge
  const grouped = new Map((res.groups || []).flatMap((g) => g.checks.map((c) => [c, g])));
  const report = Object.fromEntries(Object.entries(full).map(([k, v]) => [k, v.filter((i) => allMode() || lintProject(i) === project)]));
  const count = (r) => Object.entries(r).reduce((a, [c, v]) => a + (grouped.has(c) ? 0 : v.length), 0);
  const here = count(report), all = count(full);
  // Badge and tab show this project's findings; the tab points to the rest so findings elsewhere are not missed.
  $("lint-count").textContent = here || "";
  $("lint-count").title = `${here} in ${project}, ${all} in all projects`;
  if (!render) return;
  const box = $("lint"); box.replaceChildren();
  const folds = new Map();
  for (const [check, items] of Object.entries(report)) {
    if (!items.length) continue;
    const [title, fix] = hints[check] || LINT_INFO[check] || [check.replace(/_/g, " "), ""];
    const ul = el("ol", "hits");
    for (const item of items) { // "file.md: detail" -> the note's title, the detail under it
      const m = item.match(/^([\w./-]+\.md)(?::\s*|\s+)?(.*)$/);
      if (!m) { ul.append(el("li", "hit-text", item)); continue; }
      const id = m[1].includes("/") ? m[1] : `memory/${m[1]}`;
      const li = hitItem(byId.get(id)?.label || m[1].split("/").pop(), m[2].replace(/\*\*/g, ""), () => openNote(id));
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
  if (!here) box.append(emptyState(`No findings in ${project === ALL ? "any project" : shown(project)}.`));
  if (all > here) {
    const b = el("button", "btn small", `${all - here} in other projects`); b.onclick = () => switchProject(ALL);
    box.append(b);
  }
}
// Tasks: the task lines between agents in every project (or the one picked in the header), overdue first, then by due
// date, with where each runs and a "Start on <pc>" per pc that may take it; results handed back. Cancel turns an open
// line into a result for its requester; Remove takes a read result away (history keeps both). The badge counts every
// project's open lines, the same number as the list's Open heading without a project filter.
let agentProject = project === ALL ? "" : project; // the Agents views' project: the sidebar's, "" = all (shell.js setAgentProject)
async function loadTasks(render = true) {
  const res = await api(render ? "/ui/api/tasks?hosts=1" : "/ui/api/tasks"), all = res.tasks.filter((t) => !t.done);
  $("tasks-count").textContent = all.length || "";
  $("tasks-count").className = all.some((t) => t.overdue) ? "badge warn" : "badge";
  const tasks = res.tasks.filter((t) => !agentProject || t.project === agentProject), open = tasks.filter((t) => !t.done), back = tasks.filter((t) => t.done);
  if (!render) return;
  const box = $("tasks"); box.replaceChildren();
  const elsewhere = all.length - open.length; // the badge counts every project: say what the picker hides
  if (elsewhere) {
    const b = el("button", "btn small ck-elsewhere", `${elsewhere} open task${elsewhere > 1 ? "s" : ""} in other projects: show all`);
    b.type = "button"; b.onclick = () => switchProject(ALL); box.append(b);
  }
  // the workers of these tasks, from the Machines list: progress and Stop/Restart live with the task, not the pc
  const machines = await api("/ui/api/machines").then((r) => (Array.isArray(r) ? r : r.machines || [])).catch(() => []);
  machinesCache = machines;
  const workersOf = (t) => machines.flatMap((m) => Object.values(m.agents || {}).flatMap((ag) => (ag.workers || []).map((w) => ({ w, m }))))
    .filter(({ w }) => w.project === t.project && (w.id === t.id || w.id === t.id.replace(/-(cont|fix)$/i, "")));
  const devices = [...new Set(machines.filter((m) => m.own).flatMap((m) => m.tokens || []))].sort();
  const row = (t) => {
    const n = byId.get(t.path), li = hitItem(`${t.id} · ${t.owner}`, t.text, () => openNote(t.path, { find: [t.id.toLowerCase()] }));
    const meta = el("span", "hit-text", [t.done ? "" : `${t.from || "?"} → ${t.owner}`, t.done ? "" : `start ${t.start}`,
      t.due && `due ${t.due}`, t.done && t.since && `handed back ${ago(t.since)}`, t.claimed && "claimed", t.waiting_reason, t.runs, n ? n.label : t.path.split("/").pop(),
      agentProject ? "" : shown(t.project)].filter(Boolean).join(" · "));
    if (t.legacy) meta.prepend(el("span", "badge", "legacy"), " ");
    if (t.stalled) meta.prepend(el("span", "new", "stalled"), " ");
    if (t.overdue) meta.prepend(el("span", "chip bad", "overdue"), " ");
    li.className = t.waiting_reason ? "task waiting" : "task"; li.firstChild.append(meta);
    li.firstChild.title = t.line + (t.legacy ? "\nLegacy line: no from/start, so it may start now." : "") +
      (t.author ? `\nWritten by ${t.author}` : "") + (t.stalled ? "\nWoken, but nobody claimed it within 30 minutes." : "");
    const acts = el("div", "task-acts"), act = (text, title, fn, cls = "btn small") => { const b = el("button", cls, text); b.type = "button"; b.title = title; b.onclick = fn; acts.append(b); return b; };
    const run = (fn) => async () => { try { await fn(); loadTasks(true); } catch (e) { toast(e.message); } };
    if (t.can_approve) act("Approve", "Let this task start (it waits for the project owner's or an admin's approval)", run(() => post("/ui/api/task/approve", { project: t.project, id: t.id })));
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
      form.append(who, where, save);
      const b = act("Assign…", "Give this task to another agent or person, optionally on one pc", () => { form.hidden = !form.hidden; });
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
      }));
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
  if (!tasks.length) box.append(el("p", "muted", "No open tasks."));
}

// Machines: every pc of the viewer's tokens (an admin sees all) with its agents, workers and recent sessions. The
// owner steers a pc with fixed actions only; the server queues them and the pc's companion fetches them.
const AGENT_NAME = { claude: "Claude Code", codex: "Codex", agy: "agy" };
const YOLO_WARNING = "YOLO changes this agent's own global setting on that pc (Claude Code: bypassPermissions, Codex: approval never + no sandbox, agy: always-proceed), so EVERY session of it there runs every command and edits every file without asking: the owner's own sessions too, not only workers and chats, also for tasks other people wrote. Risky actions are flagged, never stopped. Turning it off puts the previous settings back.";
const WORKERS_WARNING = "Allow workers on this pc? For every ready task in a linked project it may then start a fresh agent session on its own, in a git worktree of its own, without anyone watching (at most the max per agent). Only for a pc that may work by itself: not a work laptop or a shared family pc.";
const mins = (sec) => `${Math.max(1, Math.round((Date.now() / 1000 - sec) / 60))} min`;
// A running worker's progress from its companion: newest commit, commits, changed files, the last log lines.
function workerProgress(p) {
  const box = el("span", "progress");
  box.append(el("span", "progress-sum", [p.commits ? `${p.commits} commit${p.commits > 1 ? "s" : ""}` : "no commits yet", `${p.changed} changed`, p.commit && `last: ${p.commit}`].filter(Boolean).join(" · ")));
  if ((p.log || []).length) box.append(el("code", "progress-log", p.log.join("\n")));
  return box;
}
async function machineAction(m, action, args = {}) {
  try {
    await api("/ui/api/machines/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: m.id, action, args }) });
  } catch (e) { toast(e.message); return loadMachines(); }
  // the pc fetches the action within ~10 s and reports its new state right after: refresh until nothing waits
  for (let i = 0; i < 20; i++) {
    await loadMachines();
    const row = (machinesCache || []).find((x) => x.id === m.id);
    if (!row || !(row.actions || []).some((x) => x.status === "waiting")) break;
    await new Promise((r) => setTimeout(r, 3000));
  }
  setTimeout(loadMachines, 4000); // the report that follows the action
}
function machineButton(text, title, onclick) {
  const b = el("button", "btn small", text); b.type = "button"; b.title = title;
  // the pc carries the action out a moment later: a spinner and "…ing" until it reports back (machineAction waits)
  const busy = /Turn on/.test(text) ? "Turning on…" : /Turn off/.test(text) ? "Turning off…" : "Working…";
  b.onclick = async () => {
    const run = onclick();
    if (!run || typeof run.then !== "function") return; // cancelled (a prompt answered no): nothing to wait for
    b.disabled = true; b.classList.add("busy"); b.textContent = busy; b.setAttribute("aria-busy", "true");
    try { await run; } finally { b.disabled = false; b.classList.remove("busy"); b.removeAttribute("aria-busy"); b.textContent = text; }
  };
  return b;
}
// The Machines view: one card per pc.
async function loadMachines() {
  const res = await api("/ui/api/machines");
  machinesCache = Array.isArray(res) ? res : res.machines || [];
  if (!localMachine) await probeCompanion();
  const box = $("machines"); box.replaceChildren();
  const narrow = matchMedia("(max-width: 699.98px)").matches;
  const sha = async (t) => [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(t)))].map((b) => b.toString(16).padStart(2, "0")).join("");
  const here = localMachine && crypto.subtle ? (await Promise.all(res.machines.map(async (m) => (await sha(m.id)) === localMachine ? m.id : ""))).find(Boolean) : "";
  for (const m of res.machines) {
    const card = el("article", "machine" + (m.online ? "" : " offline")), head = el("div", "machine-head");
    head.append(el("h3", null, m.label));
    head.append(el("span", `pill ${m.online ? "ok" : ""}`, m.old ? "old plugin" : m.online ? "online" : `offline since ${ago(m.at)}`));
    if (here && m.id === here) head.append(el("span", "pill this", "This pc"));
    card.append(head, el("p", "machine-meta", [m.tokens.join(", "), m.companion ? `companion ${m.companion.version || "?"}` : !m.old && "no companion"].filter(Boolean).join(" · ")));
    const c = m.companion;
    if (c) {
      const line = el("p", "machine-meta", `Workers ${c.workersAllowed ? "allowed" : "off"}${c.game ? ` · ${c.game}` : ""}`);
      if (m.own) line.append(" ", machineButton(c.workersAllowed ? "Turn off" : "Allow workers", "Whether this pc may run workers at all",
        () => (c.workersAllowed || confirm(`${WORKERS_WARNING}\n\n${m.label}`)) && machineAction(m, "workers_allowed", { on: !c.workersAllowed })));
      card.append(line);
    }
    const agents = el("div", "machine-agents");
    card.append(agents);
    for (const [a, ag] of Object.entries(m.agents)) {
      const row = el("div", "machine-agent"), top = el("div", "machine-agent-head");
      top.append(el("strong", null, AGENT_NAME[a] || a), el("span", "muted", ag.plugin ? `plugin ${ag.plugin}` : "plugin ?"));
      if (ag.yolo) top.append(el("span", "pill yolo", "YOLO"));
      if (ag.trusted === false) top.append(el("span", "pill warn", "hooks not trusted"));
      if (c) top.append(el("span", `pill ${ag.limit >= 90 ? "warn" : ""}`, ag.limit == null ? "limit unknown" : `limit ${Math.round(ag.limit)}%`));
      row.append(top);
      if (ag.error) {
        const e = ag.error, w = el("p", "machine-error", `hook ${e.hook || "?"}${e.event ? ` (${e.event})` : ""} failed · ${(e.stderr || e.text || "").split("\n")[0].slice(0, 160)} · ${ago(e.at)}`);
        w.title = e.stderr || ""; row.append(w);
      }
      const ws = el("ul", "machine-list");
      for (const w of ag.workers) {
        const li = el("li", `worker ${w.status}`), text = el("span", "worker-text");
        const head = el("span"); text.append(head);
        head.append(el("strong", null, w.id), ` · ${w.project || "?"} · ${w.status === "running" ? `running ${mins(w.started)}` : `${w.status} ${ago(w.ended || w.started)}`}${w.branch ? ` · ${w.branch}` : ""}`);
        if (w.why) text.append(el("span", "worker-why", w.why));
        if (w.status === "running" && !m.online) text.append(el("span", "worker-why", `pc offline since ${ago(m.at)}: nobody knows whether it still runs`));
        if (w.status === "running" && w.progress) text.append(workerProgress(w.progress));
        li.append(text);
        if (w.yolo) li.append(el("span", "pill yolo", "YOLO"));
        li.append(machineButton("open", "How to look at this worker", () => toast(w.open || "No details yet.", false)));
        if (m.own && m.online && w.status === "running") li.append(machineButton("stop", "Stop this worker", () => confirm(`Stop the worker on ${w.id}?`) && machineAction(m, "stop_worker", { project: w.project, id: w.id })));
        if (m.own && m.online && w.status === "failed") li.append(machineButton("restart", "Start this worker again now (past the 6 h hold)", () => machineAction(m, "restart", { project: w.project, id: w.id })));
        if (m.own && w.status !== "running" && w.checkpoint) li.append(machineButton("roll back", "Put its worktree back as it was before its last turn (a checkpoint of now is kept)",
          () => confirm(`Roll ${w.id} back to before its last turn?`) && machineAction(m, "rollback", { project: w.project, id: w.id })));
        ws.append(li);
      }
      if (ag.workers.length) row.append(ws);
      if (ag.sessions.length) {
        const d = el("details", "machine-sessions"), list = el("ul", "machine-list");
        d.open = !narrow && ag.sessions.length <= 3;
        d.append(el("summary", null, `${ag.sessions.length} session${ag.sessions.length > 1 ? "s" : ""} in the last 2 h`));
        for (const s of ag.sessions) list.append(el("li", null, `${s.project || "?"} · ${s.task || "free"}${s.worker ? " (worker)" : ""} · ${ago(s.at)}`));
        d.append(list); row.append(d);
      }
      if (m.own && c && !m.old) {
        const ctl = el("div", "machine-controls");
        if (ag.trusted === false) ctl.append(machineButton("Trust hooks", "Trust the knowledge hooks the agent's own way", () => machineAction(m, "trust_hooks", { agent: a })));
        ctl.append(machineButton(ag.yolo ? "YOLO: ON · Turn off" : "YOLO: off · Turn on", "Run without asking: the agent's global setting on that pc", () => {
          if (ag.yolo) return machineAction(m, "yolo", { agent: a, on: false });
          if (prompt(`${YOLO_WARNING}\n\nType YOLO to turn it on for ${AGENT_NAME[a] || a} on ${m.label}.`) === "YOLO") return machineAction(m, "yolo", { agent: a, on: true, confirm: "YOLO" });
        }));
        const max = el("select", "small"); max.title = "At most this many workers of this agent at once";
        for (const n of [0, 1, 2, 3, 4]) { const o = el("option", null, `max ${n} worker${n === 1 ? "" : "s"}`); o.value = n; o.selected = n === ((c.maxWorkers || {})[a] ?? 1); max.append(o); }
        max.onchange = () => machineAction(m, "max_workers", { agent: a, n: Number(max.value) });
        ctl.append(max);
        row.append(ctl);
      }
      agents.append(row);
    }
    if (m.own && !m.old) {
      const ctl = el("div", "machine-controls");
      if (c) {
        if ((c.agents || []).length && (c.folders?.linked || []).length) ctl.append(machineButton("Chat", "Talk to an agent on this pc (Chats)", async () => { go("#/chats"); if (!ck.machines.length) await ckSummary().catch(() => {}); ckNewChat(m.id); }));
        ctl.append(machineButton("Update plugin", "Update the knowledge plugin of every agent there", () => machineAction(m, "update_plugin")));
      }
      ctl.append(machineButton("Forget", "Remove this pc from the list (it comes back when it reports again)", async () => {
        if (!confirm(`Forget ${m.label}?`)) return;
        try { await api("/ui/api/machines/forget", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: m.id }) }); } catch (e) { toast(e.message); }
        loadMachines();
      }));
      card.append(ctl);
    }
    const fl = c && c.folders;
    if (fl && (fl.linked.length || (m.own && fl.candidates.length))) {
      const d = el("details", "machine-sessions"), list = el("ul", "machine-list");
      d.append(el("summary", null, `Folders (${fl.linked.length} linked)`));
      for (const f of fl.linked) {
        const li = el("li"); li.append(el("span", null, `${f.path} → ${f.project}`));
        if (m.own) li.append(machineButton("unlink", "Remove its .knowledge-project file (nothing else)", () => confirm(`Unlink ${f.path}?`) && machineAction(m, "unlink_folder", { path: f.path })));
        list.append(li);
      }
      d.append(list);
      if (m.own && fl.candidates.length && (c.projects || []).length) {
        const ctl = el("div", "machine-controls"), path = el("select", "small"), proj = el("select", "small");
        for (const f of fl.candidates) { const o = el("option", null, f.path); o.value = f.path; path.append(o); }
        for (const p of c.projects) { const o = el("option", null, p); o.value = p; proj.append(o); }
        ctl.append(path, proj, machineButton("Link", "Write .knowledge-project in that folder", () => machineAction(m, "link_folder", { path: path.value, project: proj.value })));
        d.append(ctl);
      }
      card.append(d);
    }
    if (m.actions.length) {
      const d = el("details", "machine-sessions"), list = el("ul", "machine-list");
      d.append(el("summary", null, `Actions (${m.actions.length})`));
      for (const x of m.actions.slice().reverse()) list.append(el("li", x.status === "failed" ? "machine-error" : null,
        `${x.action}${x.args && Object.keys(x.args).length ? ` ${Object.values(x.args).join(" ")}` : ""} · ${x.status === "waiting" ? "waiting for machine" : x.status}${x.result ? `: ${x.result}` : ""} · ${x.by} ${ago(x.at)}`));
      d.append(list); card.append(d);
    }
    box.append(card);
  }
  if (!res.machines.length) box.append(el("p", "muted", "No machine has reported yet. Machines appear once a plugin 0.15.0 or newer asks the server."));
}
// Lint checks in plain words, with what to do about them (unknown checks fall back to their name).
const LINT_INFO = {
  usage_outlier: ["Session that used far more tokens than usual", "Look at that session's log: a loop, a huge file read or a runaway subagent? Nothing to fix when the work was simply big."],
  worker_unmerged: ["Worker branch not merged after a week", "A worker finished its task on a branch nobody merged: review the branch on that pc and merge it (the companion then removes the worktree), or delete the branch and worktree by hand."],
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

// ---------------------------------------------------------------- Agents: Inbox (the attention board) and Chats
// Chats with agents on your pcs: one card per chat (pc, agent, folder), the answer streaming in. The server only
// relays: the pc's companion pulls each message and runs one turn of the agent (plugin/hooks/chat.py). Everything is
// text (textContent): nothing an agent prints becomes markup. Inbox and Chats are routes (shell.js): #/chats/<chat id>
// is one chat (its card is keyed by pc and chat id).
const AGENT_MARK = { claude: "C", codex: "X", agy: "A" };
const ck = { cards: new Map(), machines: [], chats: [], board: [], view: "", timer: 0, summaryAt: 0, doneOpen: false, focus: "", hit: "", boardAt: 0, filter: "", waits: new Map() };
const ckKey = (c) => `${c.machine}/${c.id}`;
const ckBusy = (s) => s === "running" || s === "queued";
const ckShort = (path) => (path || "").split(/[\\/]/).filter(Boolean).pop() || path;
const ckScope = (x) => !agentProject || x.project === agentProject; // the header's project filter

// Polling of the pcs, chats and board runs while an Agents view shows; the router turns it on and off.
function ckRun(on) { clearTimeout(ck.timer); if (on) { ck.summaryAt = 0; ckTick(); } }

// The bar and the board: the pcs, the chats and the board every ~5 s on the board (10 s in a chat); the open chat
// every 1.5 s while it runs, else 4 s. Only the chats on screen are polled (the open one, or the board's cards on
// screen and zoomed in, mission.js bdLive): showing a chat's log is what marks it read.
async function ckTick() {
  if ($("cockpit").hidden) return;
  let live = [];
  try {
    if (Date.now() - ck.summaryAt > (ck.view ? 10000 : 5000)) await ckSummary();
    live = [ck.cards.get(ck.view), ...bdLive()].filter(Boolean);
    await Promise.all(live.map(ckPoll));
  } catch (e) { $("ck-board").title = e.message; }
  clearTimeout(ck.timer);
  ck.timer = setTimeout(ckTick, live.some((c) => ckBusy(c.meta.status)) ? 1500 : 4000);
}
async function ckSummary() {
  ck.summaryAt = Date.now();
  const [ms, cs, bd] = await Promise.all([api("/ui/api/machines"), api("/ui/api/chats"), api("/ui/api/board").catch(() => ({ items: [] }))]);
  ck.machines = ms.machines.filter((m) => m.own && !m.old);
  for (const m of ck.machines) for (const x of m.actions || []) { // a Merge queued from a card: say how it went on the pc
    const what = ck.waits.get(x.id);
    if (what && (x.status === "done" || x.status === "failed")) { ck.waits.delete(x.id); toast(`${what}: ${x.result || x.status}`, x.status === "failed"); }
  }
  ck.chats = cs.chats; ck.board = bd.items; ck.boardAt = Date.now(); inboxCount(ck.board);
  const now = Date.now() / 1000;
  let live = ck.chats.filter((c) => ckBusy(c.status)).length;
  const worst = {}; // per agent its fullest usage window over the pcs
  for (const m of ck.machines) for (const [a, ag] of Object.entries(m.agents || {})) {
    live += (ag.workers || []).filter((w) => w.status === "running").length + (ag.sessions || []).filter((s) => !s.worker && now - s.at < 600).length;
    if (ag.limit != null && ag.resets > now && (!worst[a] || ag.limit > worst[a].limit)) worst[a] = { ...ag, agent: a, label: m.label };
  }
  $("ck-live-n").textContent = live; $("ck-live").classList.toggle("on", live > 0);
  // one chip per agent with a known limit: "Claude 82% · Mon 12:00"; amber from 80%, red from 95%
  const chips = Object.values(worst).sort((a, b) => b.limit - a.limit).map((w) => {
    const at = new Date(w.resets * 1000), far = w.resets - now > 86400; // a weekly or monthly window: say the day too
    const when = far ? at.toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const c = el("span", `chip${w.limit >= 95 ? " bad" : w.limit >= 80 ? " warn" : ""}`, `${(AGENT_NAME[w.agent] || w.agent).replace(" Code", "")} ${Math.round(w.limit)}% · ${when}`);
    c.title = `${AGENT_NAME[w.agent] || w.agent} on ${w.label}: ${Math.round(w.limit)}% of its usage limit used; resets ${at.toLocaleString()}`;
    return c;
  });
  $("ck-resets").replaceChildren(...chips); $("ck-resets").hidden = !chips.length;
  for (const [k, card] of ck.cards) {
    if (card.worker) {
      const hit = ckFindWorker(`worker/${card.worker}`);
      if (!hit) ckDrop(k);
      else { card.m = hit.m; card.w = hit.w; card.meta = { ...card.meta, online: hit.m.online, status: hit.w.status }; }
      continue;
    }
    if (card.meta.draft) continue; // a new chat before its first message: no chat on the server yet
    if (card.meta.parent) { if (!ck.chats.some((x) => x.id === card.meta.parent)) ckDrop(k); continue; } // a subagent's chat lives with its chat
    const c = ck.chats.find((x) => ckKey(x) === k);
    if (!c) ckDrop(k);
    else card.meta = { ...card.meta, ...c };
  }
  if (ck.view && !ck.cards.has(ck.view)) { // a route's chat id (or an older pc/id key) once the chats are known
    const c = ck.chats.find((x) => x.id === ck.view || ckKey(x) === ck.view), hit = ckFindWorker(ck.view);
    if (c) ck.view = ckKey(ckCard(c).meta);
    else if (hit) ck.view = ckKey(ckWorkerCard(hit.m, hit.w).meta);
    else { // gone or never there: the list, without a dead chat in the URL
      ck.view = "";
      if (shellRoute.view === "chats" && shellRoute.arg) { history.replaceState(history.state, "", "#/chats"); shellRoute = { ...shellRoute, arg: "" }; lastRoute.chats = "#/chats"; }
    }
  }
  ckHistory(); ckBoard(); ckLayout();
}
// The Chats view's list: the chats waiting on you first, then the running ones, then the newest; a dot and a word
// say which (not colour alone). The filter matches title, pc, agent and folder; the buttons narrow to one state.
const CK_STATE = { needs: "needs you", running: "running", idle: "idle", closed: "closed" };
function ckState(c) { // an approval waiting in a running turn needs you too
  const needs = (ck.board || []).some((i) => i.col === "needs" && i.machine === c.machine && (i.kind === "chat" ? i.id : i.kind === "ask" ? i.chat : "") === c.id);
  return needs ? "needs" : ckBusy(c.status) ? "running" : c.closed ? "closed" : "idle";
}
function ckHistory() {
  const box = $("ck-history"), words = ck.filter.toLowerCase().split(/\s+/).filter(Boolean), only = $("ck-only").dataset.only || "";
  const rank = { needs: 0, running: 1, idle: 2, closed: 3 };
  const rows = ck.chats.filter(ckScope).map((c) => [c, ckState(c)])
    .filter(([c, st]) => (only ? st === only : st !== "closed") /* a closed chat only under Closed */ && words.every((w) => [c.title, c.label, c.folder, AGENT_NAME[c.agent] || c.agent].join(" ").toLowerCase().includes(w)))
    .sort((a, b) => rank[a[1]] - rank[b[1]] || (b[0].updated || 0) - (a[0].updated || 0));
  box.replaceChildren();
  for (const [c, st] of rows.slice(0, 60)) {
    const li = el("li"), b = el("button", `ck-chat ${st}`); b.type = "button";
    b.append(el("span", "ck-dot", ""), el("span", "ck-chat-title", c.title || "(no message yet)"),
      el("small", null, `${CK_STATE[st]} · ${AGENT_NAME[c.agent] || c.agent} · ${c.label} · ${ckShort(c.folder)} · ${ago(c.updated)}`));
    if (ckKey(c) === ck.view) b.setAttribute("aria-current", "true");
    b.onclick = () => ckShow(c);
    li.append(b); box.append(li);
    for (const s of (c.sub_list || []).slice().reverse()) { // its subagents, each a chat of its own, under it
      const sl = el("li", "ck-subrow"), sb = el("button", `ck-chat ck-subchat ${s.status === "running" ? "running" : "idle"}`); sb.type = "button";
      sb.append(el("span", "ck-dot", ""), el("span", "ck-chat-title", `↳ ${s.label}`), el("small", null, `subagent · ${s.status}`));
      if (ck.view === `${ckKey(c)}~${s.id}`) sb.setAttribute("aria-current", "true");
      sb.onclick = () => go(`#/chats/${encodeURIComponent(c.id)}/sub/${encodeURIComponent(s.id)}`);
      sl.append(sb); box.append(sl);
    }
  }
  if (!box.childNodes.length) box.append(el("li", "muted ck-none", !ck.chats.length ? "No chats yet." : words.length || only ? "No chat matches." : "No chats in this project."));
  for (const x of $("ck-only").querySelectorAll("button")) {
    const n = x.dataset.only ? ck.chats.filter((c) => ckScope(c) && ckState(c) === x.dataset.only).length : 0;
    x.setAttribute("aria-pressed", String(x.dataset.only === only)); x.lastChild.textContent = n ? ` ${n}` : "";
  }
}
let ckFilterTimer;
$("ck-filter").oninput = () => { clearTimeout(ckFilterTimer); ckFilterTimer = setTimeout(() => { ck.filter = $("ck-filter").value; ckHistory(); }, 120); };
for (const x of $("ck-only").querySelectorAll("button")) x.onclick = () => { $("ck-only").dataset.only = x.dataset.only; ckHistory(); };
// Open one chat (its route), or go back from it when `meta` is empty.
function ckShow(meta) {
  if (!meta) return goBack("#/chats");
  go(`#/chats/${encodeURIComponent(ckCard(meta).meta.id)}`);
}
function ckScrollBottom(card, force = false) {
  if (!card?.log) return;
  requestAnimationFrame(() => {
    if (force || card.stickToBottom !== false) {
      card.log.scrollTop = card.log.scrollHeight;
      card.stickToBottom = true;
      card.savedScrollTop = card.log.scrollTop;
    } else if (card.savedScrollTop != null) {
      card.log.scrollTop = card.savedScrollTop;
    }
  });
}

// The open chat in the Chats view, or a line to pick one. The card's node is moved only when it changes: moving it
// would drop the focus from its text box.
const ckPick = el("p", "ck-none ck-pick");
function ckLayout() {
  const card = ck.cards.get(ck.view), show = card ? card.root : ckPick;
  ckPick.textContent = ck.chats.length ? "Pick a chat on the left, or start a new one." : "No chats yet: start one with + New chat.";
  if ($("ck-canvas").firstChild !== show || $("ck-canvas").childNodes.length !== 1) $("ck-canvas").replaceChildren(show);
  document.body.classList.toggle("in-chat", !!card && !$("cockpit").hidden && $("cockpit").dataset.mode === "chats");
  if (card) ckScrollBottom(card, card.savedScrollTop == null);
  bdShow(); // the board (mission.js) instead of the list, on #/chats?view=board
}

// The attention board: what needs you first (a reply nobody read, a refused tool call, a failed worker, a task to
// approve, a result for you), then what runs and what finished in the last 24 h; grouped per project. Open task lines
// live under Tasks (one place for them). A phone shows the columns one under the other in that order.
const CK_COLS = [["needs", "Needs you"], ["running", "Running"], ["done", "Done (24 h)"]];
// Items that say the same thing (eleven tasks waiting for the same missing pc) fold into one group with a count.
const ckSame = (i) => (i.kind === "ask" || i.kind === "chat" ? "" : `${i.kind}|${i.col}|${i.kind === "worker" ? `${i.status}|${i.why || (i.offline_since ? "offline" : "")}` : i.line}`);
function ckBunches(list) {
  const by = new Map();
  for (const i of list) { const k = ckSame(i) || `one|${i.key}`; by.set(k, [...(by.get(k) || []), i]); }
  return [...by.values()].map((g) => {
    if (g.length < 2) return ckItem(g[0]);
    const d = el("details", "ck-bunch"), sum = el("summary"), first = g[0];
    d.open = g.some((i) => i.key === ck.hit); // a deep link into the group opens it
    sum.append(el("span", "ck-count", `${g.length}×`), el("span", "ck-bunch-line", first.kind === "worker" ? `workers ${first.status}${first.why ? `: ${first.why}` : first.offline_since ? ": pc offline" : ""}` : first.line || first.title));
    if (g.some((i) => i.overdue)) sum.append(el("span", "chip bad", "overdue"));
    d.append(sum, ...g.map(ckItem));
    return d;
  });
}
function ckBoard() {
  const items = (ck.board || []).filter((i) => i.col !== "open" && ckScope(i)), box = $("ck-board");
  if (!items.length && !ck.chats.length) {
    const empty = el("div", "ck-empty");
    empty.append(el("h2", null, "Your agents, from anywhere"),
      el("p", null, ck.machines.some((m) => m.online && m.companion) ? "Start a chat with Claude Code, Codex or agy on one of your pcs, in a folder linked to a project there."
        : "None of your pcs is online with a companion right now. Chats run on a pc through its knowledge companion (plugin 0.16 or newer)."));
    const b = el("button", "btn primary", "+ New chat"); b.type = "button"; b.onclick = () => ckNewChat(); empty.append(b);
    box.replaceChildren(empty);
  } else box.replaceChildren(...CK_COLS.map(([id, name]) => {
    const list = items.filter((i) => i.col === id), col = el("section", `ck-col ck-col-${id}${list.length ? " has-items" : ""}`), head = el("h2", "ck-col-head");
    col.setAttribute("aria-label", `${name}: ${list.length}`);
    head.append(el("span", null, name), el("span", "ck-count", String(list.length)));
    col.append(head);
    if (id === "done") { // a phone folds it to one line ("9 done") that unfolds; a desktop hides this button
      const fold = el("button", "ck-done-toggle", `${list.length} done${ck.doneOpen ? " · hide" : ""}`); fold.type = "button";
      fold.setAttribute("aria-expanded", String(ck.doneOpen));
      fold.onclick = () => { ck.doneOpen = !ck.doneOpen; ckBoard(); box.querySelector(".ck-done-toggle")?.focus(); };
      col.classList.toggle("open", ck.doneOpen); col.append(fold);
    }
    if (!list.length) col.append(el("p", "ck-none", id === "needs" ? "Nothing waits for you." : "Nothing here."));
    for (const p of [...new Set(list.map((i) => i.project || ""))]) {
      const group = el("div", "ck-group");
      group.append(el("h3", null, p ? shown(p) : "no project"), ...ckBunches(list.filter((i) => (i.project || "") === p)));
      col.append(group);
    }
    return col;
  }));
  if (items.length || ck.chats.length) {
    const more = el("p", "ck-foot muted", "Open task lines live under "), a = el("a", null, "Tasks"); a.href = "#/tasks";
    more.append(a, "; the board shows only what needs you, runs or just finished."); box.append(more);
  }
  // A project filter never hides what needs you without saying so.
  const elsewhere = (ck.board || []).filter((i) => i.col === "needs" && !ckScope(i)).length;
  if (elsewhere) {
    const b = el("button", "btn small ck-elsewhere", `${elsewhere} more need${elsewhere > 1 ? "" : "s"} you in other projects: show all`);
    b.type = "button"; b.onclick = () => switchProject(ALL); box.prepend(b); // the one picker: the sidebar shows All too
  }
  const n = items.filter((i) => i.col === "needs").length, asks = items.filter((i) => i.kind === "ask").length;
  const said = (n ? `${n} item${n > 1 ? "s" : ""} need${n > 1 ? "" : "s"} you` : "Nothing needs you") + (asks ? `, ${asks} approval${asks > 1 ? "s" : ""} waiting` : "");
  if ($("ck-needs-live").textContent !== said) $("ck-needs-live").textContent = said;
  ckFocus();
}
// #/inbox/<key> (the Matrix links): once the board is in, scroll to that item and mark it; a chat's item opens its
// chat. An unknown key leaves the plain inbox. ck.hit keeps the mark across the board's refreshes.
function ckFocus() {
  const key = ck.focus;
  if (!key || !ck.boardAt || ck.boardAt < (ck.focusAt || 0)) return; // a board older than the link: wait for the fresh one
  ck.focus = "";
  const i = (ck.board || []).find((x) => x.key === key);
  if (i && (i.kind === "chat" || i.kind === "ask")) return go(`#/chats/${encodeURIComponent(i.kind === "ask" ? i.chat : i.id)}`, true);
  if (i && i.col === "open") { history.replaceState(history.state, "", "#/tasks"); return applyRoute(); } // Tasks has open lines
  if (!i) { // gone or dismissed: the plain inbox
    history.replaceState(history.state, "", "#/inbox"); shellRoute = { ...shellRoute, arg: "" }; ck.hit = "";
    for (const c of $("ck-board").querySelectorAll(".ck-item.hit")) c.classList.remove("hit");
    return;
  }
  ck.hit = key;
  if (!ckScope(i)) { ck.focus = key; return switchProject(ALL); } // filtered away: show all (the sidebar too), then find it
  if (i.col === "done" && !ck.doneOpen) { ck.doneOpen = true; ck.focus = key; return ckBoard(); }
  const card = [...$("ck-board").querySelectorAll(".ck-item")].find((c) => c.dataset.key === key);
  if (!card) return;
  card.closest("details")?.setAttribute("open", "");
  for (const c of $("ck-board").querySelectorAll(".ck-item.hit")) c.classList.remove("hit");
  card.classList.add("hit");
  card.scrollIntoView({ block: "center", behavior: calm ? "auto" : "smooth" });
  card.querySelector(".ck-item-main").focus({ preventScroll: true });
}
async function ckDo(path, body) {
  try { await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
  catch (e) { toast(e.message); }
  ck.summaryAt = 0; ckTick();
}
function ckItem(i) {
  const card = el("article", `ck-item ${i.col}${i.key && i.key === ck.hit ? " hit" : ""}${i.result ? " result" : ""}`), main = el("button", "ck-item-main"), top = el("span", "ck-item-top");
  main.type = "button"; card.dataset.key = i.key || "";
  top.append(el("span", "ck-mark", AGENT_MARK[i.agent] || (i.owner || "?")[0].toUpperCase()), el("strong", null, i.kind === "ask" ? ckUnbidi(i.title) : i.title));
  if (i.overdue) top.append(el("span", "chip bad", "overdue"));
  if (i.subs) { const s = el("span", "chip live", `${i.subs} subagent${i.subs > 1 ? "s" : ""}`); s.title = "Subagents of this chat running now"; top.append(s); }
  const where = [i.kind === "review" ? `review · ${AGENT_NAME[i.agent] || i.agent}` : i.kind === "worker" ? "worker" : i.kind === "task" ? (i.result ? "result" : `task for ${i.owner}`) : i.kind === "ask" ? "approval" : "", i.label, ckShort(i.folder),
    i.due && !i.result ? `due ${i.due}` : "", i.at ? ago(i.at) : ""];
  main.append(top, el("span", "ck-item-where", where.filter(Boolean).join(" · ")), el("span", "ck-item-line", i.line));
  if (i.kind === "worker" && i.why && !i.line.includes(i.why)) main.append(el("span", "ck-item-why", i.why)); // the line says why and "pc offline since"
  if (i.progress) main.append(workerProgress(i.progress));
  if (i.kind === "review") main.append(ckReview(i));
  card.append(main);
  main.title = i.kind === "chat" || i.kind === "ask" ? "Open this chat" : i.kind === "task" || i.kind === "review" ? "Open its line in the memo" : "How to look at this worker";
  main.onclick = () => {
    if (i.kind === "chat") return ckShow(ck.chats.find((c) => ckKey(c) === `${i.machine}/${i.id}`) || i);
    if (i.kind === "ask") return ckShow(ck.chats.find((c) => ckKey(c) === `${i.machine}/${i.chat}`));
    if (i.kind === "task") { return openNote(i.path, { find: [i.id.toLowerCase()] }); }
    if (i.kind === "review") return openNote(i.path, { find: [`${i.task.toLowerCase()}-done`] });
    const more = card.querySelector(".ck-item-open");
    if (more) more.remove(); else card.append(el("p", "ck-item-open", i.open || "No details yet."));
  };
  const acts = el("div", "ck-item-acts"), act = (text, title, fn) => { const b = el("button", "btn small", text); b.type = "button"; b.title = title; b.onclick = fn; acts.append(b); };
  if (i.git) acts.append(ckGitPill(i));
  if (i.approve) act("Approve", "Let this task start", () => ckDo("/ui/api/task/approve", { project: i.project, id: i.id }));
  if (i.kind === "ask") { // the board shows a short title: Allow only when that title is the whole request
    for (const [d, text, title] of ckAskButtons(i).filter(([d]) => i.full !== false || d === "deny")) act(text, title, () => ckDo("/ui/api/chat/approve", { machine: i.machine, id: i.chat, ask: i.id, decision: d }));
    if (i.full === false) act("Open chat", "The request is longer than the board shows: read it whole in its chat", () => main.click());
  }
  for (const h of i.hosts || []) act(`Start on ${h.label}`, "Start a worker for this task there now",
    () => ckDo("/ui/api/machines/action", { machine: h.machine, action: "start_worker", args: { project: i.project, id: i.id } }));
  if (i.kind === "worker") act("Follow", "Watch this worker, steer it or take it over", () => ckFollow(i));
  if (i.kind === "worker" && (i.can_stop ?? i.status === "running")) act("Stop", "Stop this worker",
    () => confirm(`Stop the worker on ${i.id}?`) && ckDo("/ui/api/machines/action", { machine: i.machine, action: "stop_worker", args: { project: i.project, id: i.id } }));
  if (i.kind === "worker" && i.status === "failed" && i.can_restart) act("Restart", "Start this worker again now",
    () => ckDo("/ui/api/machines/action", { machine: i.machine, action: "restart", args: { project: i.project, id: i.id } }));
  if (i.kind === "review") {
    act("View diff", `The changes on ${i.branch}, read on ${i.label}`, () => ckDiff(i));
    if (i.can_merge) act("Merge", `git merge --no-ff ${i.branch} into ${i.into} on ${i.label}`, () => ckMerge(i));
    act("Reject", "Keep the branch, take the card off the board; optionally send it back to the agent", () => ckReject(card, i));
  }
  if (i.result) act("Open memo", "Read the whole line in its memo; remove it there (or under Tasks) once read", () => main.click());
  if (i.kind === "chat" && i.col !== "running") act("Close", "Close this chat: off the board and the chat list (under Closed); a new message opens it again",
    () => ckDo("/ui/api/chat/close", { machine: i.machine, id: i.id, closed: true }));
  if (i.col === "needs" && !i.approve && i.kind !== "ask" && i.kind !== "review" && i.kind !== "chat") act("Dismiss", "Take it off the board until it changes again", () => ckDo("/ui/api/board/dismiss", { key: i.key, sig: i.sig }));
  if (i.kind === "chat" && i.col === "running") act("Stop", "Stop this turn", () => ckDo("/ui/api/chat/stop", { machine: i.machine, id: i.id }));
  if (acts.childNodes.length) card.append(acts);
  return card;
}

// A finished worker's branch, for review: what the pc counted, what the worker said it did, left and tested. The
// server runs nothing of it; Merge is a fixed action of that pc's companion, which refuses a dirty or other checkout.
function ckReview(i) {
  const box = el("span", "progress ck-review"), row = (k, v) => { const r = el("span", "ck-review-row"); r.append(el("b", null, k), " ", v); box.append(r); };
  if (i.commit) row("last commit", i.commit);
  if (i.done) row("done", i.done);
  if (i.left) row("left", i.left);
  row("tests", i.tests || "not reported by the worker");
  if (i.blocked) { const r = el("span", "ck-review-row ck-item-why"); r.append(el("b", null, "merge by hand"), " ", i.blocked); box.append(r); }
  if (i.files.length) box.append(el("code", "progress-log", i.files.join("\n") + (i.nfiles > i.files.length ? `\n… ${i.nfiles - i.files.length} more` : "")));
  return box;
}
async function ckMerge(i) {
  if (!confirm(`Merge ${i.branch} into ${i.into} on ${i.label}?\n\nIts companion runs git merge --no-ff in the main checkout, only when that is on ${i.into} and has no uncommitted changes. A conflict is aborted, nothing is pushed.`)) return;
  try {
    const { id } = await api("/ui/api/machines/action", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ machine: i.machine, action: "merge", args: { project: i.project, id: i.id } }) });
    ck.waits.set(id, `Merge ${i.branch} on ${i.label}`);
    toast(`Asked ${i.label} to merge ${i.branch} into ${i.into}`, false);
  } catch (e) { toast(e.message); }
  ck.summaryAt = 0; ckTick();
}
function ckReject(card, i) {
  const old = card.querySelector(".ck-reject");
  if (old) return old.remove();
  const form = el("form", "ck-reject"), reason = el("input", "small"), lab = el("label", "ck-reject-back"), back = el("input"), go = el("button", "btn small danger", "Reject");
  reason.placeholder = "Why (optional; needed to send it back)"; reason.maxLength = 300; reason.setAttribute("aria-label", "Reason");
  back.type = "checkbox"; lab.append(back, ` send back to ${AGENT_NAME[i.agent] || i.agent} as ${i.task}-fix`);
  go.type = "submit";
  form.append(reason, lab, go);
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (back.checked && !reason.value.trim()) return toast("Sending it back needs a reason: the agent only gets that line.");
    go.disabled = true;
    try {
      await api("/ui/api/review/reject", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: i.key, reason: reason.value, send_back: back.checked }) });
      toast(back.checked ? `Sent ${i.task} back to ${AGENT_NAME[i.agent] || i.agent}` : `Review of ${i.task} rejected; the branch stays`, false);
    } catch (err) { toast(err.message); go.disabled = false; return; }
    ck.summaryAt = 0; ckTick();
  };
  card.append(form); reason.focus();
}

// The git pill: branch · changed files · commits ahead/behind the branch it is compared with (the companion counts,
// once a round). A tap asks that pc for the diff, read-only, through the chats' pull channel.
function ckGitText(g) { return `${g.branch} · ●${g.changed}${g.base ? ` · ↑${g.ahead}↓${g.behind}` : ""}`; }
function ckGitPill(i) {
  const g = i.git, b = el("button", "btn small ck-git", ckGitText(g)); b.type = "button";
  b.setAttribute("aria-label", `Git: branch ${g.branch}, ${g.changed} changed files${g.base ? `, ${g.ahead} commits ahead and ${g.behind} behind ${g.base}` : ""}. Show the changes`);
  b.title = g.base ? `Compared with ${g.base}. Show the changes` : "Show the changes";
  b.onclick = (e) => { e.stopPropagation(); ckDiff(i); };
  return b;
}
let ckDiffAsk = 0;
async function ckDiff(i) {
  const body = $("ck-diff-body"), ask = ++ckDiffAsk, say = (cls, text) => body.replaceChildren(el("p", cls, text));
  $("ck-diff-title").textContent = `Changes · ${i.title}`;
  $("ck-diff-where").textContent = `${i.label} · ${i.kind === "worker" || i.kind === "review" ? `worker ${i.id}` : i.folder}${i.git ? ` · ${ckGitText(i.git)}` : ""}`;
  say("muted", `Asking ${i.label}…`);
  if (!$("ck-diff-dialog").open) $("ck-diff-dialog").showModal();
  try {
    const { id } = await api("/ui/api/git/diff", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(i.kind === "worker" || i.kind === "review" ? { machine: i.machine, worker: `${i.project}/${i.id}` } : { machine: i.machine, chat: i.id }) });
    for (let n = 0; n < 40 && ask === ckDiffAsk && $("ck-diff-dialog").open; n++) {
      await new Promise((r) => setTimeout(r, n ? 1000 : 300));
      const d = await api(`/ui/api/git/diff?machine=${i.machine}&id=${id}`);
      if (d.status !== "done") continue;
      if (d.error) return say("error", d.error);
      return body.replaceChildren(d.text.trim() ? diffView(d.text) : el("p", "muted", "No changes."));
    }
    if (ask === ckDiffAsk && $("ck-diff-dialog").open) say("error", `${i.label} did not answer: its companion may be older than 0.17 or busy.`);
  } catch (e) { say("error", e.message); }
}
$("ck-diff-close").onclick = () => $("ck-diff-dialog").close();

function ckCard(meta) {
  const k = ckKey(meta);
  if (ck.cards.has(k)) return ck.cards.get(k);
  const root = el("article", "ck-card"), head = el("header", "ck-head"), title = el("div", "ck-title");
  const name = el("strong"), where = el("span"), state = el("span", "ck-state"), x = el("button", "btn icon small quiet ck-x", "×");
  x.type = "button"; x.title = "Back to the board (the chat stays under Earlier chats)"; x.setAttribute("aria-label", "Back to the board");
  title.append(name, where);
  const git = el("span", "ck-git-slot"), open = el("button", "btn icon small quiet ck-x bd-only", "↗"); // on the board: the full chat
  open.type = "button"; open.title = "Open the full chat"; open.setAttribute("aria-label", "Open the full chat");
  const mark = el("span", "ck-mark", AGENT_MARK[meta.agent] || "+"), close = el("button", "btn small quiet ck-close", "Close");
  const tok = el("span", "chip ck-tok"); tok.hidden = true;
  close.type = "button"; close.hidden = true;
  head.append(el("span", "ck-dot bd-only"), mark, title, git, tok, state, close, open, x);
  const log = el("div", "ck-log"), form = el("form", "ck-form"), input = el("textarea"), btns = el("div", "ck-btns");
  log.setAttribute("aria-live", "polite"); log.setAttribute("role", "log");
  log.addEventListener("click", (e) => {
    const a = e.target.closest("a"); if (!a) return;
    const href = a.getAttribute("href") || "";
    if (/^https?:/.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
  });
  log.addEventListener("scroll", () => {
    const dist = log.scrollHeight - log.scrollTop - log.clientHeight;
    card.stickToBottom = dist < 80;
    card.savedScrollTop = log.scrollTop;
  }, { passive: true });
  input.rows = 1; input.placeholder = "Message the agent…"; input.setAttribute("aria-label", "Message (Enter a new line, Ctrl+Enter sends)");
  const stop = el("button", "btn small ck-stop", "Stop"), send = el("button", "btn small primary ck-send", "Send"), note = el("p", "ck-note");
  // a live agent at work: Queue (after this turn) and Force (stop the step, then this) in a small menu beside Send
  const more = el("details", "menu up ck-more"), moreSum = el("summary", "btn small ck-send-more", "▾"), moreList = el("div", "menu-list");
  moreSum.title = "Queue or Force"; moreSum.setAttribute("aria-label", "Queue or Force this message");
  const queueBtn = el("button", "ck-queue"), forceBtn = el("button", "ck-forcebtn");
  queueBtn.append("Queue", el("span", "muted", "after this turn · Alt+Enter")); forceBtn.append("Force", el("span", "muted", "stop the step, then this · Ctrl+Shift+Enter"));
  queueBtn.type = forceBtn.type = "button"; queueBtn.hidden = forceBtn.hidden = more.hidden = true;
  moreList.append(queueBtn, forceBtn); more.append(moreSum, moreList);
  // under the box: the model (one chip, its effort inside) and, in a new chat, where it runs (ckPicker)
  const picks = el("div", "ck-picks"); picks.hidden = true;
  // files for the next message: picked, pasted (a screenshot) or dropped; they go up when it is sent
  const attachBtn = el("button", "btn icon small quiet ck-attach"), filePick = el("input"), files = el("div", "ck-files");
  attachBtn.innerHTML = CK_ICON.clip;
  attachBtn.type = "button"; attachBtn.title = "Attach files or a screenshot (or paste or drop them here)"; attachBtn.setAttribute("aria-label", "Attach files");
  filePick.type = "file"; filePick.multiple = true; filePick.hidden = true; files.hidden = true;
  const mic = el("button", "btn icon small quiet ck-mic"); // speech to text into the box (server's KNOWLEDGE_STT_URL)
  mic.innerHTML = CK_ICON.mic;
  mic.type = "button"; mic.hidden = !ckMicOk(); mic.title = "Speak (click to start and stop, or hold; Ctrl+Space). The text lands in the box; you send it."; mic.setAttribute("aria-label", "Speak a message");
  // one box: the files, the text, and a bar under it (attach and model left; microphone, Stop and Send right)
  const box = el("div", "ck-box"), bar = el("div", "ck-bar"), barL = el("div", "ck-bar-l"), sendGroup = el("span", "ck-sendgroup");
  stop.type = "button"; send.type = "submit"; sendGroup.append(send, more);
  barL.append(attachBtn, filePick, picks); btns.append(mic, stop, sendGroup); bar.append(barL, btns);
  box.append(files, input, bar); form.append(box, note);
  box.addEventListener("click", (e) => { if (e.target === box || e.target === bar || e.target === barL) input.focus(); });
  const asks = el("div", "ck-asks"); asks.hidden = true; asks.setAttribute("aria-live", "assertive");
  const subbar = el("div", "ck-subbar"); subbar.hidden = true; subbar.setAttribute("aria-label", "Subagents running now");
  root.append(head, log, subbar, asks, form);
  const card = { meta: { ...meta }, root, name, where, git, state, log, asks, form, input, stop, send, note, picks, mark, close, tok, subbar, queueBtn, forceBtn, more, filesBox: files, files: [], mode: "", pick: null, after: 0, online: meta.online !== false, stickToBottom: true, savedScrollTop: null };
  x.onclick = () => ckShow(null);
  open.onclick = () => ckShow(card.meta);
  input.addEventListener("keydown", (e) => {
    // Enter is a new line; Ctrl/Cmd+Enter presses the button (Send, Queue or Force)
    if (e.key === " " && e.ctrlKey && !mic.hidden) { e.preventDefault(); return mic.toggle(); }
    if (e.key === "Enter" && e.altKey && !e.isComposing && !queueBtn.hidden) { e.preventDefault(); return queueBtn.click(); } // queue
    if (e.key === "Enter" && e.shiftKey && (e.ctrlKey || e.metaKey) && !e.isComposing && !forceBtn.hidden) { e.preventDefault(); return forceBtn.click(); }
    if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey) || e.isComposing) return;
    e.preventDefault(); form.requestSubmit();
  });
  const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 220)}px`; }; // the box grows with the text
  input.addEventListener("input", () => { grow(); ckPaint(card); }); // typing turns Force back into Queue
  attachBtn.onclick = () => filePick.click();
  ckMic(card, mic);
  filePick.onchange = () => { ckAddFiles(card, filePick.files); filePick.value = ""; };
  input.addEventListener("paste", (e) => { const fs = e.clipboardData?.files; if (fs?.length) { e.preventDefault(); ckAddFiles(card, fs); } });
  form.addEventListener("dragover", (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) { e.preventDefault(); form.classList.add("drop"); } });
  form.addEventListener("dragleave", (e) => { if (!form.contains(e.relatedTarget)) form.classList.remove("drop"); });
  form.addEventListener("drop", (e) => { if (!e.dataTransfer?.files?.length) return; e.preventDefault(); form.classList.remove("drop"); ckAddFiles(card, e.dataTransfer.files); });
  form.onsubmit = async (e) => {
    e.preventDefault();
    let text = input.value.trim(); const mode = card.mode || "now";
    card.mode = ""; // Queue or Force counts for this press only, also when it goes no further
    if (!text && !card.files.length && card.send.classList.contains("force")) { // empty box with messages waiting: the next one goes in now
      send.disabled = true;
      try { await api("/ui/api/chat/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id, force: true }) }); }
      catch (err) { note.className = "ck-note bad"; note.textContent = err.message; }
      return ckPoll(card);
    }
    if ((!text && !card.files.length) || send.disabled) return;
    send.disabled = true;
    if (card.files.length) { // up first; the message names them (the companion puts them on that pc)
      try {
        if (card.worker) throw new Error("Attachments do not reach a worker; open it as a chat first.");
        const machine = card.meta.draft ? (card.where_ || "").split("\n")[0] : card.meta.machine;
        if (machine) text = [text, ...(await ckUpload(machine, card.files))].filter(Boolean).join("\n");
      } catch (err) { note.className = "ck-note bad"; note.textContent = err.message; send.disabled = false; return; }
    }
    card.stickToBottom = true;
    ckScrollBottom(card, true);
    try {
      if (card.worker) await api("/ui/api/worker/steer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, worker: card.worker, text }) });
      else if (card.meta.draft) return await ckStart(card, text);
      else if (card.meta.parent) { // to the main agent, which passes it on
        await api("/ui/api/chat/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, id: card.meta.parent, text: ckSubWrap(card, text), mode: "now" }) });
        toast("Sent to the main agent, which passes it on", false);
      }
      else {
        // only a choice made here: a saved model the pc no longer lists must not turn into Default unasked
        const pick = card.pick?.touched ? { model: card.pick.id, effort: card.pick.effort } : {};
        // mode: Send into the turn (a live agent) or after it (others); Queue or Force from their buttons
        await api("/ui/api/chat/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id, text, mode, ...pick }) });
        Object.assign(card.meta, pick); card.pick = null;
      }
      input.value = ""; input.style.height = ""; card.files = []; ckFilesPaint(card); if (!card.worker) card.meta.status = "queued";
    } catch (err) { note.className = "ck-note bad"; note.textContent = err.message; }
    ckPaint(card); await ckPoll(card); clearTimeout(ck.timer); ck.timer = setTimeout(ckTick, 1500);
  };
  close.onclick = async () => { // off the board and the list (Closed); a new message opens it again
    const closed = !card.meta.closed;
    try {
      await api("/ui/api/chat/close", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id, closed }) });
      card.meta.closed = closed; ck.summaryAt = 0;
      if (closed) { toast("Closed: under Closed in the list; a new message opens it again", false); ckShow(null); } else ckPaint(card);
      ckTick();
    } catch (err) { note.className = "ck-note bad"; note.textContent = err.message; }
  };
  queueBtn.onclick = () => { more.open = false; card.mode = "queue"; form.requestSubmit(); };
  forceBtn.onclick = () => { more.open = false; card.mode = "force"; form.requestSubmit(); };
  stop.onclick = async () => {
    if (card.worker) return ckWorkerAct(card, "stop_worker");
    try { await api("/ui/api/chat/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id }) }); }
    catch (err) { note.className = "ck-note bad"; note.textContent = err.message; }
    ckPoll(card);
  };
  ck.cards.set(k, card); ckPaint(card);
  return card;
}
// The chat list's width: drag its edge (or arrow keys on it); kept in the browser.
{
  const box = $("ck-chats"), bar = $("ck-resize"), clamp = (w) => Math.max(200, Math.min(640, Math.round(w)));
  const set = (w, keep = true) => { box.style.setProperty("--ck-list", `${w}px`); bar.setAttribute("aria-valuenow", w); if (keep) store.set("kn_ck_list", w); };
  set(clamp(+store.get("kn_ck_list") || 300), false);
  bar.setAttribute("aria-valuemin", 200); bar.setAttribute("aria-valuemax", 640);
  bar.addEventListener("pointerdown", (e) => {
    e.preventDefault(); bar.setPointerCapture(e.pointerId); bar.classList.add("drag");
    const left = box.getBoundingClientRect().left;
    const move = (m) => set(clamp(m.clientX - left));
    const stop = () => { bar.removeEventListener("pointermove", move); bar.classList.remove("drag"); };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", stop, { once: true });
    bar.addEventListener("pointercancel", stop, { once: true }); // a touch the browser took over: no move left behind
  });
  bar.addEventListener("keydown", (e) => {
    const w = +bar.getAttribute("aria-valuenow");
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); set(clamp(w + (e.key === "ArrowRight" ? 20 : -20))); }
  });
  bar.addEventListener("dblclick", () => set(300));
}
// Settings (#/settings): the person's own choices, kept on the server (/ui/api/me/prefs). Now: the model a new chat
// starts with ("" = the one used last), from the models the pcs online now offer.
async function loadSettings() {
  if (!ck.machines.length) await ckSummary().catch(() => {});
  const cat = ckCatalog(ck.machines.filter((m) => m.online)), sel = $("set-model"), eff = $("set-effort");
  const now = me.prefs?.chat_default, key = (x) => `${x.agent}/${x.id}`;
  sel.replaceChildren(new Option("The one I used last", ""));
  for (const fam of [...new Set(cat.map((m) => ckFamily(m.id)))]) {
    const g = document.createElement("optgroup"); g.label = fam;
    for (const m of cat.filter((y) => ckFamily(y.id) === fam)) g.append(new Option(`${m.name} · ${AGENT_NAME[m.agent] || m.agent}`, key(m)));
    sel.append(g);
  }
  if (now && !cat.some((m) => key(m) === key(now))) sel.append(new Option(`${now.id || "own model"} · ${AGENT_NAME[now.agent] || now.agent} (no pc online offers it now)`, key(now)));
  sel.value = now ? key(now) : "";
  const fill = (want) => {
    const m = cat.find((y) => key(y) === sel.value), lv = m ? levels(m) : [];
    eff.replaceChildren(new Option("auto (the agent's setting)", ""), ...lv.map((e) => new Option(e === m.default ? `${e} (this model's default)` : e, e)));
    eff.value = lv.includes(want) ? want : m?.default || "";
    eff.disabled = !lv.length;
  };
  fill(now?.effort || "");
  sel.onchange = () => fill("");
  const on = $("set-notify-on"), note = $("set-notify-note");
  on.checked = notifyOn(); on.disabled = !window.Notification;
  if (!window.Notification) note.textContent = "This browser has no notifications (on a phone: Matrix, admin page Settings: needs_per_hour).";
  on.onchange = async () => {
    if (on.checked && Notification.permission !== "granted") on.checked = (await Notification.requestPermission()) === "granted";
    if (!on.checked && Notification.permission === "denied") toast("Notifications are blocked for this site in the browser settings", true);
    store.set("kn_notify", on.checked ? "1" : "0");
  };
  $("set-chat").onsubmit = async (e) => {
    e.preventDefault();
    const [agent, ...id] = sel.value.split("/"), chat_default = sel.value ? { agent, id: id.join("/"), effort: eff.value } : null;
    try {
      const res = await api("/ui/api/me/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chat_default }) });
      me.prefs = res.prefs; toast("Saved", false);
    } catch (err) { toast(err.message, true); }
  };
}
// A subagent as a chat of its own (#/chats/<chat>/sub/<id>): its own output only, taken from its chat's events (sub);
// a message goes to the main agent, which hands it on (Claude: SendMessage to its agent id; Codex: send_input to its
// thread); agy has no way to reach a subagent, so there it is read-only.
function ckSubCard(c, sid) {
  const card = ckCard({ ...c, id: `${c.id}~${sid}`, parent: c.id, sub: sid, title: "", status: "idle" });
  card.close.hidden = true; card.picks.hidden = true;
  card.input.placeholder = "Message this subagent (its main agent passes it on)… (Ctrl+Enter sends)";
  return card;
}
const ckSubInfo = (card) => (ck.chats.find((x) => x.id === card.meta.parent && x.machine === card.meta.machine)?.sub_list || []).find((s) => s.id === card.meta.sub) || {};
function ckSubWrap(card, text) {
  const s = ckSubInfo(card), name = s.label || "your subagent";
  if (card.meta.agent === "codex") return `Pass this on to your subagent "${name}" with send_input (thread ${s.aid || card.meta.sub}), then tell me what it answers:\n\n${text}`;
  if (card.meta.agent === "agy") return `Pass this on to your subagent "${name}" with send_message (Recipient: ${s.aid || "its conversation"}), then tell me what it answers:\n\n${text}`;
  return `Pass this on to your subagent "${name}" with SendMessage (to: ${s.aid || card.meta.sub}) so it goes on with it, then tell me what it answers:\n\n${text}`;
}
async function ckSubPoll(card) {
  let res;
  try { res = await api(`/ui/api/chat?machine=${card.meta.machine}&id=${card.meta.parent}&after=${card.after}&peek=1`); }
  catch (e) { card.note.className = "ck-note bad"; card.note.textContent = e.message; return; }
  const mine = res.events.filter((e) => e.sub === card.meta.sub).map((e) => (e.kind === "sub" ? (e.status === "running"
    ? { ...e, kind: "info", text: `Subagent: ${e.text}`, sub: undefined } : { ...e, kind: "text", text: `${e.text}\n\n`, sub: undefined }) : { ...e, sub: undefined }));
  ckEvents(card, { ...res, events: mine });
  card.after = Math.max(card.after, ...res.events.map((e) => e.n), 0);
  const s = ckSubInfo(card), parent = res.chat;
  card.meta = { ...card.meta, label: res.label, online: res.online, status: s.status === "running" ? "running" : "idle" };
  card.online = res.online;
  card.name.textContent = s.label || "Subagent";
  card.where.textContent = `Subagent of “${parent.title || "a chat"}” · ${AGENT_NAME[card.meta.agent] || card.meta.agent} · ${res.label}`;
  card.state.hidden = false; card.state.className = `ck-state ${s.status === "running" ? "running" : ""}`; card.state.textContent = s.status || "done";
  const readOnly = card.meta.agent === "agy" && !s.aid; // agy: its conversation id comes with the subagent's end
  card.input.disabled = card.send.disabled = readOnly || !res.online;
  card.stop.hidden = card.queueBtn.hidden = card.forceBtn.hidden = true; card.send.textContent = "Send";
  if (!card.note.classList.contains("bad")) card.note.textContent = readOnly ? "agy names this subagent's conversation once it has ended: then you can write to it here." : "Your message goes to the main agent, which passes it on to this subagent.";
}
function ckDrop(k) { ck.cards.delete(k); if (ck.view === k) ck.view = ""; }
const CK_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]; // the server's EFFORTS, in order
// The model picker: one chip under the box ("Opus · high ▾"), a panel with the models grouped by family and the chosen
// model's effort levels. Picking a model sets its own default effort; the agent (Claude Code, Codex, agy) follows from
// the model. In a running chat only its agent's models are open: a conversation cannot move to another agent.
const CK_FAMILY = [[/^(claude|fable|opus|sonnet|haiku)/i, "Claude", "claude"], [/^(gpt|o\d|codex)/i, "GPT", "codex"], [/^gemini/i, "Gemini", "agy"]];
const ckFamily = (id) => (CK_FAMILY.find(([re]) => re.test(id)) || [0, "Other"])[1];
function ckCatalog(pcs) { // one entry per agent and model: [{agent, id, name, efforts, default, pcs}]
  const by = new Map();
  for (const m of pcs) for (const agent of m.companion?.agents || []) for (const x of (m.companion.models || {})[agent]?.length
    ? m.companion.models[agent] : [{ id: "", name: `${AGENT_NAME[agent] || agent} (its own model)`, efforts: [] }]) { // a companion before 0.22 lists none
    let e = by.get(`${agent}/${x.id}`);
    if (!e) by.set(`${agent}/${x.id}`, e = { agent, ...x, pcs: [] });
    e.pcs.push(m.id);
  }
  // Claude, GPT, Gemini, the rest; in a family its own agent's models first (Claude Code's Opus before agy's)
  const fam = (x) => { const i = CK_FAMILY.findIndex(([re]) => re.test(x.id)); return i < 0 ? CK_FAMILY.length : i; };
  const home = (x) => (CK_FAMILY[fam(x)]?.[2] === x.agent ? 0 : 1);
  return [...by.values()].map((x, n) => [x, n]).sort(([a, i], [b, j]) => fam(a) - fam(b) || home(a) - home(b) || i - j).map(([x]) => x);
}
const levels = (m) => CK_EFFORTS.filter((e) => (m.efforts || []).includes(e));
const ckModelOf = (card) => (card.catalog || []).find((x) => x.agent === card.pick?.agent && x.id === card.pick?.id);
function ckPicker(card) {
  const draft = card.meta.draft, mine = ck.machines.find((m) => m.id === card.meta.machine);
  card.catalog = ckCatalog(draft ? ckDraftPcs() : mine ? [mine] : []);
  if (!draft && !card.catalog.some((x) => x.agent === card.meta.agent)) { card.picks.hidden = true; return; }
  if (!card.pick?.touched) {
    const last = JSON.parse(store.get("kn_chat_pick") || "null"), pref = me.prefs?.chat_default;
    const offered = (x) => x && card.catalog.some((y) => y.agent === x.agent && y.id === x.id);
    const saved = draft ? (offered(pref) ? pref : last) // Settings, else (or when no pc online runs it) the last one used
      : { agent: card.meta.agent, id: card.meta.model || "", effort: card.meta.effort || "" };
    const hit = saved && card.catalog.find((x) => x.agent === saved.agent && x.id === saved.id);
    const ok = hit || (!draft && saved.id === ""), first = draft ? card.catalog.find((x) => x.agent === "claude") || card.catalog[0] : null;
    card.pick = ok ? { ...saved, effort: !hit || levels(hit).includes(saved.effort) ? saved.effort : hit.default || "" }
      : first ? { agent: first.agent, id: first.id, effort: first.default || "" }
      : draft ? null : { agent: card.meta.agent, id: "", effort: "" }; // a model the pc no longer lists: the agent's own, never another agent's
  }
  const sig = JSON.stringify([card.catalog.map((x) => [x.agent, x.id, x.efforts, x.default, x.pcs]), card.pick, card.meta.agent, draft && agentProject]);
  if (card.picks.dataset.sig === sig) return; // unchanged: a poll must not close an open panel
  card.picks.dataset.sig = sig;
  card.picks.hidden = !card.pick;
  if (!card.pick) return card.picks.replaceChildren();
  const x = ckModelOf(card), menu = el("details", "menu up ck-model"), sum = el("summary", "btn small");
  sum.textContent = `${x ? x.name : `${AGENT_NAME[card.pick.agent] || card.pick.agent} default`}${card.pick.effort ? ` · ${card.pick.effort}` : ""}`; // ▾ from .menu
  sum.title = `Model and effort from the next turn on (${AGENT_NAME[card.pick.agent] || card.pick.agent})`;
  const panel = el("div", "menu-list ck-model-list");
  if (card.catalog.some((m) => m.agent === card.pick.agent && m.id)) { // back to what the agent itself would pick
    const own = el("button", "ck-mopt ck-own", `${AGENT_NAME[card.pick.agent] || card.pick.agent}'s own model`); own.type = "button";
    own.setAttribute("aria-pressed", String(!card.pick.id));
    own.onclick = (e) => { e.stopPropagation(); card.pick = { agent: card.pick.agent, id: "", effort: "", touched: true }; ckPicker(card); ckDraftWhere(card); };
    panel.append(own);
  }
  const fams = [...new Set(card.catalog.map((m) => ckFamily(m.id)))];
  for (const fam of fams) {
    const row = el("div", "ck-mrow"), opts = el("div", "ck-mopts");
    row.append(el("span", "ck-mfam", fam), opts);
    for (const m of card.catalog.filter((y) => ckFamily(y.id) === fam)) {
      const b = el("button", "ck-mopt"), other = !draft && m.agent !== card.meta.agent;
      b.type = "button"; b.append(document.createTextNode(m.name), el("small", null, AGENT_NAME[m.agent] || m.agent));
      b.setAttribute("aria-pressed", String(m === x)); b.disabled = other;
      b.title = other ? `${AGENT_NAME[m.agent] || m.agent}: start a new chat for this model` : m.id;
      b.onclick = (e) => {
        e.stopPropagation(); // the chip is rebuilt: the outside-click listener would see a detached button and close it
        card.pick = { agent: m.agent, id: m.id, effort: m.default || "", touched: true }; ckPicker(card); ckDraftWhere(card);
        const again = card.picks.querySelector("details.ck-model"); if (again && levels(m).length) again.open = true; // then the effort
      };
      opts.append(b);
    }
    panel.append(row);
  }
  if (x && levels(x).length) { // agy has none: the model name carries the level
    const row = el("div", "ck-mrow ck-effort"), seg = el("div", "seg");
    row.append(el("span", "ck-mfam", "Effort"), seg);
    for (const e of ["", ...levels(x)]) {
      const b = el("button", null, e || "auto"); b.type = "button";
      b.title = e ? `${e}${e === x.default ? " (this model's default)" : ""}` : "The agent's own setting";
      b.setAttribute("aria-pressed", String(card.pick.effort === e));
      b.onclick = () => { card.pick = { ...card.pick, effort: e, touched: true }; menu.open = false; ckPicker(card); card.input.focus(); };
      seg.append(b);
    }
    panel.append(row);
  }
  menu.append(sum, panel);
  card.picks.replaceChildren(menu, ...(draft ? [ckDraftFolder(card)] : []));
}
document.addEventListener("click", (e) => { // a click outside closes the model panel
  for (const d of document.querySelectorAll("details.ck-model[open]")) if (!d.contains(e.target)) d.open = false;
});

// A new chat is a draft in the chat pane (#/chats/new): an empty box with the model chip and the folder; the chat is
// made on the first message. The project is the sidebar's; the pc follows from the model and the folder.
const CK_DRAFT = { machine: "draft", id: "new", draft: true, agent: "", title: "", folder: "", label: "", status: "idle" };
function ckDraftPcs() { // own pcs online with a companion that reports models and has a linked folder in the project
  return ck.machines.filter((m) => m.own !== false && m.online && (m.companion?.agents || []).length
    && (m.companion?.folders?.linked || []).some((f) => !agentProject || f.project === agentProject));
}
function ckDraftFolders(card) { // [{machine, label, path, project}] that can run the chosen model
  const x = ckModelOf(card), pcs = ckDraftPcs().filter((m) => x ? x.pcs.includes(m.id) : (m.companion.agents || []).includes(card.pick?.agent));
  return pcs.flatMap((m) => (m.companion.folders.linked || []).filter((f) => !agentProject || f.project === agentProject)
    .map((f) => ({ machine: m.id, label: m.label, path: f.path, project: f.project })));
}
function ckDraftFolder(card) {
  const list = ckDraftFolders(card), sel = el("select", "small"), many = new Set(list.map((f) => f.machine)).size > 1;
  sel.setAttribute("aria-label", "Folder"); sel.title = "Where it runs: a folder linked to the project on that pc";
  for (const f of list) sel.append(new Option(`${f.project} · ${ckShort(f.path)}${many ? ` · ${f.label}` : ""}`, `${f.machine}\n${f.path}`));
  const want = card.where_ || (card.pick?.folder ? `${card.pick.machine}\n${card.pick.folder}` : "");
  if ([...sel.options].some((o) => o.value === want)) sel.value = want;
  card.where_ = sel.value;
  sel.onchange = () => { card.where_ = sel.value; ckDraftWhere(card); };
  sel.hidden = list.length < 2; // one place: nothing to pick, the header says where
  return sel;
}
function ckDraftWhere(card) {
  if (!card.meta.draft) return;
  const [machine, folder] = (card.where_ || "").split("\n"), m = ck.machines.find((y) => y.id === machine);
  card.meta = { ...card.meta, machine: "draft", agent: card.pick?.agent || "", folder: folder || "", label: m?.label || "", target: machine || "" };
  ckPaint(card);
}
function ckDraft(machine = "") {
  ckDrop(ckKey(CK_DRAFT));
  const card = ckCard({ ...CK_DRAFT, online: true });
  ckPicker(card);
  if (machine) { // Machines → Chat: that pc, with a model it runs
    if (!ckModelOf(card)?.pcs.includes(machine)) {
      const x = card.catalog.find((y) => y.pcs.includes(machine));
      if (x) card.pick = { agent: x.agent, id: x.id, effort: x.default || "", touched: true };
    }
    const f = ckDraftFolders(card).find((y) => y.machine === machine);
    if (f) card.where_ = `${f.machine}\n${f.path}`;
    else { card.note.className = "ck-note bad"; card.note.textContent = `${ck.machines.find((m) => m.id === machine)?.label || "That pc"} has no folder linked to ${agentProject ? shown(agentProject) : "a project"}: pick another project in the sidebar, or link one under Machines.`; }
    delete card.picks.dataset.sig; ckPicker(card);
  }
  ckDraftWhere(card);
  return card;
}
async function ckStart(card, text) { // the draft's first message: make the chat there, send it, open it
  const [machine, folder] = (card.where_ || "").split("\n"), p = card.pick;
  if (card.starting) return; // a second Ctrl+Enter while the first is on its way would make a second chat
  if (!machine || !p) throw new Error(ck.machines.some((m) => m.online && m.companion) ? "No folder in this project on a pc that runs this model: link one under Machines, Folders." : "None of your pcs is online with a companion.");
  card.starting = true;
  try {
    const res = await api("/ui/api/chats", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine, agent: p.agent, folder }) });
    const m = ck.machines.find((y) => y.id === machine);
    let failed = "";
    try {
      await api("/ui/api/chat/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine, id: res.chat.id, text, model: p.id, effort: p.effort }) });
      store.set("kn_chat_pick", JSON.stringify({ agent: p.agent, id: p.id, effort: p.effort, machine, folder }));
    } catch (err) { failed = err.message; } // the chat exists now: go there with the text kept, never make another
    ckDrop(ckKey(CK_DRAFT)); ck.summaryAt = 0;
    const chat = ckCard({ ...res.chat, ...(failed ? {} : { model: p.id, effort: p.effort, status: "queued" }), machine, label: m?.label || machine, online: true });
    if (failed) { chat.input.value = text; chat.note.className = "ck-note bad"; chat.note.textContent = failed; }
    go(`#/chats/${encodeURIComponent(chat.meta.id)}`, true); // Back from it goes to the list, not to an empty draft
    ckTick();
  } finally { card.starting = false; }
}
function ckPaint(card) {
  if (card.worker) return ckWorkerPaint(card);
  if (card.meta.parent) return; // a subagent's chat: painted by its poll (ckSubPoll)
  const m = card.meta, busy = ckBusy(m.status);
  card.mark.textContent = AGENT_MARK[m.agent] || "+";
  if (m.draft) {
    card.name.textContent = "New chat";
    card.where.textContent = m.folder ? `${AGENT_NAME[m.agent] || m.agent} · ${m.label} · ${ckShort(m.folder)}` : "";
    card.where.title = m.folder || ""; card.state.hidden = true; card.stop.hidden = true; card.send.textContent = "Send"; card.send.disabled = !!card.starting;
    if (!card.note.classList.contains("bad")) card.note.textContent = !ckDraftPcs().length
      ? (agentProject ? `No pc of yours is online with a folder linked to ${shown(agentProject)}.` : "None of your pcs is online with a companion and a linked folder.")
      : "Runs on your pc in the project's folder, with a worker's permissions there (YOLO only where you turned it on).";
    return;
  }
  card.state.hidden = false;
  card.tok.hidden = !m.used && !m.ctx; // the conversation's tokens: its context now and all it used
  card.tok.textContent = [m.ctx && `${ckNum(m.ctx)} context`, m.used && `${ckNum(m.used)} used`].filter(Boolean).join(" · ");
  card.tok.title = "Context: what the agent sent for its last step. Used: every token of this chat so far, cache reads included.";
  card.close.hidden = busy; card.close.textContent = m.closed ? "Open again" : "Close";
  card.close.title = m.closed ? "Back on the board and in the list" : "Off the board and the chat list (under Closed); a new message opens it again";
  card.name.textContent = m.title || "New chat";
  card.where.textContent = `${AGENT_NAME[m.agent] || m.agent} · ${m.label} · ${ckShort(m.folder)}${m.project ? ` (${m.project})` : ""}`;
  card.where.title = m.folder || "";
  const item = (ck.board || []).find((i) => i.kind === "chat" && `${i.machine}/${i.id}` === ckKey(m));
  if (card.git.dataset.v !== JSON.stringify(item?.git || null)) {
    card.git.dataset.v = JSON.stringify(item?.git || null); card.git.replaceChildren(...(item?.git ? [ckGitPill(item)] : []));
  }
  card.state.className = `ck-state ${!card.online ? "offline" : busy ? "running" : ""}`;
  card.state.textContent = !card.online ? "pc offline" : m.status === "queued" ? "waiting for the pc" : busy ? "working" : "ready";
  card.root.classList.toggle("running", busy);
  // while the agent answers, a message waits in line (the server keeps up to 5) and goes in when this turn ends
  // with messages waiting and nothing typed the button forces: the turn stops and the first waiting message goes in
  const force = busy && m.status === "running" && m.waiting > 0 && !card.input.value.trim();
  const live = ckLive(m), typed = !!card.input.value.trim();
  card.stop.hidden = !busy; card.send.disabled = !card.online; card.send.classList.toggle("force", force);
  card.queueBtn.hidden = card.forceBtn.hidden = card.more.hidden = !(live && busy && typed); // a live agent at work: three ways in
  card.send.classList.toggle("split", !card.more.hidden);
  card.send.textContent = force ? "Force" : busy && !live ? "Queue" : "Send";
  card.send.title = force ? `Send the first of ${m.waiting} waiting message${m.waiting > 1 ? "s" : ""} into the turn${live ? ": its step stops and it goes in" : ": the agent reads it after its next step"} (Stop ends the turn)`
    : busy && live ? (m.agent === "agy" ? "Right after this turn, in the same agy (it takes no message mid-turn) (Ctrl+Enter)" : "Into this turn: the agent takes it in as it works (Ctrl+Enter)")
    : busy ? "The agent gets this when its current turn ends" : "";
  if (!card.note.classList.contains("bad") || busy) {
    card.note.className = "ck-note";
    card.note.textContent = !card.online ? `${m.label} is offline${card.at ? ` since ${ago(card.at)}` : ""}: a chat needs its companion running.` : "";
  }
}
async function ckPoll(card) {
  if (!card) return;
  if (card.worker) return ckWorkerPoll(card);
  if (card.meta.parent) return ckSubPoll(card);
  if (card.meta.draft) return ckPicker(card), ckDraftWhere(card);
  let res;
  // only the chat opened full counts as read; a card glanced at on the board stays under Needs you
  const peek = ck.view !== ckKey(card.meta) ? "&peek=1" : "";
  try { res = await api(`/ui/api/chat?machine=${card.meta.machine}&id=${card.meta.id}&after=${card.after}${peek}`); }
  catch (e) { card.note.className = "ck-note bad"; card.note.textContent = e.message; return; }
  card.meta = { ...card.meta, ...res.chat, label: res.label }; card.online = res.online; card.at = res.at;
  ckPicker(card);
  ckEvents(card, res);
  if (!card.meta.subs) ckSubsEnded(card); // the server knows none runs (its turn ended, or it was lost): no block says running
  ckAsks(card, res.asks || [], (res.now || Date.now() / 1000) - Date.now() / 1000);
  ckPaint(card);
}
const CK_ICON = { // inline, in the text colour
  clip: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l8.6-8.6a3.7 3.7 0 0 1 5.2 5.2l-8.6 8.6a1.8 1.8 0 0 1-2.6-2.6l7.9-7.9"/></svg>',
  mic: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
};
// Attachments: at most CK_FILES of 10 MB each per message, shown as chips until it is sent.
const CK_FILES = 5, CK_FILE_MB = 10, CK_REF = /⟦file:([0-9a-f]{12})\/([^⟧]+)⟧/g, CK_IMG = /\.(png|jpe?g|gif|webp)$/i;
function ckAddFiles(card, list) {
  for (const f of list) {
    if (f.size > CK_FILE_MB * 1024 * 1024) { card.note.className = "ck-note bad"; card.note.textContent = `${f.name}: larger than ${CK_FILE_MB} MB`; continue; }
    if (card.files.length >= CK_FILES) { card.note.className = "ck-note bad"; card.note.textContent = `At most ${CK_FILES} files per message`; break; }
    card.files.push(f);
  }
  ckFilesPaint(card);
}
function ckFilesPaint(card) {
  card.filesBox.replaceChildren(...card.files.map((f, i) => {
    const chip = el("span", "chip ck-filechip", f.name || "screenshot.png"), x = el("button", "btn icon small quiet", "×");
    x.type = "button"; x.title = "Remove"; x.setAttribute("aria-label", `Remove ${f.name}`);
    x.onclick = () => { card.files.splice(i, 1); ckFilesPaint(card); };
    chip.append(x); return chip;
  }));
  card.filesBox.hidden = !card.files.length;
}
async function ckUpload(machine, files) {
  const refs = [];
  for (const f of files) {
    const res = await api(`/ui/api/chat/file?machine=${encodeURIComponent(machine)}`, { method: "POST", headers: { "X-Filename": encodeURIComponent(f.name || "screenshot.png") }, body: f });
    refs.push(res.ref);
  }
  return refs;
}
// Speaking a message: the browser records (MediaRecorder; needs https), the server passes it to its speech-to-text
// server and the text goes into the box at the cursor; nothing is sent by itself. Click starts and stops; held down
// longer than a moment, letting go stops (push to talk).
const ckMicOk = () => !!(me.stt && window.MediaRecorder && navigator.mediaDevices?.getUserMedia);
function ckMic(card, btn) {
  let rec = null, held = 0, timer = null;
  const label = () => { const s = Math.round((Date.now() - rec.t0) / 1000); btn.textContent = `● ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; if (s >= 300) stop(); };
  const say = (msg) => { card.note.className = "ck-note bad"; card.note.textContent = msg; };
  async function start() {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { return say(`Microphone: ${e.name === "NotAllowedError" ? "not allowed in this browser" : e.message}`); }
    rec = new MediaRecorder(stream); rec.t0 = Date.now(); const parts = [];
    rec.ondataavailable = (e) => e.data.size && parts.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop()); clearInterval(timer); rec = null;
      btn.classList.remove("rec"); btn.textContent = "…"; btn.disabled = true;
      const blob = new Blob(parts, { type: parts[0]?.type || "audio/webm" });
      try {
        const { text } = await api("/ui/api/transcribe", { method: "POST", headers: { "Content-Type": blob.type }, body: blob });
        if (text) {
          const i = card.input, a = i.selectionStart ?? i.value.length, b = i.selectionEnd ?? a;
          const pre = i.value.slice(0, a), add = (pre && !/\s$/.test(pre) ? " " : "") + text;
          i.value = pre + add + i.value.slice(b); i.selectionStart = i.selectionEnd = a + add.length;
          i.dispatchEvent(new Event("input"));
        } else say("Nothing heard");
      } catch (e) { say(e.message); }
      btn.innerHTML = CK_ICON.mic; btn.disabled = false; card.input.focus();
    };
    rec.start(); btn.classList.add("rec"); label(); timer = setInterval(label, 500);
  }
  const stop = () => rec && rec.state === "recording" && rec.stop();
  btn.toggle = () => (rec ? stop() : start());
  btn.addEventListener("pointerdown", (e) => { e.preventDefault(); if (rec) return stop(); held = Date.now(); start(); });
  btn.addEventListener("pointerup", () => { if (rec && held && Date.now() - held > 700) stop(); held = 0; });
  btn.addEventListener("click", (e) => { if (e.detail === 0) btn.toggle(); }); // the keyboard (Enter/Space on the button)
}
// A message of the owner: its text, each attachment a thumbnail (images) or a download link.
function ckUserMsg(card, text) {
  const box = el("div", "ck-msg user");
  let at = 0;
  for (const m of text.matchAll(CK_REF)) {
    box.append(text.slice(at, m.index)); at = m.index + m[0].length;
    const a = el("a", "ck-file"), url = `/ui/api/chat/file?machine=${encodeURIComponent(card.meta.machine)}&file=${encodeURIComponent(`${m[1]}/${m[2]}`)}`;
    a.href = url; a.target = "_blank"; a.rel = "noopener";
    if (CK_IMG.test(m[2])) { const img = el("img", "ck-img"); img.src = url; img.alt = m[2]; img.loading = "lazy"; a.append(img); }
    else { a.textContent = m[2]; a.download = m[2]; }
    box.append(a);
  }
  box.append(text.slice(at));
  return box;
}
// New events into a card's log: text as sanitized Markdown, the rest as lines; never one twice.
function ckEvents(card, res) {
  if (res.trimmed && !card.after && res.events.length) card.log.append(el("p", "ck-line info", "Older output was trimmed (the chat keeps its newest part)."));
  const near = card.stickToBottom !== false || (card.log.scrollHeight - card.log.scrollTop - card.log.clientHeight < 120);
  for (const e of res.events) {
    if (e.n <= card.after) continue; // two polls crossed (board + chat): never append an event twice
    card.after = Math.max(card.after, e.n);
    const last = card.log.lastElementChild;
    if (e.sub) { ckSub(card, e); continue; }
    if (e.kind === "text") { // Markdown, sanitized as a note is (DOMPurify): an agent's output never becomes script
      const msg = last && last.classList.contains("agent") ? last : card.log.appendChild(el("div", "ck-msg agent md"));
      msg.dataset.raw = (msg.dataset.raw || "") + e.text;
      msg.innerHTML = renderMd(msg.dataset.raw.replace(/^\n+/, ""));
    } else card.log.append(e.kind === "user" ? ckUserMsg(card, e.text) : el("div", `ck-line ${e.kind}`, e.text));
  }
  if (res.events.length) {
    const hasUser = res.events.some((e) => e.kind === "user");
    if (near || hasUser) ckScrollBottom(card, hasUser);
  }
}

// A subagent in the log: one folded block per subagent (its task, running/done, its tool calls, its report), so its
// work never mixes into the agent's own answer. Text only (textContent); its report is Markdown like any answer.
function ckSub(card, e) {
  card.subs = card.subs || new Map();
  let b = card.subs.get(e.sub);
  if (!b) {
    const d = el("details", "ck-sub"), sum = el("summary"), label = el("span", "ck-sub-label", e.kind === "sub" ? e.text : "subagent"), st = el("span", "chip");
    const tk = el("span", "ck-sub-tok");
    sum.append(el("span", "ck-sub-tag", "Subagent"), label, tk, st);
    const tools = el("div", "ck-sub-tools"), report = el("div", "ck-sub-report md");
    d.append(sum, tools, report);
    card.log.append(d);
    b = { d, label, st, tools, report, tk };
    card.subs.set(e.sub, b);
  }
  if (e.kind === "usage") { b.tokens = e.total ?? (b.tokens || 0) + (e.tokens || 0); b.tk.textContent = `${ckNum(b.tokens)} tokens`; return ckSubbar(card); }
  if (e.kind === "sub" && e.status === "running") b.label.textContent = e.text;
  else if (e.kind === "sub") report(b, e);
  else b.tools.append(el("div", `ck-line ${e.kind}`, e.text));
  const status = e.kind === "sub" ? e.status || "running" : b.status || "running";
  b.status = status; b.d.classList.toggle("running", status === "running");
  b.st.className = `chip${status === "failed" ? " bad" : status === "done" ? "" : " live"}`;
  b.st.textContent = status === "running" ? "running" : status;
  ckSubbar(card);
  function report(x, ev) { x.report.innerHTML = renderMd(ev.text); }
}
const ckLive = (m) => (ck.machines.find((x) => x.id === m.machine)?.companion?.live || []).includes(m.agent); // its pc runs this agent live
function ckSubsEnded(card) {
  for (const b of (card.subs || new Map()).values()) {
    if (b.status !== "running") continue;
    b.status = "ended"; b.d.classList.remove("running"); b.st.className = "chip"; b.st.textContent = "ended";
    b.st.title = "It did not report its end in this chat (its turn ended or was cut off)";
  }
  ckSubbar(card);
}
const ckNum = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));
// The subagents running now, pinned above the box: the log scrolls on, these stay; a click opens its block.
function ckSubbar(card) {
  const run = [...(card.subs || new Map()).values()].filter((b) => b.status === "running");
  card.subbar.hidden = !run.length;
  card.subbar.replaceChildren(...run.map((b) => {
    const x = el("button", "ck-subpin"); x.type = "button";
    x.append(el("span", "ck-dot live"), el("span", "ck-sub-label", b.label.textContent), ...(b.tokens ? [el("small", null, `${ckNum(b.tokens)} tokens`)] : []));
    x.title = "Open this subagent's block";
    x.onclick = () => { b.d.open = true; b.d.scrollIntoView({ block: "nearest", behavior: "smooth" }); };
    return x;
  }));
}

// Follow a worker (#/chats/worker/<project>/<ID>): a worker is a chat nobody types in. Its card is a chat card whose
// log is the worker's transcript (its companion sends output only while someone follows it), with a banner and the
// worker's own actions. Its owner may steer it (the message reaches the agent after its next step, in its own
// session) or take it over (stopped, and its conversation goes on as an ordinary chat on that pc).
const ckWorkerRoute = (w) => `#/chats/worker/${encodeURIComponent(w.project)}/${encodeURIComponent(w.id)}`;
function ckFindWorker(arg) { // "worker/<project>/<id>" -> the newest run of that task on your own pcs
  const m = /^worker\/([^/]+)\/([^/]+)$/.exec(arg || "");
  if (!m) return null;
  return ck.machines.flatMap((pc) => Object.values(pc.agents || {}).flatMap((a) => (a.workers || []).map((w) => ({ m: pc, w }))))
    .filter(({ w }) => w.project === m[1] && w.id === m[2]).sort((a, b) => (b.w.started || 0) - (a.w.started || 0))[0] || null;
}
function ckFollow(w) { go(ckWorkerRoute(w)); }
function ckFollowButton(w, cls = "btn small") {
  const b = el("button", cls, "Follow"); b.type = "button";
  b.title = w.status === "running" ? "Watch this worker live, steer it or take it over" : "Read what this worker did";
  b.onclick = (e) => { e.stopPropagation(); ckFollow(w); };
  return b;
}
function ckWorkerCard(m, w) {
  const card = ckCard({ machine: m.id, id: `worker/${w.project}/${w.id}`, agent: w.agent, title: `Worker ${w.id}`, label: m.label,
    folder: w.branch || "", project: w.project, status: w.status, online: m.online });
  if (card.worker) return card;
  card.worker = `${w.project}/${w.id}`; card.w = w; card.m = m;
  card.root.classList.add("worker");
  card.banner = el("div", "ck-banner"); card.bannerText = el("p", "ck-banner-text"); card.acts = el("div", "ck-banner-acts");
  card.banner.append(card.bannerText, card.acts);
  card.root.insertBefore(card.banner, card.log);
  card.send.textContent = "Steer";
  card.input.setAttribute("aria-label", "Message for the worker");
  return card;
}
async function ckWorkerAct(card, action) {
  const w = card.w;
  if (action === "stop_worker" && !confirm(`Stop the worker on ${w.id}?`)) return;
  if (action === "takeover" && !confirm(`Take over ${w.id}?\n\nIts worker is stopped (the line stays yours) and its conversation goes on as a chat on ${card.meta.label}, in its worktree.`)) return;
  try {
    await api("/ui/api/machines/action", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ machine: card.meta.machine, action, args: { project: w.project, id: w.id } }) });
    if (action === "takeover") { card.taking = true; toast(`Asked ${card.meta.label} to hand ${w.id} over to a chat`, false); }
  } catch (e) { toast(e.message); }
  ck.summaryAt = 0; ckTick();
}
async function ckWorkerPoll(card) {
  let res;
  try { res = await api(`/ui/api/worker?machine=${card.meta.machine}&worker=${encodeURIComponent(card.worker)}&after=${card.after}${ck.view === ckKey(card.meta) ? "" : "&peek=1"}`); }
  catch (e) { card.note.className = "ck-note bad"; card.note.textContent = e.message; return; }
  if (card.started !== undefined && res.worker.started !== card.started) { card.log.replaceChildren(); card.after = 0; card.subs = null; } // a new run of the task
  card.started = res.worker.started; card.w = { ...card.w, ...res.worker };
  card.meta = { ...card.meta, status: res.worker.status, label: res.label }; card.online = res.online; card.at = res.at;
  ckEvents(card, res);
  if (!card.log.childNodes.length) card.log.append(el("p", "ck-line info ck-wait", res.worker.status === "running" ? "Following: new output shows here as the worker prints it." : "No output was kept for this worker."));
  else card.log.querySelector(".ck-wait")?.remove();
  if (card.taking && res.worker.chat) { // taken over: on to its chat once the list knows it
    card.taking = false; await ckSummary(); return go(`#/chats/${encodeURIComponent(res.worker.chat)}`);
  }
  ckPaint(card);
}
function ckWorkerPaint(card) {
  const m = card.meta, w = card.w, run = w.status === "running", live = run && card.online;
  card.name.textContent = `Worker ${w.id}`;
  card.where.textContent = `${AGENT_NAME[w.agent] || w.agent} · ${m.label}${w.branch ? ` · ${w.branch}` : ""}${w.project ? ` (${w.project})` : ""}`;
  card.state.className = `ck-state ${!card.online ? "offline" : run ? "running" : ""}`;
  card.state.textContent = !card.online ? "pc offline" : run ? "working" : w.chat ? "taken over" : w.status;
  card.root.classList.toggle("running", run);
  card.bannerText.textContent = `Worker for ${w.id} on ${m.label} · ${w.chat ? "taken over as a chat" : run ? "running" : w.status}` +
    (live ? ". Steer: your message reaches it after its next step. Take over: stop it and go on as a chat." : w.chat ? "." : ". Read only.");
  const sig = JSON.stringify([w.status, w.chat, !!w.review, !!w.git, card.online]);
  if (card.acts.dataset.sig !== sig) {
    card.acts.dataset.sig = sig; card.acts.replaceChildren();
    const act = (text, title, fn) => { const b = el("button", "btn small", text); b.type = "button"; b.title = title; b.onclick = fn; card.acts.append(b); };
    if (card.online && run) act("Stop", "Stop this worker (this pc keeps its lease until it runs out)", () => ckWorkerAct(card, "stop_worker"));
    if (card.online && w.status === "failed") act("Restart", "Start this worker again now", () => ckWorkerAct(card, "restart"));
    if (w.review || w.git) act("Review", "The changes on its branch, read on that pc", () => ckDiff({ kind: "worker", machine: m.machine, project: w.project, id: w.id, title: w.id, label: m.label }));
    if (card.online && !w.chat && w.status !== "merged") act("Take over", "Stop the worker and go on with its conversation as a chat on that pc", () => ckWorkerAct(card, "takeover"));
    if (w.chat) act("Open chat", "The chat this worker became", () => go(`#/chats/${encodeURIComponent(w.chat)}`));
  }
  card.form.hidden = !live;
  card.stop.hidden = true; card.send.disabled = !live;
  card.input.placeholder = "Steer the worker… (it reads this after its next step; Ctrl+Enter sends)";
  if (!card.note.classList.contains("bad")) { card.note.className = "ck-note"; card.note.textContent = !card.online ? `${m.label} is offline${card.at ? ` since ${ago(card.at)}` : ""}.` : ""; }
}

// Approvals: the turn waits while the agent wants a tool its allowed set does not hold. One block per open approval
// inside the chat card (also a Needs-you item on the board); the answer goes to the pc with its next poll.
function ckAskButtons(a) {
  return [["once", "Allow once", "Allow this one call"],
    ...(a.once_only ? [] : [["chat", "Allow for this chat", "Allow this same call for the rest of this chat"]]),
    ["deny", "Deny", "Refuse it; the agent is told and goes on without it"]];
}
const ckUnbidi = (t) => String(t || "").replace(/[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, (c) => `<U+${c.codePointAt(0).toString(16).toUpperCase().padStart(4, "0")}>`);
function ckAsks(card, asks, skew) {
  asks = asks.map((a) => ({ ...a, tool: ckUnbidi(a.tool), detail: ckUnbidi(a.detail), reason: ckUnbidi(a.reason) }));
  const sig = JSON.stringify(asks.map((a) => a.id));
  if (card.asks.dataset.sig === sig) return;
  card.asks.dataset.sig = sig; card.asks.hidden = !asks.length;
  card.asks.replaceChildren(...asks.map((a) => {
    const box = el("section", "ck-ask"), acts = el("div", "ck-ask-acts"), left = Math.max(0, Math.round((a.expires - skew - Date.now() / 1000) / 60));
    box.setAttribute("aria-label", `Approval: ${a.tool}`);
    box.append(el("p", "ck-ask-head", `The agent asks to use ${a.tool}`));
    if (a.detail.length > 300 || a.detail.split("\n").length > 4) { // long: the whole text, folded open on a tap
      const more = el("details", "ck-ask-more"), sum = el("summary", null, `${a.detail.slice(0, 120).split("\n")[0]}… (${a.detail.length} characters: show all)`);
      more.append(sum, el("code", "ck-ask-what", a.detail)); box.append(more);
    } else box.append(el("code", "ck-ask-what", a.detail));
    if (a.reason) box.append(el("p", "ck-ask-why", a.reason));
    box.append(el("p", "ck-ask-when", `${a.once_only ? "git push or outside the folder: once at most. " : ""}Denied by itself in ${left} min.`));
    for (const [d, text, title] of ckAskButtons(a)) {
      const b = el("button", `btn small${d === "once" ? " primary" : d === "deny" ? " danger" : ""} ck-ask-${d}`, text); b.type = "button"; b.title = title;
      b.onclick = async () => {
        for (const x of acts.querySelectorAll("button")) x.disabled = true;
        try { await api("/ui/api/chat/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id, ask: a.id, decision: d }) }); }
        catch (err) { card.note.className = "ck-note bad"; card.note.textContent = err.message; }
        card.asks.dataset.sig = ""; ck.summaryAt = 0; ckPoll(card);
      };
      acts.append(b);
    }
    box.append(acts);
    return box;
  }));
}

// New chat: a pc of your own with a companion, an agent installed there, a folder linked there.
function ckNewChat(machine = "") { ck.draftFor = machine; go("#/chats/new"); } // a draft in the chat pane (ckDraft)
$("ck-new").onclick = async () => { if (!ck.machines.length) await ckSummary().catch(() => {}); ckNewChat(); };

// ---------------------------------------------------------------- live updates (Server-Sent Events)
let liveTimer;
async function onChanged() {
  await refreshData();
  loadRecent();
  loadLint(!$("tab-lint").hidden);
  loadTasks(!$("tab-tasks").hidden);
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
  es.addEventListener("changed", () => { clearTimeout(liveTimer); liveTimer = setTimeout(onChanged, 700); });
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
    if (es.readyState === EventSource.CLOSED) { setTimeout(connectLive, liveRetry); liveRetry = Math.min(liveRetry * 2, 60000); }
  };
}

let me = { admin: false, writable: [], labels: {} };
const shown = (p) => (me.labels || {})[p] || p; // the display name the admin gave a project
async function loadMe() {
  // Projects created in the admin page have roots <name> (docs) and <name>-memory, like the built-in ones.
  me = await api("/ui/api/me");
  PROJECTS = me.projects;
  for (const p of PROJECTS) if (!MEMORY_ROOT[p]) { MEMORY_ROOT[p] = `${p}-memory`; ROOT_PROJECT[p] = p; ROOT_PROJECT[`${p}-memory`] = p; }
  if (!PROJECTS.includes(project) && project !== ALL) project = PROJECTS[0];
  agentProject = project === ALL ? "" : project;
  navMe(me);
  if (!me.admin) $("ingest-open").hidden = true;
  $("new-memo").hidden = !me.writable.includes(project);
  $("add-menu").hidden = $("new-memo").hidden && $("ingest-open").hidden;
}

// The URL says which view shows (shell.js). The Agents views need no map data, so they show as soon as the user is
// known; the knowledge views once the map data is in.
let routedEarly = false;
loadMe().then(() => {
  if (!["notes", "map", "changes", "health"].includes(routeView())) { applyRoute(); routedEarly = true; }
  return loadGraph();
}).catch((e) => {
  $("content").prepend(el("p", "error load-error", `Could not load the knowledge base: ${e.message}. Reload the page; if it stays, the server may be restarting.`));
  throw e;
}).then(() => {
  connectLive();
  loadLint(false); loadTasks(false); loadRecent();
  if (!routedEarly) applyRoute();
  if ($("cockpit").hidden) ckSummary().catch(() => {}); // the Inbox badge, on every view
  probeCompanion();
});
