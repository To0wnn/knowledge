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
const projectOf = (path) => ROOT_PROJECT[path.split("/")[0]] || "homelab";
const api = (path, opts = {}) => fetch(path, { credentials: "same-origin", ...opts, headers: { "X-Knowledge-UI": "1", ...(opts.headers || {}) } })
  .then(async (r) => {
    if (r.status === 401) location.reload();
    const j = await r.json().catch(() => ({ error: `${r.status} ${r.statusText || "no answer"}: the server may be restarting` })); // a proxy error page is no JSON
    if (!r.ok) throw new Error(j.error || r.statusText); return j;
  });
// Note text can come from guests, write tokens and ingested sources: no forms, inputs, styles or ids (an id could
// shadow the app's own elements), and images only from this server.
const PURIFY = { FORBID_TAGS: ["style", "form", "input", "textarea", "select", "button", "dialog", "iframe", "object", "embed"],
  FORBID_ATTR: ["style", "id", "name", "form", "formaction"] };
DOMPurify.addHook("afterSanitizeAttributes", (n) => {
  if (n.tagName === "IMG" && !/^(\/(?!\/)|data:image\/)/.test(n.getAttribute("src") || "")) n.removeAttribute("src");
});
const renderMd = (text) => DOMPurify.sanitize(marked.parse(text, { gfm: true }), PURIFY);
const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* blocked */ } } };

let data = { nodes: [], links: [] };
let byId = new Map(), neighbors = new Map(), slugToId = new Map(), clusterColor = new Map();
let selected = null, hover = null, focusSet = new Set();
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
    .onNodeClick((n) => (selected === n.id ? setMap(false) : openNote(n.id))) // a second click reads it
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
function renderProjects() {
  const box = $("projects"); box.replaceChildren();
  [...PROJECTS, ALL].forEach((p, i) => {
    const b = el("button"); b.title = p === ALL ? "All projects on one map, with the links between them (0)" : `${shown(p)} (${i + 1})`;
    b.setAttribute("aria-pressed", String(p === project));
    b.append(p === ALL ? "All" : shown(p), el("span", "n", String(p === ALL ? data.nodes.length : data.nodes.filter((x) => x.project === p).length)));
    b.onclick = () => switchProject(p);
    box.append(b);
  });
  box.querySelector('[aria-pressed="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" }); // in sight when scrolled
}

function switchProject(p, refit = true) {
  releaseFocus({ instant: true });
  if (p === project || (p !== ALL && !PROJECTS.includes(p))) return;
  project = p; store.set("kn_project", p);
  $("new-memo").hidden = !me.writable.includes(p);
  $("add-menu").hidden = $("new-memo").hidden && $("ingest-open").hidden;
  selected = null; setFocus(null);
  indexGraph(); renderProjects(); renderTopics(); applyView(refit);
  if (!current) homeView();
  if ($("q").value.trim()) runSearch();
  loadLint(!$("tab-lint").hidden);
  loadTasks(!$("tab-tasks").hidden);
  loadRecent();
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
}

// The middle column shows the note, or the map of the project instead.
function setMap(on) {
  mapOn = on; $("map-btn").setAttribute("aria-pressed", String(on)); store.set("kn_map", on ? "1" : "0"); // kept over a reload
  $("graph").hidden = !on; $("note").hidden = on;
  if (current) renderSide();
  if (Graph) { Graph.linkDirectionalParticles(Graph.linkDirectionalParticles()); on ? Graph.resumeAnimation() : Graph.pauseAnimation(); }
  if (!on) return;
  requestAnimationFrame(() => { // once the map has its size
    Graph.width($("graph").clientWidth).height($("graph").clientHeight);
    if (selected) { gather(selected, 1).then(zoomToFocus); if (settling) zoomToFocus.again = true; } else Graph.zoomToFit(400, 40);
  });
}
$("map-btn").onclick = () => setMap(!mapOn);

// Theme: Auto (the system's), Light or Dark; theme.js applies the stored pick before the page paints.
const THEMES = ["auto", "light", "dark"];
const showTheme = () => { $("theme-btn").textContent = `Theme: ${{ auto: "Auto", light: "Light", dark: "Dark" }[document.documentElement.dataset.theme || "auto"]}`; };
$("theme-btn").onclick = () => {
  const next = THEMES[(THEMES.indexOf(document.documentElement.dataset.theme || "auto") + 1) % THEMES.length];
  if (next === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
  store.set("kn_theme", next); showTheme(); themeChanged();
};
showTheme();

// ---------------------------------------------------------------- note preview, deep links and editor
function splitFront(text) {
  const m = text.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!m) return { meta: {}, body: text };
  const meta = {};
  for (const line of m[1].split("\n")) { const k = line.match(/^\s*(name|description|type|title|source):\s*(.*)$/); if (k) meta[k[1]] = k[2].replace(/^"|"$/g, ""); }
  return { meta, body: text.slice(m[0].length) };
}

const ago = (sec) => { const d = Date.now() / 1000 - sec; return d < 90 ? "just now" : d < 3600 ? `${Math.round(d / 60)} min ago` : d < 86400 ? `${Math.round(d / 3600)} h ago` : new Date(sec * 1000).toLocaleDateString(); };

// Clicking empty canvas clears the selection and the #note in the URL, so a refresh opens nothing.
function deselect({ push = true } = {}) {
  if (editing) return; // keep unsaved edits on screen
  selected = null; current = null; setFocus(null); animateHover();
  if (push && location.hash) history.pushState(null, "", location.pathname + location.search);
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
      const more = el("button", "ghost small", `All ${ns.length}`);
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
  if (!res.files.length) list.append(el("li", "muted", "No changes yet."));
  const all = el("button", "ghost small", "All changes"); all.onclick = () => selectTab("changes");
  box.append(el("h3", null, "Recently changed"), list, all);
  const lint = $("lint-count").textContent;
  if (lint) {
    const b = el("button", "ghost small", `${lint} lint findings`); b.onclick = () => selectTab("lint");
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

// Returns false when nothing was opened (the user kept their edits, or the note could not be read).
async function openNote(id, { push = true, find = null } = {}) {
  if (keepEdits()) return false;
  editing = false; historyOpen = false;
  let note;
  try { note = await api(`/ui/api/note?path=${encodeURIComponent(id)}`); } catch (e) { $("note").textContent = e.message; return false; }
  const p = projectOf(note.path);
  if (p !== "shared" && p !== project && !allMode()) switchProject(p, false); // the All map stays the All map
  if (push && decode(location.hash.slice(1)) !== note.path) history.pushState(null, "", `#${note.path}`);
  current = note; document.body.classList.remove("home");
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
  else if (matchMedia("(max-width: 720px)").matches && push) $(mapOn ? "side" : "note").scrollIntoView({ behavior: calm ? "auto" : "smooth" }); // phone: below the list
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
    return target ? `[${slug}](#${target})` : all;
  });
  const pane = $("note"); pane.replaceChildren();
  const head = el("div", "note-head");
  head.append(el("h2", null, n ? n.label : note.path.split("/").pop()));
  if (n) { const pill = el("span", "pill", n.cluster || n.group); pill.style.borderColor = nodeColor(n); head.append(pill); }
  const tools = el("div", "note-tools");
  const copy = el("button", "ghost small", "Copy link");
  copy.onclick = () => navigator.clipboard?.writeText(`${location.origin}/ui#${note.path}`).then(() => { copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy link"), 1500); });
  tools.append(copy);
  if (note.writable) { const edit = el("button", "ghost small", touch ? "Edit" : "Edit (E)"); edit.onclick = startEdit; tools.append(edit); }
  const hist = el("button", "ghost small", "History"); hist.onclick = () => showHistory(); tools.append(hist);
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
    const read = el("button", null, "Read note"); read.onclick = () => setMap(false); card.append(read);
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
      const a = el("a"); a.href = `#${x.id}`;
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
  const back = el("button", "ghost small", "Back to the note"); back.onclick = renderNote;
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
        const restore = el("button", "ghost small", "Restore this version");
        restore.onclick = async () => {
          if (!confirm("Replace the current note with this version? The current text stays in the history.")) return;
          try {
            await api("/ui/api/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: note.path, text: ver.text, base_version: note.version }) });
            await openNote(note.path, { push: false }); refreshData();
          } catch (e) { alert(e.message); }
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
  const save = el("button", null, "Save (Ctrl+S)"), cancel = el("button", "ghost", "Cancel");
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
window.addEventListener("popstate", () => { const id = decode(location.hash.slice(1)); if (id) openNote(id, { push: false }); else deselect({ push: false }); });
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
  const text = `---\nname: ${type}_${slug}\ndescription: ${JSON.stringify(desc)}\nmetadata:\n  type: ${type}\n---\n\n${desc}\n\n## Stand\n\n## Keuzes\n`; // the house-rules layout
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
// One search box: note titles of every project (instant, from the graph), then full text and meaning in this project.
let searchTimer, searchSeq = 0;
async function runSearch() {
  const q = $("q").value.trim(), seq = ++searchSeq;
  $("results").hidden = !q; $("topics").hidden = !!q;
  if (!q) { $("hits").replaceChildren(); $("sem").replaceChildren(); $("titles").replaceChildren(); return; }
  $("sem").replaceChildren(); $("sem-head").hidden = true;
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
    const li = el("li"), b = el("button");
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
  const li = el("li"), b = el("button");
  b.append(el("span", "hit-path", top)); if (sub) b.append(el("span", "hit-text", sub));
  b.onclick = onclick; li.append(b); return li;
}
$("q").addEventListener("input", () => { clearTimeout(searchTimer); selectTab("notes"); searchTimer = setTimeout(runSearch, 150); });
$("q").addEventListener("keydown", (e) => { // Enter opens the first result
  if (e.key === "Enter") { e.preventDefault(); clearTimeout(searchTimer); runSearch().then(() => $("results").querySelector("button")?.click()); }
});

document.addEventListener("keydown", (e) => {
  const typing = /input|textarea|select/i.test(document.activeElement.tagName) || document.querySelector("dialog[open]");
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("q").focus(); $("q").select(); return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "/") { e.preventDefault(); $("q").focus(); }
  else if (/^[1-9]$/.test(e.key) && PROJECTS[Number(e.key) - 1]) switchProject(PROJECTS[Number(e.key) - 1]);
  else if (e.key === "0") switchProject(ALL);
  else if (e.key.toLowerCase() === "e" && current?.writable) { e.preventDefault(); startEdit(); }
  else if (e.key.toLowerCase() === "m") setMap(!mapOn);
  else if (e.key === "Escape") deselect();
});

// ---------------------------------------------------------------- tabs, changes, lint, ingest
function selectTab(name) {
  store.set("kn_tab", name); // kept over a reload, like the map
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const t of ["notes", "changes", "tasks", "lint"]) $(`tab-${t}`).hidden = t !== name;
  if (name === "lint") loadLint(true);
  if (name === "tasks") loadTasks(true);
  if (name === "changes") loadRecent(true);
}
for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => selectTab(b.dataset.tab);

// Changes: newest first, per day; lines added/removed, who, and the question of the session that made the change.
// A click opens the diff. Changes since the last look at this tab are marked; the badge counts them meanwhile.
async function loadRecent(look = false) {
  const p = project, res = await api(`/ui/api/recent?project=${p}`);
  if (p !== project) return; // switched meanwhile: that project's own call follows
  const key = `kn_seen_${p}`, seen = Number(store.get(key)) || Date.now() / 1000;
  const fresh = res.files.filter((f) => f.mtime > seen).length;
  $("changes-count").textContent = fresh && !look ? fresh : "";
  if (!look && $("tab-changes").hidden) { if (!store.get(key)) store.set(key, String(seen)); return; }
  const box = $("recent"); box.replaceChildren();
  let day = "";
  for (const f of res.files) {
    const d = new Date(f.mtime * 1000).toDateString();
    if (d !== day) {
      day = d;
      const today = new Date().toDateString(), yesterday = new Date(Date.now() - 864e5).toDateString();
      box.append(el("li", "day", d === today ? "Today" : d === yesterday ? "Yesterday" : new Date(f.mtime * 1000).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })));
    }
    const n = byId.get(f.path), li = el("li"), b = el("button");
    const top = el("span", "hit-path", n ? n.label : f.path.split("/").pop());
    if (f.mtime > seen) top.append(el("span", "new", "new"));
    const meta = el("span", "hit-text");
    if (f.added != null) meta.append(el("span", "plus", `+${f.added}`), " ", el("span", "minus", `−${f.removed}`), " · ");
    meta.append(`${ago(f.mtime)} · ${f.session ? f.session.device : f.by === "VM" ? "on the VM" : writer(f.by)}`);
    b.append(top, meta);
    if (f.session?.asked) b.append(el("span", "hit-text why", `“${f.session.asked}”`));
    b.title = f.path;
    b.onclick = async () => { if (await openNote(f.path) && f.version) { setMap(false); showHistory(f.version); } };
    li.append(b); box.append(li);
  }
  if (!res.files.length) box.append(el("li", "muted", "No changes yet."));
  store.set(key, String(Math.max(seen, ...res.files.map((f) => f.mtime))));
}

function highlight(ids) {
  // Show a set of notes in the graph: dim the rest and zoom to them.
  focusSet = new Set(ids);
  const pts = ids.map((i) => byId.get(i)).filter((n) => n && n.x != null);
  if (pts.length) { animateHover(); Graph.zoomToFit(600, 80, (n) => focusSet.has(n.id)); }
}

async function loadLint(render = true) {
  if (!me.admin) return; // lint spans every project: admins only
  const full = await api("/ui/api/lint");
  const report = Object.fromEntries(Object.entries(full).map(([k, v]) => [k, v.filter((i) => allMode() || lintProject(i) === project)]));
  const here = Object.values(report).reduce((a, v) => a + v.length, 0);
  const all = Object.values(full).reduce((a, v) => a + v.length, 0);
  // Badge and tab show this project's findings; the tab points to the rest so findings elsewhere are not missed.
  $("lint-count").textContent = here || "";
  $("lint-count").title = `${here} in ${project}, ${all} in all projects`;
  if (!render) return;
  const box = $("lint"); box.replaceChildren();
  for (const [check, items] of Object.entries(report)) {
    if (!items.length) continue;
    const ul = el("ol", "hits"), [title, fix] = LINT_INFO[check] || [check.replace(/_/g, " "), ""];
    for (const item of items) { // "file.md: detail" -> the note's title, the detail under it
      const m = item.match(/^([\w./-]+\.md)(?::\s*|\s+)?(.*)$/);
      if (!m) { ul.append(el("li", "hit-text", item)); continue; }
      const id = m[1].includes("/") ? m[1] : `memory/${m[1]}`;
      const li = hitItem(byId.get(id)?.label || m[1].split("/").pop(), m[2].replace(/\*\*/g, ""), () => openNote(id));
      li.firstChild.title = item; ul.append(li);
    }
    const h = el("h3", null, `${title} (${items.length})`);
    box.append(h, ...(fix ? [el("p", "lint-fix muted", fix)] : []), ul);
  }
  if (!here) box.textContent = "No findings.";
  if (all > here) {
    const b = el("button", "ghost small", `${all - here} in other projects`); b.onclick = () => switchProject(ALL);
    box.append(b);
  }
}
// Tasks: the task lines between agents in this project (all projects on the All map), overdue first, then by due
// date; results handed back; and which agent sessions can pick work up. "Done" removes the line (history keeps it).
async function loadTasks(render = true) {
  const p = project, res = await api("/ui/api/tasks");
  if (p !== project) return;
  const here = (x) => allMode() || x.project === project;
  const tasks = res.tasks.filter(here), open = tasks.filter((t) => !t.done), back = tasks.filter((t) => t.done);
  $("tasks-count").textContent = open.length || "";
  $("tasks-count").className = open.some((t) => t.overdue) ? "badge warn" : "badge";
  if (!render) return;
  const box = $("tasks"); box.replaceChildren();
  const row = (t) => {
    const n = byId.get(t.path), li = hitItem(`${t.id} · ${t.owner}`, t.text, () => openNote(t.path, { find: [t.id.toLowerCase()] }));
    const meta = el("span", "hit-text", [t.done ? "" : `${t.from || "?"} → ${t.owner}`, t.done ? "" : `start ${t.start}`,
      t.due && `due ${t.due}`, t.claimed && "claimed", t.waiting_reason, n ? n.label : t.path.split("/").pop(),
      allMode() ? t.project : ""].filter(Boolean).join(" · "));
    if (t.legacy) meta.prepend(el("span", "badge", "legacy"), " ");
    if (t.stalled) meta.prepend(el("span", "new", "stalled"), " ");
    if (t.overdue) meta.prepend(el("span", "new", "overdue"), " ");
    li.className = t.waiting_reason ? "task waiting" : "task"; li.firstChild.append(meta);
    li.firstChild.title = t.line + (t.legacy ? "\nLegacy line: no from/start, so it may start now." : "") +
      (t.author ? `\nWritten by ${t.author}` : "") + (t.stalled ? "\nWoken, but nobody claimed it within 30 minutes." : "");
    if (t.can_approve) {
      const ok = el("button", "ghost small", "Approve");
      ok.title = "Let this task start (it waits for the project owner's or an admin's approval)";
      ok.onclick = async () => {
        try { await api("/ui/api/task/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project: t.project, id: t.id }) }); loadTasks(true); }
        catch (e) { alert(e.message); }
      };
      li.append(ok);
    }
    if (me.writable?.includes(t.project)) {
      const done = el("button", "ghost small", "Done");
      done.title = "Remove this line from the memo (its history keeps it)";
      done.onclick = async () => {
        if (!confirm(`Remove ${t.id} from ${t.path}?`)) return;
        try { await api("/ui/api/task/done", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: t.path, line: t.line }) }); loadTasks(true); }
        catch (e) { alert(e.message); }
      };
      li.append(done);
    }
    return li;
  };
  const section = (title, items) => {
    if (!items.length) return;
    const ol = el("ol", "hits"); items.forEach((t) => ol.append(row(t)));
    box.append(el("h3", null, `${title} (${items.length})`), ol);
  };
  open.sort((a, b) => (b.overdue - a.overdue) || (a.due || "9").localeCompare(b.due || "9"));
  section("Open", open);
  section("Handed back", back);
  if (!tasks.length) box.append(el("p", "muted", "No open tasks."));
  const sessions = res.sessions.filter(here);
  if (sessions.length) {
    const ol = el("ol", "hits");
    for (const c of sessions) {
      const li = el("li", "session");
      li.append(el("span", "hit-path", `${c.agent} · ${c.device}`),
        el("span", "hit-text", [c.wake ? "can be woken" : "not wakeable", c.plugin && `plugin ${c.plugin}`, ago(c.at), allMode() ? c.project : ""].filter(Boolean).join(" · ")));
      const errs = c.errors || [];
      if (errs.length) {  // hook failures the device reported (our hooks, or read from the agent's own log)
        const last = errs[errs.length - 1], e = el("span", "hit-text warn",
          `${errs.length} hook error${errs.length === 1 ? "" : "s"} · last: ${last.hook || last.event} ${last.code ?? ""} ${ago(last.at)}`);
        e.title = errs.map((x) => `${new Date(x.at * 1000).toLocaleString()} ${x.source} ${x.hook} ${x.event} ${x.code ?? ""}\n${x.stderr}`).join("\n\n");
        li.append(e);
      }
      ol.append(li);
    }
    box.append(el("h3", null, `Agent sessions (${sessions.length})`), ol);
  }
}
// Lint checks in plain words, with what to do about them (unknown checks fall back to their name).
const LINT_INFO = {
  usage_outlier: ["Session that used far more tokens than usual", "Look at that session's log: a loop, a huge file read or a runaway subagent? Nothing to fix when the work was simply big."],
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
    const link = (label, path) => { const a = el("a", null, label); a.href = `#${path}`; a.onclick = (ev) => { ev.preventDefault(); $("ingest-dialog").close(); openNote(path); }; return a; };
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
  $("admin-link").hidden = false;
  $("admin-link").textContent = me.admin ? "Admin" : "My tokens & projects"; // non-admins manage their own there
  $("who").textContent = me.user;
  if (!me.admin) document.querySelector('[data-tab="lint"]').hidden = $("ingest-open").hidden = true;
  $("new-memo").hidden = !me.writable.includes(project);
  $("add-menu").hidden = $("new-memo").hidden && $("ingest-open").hidden;
}

loadMe().then(loadGraph).catch((e) => {
  $("note").replaceChildren(el("p", "error", `Could not load the knowledge base: ${e.message}. Reload the page; if it stays, the server may be restarting.`));
  throw e;
}).then(() => {
  connectLive();
  loadLint(false); loadTasks(false); loadRecent();
  const id = decode(location.hash.slice(1));
  if (id) openNote(id, { push: false }); else homeView();
  if (store.get("kn_map") === "1") setMap(true); // the map stays open over a reload
  const tab = store.get("kn_tab");
  if (["changes", "tasks", "lint"].includes(tab) && !document.querySelector(`.tabs [data-tab="${tab}"]`).hidden) selectTab(tab); // Lint only for admins
});
