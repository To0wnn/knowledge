// Knowledge UI: force-graph "brain", search, note preview, lint and ingest. No build step, no external requests.
"use strict";

const GROUPS = ["project", "feedback", "reference", "user", "infra", "incident", "doc", "skill", "source", "index"];
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const color = (g) => css(`--g-${g}`) || css("--g-doc");
const $ = (id) => document.getElementById(id);
const api = (path, opts = {}) => fetch(path, { credentials: "same-origin", ...opts, headers: { "X-Knowledge-UI": "1", ...(opts.headers || {}) } })
  .then(async (r) => { if (r.status === 401) location.href = "/ui/login"; const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j; });

let data = { nodes: [], links: [] };
let byId = new Map(), neighbors = new Map(), slugToId = new Map();
let hidden = new Set(), selected = null, hover = null, focusSet = new Set();
const PROJECTS = ["all", "homelab", "anpitek", "trade"];
let project = "all";
try { project = PROJECTS.includes(localStorage.getItem("kn_project")) ? localStorage.getItem("kn_project") : "all"; } catch { /* storage blocked */ }
const inProject = (p) => project === "all" || p === project || p === "shared"; // skills are shared by every project
let Graph;

// ---------------------------------------------------------------- graph
function indexGraph() {
  byId = new Map(data.nodes.map((n) => [n.id, n]));
  neighbors = new Map(data.nodes.map((n) => [n.id, new Set()]));
  for (const l of data.links) {
    const s = l.source.id || l.source, t = l.target.id || l.target;
    neighbors.get(s)?.add(t); neighbors.get(t)?.add(s);
  }
  slugToId = new Map();
  for (const n of data.nodes) {
    const stem = n.id.split("/").pop().replace(/\.md$/, "");
    slugToId.set(stem.replace(/-/g, "_"), n.id);
  }
}

function visibleData() {
  const nodes = data.nodes.filter((n) => !hidden.has(n.group) && inProject(n.project));
  const keep = new Set(nodes.map((n) => n.id));
  const links = data.links.filter((l) => keep.has(l.source.id || l.source) && keep.has(l.target.id || l.target));
  return { nodes, links };
}

function drawNode(node, ctx, scale) {
  const r = 2.2 + Math.sqrt(node.degree || 0) * 1.25;
  const lit = focusSet.size === 0 || focusSet.has(node.id);
  ctx.globalAlpha = lit ? 1 : 0.15;
  ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
  ctx.fillStyle = color(node.group); ctx.fill();
  if (node.id === selected) { ctx.lineWidth = 2 / scale; ctx.strokeStyle = css("--ink"); ctx.stroke(); }
  const showLabel = node.id === selected || node.id === hover || (focusSet.size && focusSet.has(node.id)) || scale > 2.2 || (node.degree > 12 && scale > 0.9);
  if (showLabel && lit) {
    const size = Math.max(10 / scale, 2.5);
    ctx.font = `${size}px ${css("--font")}`; ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillStyle = css("--ink"); ctx.fillText(node.label, node.x, node.y + r + 1.5 / scale);
  }
  ctx.globalAlpha = 1;
}

function setFocus(id) {
  focusSet = id ? new Set([id, ...(neighbors.get(id) || [])]) : new Set();
}

async function loadGraph() {
  data = await api("/ui/api/graph");
  indexGraph();
  Graph = ForceGraph()($("graph"))
    .graphData(visibleData())
    .nodeId("id").nodeLabel((n) => `${n.label} — ${n.id}`)
    .nodeCanvasObject(drawNode)
    .nodePointerAreaPaint((n, c, ctx) => { ctx.fillStyle = c; ctx.beginPath(); ctx.arc(n.x, n.y, 4 + Math.sqrt(n.degree || 0) * 1.25, 0, 2 * Math.PI); ctx.fill(); })
    .linkColor((l) => { const on = focusSet.size && focusSet.has(l.source.id) && focusSet.has(l.target.id); return on ? css("--accent") : css("--line"); })
    .linkWidth((l) => (focusSet.size && focusSet.has(l.source.id) && focusSet.has(l.target.id) ? 1.4 : 0.6))
    .backgroundColor(css("--canvas"))
    .cooldownTicks(250) // the layout settles, then the simulation stops: no CPU while idle
    .onEngineStop(() => { if (!loadGraph.fitted) { Graph.zoomToFit(400, 40); loadGraph.fitted = true; } })
    .onNodeHover((n) => { hover = n ? n.id : null; if (!selected) setFocus(hover); $("graph").style.cursor = n ? "pointer" : ""; })
    .onNodeClick((n) => openNote(n.id, true))
    .onBackgroundClick(() => { selected = null; setFocus(null); });
  new ResizeObserver(() => Graph.width($("graph").clientWidth).height($("graph").clientHeight)).observe($("graph"));
  renderProjects(); renderFilters();
}

function renderProjects() {
  const box = $("projects"); box.replaceChildren();
  for (const p of PROJECTS) {
    const b = document.createElement("button");
    b.setAttribute("aria-pressed", String(p === project));
    const n = document.createElement("span"); n.className = "n";
    n.textContent = p === "all" ? data.nodes.length : data.nodes.filter((x) => x.project === p).length;
    b.append(p, n);
    b.onclick = () => {
      project = p; try { localStorage.setItem("kn_project", p); } catch { /* storage blocked */ }
      renderProjects(); renderFilters(); Graph.graphData(visibleData()); loadGraph.fitted = false;
      if ($("q").value.trim()) $("q").dispatchEvent(new Event("input"));
    };
    box.append(b);
  }
}

function renderFilters() {
  const shown = data.nodes.filter((n) => inProject(n.project));
  const present = new Set(shown.map((n) => n.group));
  const box = $("filters"); box.replaceChildren();
  for (const g of GROUPS.filter((g) => present.has(g))) {
    const b = document.createElement("button");
    b.className = "chip"; b.setAttribute("aria-pressed", String(!hidden.has(g)));
    const dot = document.createElement("i"); dot.style.background = color(g);
    b.append(dot, `${g} ${shown.filter((n) => n.group === g).length}`);
    b.onclick = () => { hidden.has(g) ? hidden.delete(g) : hidden.add(g); b.setAttribute("aria-pressed", String(!hidden.has(g))); Graph.graphData(visibleData()); };
    box.append(b);
  }
}

// ---------------------------------------------------------------- note preview
function splitFront(text) {
  const m = text.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!m) return { meta: {}, body: text };
  const meta = {};
  for (const line of m[1].split("\n")) { const k = line.match(/^\s*(name|description|type|title|source):\s*(.*)$/); if (k) meta[k[1]] = k[2].replace(/^"|"$/g, ""); }
  return { meta, body: text.slice(m[0].length) };
}

async function openNote(id, fromGraph = false) {
  let note;
  try { note = await api(`/ui/api/note?path=${encodeURIComponent(id)}`); } catch (e) { $("note").textContent = e.message; return; }
  selected = byId.has(id) ? id : null; setFocus(selected);
  const n = byId.get(id);
  if (n && !fromGraph && Graph) { Graph.centerAt(n.x, n.y, 600); Graph.zoom(3, 600); }
  const { meta, body } = splitFront(note.text);
  // [[slug]] -> internal link to that memo, when it exists
  const linked = body.replace(/\[\[([a-z0-9][a-z0-9_-]*)\]\]/g, (all, slug) => {
    const target = slugToId.get(slug.replace(/-/g, "_"));
    return target ? `[${slug}](#node:${target})` : all;
  });
  const html = DOMPurify.sanitize(marked.parse(linked, { gfm: true }));
  const pane = $("note"); pane.replaceChildren();
  const head = document.createElement("div"); head.className = "note-head";
  const h = document.createElement("h2"); h.textContent = n ? n.label : id.split("/").pop();
  head.append(h);
  if (n) { const p = document.createElement("span"); p.className = "pill"; p.style.background = color(n.group); p.textContent = n.group; head.append(p); }
  const path = document.createElement("div"); path.className = "note-path"; path.textContent = `${note.path} · version ${note.version}`;
  pane.append(head, path);
  if (meta.description) { const d = document.createElement("p"); d.className = "note-desc"; d.textContent = meta.description; pane.append(d); }
  const md = document.createElement("div"); md.className = "md"; md.innerHTML = html; pane.append(md);
  const back = [...(neighbors.get(id) || [])].map((x) => byId.get(x)).filter(Boolean).sort((a, b) => a.label.localeCompare(b.label));
  if (back.length) {
    const box = document.createElement("div"); box.className = "backlinks";
    const t = document.createElement("h3"); t.textContent = `Connected (${back.length})`; box.append(t);
    for (const b of back) { const a = document.createElement("a"); a.textContent = b.label; a.onclick = () => openNote(b.id); box.append(a); }
    pane.append(box);
  }
  pane.scrollTop = 0;
}

// internal links in the preview: #node:<id> and relative x.md links
$("note").addEventListener("click", (e) => {
  const a = e.target.closest("a"); if (!a) return;
  const href = a.getAttribute("href") || "";
  if (href.startsWith("#node:")) { e.preventDefault(); openNote(href.slice(6)); return; }
  if (/^[^:/]+\.md$/.test(href) && selected) { e.preventDefault(); openNote(selected.split("/").slice(0, -1).concat(href).join("/")); return; }
  if (/^https?:/.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
});

// ---------------------------------------------------------------- search
let searchTimer, searchSeq = 0;
$("q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const q = $("q").value.trim(), seq = ++searchSeq;
    selectTab("results");
    if (!q) { $("hits").replaceChildren(); $("hits-empty").hidden = false; return; }
    const scope = project === "all" ? "" : `&project=${project}`;
    let res; try { res = await api(`/ui/api/search?q=${encodeURIComponent(q)}${scope}`); } catch (e) { $("hits").textContent = e.message; return; }
    if (seq !== searchSeq) return; // a newer search is on its way
    $("hits-empty").hidden = res.hits.length > 0;
    if (!res.hits.length) $("hits-empty").textContent = "No matches.";
    $("hits").replaceChildren(...res.hits.map((h) => {
      const li = document.createElement("li"), b = document.createElement("button");
      const p = document.createElement("span"); p.className = "hit-path"; p.textContent = `${h.path}:${h.line}`;
      const t = document.createElement("span"); t.className = "hit-text"; t.textContent = h.text;
      b.append(p, t); b.onclick = () => openNote(h.path); li.append(b); return li;
    }));
  }, 150);
});
document.addEventListener("keydown", (e) => { if (e.key === "/" && document.activeElement !== $("q") && !/input|textarea/i.test(document.activeElement.tagName)) { e.preventDefault(); $("q").focus(); } });

// ---------------------------------------------------------------- tabs, lint, ingest
function selectTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const t of ["results", "lint", "ingest"]) $(`tab-${t}`).hidden = t !== name;
  if (name === "lint") loadLint();
}
for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => selectTab(b.dataset.tab);

async function loadLint() {
  const report = await api("/ui/api/lint");
  const box = $("lint"); box.replaceChildren();
  let total = 0;
  for (const [check, items] of Object.entries(report)) {
    if (!items.length) continue;
    total += items.length;
    const h = document.createElement("h3"); h.textContent = `${check.replace(/_/g, " ")} (${items.length})`;
    const ul = document.createElement("ul");
    for (const item of items) {
      const li = document.createElement("li");
      const m = item.match(/^([\w./-]+\.md)/);
      if (m) {
        const path = m[1].includes("/") ? m[1] : `memory/${m[1]}`;
        const a = document.createElement("a"); a.textContent = item; a.onclick = () => openNote(path); li.append(a);
      } else li.textContent = item;
      ul.append(li);
    }
    box.append(h, ul);
  }
  if (!total) box.textContent = "No findings.";
  $("lint-count").textContent = total || "";
}

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
    out.replaceChildren();
    const p = document.createElement("p"); p.textContent = `Saved ${res.path} (${res.chars} characters): ${res.title}`; out.append(p);
    const open = document.createElement("a"); open.textContent = "Open the source"; open.onclick = () => openNote(res.path); out.append(open);
    if (res.related.length) {
      const r = document.createElement("p"); r.textContent = "Possibly related memos:"; out.append(r);
      for (const rel of res.related) { const a = document.createElement("a"); a.textContent = rel; a.style.display = "block"; a.onclick = () => openNote(rel); out.append(a); }
    }
    const hint = document.createElement("p"); hint.className = "muted"; hint.textContent = "Next: ask Claude to integrate this source into the memos."; out.append(hint);
    e.target.reset();
    data = await api("/ui/api/graph"); indexGraph(); Graph.graphData(visibleData()); renderProjects(); renderFilters();
  } catch (err) { out.className = "error"; out.textContent = err.message; }
});

loadGraph().then(() => api("/ui/api/lint").then((r) => { $("lint-count").textContent = Object.values(r).reduce((a, v) => a + v.length, 0) || ""; }));
