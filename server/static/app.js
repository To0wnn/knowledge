// Knowledge UI: one "brain" graph per project, search, note preview and editor, recent changes, lint and ingest.
// No build step, no external requests (3D is loaded from vendor/ only when switched on).
"use strict";

const GROUPS = ["project", "feedback", "reference", "user", "infra", "incident", "doc", "skill", "source", "index"];
// Topic clusters reuse the group hues in a fixed order; clusters beyond these share the neutral doc colour.
const CLUSTER_HUES = ["project", "feedback", "reference", "user", "infra", "incident", "skill", "source"];
const PROJECTS = ["homelab", "anpitek", "trade"];
const ROOT_PROJECT = { memory: "homelab", homelab: "homelab", anpitek: "anpitek", "anpitek-memory": "anpitek",
  trade: "trade", "trade-memory": "trade", claude: "shared" };
const MEMORY_ROOT = { homelab: "memory", anpitek: "anpitek-memory", trade: "trade-memory" };

const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const projectOf = (path) => ROOT_PROJECT[path.split("/")[0]] || "homelab";
const api = (path, opts = {}) => fetch(path, { credentials: "same-origin", ...opts, headers: { "X-Knowledge-UI": "1", ...(opts.headers || {}) } })
  .then(async (r) => { if (r.status === 401) location.href = "/ui/login"; const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j; });
const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* blocked */ } } };

let data = { nodes: [], links: [] };
let byId = new Map(), neighbors = new Map(), slugToId = new Map(), clusterColor = new Map();
let hidden = new Set(), selected = null, hover = null, focusSet = new Set();
let project = PROJECTS.includes(store.get("kn_project")) ? store.get("kn_project") : "homelab";
let colorBy = store.get("kn_colorby") === "cluster" ? "cluster" : "type";
let labelMin = 0, current = null, editing = false, Graph, Graph3D = null;

const inProject = (p) => p === project || p === "shared";
const lintProject = (item) => item.startsWith("anpitek") ? "anpitek" : item.startsWith("trade") ? "trade" : "homelab";
const nodeColor = (n) => colorBy === "cluster" ? (clusterColor.get(n.cluster) || css("--g-doc")) : (css(`--g-${n.group}`) || css("--g-doc"));
const linkId = (end) => end.id || end;

// ---------------------------------------------------------------- graph data
function indexGraph() {
  byId = new Map(data.nodes.map((n) => [n.id, n]));
  neighbors = new Map(data.nodes.map((n) => [n.id, new Set()]));
  for (const l of data.links) { neighbors.get(linkId(l.source))?.add(linkId(l.target)); neighbors.get(linkId(l.target))?.add(linkId(l.source)); }
  slugToId = new Map();
  for (const n of data.nodes) {
    const key = n.id.split("/").pop().replace(/\.md$/, "").replace(/-/g, "_");
    if (!slugToId.has(key) || inProject(n.project)) slugToId.set(key, n.id); // prefer the memo of this project
  }
  // Cluster colours: the biggest topics of this project get a hue, in a fixed order.
  const sizes = new Map();
  for (const n of data.nodes) if (n.cluster && inProject(n.project)) sizes.set(n.cluster, (sizes.get(n.cluster) || 0) + 1);
  const ranked = [...sizes.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  clusterColor = new Map(ranked.slice(0, CLUSTER_HUES.length).map(([c], i) => [c, css(`--g-${CLUSTER_HUES[i]}`)]));
}

function localSet() {
  if (!$("local").checked || !selected) return null;
  const seen = new Set([selected]); let edge = [selected];
  for (let d = 0; d < Number($("depth").value); d++) {
    const next = [];
    for (const id of edge) for (const nb of neighbors.get(id) || []) if (!seen.has(nb)) { seen.add(nb); next.push(nb); }
    edge = next;
  }
  return seen;
}

function visibleData() {
  const local = localSet();
  const nodes = data.nodes.filter((n) => inProject(n.project) && !hidden.has(colorBy === "cluster" ? `c:${n.cluster}` : n.group) && (!local || local.has(n.id)));
  const keep = new Set(nodes.map((n) => n.id));
  const links = data.links.filter((l) => keep.has(linkId(l.source)) && keep.has(linkId(l.target)));
  // Labels at overview zoom: the ~15 best-connected notes in view.
  const degrees = nodes.map((n) => n.degree || 0).sort((a, b) => b - a);
  labelMin = Math.max(3, degrees[Math.min(14, degrees.length - 1)] || 0);
  return { nodes, links };
}

function applyView(refit = false) {
  if (refit) loadGraph.fitted = false;
  const v = visibleData();
  Graph.graphData(v);
  if (Graph3D) Graph3D.graphData({ nodes: v.nodes, links: v.links.map((l) => ({ source: linkId(l.source), target: linkId(l.target) })) });
}

async function refreshData() {
  // Reload after a change but keep the layout: copy positions of known nodes onto the new data.
  const old = new Map(data.nodes.map((n) => [n.id, n]));
  data = await api("/ui/api/graph");
  for (const n of data.nodes) { const o = old.get(n.id); if (o) Object.assign(n, { x: o.x, y: o.y, vx: 0, vy: 0 }); }
  indexGraph(); renderProjects(); renderFilters(); applyView();
}

// ---------------------------------------------------------------- 2D / 3D rendering
const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
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
  if (node.id === selected) { ctx.lineWidth = 2 / scale; ctx.strokeStyle = css("--ink"); ctx.stroke(); }
  const showLabel = node.id === selected || node.id === hover || (focusSet.size && focusSet.has(node.id)) || scale > 2.2 || ((node.degree || 0) >= labelMin && scale > 0.6);
  if (showLabel && lit) {
    const size = Math.max((node.id === hover ? 16 : 12.5) / scale, 3), y = node.y + r + 2 / scale;
    ctx.font = `${node.id === hover ? 600 : 400} ${size}px ${css("--font")}`; ctx.textAlign = "center"; ctx.textBaseline = "top";
    if (node.id === hover) { // a pill behind the hovered label
      const w = ctx.measureText(node.label).width + size, h = size * 1.5;
      ctx.fillStyle = css("--panel") || css("--canvas"); ctx.strokeStyle = nodeColor(node); ctx.lineWidth = 1 / scale;
      ctx.beginPath(); ctx.roundRect(node.x - w / 2, y - size * 0.25, w, h, h / 2); ctx.fill(); ctx.stroke();
    } else {
      ctx.lineWidth = 3 / scale; ctx.strokeStyle = css("--canvas"); ctx.strokeText(node.label, node.x, y); // halo keeps labels readable over links
    }
    ctx.fillStyle = css("--ink"); ctx.fillText(node.label, node.x, y);
  }
  ctx.globalAlpha = 1;
}

// Keep redrawing while hover animations run; force-graph pauses redraws when idle.
let settleTimer;
function animateHover() {
  if (calm) return;
  Graph.autoPauseRedraw(false); clearTimeout(settleTimer);
  if (!hover) settleTimer = setTimeout(() => Graph.autoPauseRedraw(true), 400);
}

function setFocus(id) {
  focusSet = id ? new Set([id, ...(neighbors.get(id) || [])]) : new Set();
  if (Graph3D) Graph3D.nodeColor(Graph3D.nodeColor()); // repaint
}

const linkLit = (l) => focusSet.size && focusSet.has(linkId(l.source)) && focusSet.has(linkId(l.target));

async function loadGraph() {
  data = await api("/ui/api/graph");
  indexGraph();
  Graph = ForceGraph()($("g2d"))
    .nodeId("id").nodeLabel(() => "") // the hover pill shows the name
    .nodeCanvasObject(drawNode)
    .nodePointerAreaPaint((n, c, ctx) => { ctx.fillStyle = c; ctx.beginPath(); ctx.arc(n.x, n.y, (baseRadius(n) + 2) * (n.__s || 1), 0, 2 * Math.PI); ctx.fill(); })
    .linkColor((l) => (linkLit(l) ? css("--accent") : css("--line")))
    .linkWidth((l) => (linkLit(l) ? 1.4 : 0.6))
    .linkDirectionalParticles((l) => (!calm && hover && linkLit(l) ? 2 : 0))
    .linkDirectionalParticleWidth(2.2).linkDirectionalParticleSpeed(0.006)
    .linkDirectionalParticleColor(() => css("--accent"))
    .backgroundColor(css("--canvas"))
    .cooldownTicks(250) // the layout settles, then the simulation stops: no CPU while idle
    .onEngineStop(() => { if (!loadGraph.fitted) { Graph.zoomToFit(400, 40); loadGraph.fitted = true; } })
    .onNodeHover((n) => { hover = n ? n.id : null; if (!selected) setFocus(hover); $("g2d").style.cursor = n ? "pointer" : ""; Graph.linkDirectionalParticles(Graph.linkDirectionalParticles()); animateHover(); })
    .onNodeClick((n) => openNote(n.id))
    .onBackgroundClick(() => { selected = null; setFocus(null); if ($("local").checked) applyView(true); });
  // More room than the force-graph defaults (-30 / 30) so labels do not pile up.
  Graph.d3Force("charge").strength(-90); Graph.d3Force("link").distance(42);
  new ResizeObserver(() => { const w = $("graph").clientWidth, h = $("graph").clientHeight; Graph.width(w).height(h); Graph3D?.width(w).height(h); }).observe($("graph"));
  renderProjects(); renderFilters(); applyView(true);
}

async function toggle3D() {
  const on = !Graph3D;
  $("toggle3d").setAttribute("aria-pressed", String(on));
  if (!on) { Graph3D._destructor(); Graph3D = null; $("g3d").replaceChildren(); $("g3d").hidden = true; $("g2d").hidden = false; Graph.resumeAnimation(); return; }
  if (!window.ForceGraph3D) {
    await new Promise((ok, fail) => { const s = document.createElement("script"); s.src = "/ui/static/vendor/3d-force-graph-1.80.1.js"; s.onload = ok; s.onerror = fail; document.head.append(s); });
  }
  Graph.pauseAnimation(); $("g2d").hidden = true; $("g3d").hidden = false;
  Graph3D = ForceGraph3D()($("g3d"))
    .width($("graph").clientWidth).height($("graph").clientHeight)
    .backgroundColor(css("--canvas"))
    .nodeId("id").nodeLabel((n) => `${n.label}`).nodeVal((n) => 1 + (n.degree || 0) / 2)
    .nodeColor((n) => (focusSet.size && !focusSet.has(n.id) ? css("--line") : nodeColor(n)))
    .linkColor((l) => (linkLit(l) ? css("--accent") : css("--muted"))).linkOpacity(0.35)
    .cooldownTicks(300)
    .onNodeClick((n) => openNote(n.id))
    .onBackgroundClick(() => { selected = null; setFocus(null); });
  applyView();
}

// ---------------------------------------------------------------- side panel controls
function renderProjects() {
  const box = $("projects"); box.replaceChildren();
  PROJECTS.forEach((p, i) => {
    const b = el("button"); b.title = `${p} (${i + 1})`;
    b.setAttribute("aria-pressed", String(p === project));
    b.append(p, el("span", "n", String(data.nodes.filter((x) => x.project === p).length)));
    b.onclick = () => switchProject(p);
    box.append(b);
  });
}

function switchProject(p, refit = true) {
  if (p === project || !PROJECTS.includes(p)) return;
  project = p; store.set("kn_project", p);
  selected = null; setFocus(null); hidden = new Set();
  indexGraph(); renderProjects(); renderFilters(); applyView(refit);
  if ($("q").value.trim()) runSearch();
  loadLint(!$("tab-lint").hidden);
  if (!$("tab-recent").hidden) loadRecent();
  if (!$("tab-insights").hidden) renderInsights();
}

function renderFilters() {
  const shown = data.nodes.filter((n) => inProject(n.project));
  const box = $("filters"); box.replaceChildren();
  const chips = colorBy === "cluster"
    ? [...clusterColor.keys()].map((c) => ({ key: `c:${c}`, label: c, color: clusterColor.get(c), count: shown.filter((n) => n.cluster === c).length }))
    : GROUPS.map((g) => ({ key: g, label: g, color: css(`--g-${g}`), count: shown.filter((n) => n.group === g).length })).filter((c) => c.count);
  for (const c of chips) {
    const b = el("button", "chip"); b.setAttribute("aria-pressed", String(!hidden.has(c.key)));
    const dot = el("i"); dot.style.background = c.color;
    b.append(dot, `${c.label} ${c.count}`);
    b.onclick = () => { hidden.has(c.key) ? hidden.delete(c.key) : hidden.add(c.key); b.setAttribute("aria-pressed", String(!hidden.has(c.key))); applyView(); };
    box.append(b);
  }
  for (const b of $("colorby").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.mode === colorBy));
}

for (const b of $("colorby").querySelectorAll("button")) b.onclick = () => {
  colorBy = b.dataset.mode; store.set("kn_colorby", colorBy); hidden = new Set(); renderFilters(); applyView();
};
$("local").onchange = () => applyView(true);
$("depth").onchange = () => { if ($("local").checked) applyView(true); };
$("toggle3d").onclick = () => toggle3D().catch((e) => alert(`3D view failed to load: ${e.message || e}`));

// ---------------------------------------------------------------- note preview, deep links and editor
function splitFront(text) {
  const m = text.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!m) return { meta: {}, body: text };
  const meta = {};
  for (const line of m[1].split("\n")) { const k = line.match(/^\s*(name|description|type|title|source):\s*(.*)$/); if (k) meta[k[1]] = k[2].replace(/^"|"$/g, ""); }
  return { meta, body: text.slice(m[0].length) };
}

const ago = (sec) => { const d = Date.now() / 1000 - sec; return d < 90 ? "just now" : d < 3600 ? `${Math.round(d / 60)} min ago` : d < 86400 ? `${Math.round(d / 3600)} h ago` : new Date(sec * 1000).toLocaleDateString(); };

async function openNote(id, { push = true } = {}) {
  if (editing && !confirm("Discard your unsaved changes?")) return;
  editing = false;
  let note;
  try { note = await api(`/ui/api/note?path=${encodeURIComponent(id)}`); } catch (e) { $("note").textContent = e.message; return; }
  const p = projectOf(note.path);
  if (p !== "shared" && p !== project) switchProject(p, false);
  if (push && decodeURIComponent(location.hash.slice(1)) !== note.path) history.pushState(null, "", `#${note.path}`);
  current = note;
  selected = byId.has(note.path) ? note.path : null; setFocus(selected);
  if ($("local").checked) applyView(true);
  const n = byId.get(note.path);
  if (n && Graph && n.x != null && !$("local").checked) { Graph.centerAt(n.x, n.y, 600); Graph.zoom(Math.max(Graph.zoom(), 2.5), 600); }
  renderNote();
}

function renderNote() {
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
  if (n) { const pill = el("span", "pill", colorBy === "cluster" && n.cluster ? n.cluster : n.group); pill.style.background = nodeColor(n); head.append(pill); }
  const tools = el("div", "note-tools");
  const copy = el("button", "ghost small", "Copy link");
  copy.onclick = () => navigator.clipboard?.writeText(`${location.origin}/ui#${note.path}`).then(() => { copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy link"), 1500); });
  tools.append(copy);
  if (note.writable) { const edit = el("button", "ghost small", "Edit (E)"); edit.onclick = startEdit; tools.append(edit); }
  const hist = el("button", "ghost small", "History"); hist.onclick = showHistory; tools.append(hist);
  pane.append(head, tools, el("div", "note-path", `${note.path} · version ${note.version}`));
  if (meta.description) pane.append(el("p", "note-desc", meta.description));
  const md = el("div", "md"); md.innerHTML = DOMPurify.sanitize(marked.parse(linked, { gfm: true })); pane.append(md);
  const rel = el("div", "backlinks"); pane.append(rel);
  const back = [...(neighbors.get(note.path) || [])].map((x) => byId.get(x)).filter(Boolean).sort((a, b) => a.label.localeCompare(b.label));
  if (back.length) {
    const box = el("div", "backlinks"); box.append(el("h3", null, `Linked (${back.length})`));
    for (const b of back) { const a = el("a", null, b.label); a.href = `#${b.id}`; box.append(a); }
    pane.append(box);
  }
  pane.scrollTop = 0;
  // Related notes by the relevance model (links, shared sources, shared neighbours, meaning), loaded after the text.
  api(`/ui/api/related?path=${encodeURIComponent(note.path)}`).then((r) => {
    if (current !== note || !r.related.length) return;
    rel.append(el("h3", null, "Related"));
    for (const x of r.related) {
      const a = el("a"); a.href = `#${x.id}`;
      a.append(x.label, el("span", "why", ` ${x.project !== project ? x.project + " · " : ""}${x.why}`));
      rel.append(a);
    }
  }).catch(() => {});
}

async function showHistory() {
  // Every version of this note (who, when), its diff against the version before, and restore.
  const note = current, pane = $("note");
  const res = await api(`/ui/api/history?path=${encodeURIComponent(note.path)}`);
  pane.replaceChildren();
  const head = el("div", "note-head"); head.append(el("h2", null, `History of ${note.path.split("/").pop()}`));
  const back = el("button", "ghost small", "Back to the note"); back.onclick = renderNote;
  pane.append(head, el("div", "note-tools"), el("div", "note-path", note.path));
  pane.querySelector(".note-tools").append(back);
  if (!res.versions.length) { pane.append(el("p", "muted", "No earlier versions yet: history starts at the first change.")); return; }
  const list = el("ol", "hits"), view = el("div", "version");
  pane.append(list, view);
  res.versions.forEach((v, i) => {
    const when = new Date(v.time * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    const li = hitItem(`${when}${i === 0 ? " (current)" : ""}`, `${v.by === "VM" ? "edited on the VM" : v.by === "before" ? "baseline" : `via ${v.by}`} · ${v.size} bytes`, async () => {
      for (const b of list.querySelectorAll("button")) b.classList.remove("active");
      li.firstChild.classList.add("active");
      const ver = await api(`/ui/api/history?path=${encodeURIComponent(note.path)}&version=${encodeURIComponent(v.id)}`);
      view.replaceChildren(el("h3", "sub", "Changes in this version"));
      const pre = el("pre", "diff");
      for (const line of (ver.diff || "(first version)").split("\n")) {
        const span = el("span", line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : null, line + "\n");
        pre.append(span);
      }
      view.append(pre);
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
  });
}

function startEdit() {
  if (!current?.writable) return;
  editing = true;
  const pane = $("note"); pane.replaceChildren();
  const head = el("div", "note-head"); head.append(el("h2", null, `Editing ${current.path.split("/").pop()}`));
  const area = el("textarea", "editor"); area.value = current.text; area.spellcheck = false;
  const err = el("p", "error"); err.hidden = true;
  const save = el("button", null, "Save (Ctrl+S)"), cancel = el("button", "ghost", "Cancel");
  const actions = el("div", "actions"); actions.append(cancel, save);
  pane.append(head, el("div", "note-path", `${current.path} · based on version ${current.version}`), area, err, actions);
  area.focus();
  cancel.onclick = () => { editing = false; renderNote(); };
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
    if (e.key === "Escape") { e.preventDefault(); cancel.click(); }
  });
}

// internal links in the preview: #<path>, [[slug]] links and relative x.md links
$("note").addEventListener("click", (e) => {
  const a = e.target.closest("a"); if (!a) return;
  const href = a.getAttribute("href") || "";
  if (href.startsWith("#") && href.length > 1) { e.preventDefault(); openNote(decodeURIComponent(href.slice(1))); return; }
  if (/^[^:/]+\.md$/.test(href) && current) { e.preventDefault(); openNote(current.path.split("/").slice(0, -1).concat(href).join("/")); return; }
  if (/^https?:/.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
});
window.addEventListener("popstate", () => { const id = decodeURIComponent(location.hash.slice(1)); if (id) openNote(id, { push: false }); });
window.addEventListener("beforeunload", (e) => { if (editing) e.preventDefault(); });

// ---------------------------------------------------------------- new memo
$("new-memo").onclick = () => {
  $("new-where").textContent = `In ${MEMORY_ROOT[project]}/ (project ${project}), with a line in its MEMORY.md.`;
  $("new-error").hidden = true; $("new-form").reset(); $("new-dialog").showModal(); $("new-title").focus();
};
$("new-cancel").onclick = () => $("new-dialog").close();
$("new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const type = $("new-type").value, title = $("new-title").value.trim(), desc = $("new-desc").value.trim();
  const slug = title.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 50);
  const file = `${type}_${slug}.md`, path = `${MEMORY_ROOT[project]}/${file}`;
  const text = `---\nname: ${type}_${slug}\ndescription: ${desc}\nmetadata:\n  type: ${type}\n---\n\n${desc}\n`;
  try {
    await api("/ui/api/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, text, base_version: "", index_line: `- [${title}](${file}) — ${desc}` }) });
    $("new-dialog").close(); await refreshData(); await openNote(path); startEdit();
  } catch (err) { $("new-error").hidden = false; $("new-error").textContent = err.message; }
});

// ---------------------------------------------------------------- search, quick open, shortcuts
let searchTimer, searchSeq = 0;
async function runSearch() {
  const q = $("q").value.trim(), seq = ++searchSeq;
  if (!q) { $("hits").replaceChildren(); $("sem").replaceChildren(); $("sem-head").hidden = true; $("hits-empty").hidden = false; return; }
  let res; try { res = await api(`/ui/api/search?q=${encodeURIComponent(q)}&project=${project}`); } catch (e) { $("hits").textContent = e.message; return; }
  if (seq !== searchSeq) return; // a newer search is on its way
  $("hits-empty").hidden = res.hits.length > 0;
  if (!res.hits.length) $("hits-empty").textContent = "No matches.";
  $("hits").replaceChildren(...res.hits.map((h) => hitItem(`${h.path}:${h.line}`, h.text, () => openNote(h.path))));
  // Meaning-based results next to the exact matches: finds notes that use other words.
  let sem; try { sem = await api(`/ui/api/semantic?q=${encodeURIComponent(q)}&project=${project}`); } catch { return; }
  if (seq !== searchSeq) return;
  const seen = new Set(res.hits.map((h) => h.path));
  const extra = sem.hits.filter((h) => !seen.has(h.path));
  $("sem-head").hidden = false;
  $("sem-head").textContent = sem.hits.length ? "By meaning" : `By meaning: ${sem.status}`;
  $("sem").replaceChildren(...extra.map((h) => hitItem(`${(byId.get(h.path) || {}).label || h.path} · ${Math.round(h.score * 100)}%`, `${h.path}:${h.line} — ${h.text}`, () => openNote(h.path))));
}
function hitItem(top, sub, onclick) {
  const li = el("li"), b = el("button");
  b.append(el("span", "hit-path", top), el("span", "hit-text", sub)); b.onclick = onclick; li.append(b); return li;
}
$("q").addEventListener("input", () => { clearTimeout(searchTimer); selectTab("results"); searchTimer = setTimeout(runSearch, 150); });

function quickOpen() {
  $("quick").showModal(); $("quick-q").value = ""; renderQuick(); $("quick-q").focus();
}
let quickSel = 0;
function renderQuick() {
  const words = $("quick-q").value.toLowerCase().split(/\s+/).filter(Boolean);
  const list = data.nodes.filter((n) => words.every((w) => `${n.label} ${n.id}`.toLowerCase().includes(w)))
    .sort((a, b) => (inProject(b.project) - inProject(a.project)) || (b.degree - a.degree)).slice(0, 12);
  quickSel = Math.min(quickSel, Math.max(0, list.length - 1));
  $("quick-list").replaceChildren(...list.map((n, i) => {
    const li = hitItem(n.label, `${n.project} · ${n.id}`, () => { $("quick").close(); openNote(n.id); });
    if (i === quickSel) li.firstChild.classList.add("active");
    return li;
  }));
}
$("quick-q").addEventListener("input", () => { quickSel = 0; renderQuick(); });
$("quick-q").addEventListener("keydown", (e) => {
  const items = $("quick-list").querySelectorAll("button");
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); quickSel = (quickSel + (e.key === "ArrowDown" ? 1 : -1) + items.length) % Math.max(1, items.length); renderQuick(); }
  if (e.key === "Enter") { e.preventDefault(); items[quickSel]?.click(); }
});
$("quick-open").onclick = quickOpen;

document.addEventListener("keydown", (e) => {
  const typing = /input|textarea|select/i.test(document.activeElement.tagName) || document.querySelector("dialog[open]");
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); quickOpen(); return; }
  if (typing) return;
  if (e.key === "/") { e.preventDefault(); $("q").focus(); }
  else if (["1", "2", "3"].includes(e.key)) switchProject(PROJECTS[Number(e.key) - 1]);
  else if (e.key.toLowerCase() === "e" && current?.writable) { e.preventDefault(); startEdit(); }
  else if (e.key.toLowerCase() === "l") { $("local").checked = !$("local").checked; applyView(true); }
  else if (e.key === "Escape") { selected = null; setFocus(null); if ($("local").checked) applyView(true); }
});

// ---------------------------------------------------------------- tabs, recent, lint, ingest
function selectTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const t of ["results", "recent", "insights", "lint", "ingest"]) $(`tab-${t}`).hidden = t !== name;
  if (name === "lint") loadLint(true);
  if (name === "insights") renderInsights();
  if (name === "recent") loadRecent();
}
for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => selectTab(b.dataset.tab);

async function loadRecent() {
  const res = await api(`/ui/api/recent?project=${project}`);
  const box = $("recent"); box.replaceChildren();
  let day = "";
  for (const f of res.files) {
    const d = new Date(f.mtime * 1000).toDateString();
    if (d !== day) {
      day = d;
      const today = new Date().toDateString(), yesterday = new Date(Date.now() - 864e5).toDateString();
      box.append(el("li", "day", d === today ? "Today" : d === yesterday ? "Yesterday" : new Date(f.mtime * 1000).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })));
    }
    const n = byId.get(f.path);
    box.append(hitItem(n ? n.label : f.path.split("/").pop(), `${ago(f.mtime)} · ${f.by === "VM" ? "edited on the VM" : `via ${f.by}`} · ${f.path}`, () => openNote(f.path)));
  }
  if (!res.files.length) box.append(el("li", "muted", "No files yet."));
}

function highlight(ids) {
  // Show a set of notes in the graph: dim the rest and zoom to them.
  selected = null; focusSet = new Set(ids);
  if (Graph3D) Graph3D.nodeColor(Graph3D.nodeColor());
  const pts = ids.map((i) => byId.get(i)).filter((n) => n && n.x != null);
  if (pts.length && !Graph3D) Graph.zoomToFit(600, 80, (n) => focusSet.has(n.id));
}

function renderInsights() {
  const ins = data.insights || {}, box = $("insights"); box.replaceChildren();
  const mine = (x) => (x.project || projectOf(x.id || x.source)) === project;
  const label = (id) => (byId.get(id) || {}).label || id;
  const section = (title, hint, items, render) => {
    if (!items.length) return;
    box.append(el("h3", null, `${title} (${items.length})`), el("p", "muted small", hint));
    const ul = el("ul");
    for (const it of items.slice(0, 15)) { const li = el("li"), a = el("a"); render(it, a); li.append(a); ul.append(li); }
    box.append(ul);
  };
  section("Surprising connections", "Links between different topics: worth a look, sometimes a new insight.", (ins.surprising || []).filter(mine), (s, a) => {
    a.textContent = `${label(s.source)} ↔ ${label(s.target)}`; a.onclick = () => highlight([s.source, s.target]);
  });
  section("Bridge notes", "Notes that connect three or more topics; keep them accurate.", (ins.bridges || []).filter(mine), (b, a) => {
    a.textContent = `${label(b.id)} — ${b.topics} topics`; a.onclick = () => { highlight([b.id, ...(neighbors.get(b.id) || [])]); };
  });
  section("Isolated notes", "One link or none: link them from a related memo or merge them.", (ins.isolated || []).filter(mine), (i, a) => {
    a.textContent = label(i.id); a.onclick = () => openNote(i.id);
  });
  const topics = (data.communities || []).filter((c) => c.project === project);
  section("Topics", "Louvain communities; cohesion = share of possible links inside the topic. Below 0.15 is loose.", topics, (c, a) => {
    a.textContent = `${c.name} — ${c.size} notes, cohesion ${c.cohesion}${c.cohesion < 0.15 && c.size >= 3 ? " (loose)" : ""}`;
    a.onclick = () => highlight(data.nodes.filter((n) => n.cluster === c.name && inProject(n.project)).map((n) => n.id));
  });
  if (!box.children.length) box.textContent = "Nothing to report for this project.";
}

async function loadLint(render = true) {
  const full = await api("/ui/api/lint");
  const report = Object.fromEntries(Object.entries(full).map(([k, v]) => [k, v.filter((i) => lintProject(i) === project)]));
  const total = Object.values(report).reduce((a, v) => a + v.length, 0);
  $("lint-count").textContent = total || "";
  if (!render) return;
  const box = $("lint"); box.replaceChildren();
  for (const [check, items] of Object.entries(report)) {
    if (!items.length) continue;
    const ul = el("ul");
    for (const item of items) {
      const li = el("li"), m = item.match(/^([\w./-]+\.md)/);
      if (m) { const a = el("a", null, item); a.href = `#${m[1].includes("/") ? m[1] : `memory/${m[1]}`}`; li.append(a); } else li.textContent = item;
      ul.append(li);
    }
    box.append(el("h3", null, `${check.replace(/_/g, " ")} (${items.length})`), ul);
  }
  if (!total) box.textContent = "No findings.";
}
$("lint").addEventListener("click", (e) => { const a = e.target.closest("a"); if (a) { e.preventDefault(); openNote(decodeURIComponent(a.getAttribute("href").slice(1))); } });

$("ingest-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = $("ingest-result"), title = $("in-title").value.trim();
  out.className = "muted"; out.textContent = "Working…";
  try {
    let res;
    const file = $("in-file").files[0];
    if (file) {
      res = await api("/ui/api/ingest", { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream", "X-Filename": file.name, "X-Title": title } });
    } else {
      const body = $("in-url").value.trim() ? { url: $("in-url").value.trim(), title } : { text: $("in-text").value, title };
      res = await api("/ui/api/ingest", { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
    }
    out.replaceChildren(el("p", null, `Saved ${res.path} (${res.chars} characters): ${res.title}`));
    const open = el("a", null, "Open the source"); open.onclick = () => openNote(res.path); out.append(open);
    if (res.related.length) {
      out.append(el("p", null, "Possibly related memos:"));
      for (const rel of res.related) { const a = el("a", null, rel); a.style.display = "block"; a.onclick = () => openNote(rel); out.append(a); }
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
  if (!$("tab-recent").hidden) loadRecent();
  if (!$("tab-insights").hidden) renderInsights();
  loadLint(!$("tab-lint").hidden);
  if (!current) return;
  let fresh; try { fresh = await api(`/ui/api/note?path=${encodeURIComponent(current.path)}`); } catch { return; }
  if (fresh.version === current.version) return;
  if (editing) { // never overwrite what the user is typing: warn instead
    const warn = document.querySelector("#note .live-warn") || el("p", "error live-warn");
    warn.textContent = `This note was just changed elsewhere (now version ${fresh.version}). Saving will be refused; copy your text first.`;
    $("note").querySelector(".actions")?.before(warn);
    return;
  }
  const top = $("note").scrollTop;
  current = fresh; renderNote(); $("note").scrollTop = top;
}
function connectLive() {
  const es = new EventSource("/ui/api/events");
  es.addEventListener("changed", () => { clearTimeout(liveTimer); liveTimer = setTimeout(onChanged, 700); });
  es.addEventListener("hello", (e) => {
    $("live").classList.add("on");
    // A new release was deployed while this tab was open (the stream reconnects after the restart): reload, unless
    // the user is typing, then say so instead.
    if (e.data && e.data !== document.documentElement.dataset.release) {
      if (!editing) location.reload();
      else $("live").title = "A new version is available: save your edit, then reload";
    }
  });
  es.onerror = () => $("live").classList.remove("on"); // the browser reconnects by itself (retry: 3 s)
}

loadGraph().then(() => {
  connectLive();
  loadLint(false);
  const id = decodeURIComponent(location.hash.slice(1));
  if (id) openNote(id, { push: false });
});
