// Mission map (Agents cockpit > Tools): the task lines between agents as a graph, always laid out automatically.
// Nodes = tasks (real <button>s), one horizontal lane per owner (agent or person), edges = "start after <ID>",
// hand-overs, -cont continuations and -done results (SVG). Left to right by dependency: a small layered (Sugiyama)
// layout, missionLayout(), a pure function the UI smoke test checks. A phone gets the chains per lane as a list.
// Uses app.js globals: $, el, api, project, PROJECTS, ALL, shown, openNote, openCockpit, selectTab.
"use strict";

const MS_STATUS = { open: "open", waiting: "waiting", approval: "needs approval", claimed: "claimed", running: "running",
  done: "done", stalled: "stalled" };
const MS_EDGE = { dep: "start after", handover: "hand-over", cont: "continued", done: "result back" };
const MS_SIZE = { w: 200, h: 74, gx: 70, gy: 12, lane: 148, pad: 14 };
const ms = { root: null, data: null, lay: null, all: false, project: "", view: { x: 0, y: 0, k: 1 }, fitted: false,
  timer: 0, sel: "", dragged: false, pointers: new Map(), pinch: null };

// The layout: layer = longest path over the task-to-task edges (a cycle, which only bad data makes, is cut where the
// walk meets itself); tasks without any edge fill the emptiest columns of their lane so a lane stays low; then 4
// barycentre sweeps order the tasks inside each lane column by the rows of their neighbours. Returns positions in
// px: {pos: Map(key -> {x, y, w, h, layer, lane}), lanes: [{id, name, y, h}], width, height}.
function missionLayout(g, s = MS_SIZE) {
  const known = new Set(g.nodes.map((n) => n.key));
  const inc = new Map(), out = new Map();
  for (const n of g.nodes) { inc.set(n.key, []); out.set(n.key, []); }
  for (const e of g.edges) if (known.has(e.from) && known.has(e.to) && e.from !== e.to) { out.get(e.from).push(e.to); inc.get(e.to).push(e.from); }
  const layer = new Map(), walking = new Set();
  const depth = (k) => {
    if (layer.has(k)) return layer.get(k);
    if (walking.has(k)) return -1;
    walking.add(k);
    let d = 0; for (const p of inc.get(k)) d = Math.max(d, depth(p) + 1);
    walking.delete(k); layer.set(k, d); return d;
  };
  const lone = (k) => !inc.get(k).length && !out.get(k).length;
  for (const n of g.nodes) if (!lone(n.key)) depth(n.key);
  const laneIds = g.lanes.map((l) => l.id);
  for (const n of g.nodes) if (!laneIds.includes(n.lane)) laneIds.push(n.lane);
  const cells = new Map(laneIds.map((l) => [l, new Map()])); // lane -> layer -> [keys]
  const put = (k, lane, d) => { const c = cells.get(lane); if (!c.has(d)) c.set(d, []); c.get(d).push(k); layer.set(k, d); };
  const rank = (n) => Object.keys(MS_STATUS).indexOf(n.status);
  const sorted = [...g.nodes].sort((a, b) => rank(a) - rank(b) || a.id.localeCompare(b.id));
  for (const n of sorted) if (!lone(n.key)) put(n.key, n.lane, layer.get(n.key));
  // a task without links goes to the first column (it can start without waiting on anything); only when that
  // column would grow higher than the lane already is (at least 3) the rest fill the emptiest columns
  const maxLayer = Math.max(0, ...layer.values());
  for (const lane of laneIds) {
    const alone = sorted.filter((n) => n.lane === lane && lone(n.key)), c = cells.get(lane);
    const high = Math.max(3, ...[...c.values()].map((x) => x.length));
    const cols = Math.max(maxLayer + 1, Math.ceil(alone.length / high));
    for (const n of alone) {
      let best = 0;
      if ((c.get(0) || []).length >= high) for (let d = 1; d < cols; d++) if ((c.get(d) || []).length < (c.get(best) || []).length) best = d;
      put(n.key, lane, best);
    }
  }
  // lane bands: as high as their fullest column
  const lanes = [], laneTop = new Map(); let y = 0;
  for (const id of laneIds) {
    const rows = Math.max(1, ...[...cells.get(id).values()].map((c) => c.length));
    const h = s.pad * 2 + rows * s.h + (rows - 1) * s.gy;
    lanes.push({ id, name: (g.lanes.find((l) => l.id === id) || { name: id }).name, y, h });
    laneTop.set(id, y); y += h;
  }
  const row = (k) => { const n = g.nodes.find((m) => m.key === k); return laneTop.get(n.lane) + cells.get(n.lane).get(layer.get(k)).indexOf(k) * (s.h + s.gy); };
  const top = Math.max(0, ...layer.values());
  for (let sweep = 0; sweep < 4; sweep++) {
    const forward = sweep % 2 === 0, nb = forward ? inc : out;
    for (let i = 0; i <= top; i++) {
      const d = forward ? i : top - i;
      for (const c of cells.values()) {
        const keys = c.get(d); if (!keys || keys.length < 2) continue;
        const at = new Map(keys.map((k, j) => {
          const ys = nb.get(k).map(row);
          return [k, ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : row(k)];
        }));
        keys.sort((a, b) => at.get(a) - at.get(b));
      }
    }
  }
  const pos = new Map();
  for (const n of g.nodes) {
    const d = layer.get(n.key);
    pos.set(n.key, { x: s.lane + d * (s.w + s.gx), y: s.pad + row(n.key), w: s.w, h: s.h, layer: d, lane: n.lane });
  }
  const width = s.lane + (Math.max(0, ...layer.values()) + 1) * (s.w + s.gx);
  return { pos, lanes, width, height: Math.max(y, 1) };
}

function msBuild() {
  const root = el("section", "ms"); root.id = "mission"; root.hidden = true; root.setAttribute("aria-label", "Mission map");
  root.innerHTML = `<div class="ms-bar">
      <button type="button" class="ck-pill" id="ms-close" title="Back to the agents (Esc)">&larr;&nbsp;Agents</button>
      <h2 class="ms-title">Mission map</h2>
      <select id="ms-project" aria-label="Project"></select>
      <button type="button" class="ck-pill" id="ms-all" aria-pressed="false" title="Also results handed back more than a day ago">Show all</button>
      <span class="ck-gap"></span>
      <span class="ms-count muted" id="ms-count" aria-live="polite"></span>
      <button type="button" class="ck-pill" id="ms-fit" title="Fit the map to the screen (F)">Fit</button>
    </div>
    <div class="ms-legend" id="ms-legend"></div>
    <div class="ms-stage" id="ms-stage"><div class="ms-world" id="ms-world"></div></div>
    <div class="ms-list" id="ms-list"></div>
    <aside class="ms-panel" id="ms-panel" hidden aria-label="Task"></aside>`;
  document.body.append(root);
  ms.root = root;
  const legend = $("ms-legend");
  for (const [k, v] of Object.entries(MS_STATUS)) legend.append(el("span", `ms-chip s-${k}`, v));
  for (const [k, v] of Object.entries(MS_EDGE)) {
    const s = el("span", "ms-edge-key");
    s.innerHTML = `<svg viewBox="0 0 28 8" aria-hidden="true"><path class="ms-e k-${k}" d="M1 4H27"/></svg>`;
    s.append(v); legend.append(s);
  }
  $("ms-close").onclick = () => openMission(false);
  $("ms-fit").onclick = msFit;
  $("ms-all").onclick = () => { ms.all = !ms.all; $("ms-all").setAttribute("aria-pressed", ms.all); ms.fitted = false; ms.data = null; msLoad(); };
  $("ms-project").onchange = () => { ms.project = $("ms-project").value; ms.fitted = false; msLoad(); };
  msPanZoom($("ms-stage"));
  window.addEventListener("keydown", (e) => { // capture: before the cockpit's own Escape
    if (root.hidden || e.ctrlKey || e.metaKey || e.altKey || /input|textarea|select/i.test(document.activeElement.tagName)) return;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); if (!$("ms-panel").hidden) msClosePanel(); else openMission(false); }
    else if (e.key.toLowerCase() === "f") { e.preventDefault(); e.stopPropagation(); msFit(); }
  }, true);
  matchMedia("(max-width: 640px)").addEventListener("change", () => msRender());
}

function openMission(on) {
  if (!ms.root) msBuild();
  ms.root.hidden = !on; clearTimeout(ms.timer);
  if (!on) return;
  const sel = $("ms-project"); sel.replaceChildren();
  sel.append(new Option("All projects", ""));
  for (const p of PROJECTS) sel.append(new Option(shown(p), p));
  ms.project = project === ALL ? "" : project; sel.value = ms.project; ms.fitted = false; ms.data = null;
  msLoad();
  $("ms-close").focus();
}

async function msLoad() {
  clearTimeout(ms.timer);
  if (ms.root.hidden) return;
  try {
    const q = new URLSearchParams({ project: ms.project, ...(ms.all ? { all: "1" } : {}) });
    const g = await api(`/ui/api/mission?${q}`);
    if (JSON.stringify(g) !== JSON.stringify(ms.data)) { ms.data = g; msRender(); } // a re-render would drop the focus
  } catch (e) { $("ms-count").textContent = e.message; }
  if (!document.hidden) ms.timer = setTimeout(msLoad, 20000); // tasks change when agents write memos
}

const msNarrow = () => matchMedia("(max-width: 640px)").matches;
const msNode = (k) => ms.data.nodes.find((n) => n.key === k);

function msRender() {
  const g = ms.data; if (!g) return;
  const open = g.nodes.filter((n) => n.status !== "done").length;
  $("ms-count").textContent = `${open} open · ${g.nodes.length - open} done${ms.all ? "" : ` (last ${g.done_hours} h)`}`;
  ms.lay = missionLayout(g);
  $("ms-stage").hidden = msNarrow(); $("ms-list").hidden = !msNarrow();
  if (!g.nodes.length) {
    $("ms-world").replaceChildren(); $("ms-list").replaceChildren(el("p", "muted ms-empty", "No task lines here."));
    return;
  }
  if (msNarrow()) msList(); else msGraph();
  if (ms.sel && msNode(ms.sel)) msOpenPanel(ms.sel); else if (ms.sel) msClosePanel();
}

function msButton(n, cls) {
  const b = el("button", `${cls} s-${n.status}`); b.type = "button"; b.dataset.key = n.key;
  b.append(el("span", "ms-id", n.id), el("span", "ms-st", MS_STATUS[n.status]), el("span", "ms-tx", n.text || ""));
  b.title = n.line; b.setAttribute("aria-label", `${n.id}, ${MS_STATUS[n.status]}, ${n.owner}: ${n.text}`);
  b.onclick = () => { if (ms.dragged) return; msOpenPanel(n.key); };
  return b;
}

function msGraph() {
  const g = ms.data, L = ms.lay, world = $("ms-world"), s = MS_SIZE;
  world.replaceChildren();
  world.style.width = `${L.width}px`; world.style.height = `${L.height}px`;
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "ms-edges"); svg.setAttribute("width", L.width); svg.setAttribute("height", L.height);
  svg.setAttribute("aria-hidden", "true");
  const defs = document.createElementNS(ns, "defs");
  for (const k of Object.keys(MS_EDGE)) {
    const m = document.createElementNS(ns, "marker");
    for (const [a, v] of Object.entries({ id: `ms-arrow-${k}`, viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 9, markerHeight: 9, markerUnits: "userSpaceOnUse", orient: "auto" })) m.setAttribute(a, v);
    const p = document.createElementNS(ns, "path"); p.setAttribute("d", "M0 0L8 4L0 8z"); p.setAttribute("class", `ms-mk k-${k}`);
    m.append(p); defs.append(m);
  }
  svg.append(defs);
  const laneOf = new Map(L.lanes.map((l) => [l.id, l]));
  L.lanes.forEach((l, i) => {
    const r = document.createElementNS(ns, "rect");
    for (const [a, v] of Object.entries({ x: 0, y: l.y, width: L.width, height: l.h, class: `ms-band${i % 2 ? " odd" : ""}` })) r.setAttribute(a, v);
    svg.append(r);
    const lab = el("div", "ms-lane"); lab.style.top = `${l.y}px`; lab.style.height = `${l.h}px`; lab.style.width = `${s.lane - 28}px`;
    const count = g.nodes.filter((n) => n.lane === l.id).length;
    lab.append(el("strong", null, l.name), el("span", "muted", count ? `${count} task${count > 1 ? "s" : ""}` : "requester"));
    world.append(lab);
  });
  for (const e of g.edges) {
    const t = L.pos.get(e.to); if (!t) continue;
    const f = e.from.startsWith("lane:") ? laneOf.get(e.from.slice(5)) : L.pos.get(e.from); if (!f) continue;
    const sx = e.from.startsWith("lane:") ? s.lane - 28 : f.x + f.w, sy = e.from.startsWith("lane:") ? f.y + f.h / 2 : f.y + f.h / 2;
    const tx = t.x, ty = t.y + t.h / 2, dx = Math.max(36, Math.abs(tx - sx) / 2);
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", `M${sx} ${sy}C${sx + dx} ${sy},${tx - dx} ${ty},${tx - 2} ${ty}`);
    p.setAttribute("class", `ms-e k-${e.kind}`); p.setAttribute("marker-end", `url(#ms-arrow-${e.kind})`);
    p.dataset.from = e.from; p.dataset.to = e.to;
    svg.append(p);
  }
  world.prepend(svg);
  const multi = !ms.project && new Set(g.nodes.map((n) => n.project)).size > 1;
  for (const n of g.nodes) {
    const p = L.pos.get(n.key), b = msButton(n, "ms-n");
    b.style.left = `${p.x}px`; b.style.top = `${p.y}px`; b.style.width = `${p.w}px`; b.style.height = `${p.h}px`;
    if (multi) b.querySelector(".ms-st").textContent += ` · ${shown(n.project)}`;
    b.onfocus = () => { msShow(p); msHot(n.key); };
    b.onpointerenter = () => msHot(n.key); b.onpointerleave = () => msHot(ms.sel);
    world.append(b);
  }
  if (!ms.fitted) { msFit(); ms.fitted = true; } else msApply();
}

// The links of one task stand out (hand-overs are faint otherwise: nearly every task has one).
function msHot(key) {
  for (const p of ms.root.querySelectorAll(".ms-e[data-to]")) p.classList.toggle("hot", !!key && (p.dataset.to === key || p.dataset.from === key));
}

// Phone: per lane the chains of tasks linked inside that lane, "A → B → C", wrapping (no sideways scroll).
function msList() {
  const g = ms.data, L = ms.lay, box = $("ms-list"); box.replaceChildren();
  const parent = new Map(g.nodes.map((n) => [n.key, n.key]));
  const find = (k) => (parent.get(k) === k ? k : find(parent.get(k)));
  for (const e of g.edges) {
    const a = msNode(e.from), b = msNode(e.to);
    if (a && b && a.lane === b.lane) parent.set(find(a.key), find(b.key));
  }
  const at = (k) => { const p = L.pos.get(k); return p.layer * 1e6 + p.y; };
  for (const l of L.lanes) {
    const mine = g.nodes.filter((n) => n.lane === l.id); if (!mine.length) continue;
    const chains = new Map();
    for (const n of mine) { const r = find(n.key); if (!chains.has(r)) chains.set(r, []); chains.get(r).push(n); }
    const sec = el("section", "ms-lane-list");
    sec.append(el("h3", null, `${l.name} (${mine.length})`));
    for (const c of [...chains.values()].sort((a, b) => Math.min(...a.map((n) => at(n.key))) - Math.min(...b.map((n) => at(n.key))))) {
      const row = el("div", "ms-chain");
      c.sort((a, b) => at(a.key) - at(b.key)).forEach((n, i) => {
        if (i) row.append(el("span", "ms-arrow", "→"));
        const from = g.edges.find((e) => e.to === n.key && (e.from.startsWith("lane:") ? e.from.slice(5) !== n.lane : msNode(e.from)?.lane !== n.lane));
        const b = msButton(n, "ms-c");
        const lane = (id) => (L.lanes.find((x) => x.id === id) || { name: id }).name;
        if (from) b.append(el("span", "ms-from", `from ${from.from.startsWith("lane:") ? lane(from.from.slice(5)) : `${msNode(from.from).id} (${lane(msNode(from.from).lane)})`}`));
        row.append(b);
      });
      sec.append(row);
    }
    box.append(sec);
  }
}

// Pan and zoom: wheel zooms at the pointer, a drag pans (also when it starts on a task; then it is no click), two
// fingers pinch. The world is one transformed layer, so nodes stay real buttons.
function msApply() { const v = ms.view; $("ms-world").style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`; }
function msZoom(k, cx, cy) {
  const v = ms.view, n = Math.min(2.5, Math.max(0.15, k));
  v.x = cx - (cx - v.x) * (n / v.k); v.y = cy - (cy - v.y) * (n / v.k); v.k = n; msApply();
}
function msFit() {
  const st = $("ms-stage"), L = ms.lay; if (!L || st.hidden) return;
  const W = st.clientWidth, H = st.clientHeight, k = Math.min(1.2, Math.max(0.15, Math.min((W - 32) / L.width, (H - 32) / L.height)));
  ms.view = { k, x: (W - L.width * k) / 2, y: Math.max(16, (H - L.height * k) / 2) }; msApply();
}
function msShow(p) { // keyboard focus on a task outside the screen brings it in
  const st = $("ms-stage"), v = ms.view, x = v.x + p.x * v.k, y = v.y + p.y * v.k;
  if (x < 0 || y < 0 || x + p.w * v.k > st.clientWidth || y + p.h * v.k > st.clientHeight) {
    v.x = st.clientWidth / 2 - (p.x + p.w / 2) * v.k; v.y = st.clientHeight / 2 - (p.y + p.h / 2) * v.k; msApply();
  }
}
function msPanZoom(st) {
  st.addEventListener("wheel", (e) => {
    e.preventDefault(); const r = st.getBoundingClientRect();
    msZoom(ms.view.k * Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0015)), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  st.addEventListener("pointerdown", (e) => {
    ms.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY });
    ms.dragged = false; ms.pinch = null;
  });
  st.addEventListener("pointermove", (e) => {
    const p = ms.pointers.get(e.pointerId); if (!p) return;
    const r = st.getBoundingClientRect();
    if (ms.pointers.size === 2) {
      const [a, b] = [...ms.pointers.values()]; p.x = e.clientX; p.y = e.clientY;
      const d = Math.hypot(a.x - b.x, a.y - b.y), mx = (a.x + b.x) / 2 - r.left, my = (a.y + b.y) / 2 - r.top;
      if (ms.pinch) { msZoom(ms.view.k * d / ms.pinch.d, mx, my); ms.view.x += mx - ms.pinch.mx; ms.view.y += my - ms.pinch.my; msApply(); }
      ms.pinch = { d, mx, my }; ms.dragged = true; return;
    }
    if (!ms.dragged && Math.hypot(e.clientX - p.sx, e.clientY - p.sy) < 5) return;
    if (!ms.dragged) { ms.dragged = true; st.setPointerCapture(e.pointerId); st.classList.add("panning"); }
    ms.view.x += e.clientX - p.x; ms.view.y += e.clientY - p.y; p.x = e.clientX; p.y = e.clientY; msApply();
  });
  const up = (e) => {
    ms.pointers.delete(e.pointerId); st.classList.remove("panning");
    if (ms.dragged) setTimeout(() => { ms.dragged = false; }, 0); // the click that ends a drag is no click on a task
  };
  st.addEventListener("pointerup", up); st.addEventListener("pointercancel", up);
}

// The side panel (a bottom sheet on a phone): the full line, the memo, where it runs, and the existing actions.
function msClosePanel() {
  $("ms-panel").hidden = true; ms.sel = ""; msHot("");
  for (const b of ms.root.querySelectorAll(".sel")) b.classList.remove("sel");
}
function msOpenPanel(key) {
  const n = msNode(key), box = $("ms-panel"); if (!n) return;
  ms.sel = key; box.hidden = false; box.replaceChildren(); msHot(key);
  for (const b of ms.root.querySelectorAll("[data-key]")) b.classList.toggle("sel", b.dataset.key === key);
  const head = el("div", "ms-panel-head");
  head.append(el("h3", null, n.id), el("span", `ms-chip s-${n.status}`, MS_STATUS[n.status]));
  const x = el("button", "ck-icon ms-x", "×"); x.type = "button"; x.title = "Close (Esc)"; x.setAttribute("aria-label", "Close"); x.onclick = msClosePanel;
  head.append(x);
  box.append(head, el("p", "ms-line", n.line));
  const dl = el("dl", "ms-facts");
  const fact = (k, v) => { if (v) dl.append(el("dt", null, k), el("dd", null, v)); };
  fact("Project", shown(n.project)); fact("Owner", n.owner); fact("From", n.from); fact("Start", n.start);
  fact("Due", n.due); fact("Where", n.where); fact("Waits", n.waiting_reason); fact("Written by", n.author);
  fact("Memo", n.path);
  if (n.status === "done" && n.since) fact("Handed back", new Date(n.since * 1000).toLocaleString());
  box.append(dl);
  const acts = el("div", "ms-actions");
  const memo = el("button", "ck-pill", "Open memo"); memo.type = "button";
  memo.onclick = () => { openMission(false); openCockpit(false); openNote(n.path, { find: [n.id.toLowerCase()] }); };
  acts.append(memo);
  if (n.can_approve) {
    const ok = el("button", "ck-pill ck-primary", "Approve"); ok.type = "button";
    ok.title = "Let this task start (it waits for the project owner's or an admin's approval)";
    ok.onclick = async () => {
      ok.disabled = true;
      try { await api("/ui/api/task/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project: n.project, id: n.id }) }); msLoad(); }
      catch (e) { ok.disabled = false; alert(e.message); }
    };
    acts.append(ok);
  }
  box.append(acts);
  if (["running", "claimed", "stalled"].includes(n.status)) { // the worker on its pc, as the Machines list shows it
    const w = el("p", "muted ms-worker", "Looking for its worker…"); box.append(w);
    api("/ui/api/machines").then((res) => {
      if (ms.sel !== key) return;
      const hit = res.machines.flatMap((m) => Object.values(m.agents).flatMap((a) => a.workers.map((wk) => ({ m, wk }))))
        .find(({ wk }) => wk.id === n.id && wk.project === n.project);
      if (!hit) { w.textContent = "No worker of yours reports this task."; return; }
      w.textContent = `Worker on ${hit.m.label}: ${hit.wk.status}. ${hit.wk.open || ""}`;
      const go = el("button", "ck-pill", "Machines"); go.type = "button"; go.title = "Stop, roll back or chat on that pc";
      go.onclick = () => { openMission(false); openCockpit(false); selectTab("tasks"); $("machines-head").scrollIntoView({ block: "start" }); };
      acts.append(go);
    }).catch(() => { w.textContent = ""; });
  }
}

document.querySelector('#ck-tools-menu [data-go="mission"]')?.addEventListener("click", (e) => {
  e.preventDefault(); $("ck-tools-menu").open = false; openMission(true);
});
