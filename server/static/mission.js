// Mission map (Tasks > Flow, #/tasks?view=flow): the task lines between agents as a graph, always laid out automatically.
// Nodes = tasks (real <button>s), one horizontal lane per owner (agent or person), edges = "start after <ID>",
// hand-overs, -cont continuations and -done results (SVG). Left to right by dependency: a small layered (Sugiyama)
// layout, missionLayout(), a pure function the UI smoke test checks. A phone gets the chains per lane as a list.
// The chat board (Chats > Board) at the end shares its pan and zoom.
// Uses app.js globals: $, el, api, agentProject, shown, openNote, store, ck*; go and shellRoute from shell.js.
"use strict";

const MS_STATUS = { open: "open", waiting: "waiting", approval: "needs approval", claimed: "claimed", running: "running",
  done: "done", stalled: "stalled" };
const MS_EDGE = { dep: "start after", handover: "hand-over", cont: "continued", done: "result back" };
const MS_SIZE = { w: 200, h: 74, gx: 70, gy: 12, lane: 148, pad: 14 };
const ms = { root: null, data: null, lay: null, all: false, project: "", view: { x: 0, y: 0, k: 1 }, fitted: false,
  timer: 0, sel: "", dragged: false, pointers: new Map(), pinch: null, world: () => $("ms-world") };

// The layout: layer = longest path over the task-to-task edges (a cycle, which only bad data makes, is cut where the
// walk meets itself); tasks without any edge stack in the first column of their lane (no order implied); then 4
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
  // a task without links can start without waiting on anything: all of them stack in the first column of their lane
  for (const n of sorted) if (lone(n.key)) put(n.key, n.lane, 0);
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
      <button type="button" class="btn small" id="ms-all" aria-pressed="false" title="Also results handed back more than a day ago">Show all</button>
      <span class="ck-gap"></span>
      <span class="ms-count muted" id="ms-count" aria-live="polite"></span>
      <button type="button" class="btn small" id="ms-fit" title="Fit the map to the screen (F)">Fit</button>
    </div>
    <div class="ms-legend" id="ms-legend"></div>
    <div class="ms-stage" id="ms-stage"><div class="ms-world" id="ms-world"></div></div>
    <div class="ms-list" id="ms-list"></div>
    <aside class="ms-panel" id="ms-panel" hidden aria-label="Task"></aside>`;
  $("tab-tasks").append(root); // the Flow half of the Tasks view; the page header holds the title and project filter
  ms.root = root;
  const legend = $("ms-legend");
  for (const [k, v] of Object.entries(MS_STATUS)) legend.append(el("span", `ms-chip s-${k}`, v));
  for (const [k, v] of Object.entries(MS_EDGE)) {
    const s = el("span", "ms-edge-key");
    s.innerHTML = `<svg viewBox="0 0 28 8" aria-hidden="true"><path class="ms-e k-${k}" d="M1 4H27"/></svg>`;
    s.append(v); legend.append(s);
  }
  $("ms-fit").onclick = msFit;
  $("ms-all").onclick = () => { ms.all = !ms.all; $("ms-all").setAttribute("aria-pressed", ms.all); ms.fitted = false; ms.data = null; msLoad(); };
  panInit($("ms-stage"), ms);
  window.addEventListener("keydown", (e) => { // capture: before the page's own keys
    if (root.hidden || e.ctrlKey || e.metaKey || e.altKey || /input|textarea|select/i.test(document.activeElement.tagName)) return;
    if (e.key === "Escape" && !$("ms-panel").hidden) { e.preventDefault(); e.stopPropagation(); msClosePanel(); }
    else if (e.key.toLowerCase() === "f") { e.preventDefault(); e.stopPropagation(); msFit(); }
  }, true);
  matchMedia("(max-width: 640px)").addEventListener("change", () => msRender());
}

function openMission(on) {
  if (!ms.root) msBuild();
  ms.root.hidden = !on; clearTimeout(ms.timer);
  if (!on) return;
  ms.project = agentProject; ms.fitted = false; ms.data = null;
  msLoad();
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

const msNarrow = () => matchMedia("(max-width: 699.98px)").matches;
const msNode = (k) => ms.data.nodes.find((n) => n.key === k);

function msRender() {
  const g = ms.data; if (!g) return;
  const open = g.nodes.filter((n) => n.status !== "done").length;
  $("ms-count").textContent = `${open} open · ${g.nodes.length - open} done${ms.all ? "" : ` (last ${g.done_hours} h)`}`;
  ms.lay = missionLayout(g);
  $("ms-stage").hidden = msNarrow(); $("ms-list").hidden = !msNarrow();
  if (!g.nodes.length) {
    const all = el("button", "btn small ck-elsewhere", "Show all projects"); all.type = "button"; all.onclick = () => switchProject(ALL);
    $("ms-world").replaceChildren(); $("ms-list").replaceChildren(el("p", "muted ms-empty", ms.project ? `No task lines in ${shown(ms.project)}.` : "No task lines here."), ...(ms.project ? [all] : []));
    $("ms-stage").hidden = true; $("ms-list").hidden = false; // the list holds the empty line: also on a desktop
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

// The links of one task stand out; its hand-overs show only then (nearly every task has one: always drawn they bundle).
function msHot(key) {
  for (const p of ms.root.querySelectorAll(".ms-e[data-to]")) p.classList.toggle("hot", !!key && (p.dataset.to === key || p.dataset.from === key));
}

// Phone: per lane the chains of tasks linked inside that lane, one full-width card under the other (no sideways scroll).
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
        if (i) row.append(el("span", "ms-arrow", "↓"));
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

// Pan and zoom, shared by the mission map and the chat board: wheel zooms at the pointer (a touchpad pinch arrives as a
// wheel with ctrlKey), a drag pans, two fingers pinch. The world is one transformed layer, so nodes stay real
// elements. `s` holds view {x, y, k}, pointers, dragged, pinch and apply(); `skip(e)` leaves a pointer to its target.
function panApply(s) { const v = s.view; s.world().style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`; s.moved?.(); }
function panZoom(s, k, cx, cy) {
  const v = s.view, n = Math.min(2.5, Math.max(0.15, k));
  v.x = cx - (cx - v.x) * (n / v.k); v.y = cy - (cy - v.y) * (n / v.k); v.k = n; panApply(s);
}
// The view that shows a box {x, y, w, h} whole, centred, at most at scale `max`.
function panFit(s, st, b, max = 1.2) {
  const W = st.clientWidth, H = st.clientHeight, k = Math.min(max, Math.max(0.15, Math.min((W - 32) / b.w, (H - 32) / b.h)));
  s.view = { k, x: (W - b.w * k) / 2 - b.x * k, y: Math.max(16, (H - b.h * k) / 2) - b.y * k }; panApply(s);
}
function panShow(s, st, p) { // keyboard focus on something outside the screen brings it in
  const v = s.view, x = v.x + p.x * v.k, y = v.y + p.y * v.k;
  if (x < 0 || y < 0 || x + p.w * v.k > st.clientWidth || y + p.h * v.k > st.clientHeight) {
    v.x = st.clientWidth / 2 - (p.x + p.w / 2) * v.k; v.y = st.clientHeight / 2 - (p.y + p.h / 2) * v.k; panApply(s);
  }
}
function panInit(st, s, skip = () => false) {
  st.addEventListener("wheel", (e) => {
    if (skip(e, "wheel")) return;
    e.preventDefault(); const r = st.getBoundingClientRect();
    panZoom(s, s.view.k * Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : e.ctrlKey ? 0.01 : 0.0015)), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  st.addEventListener("pointerdown", (e) => {
    if (skip(e)) return;
    s.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY });
    s.dragged = false; s.pinch = null;
  });
  st.addEventListener("pointermove", (e) => {
    const p = s.pointers.get(e.pointerId); if (!p) return;
    const r = st.getBoundingClientRect();
    if (s.pointers.size === 2) {
      const [a, b] = [...s.pointers.values()]; p.x = e.clientX; p.y = e.clientY;
      const d = Math.hypot(a.x - b.x, a.y - b.y), mx = (a.x + b.x) / 2 - r.left, my = (a.y + b.y) / 2 - r.top;
      if (s.pinch) { panZoom(s, s.view.k * d / s.pinch.d, mx, my); s.view.x += mx - s.pinch.mx; s.view.y += my - s.pinch.my; panApply(s); }
      s.pinch = { d, mx, my }; s.dragged = true; return;
    }
    if (!s.dragged && Math.hypot(e.clientX - p.sx, e.clientY - p.sy) < 5) return;
    if (!s.dragged) { s.dragged = true; st.setPointerCapture(e.pointerId); st.classList.add("panning"); }
    s.view.x += e.clientX - p.x; s.view.y += e.clientY - p.y; p.x = e.clientX; p.y = e.clientY; panApply(s);
  });
  const up = (e) => {
    s.pointers.delete(e.pointerId); st.classList.remove("panning");
    if (s.dragged) setTimeout(() => { s.dragged = false; }, 0); // the click that ends a drag is no click on a task
  };
  st.addEventListener("pointerup", up); st.addEventListener("pointercancel", up);
}
function msApply() { panApply(ms); }
function msFit() {
  const st = $("ms-stage"), L = ms.lay; if (!L || st.hidden) return;
  panFit(ms, st, { x: 0, y: 0, w: L.width, h: L.height });
}
function msShow(p) { panShow(ms, $("ms-stage"), p); }

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
  const x = el("button", "btn icon small quiet ms-x", "×"); x.type = "button"; x.title = "Close (Esc)"; x.setAttribute("aria-label", "Close"); x.onclick = msClosePanel;
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
  const memo = el("button", "btn small", "Open memo"); memo.type = "button";
  memo.onclick = () => openNote(n.path, { find: [n.id.toLowerCase()] });
  acts.append(memo);
  if (n.can_approve) {
    const ok = el("button", "btn small primary", "Approve"); ok.type = "button";
    ok.title = "Let this task start (it waits for the project owner's or an admin's approval)";
    ok.onclick = async () => {
      ok.disabled = true;
      try { await api("/ui/api/task/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project: n.project, id: n.id }) }); msLoad(); }
      catch (e) { ok.disabled = false; toast(e.message); }
    };
    acts.append(ok);
  }
  box.append(acts);
  { // the worker on its pc, as the Machines list shows it (a running or ended one: Follow reads its transcript)
    const busy = ["running", "claimed", "stalled"].includes(n.status), w = el("p", "muted ms-worker", busy ? "Looking for its worker…" : ""); box.append(w);
    api("/ui/api/machines").then((res) => {
      if (ms.sel !== key) return;
      const hit = res.machines.flatMap((m) => Object.values(m.agents).flatMap((a) => a.workers.map((wk) => ({ m, wk }))))
        .find(({ wk }) => wk.id === n.id && wk.project === n.project);
      if (!hit) { w.textContent = busy ? "No worker of yours reports this task." : ""; return; }
      w.textContent = `Worker on ${hit.m.label}: ${hit.wk.status}. ${hit.wk.open || ""}`;
      if (hit.m.own) acts.append(ckFollowButton(hit.wk));
      const pc = el("button", "btn small", "Machines"); pc.type = "button"; pc.title = "Stop, roll back or chat on that pc";
      pc.onclick = () => go("#/machines");
      acts.append(pc);
    }).catch(() => { w.textContent = ""; });
  }
}


// ---------------------------------------------------------------- Chat board (Chats > Board, #/chats?view=board)
// Desktop only (1000 px and up; a phone keeps the list). One card per chat on an endless canvas, placed automatically
// in a grid inside one frame per project (folding a frame hides its cards). A card the user drags keeps its place
// (localStorage per chat id, "Tidy up" forgets them). Zoomed in a card is the chat itself (ckCard: log, approvals,
// composer); zoomed out a summary (title, state, last line). Only cards on screen and zoomed in are polled (ckTick).
const BD = { w: 380, h: 330, gap: 20, pad: 20, head: 46, fgap: 56, far: 0.55 };
const bd = { view: { x: 0, y: 0, k: 1 }, pointers: new Map(), dragged: false, pinch: null, world: () => $("bd-world"),
  lay: null, fitted: false, wraps: new Map(), frames: new Map(), moved: () => bdMoved() };
const bdWide = matchMedia("(min-width: 1000px)");
bdWide.addEventListener("change", () => ckLayout());
const bdOn = () => shellRoute.view === "chats" && !shellRoute.arg && shellRoute.q.get("view") === "board" && bdWide.matches;
function bdSaved() {
  try {
    const b = JSON.parse(store.get("kn_board") || "{}") || {}; // a damaged entry must not break every render
    return { pos: b.pos && typeof b.pos === "object" && !Array.isArray(b.pos) ? b.pos : {}, shut: Array.isArray(b.shut) ? b.shut : [] };
  } catch { return { pos: {}, shut: [] }; }
}
const bdSave = (b) => store.set("kn_board", JSON.stringify(b));

// The layout, pure (ui_smoke checks it): projects left to right in name order, each a frame with its chats in a
// near-square grid (in the order given), dragged chats where they were put (the frame grows around them). Returns
// {frames: [{project, x, y, w, h, n, shut}], pos: Map(chat id -> {x, y, pinned}), bounds: {x, y, w, h}}.
// shortcut: frames sit in one row; many projects make it wide (Fit zooms out), wrap them into rows if that bites.
function boardLayout(chats, pinned = {}, shut = [], s = BD) {
  const frames = [], pos = new Map();
  const projects = [...new Set(chats.map((c) => c.project || ""))].sort((a, b) => (a || "￿").localeCompare(b || "￿"));
  let x0 = 0;
  for (const p of projects) {
    const mine = chats.filter((c) => (c.project || "") === p), auto = mine.filter((c) => !pinned[c.id]), closed = shut.includes(p);
    const cols = Math.max(1, Math.ceil(Math.sqrt(auto.length))), rows = Math.ceil(auto.length / cols);
    const f = { project: p, x: x0, y: 0, w: s.pad * 2 + cols * s.w + (cols - 1) * s.gap, h: closed || !rows ? s.head : s.head + rows * (s.h + s.gap) - s.gap + s.pad, n: mine.length, shut: closed };
    auto.forEach((c, i) => pos.set(c.id, { x: x0 + s.pad + (i % cols) * (s.w + s.gap), y: s.head + Math.floor(i / cols) * (s.h + s.gap), pinned: false }));
    x0 += f.w + s.fgap; // the next frame follows this grid, not where dragged cards went
    for (const c of mine) {
      if (!pinned[c.id]) continue;
      const [x, y] = pinned[c.id]; pos.set(c.id, { x, y, pinned: true });
      if (closed) continue;
      const r = Math.max(f.x + f.w, x + s.w + s.pad), b = Math.max(f.y + f.h, y + s.h + s.pad);
      f.x = Math.min(f.x, x - s.pad); f.y = Math.min(f.y, y - s.head); f.w = r - f.x; f.h = b - f.y;
    }
    frames.push(f);
  }
  const x = Math.min(0, ...frames.map((f) => f.x)), y = Math.min(0, ...frames.map((f) => f.y));
  const r = Math.max(x + 1, ...frames.map((f) => f.x + f.w)), b = Math.max(y + 1, ...frames.map((f) => f.y + f.h));
  return { frames, pos, bounds: { x, y, w: r - x, h: b - y } };
}

function bdBuild() {
  const root = el("section", "bd"); root.id = "bd"; root.hidden = true; root.setAttribute("aria-label", "Chat board");
  root.innerHTML = `<div class="bd-stage" id="bd-stage" tabindex="0" aria-label="Chat board. Drag or use the arrow keys to move around, the wheel or + and − to zoom, F fits all, Tab goes through the chats"><div class="bd-world" id="bd-world"></div></div>
    <div class="bd-tools"><button type="button" class="btn small" id="bd-tidy" title="Put the cards you dragged back into their project's grid">Tidy up</button><button type="button" class="btn small" id="bd-fit" title="Show all chats (F)">Fit</button></div>
    <div class="bd-mini" id="bd-mini" title="Overview: click to go there" aria-hidden="true"><div class="bd-mini-in" id="bd-mini-in"></div><div class="bd-mini-view" id="bd-mini-view"></div></div>`;
  $("cockpit").append(root);
  const st = $("bd-stage");
  // cards handle their own pointer (drag by the header) and wheel (a log scrolls; a pinch still zooms)
  panInit(st, bd, (e, kind) => !!e.target.closest(".bd-card") && (kind !== "wheel" || (!e.ctrlKey && !$("bd").classList.contains("far") && !!e.target.closest(".ck-log, .ck-asks, textarea"))));
  $("bd-fit").onclick = bdFit;
  $("bd-tidy").onclick = () => { const b = bdSaved(); b.pos = {}; bdSave(b); bdRender(); bdFit(); };
  st.addEventListener("keydown", (e) => { // arrows pan, + and − zoom, while the canvas itself has the focus
    if (e.target !== st || e.ctrlKey || e.metaKey || e.altKey) return;
    const d = { ArrowLeft: [80, 0], ArrowRight: [-80, 0], ArrowUp: [0, 80], ArrowDown: [0, -80] }[e.key];
    if (d) { e.preventDefault(); bd.view.x += d[0]; bd.view.y += d[1]; panApply(bd); }
    else if (e.key === "+" || e.key === "=" || e.key === "-" || e.key === "−") { e.preventDefault(); panZoom(bd, bd.view.k * (e.key === "-" || e.key === "−" ? 0.8 : 1.25), st.clientWidth / 2, st.clientHeight / 2); }
  });
  root.addEventListener("keydown", (e) => { // Esc leaves a card (also its text box) for the canvas
    if (e.key === "Escape" && !e.isComposing && document.activeElement.closest(".bd-card")) { e.preventDefault(); e.stopPropagation(); st.focus(); }
  });
  window.addEventListener("keydown", (e) => { // F fits, like the mission map (capture: before the page's own keys)
    if (root.hidden || e.ctrlKey || e.metaKey || e.altKey || e.key.toLowerCase() !== "f" || /input|textarea|select/i.test(document.activeElement.tagName) || document.querySelector("dialog[open]")) return;
    e.preventDefault(); e.stopPropagation(); bdFit();
  }, true);
  // Pointer-only on purpose: the keyboard pans with the arrows and fits with F on the stage, so the minimap adds
  // nothing for it (aria-hidden, not focusable).
  const mini = $("bd-mini"), jump = (e) => { // the overview: a click or drag centres the view there
    const L = bd.lay; if (!L) return;
    const r = mini.getBoundingClientRect(), m = bd.mini, x = L.bounds.x + (e.clientX - r.left - m.ox) / m.k, y = L.bounds.y + (e.clientY - r.top - m.oy) / m.k;
    bd.view.x = st.clientWidth / 2 - x * bd.view.k; bd.view.y = st.clientHeight / 2 - y * bd.view.k; panApply(bd);
  };
  mini.addEventListener("pointerdown", (e) => { mini.setPointerCapture(e.pointerId); jump(e); mini.onpointermove = jump; });
  mini.addEventListener("pointerup", () => { mini.onpointermove = null; });
}

// Shown or hidden by the route (ckLayout); the cockpit hides the list while it shows.
function bdShow() {
  const on = bdOn();
  for (const b of $("chats-mode").querySelectorAll("button")) b.setAttribute("aria-pressed", String((b.dataset.mode === "board") === (shellRoute.q.get("view") === "board")));
  if (!on && !$("bd")) return;
  if (!$("bd")) bdBuild();
  $("bd").hidden = !on; $("cockpit").classList.toggle("board", on);
  if (!on) return;
  bdRender();
  if (!bd.fitted && bd.lay.frames.length) { bdFit(); bd.fitted = true; } // once the chats are in
}

// The chats in board order: per project the ones waiting on you first, then the running ones, then the newest.
function bdChats() {
  const rank = { needs: 0, running: 1, idle: 2 };
  return ck.chats.filter(ckScope).map((c) => ({ c, st: ckState(c) })).filter((x) => x.st !== "closed") // closed: under the list's Closed only
    .sort((a, b) => rank[a.st] - rank[b.st] || (b.c.updated || 0) - (a.c.updated || 0));
}
// What a zoomed-out card says: the last thing in its log once loaded, else the board's line for it ("replied: …").
function bdLine(c, card) {
  const i = (ck.board || []).find((x) => x.kind === "chat" && x.machine === c.machine && x.id === c.id);
  return card.log.lastElementChild?.textContent.trim().slice(-200) || (i?.line !== "read" && i?.line) || (c.title ? "" : "No message yet.");
}

// Frames and cards are kept per project and per chat: a re-render (every summary) moves them, never rebuilds them, so
// the focus and a half-typed message stay.
function bdRender() {
  if (!$("bd") || $("bd").hidden) return;
  const rows = bdChats(), saved = bdSaved(), world = $("bd-world");
  const L = bd.lay = boardLayout(rows.map(({ c }) => ({ id: c.id, project: c.project })), saved.pos, saved.shut);
  const keep = new Set();
  for (const f of L.frames) {
    let box = bd.frames.get(f.project);
    if (!box) {
      box = el("section", "bd-frame"); const t = el("button", "bd-frame-title"); t.type = "button";
      t.append(el("span", "bd-chev", "▾"), el("strong"), el("span", "ck-count"));
      t.onclick = () => { const b = bdSaved(); b.shut = b.shut.includes(f.project) ? b.shut.filter((x) => x !== f.project) : [...b.shut, f.project]; bdSave(b); bdRender(); };
      box.append(t); bd.frames.set(f.project, box);
    }
    const t = box.firstChild, name = f.project ? shown(f.project) : "no project";
    t.children[1].textContent = name; t.children[2].textContent = String(f.n);
    t.setAttribute("aria-expanded", String(!f.shut)); t.title = f.shut ? `Show the chats of ${name}` : `Fold ${name} away`;
    box.setAttribute("aria-label", `${name}: ${f.n} chat${f.n === 1 ? "" : "s"}`);
    box.classList.toggle("shut", f.shut);
    Object.assign(box.style, { left: `${f.x}px`, top: `${f.y}px`, width: `${f.w}px`, height: `${f.h}px` });
    keep.add(box);
  }
  for (const [p, box] of bd.frames) if (!keep.has(box)) { box.remove(); bd.frames.delete(p); }
  const order = [];
  for (const f of L.frames) {
    order.push(bd.frames.get(f.project));
    for (const { c, st } of rows.filter(({ c }) => (c.project || "") === f.project)) {
      const card = ckCard(c), k = ckKey(c), p = L.pos.get(c.id);
      let w = bd.wraps.get(k);
      if (!w) {
        w = el("article", "bd-card"); w.tabIndex = 0; w.dataset.id = c.id;
        w.append(el("div", "bd-sum")); bdDrag(w, c.id); bd.wraps.set(k, w);
        w.addEventListener("focusin", () => { const q = bd.lay?.pos.get(c.id); if (q) panShow(bd, $("bd-stage"), { ...q, w: BD.w, h: BD.h }); });
        w.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === w) { e.preventDefault(); ckShow(card.meta); } });
      }
      if (card.root.parentNode !== w) w.append(card.root); // back from the full chat (ckLayout moved it there)
      w.className = `bd-card ${st}${p.pinned ? " pinned" : ""}${w.classList.contains("dragging") ? " dragging" : ""}`; w.hidden = saved.shut.includes(f.project);
      w.setAttribute("aria-label", `${c.title || "New chat"}: ${CK_STATE[st]}, ${AGENT_NAME[c.agent] || c.agent} on ${c.label}, ${ckShort(c.folder)}. Enter opens the full chat, Esc goes back to the board`);
      if (!w.classList.contains("dragging")) Object.assign(w.style, { left: `${p.x}px`, top: `${p.y}px`, width: `${BD.w}px`, height: `${BD.h}px` });
      const sum = w.firstChild, top = el("div", "bd-sum-top");
      top.append(el("span", "ck-dot"), el("span", "ck-mark", AGENT_MARK[c.agent] || "?"), el("span", "bd-sum-state", CK_STATE[st]));
      sum.replaceChildren(top, el("strong", "bd-sum-title", c.title || "New chat"), el("p", "bd-sum-line", bdLine(c, card)),
        el("small", null, `${AGENT_NAME[c.agent] || c.agent} · ${c.label} · ${ckShort(c.folder)} · ${ago(c.updated)}`));
      order.push(w);
    }
  }
  for (const [k, w] of bd.wraps) if (!ck.cards.has(k) || !order.includes(w)) { w.remove(); bd.wraps.delete(k); }
  // DOM order = layout order, so Tab walks project by project; only moved nodes are touched (a move drops the focus)
  order.forEach((n, i) => { if (world.children[i] !== n) world.insertBefore(n, world.children[i] || null); });
  if (!rows.length) world.replaceChildren(el("p", "bd-none muted", ck.chats.length ? "No chats in this project." : "No chats yet: start one with + New chat."));
  else world.querySelector(".bd-none")?.remove();
  bdMini();
}

// Drag a card by its header (zoomed out: anywhere on it); where it lands is kept for that chat.
function bdDrag(w, id) {
  w.addEventListener("pointerdown", (e) => {
    if (e.button || !(e.target.closest(".ck-head") || $("bd").classList.contains("far")) || e.target.closest("button, a, textarea, select")) return;
    const p = bd.lay.pos.get(id), s = { x: e.clientX, y: e.clientY, px: p.x, py: p.y, on: false };
    const move = (ev) => {
      if (!s.on && Math.hypot(ev.clientX - s.x, ev.clientY - s.y) < 4) return;
      if (!s.on) { s.on = true; w.setPointerCapture(ev.pointerId); w.classList.add("dragging"); }
      p.x = s.px + (ev.clientX - s.x) / bd.view.k; p.y = s.py + (ev.clientY - s.y) / bd.view.k;
      w.style.left = `${p.x}px`; w.style.top = `${p.y}px`;
    };
    const up = () => {
      w.removeEventListener("pointermove", move); w.removeEventListener("pointerup", up); w.removeEventListener("pointercancel", up);
      if (!s.on) return;
      w.classList.remove("dragging");
      const b = bdSaved(); b.pos[id] = [Math.round(p.x), Math.round(p.y)]; bdSave(b); bdRender();
    };
    w.addEventListener("pointermove", move); w.addEventListener("pointerup", up); w.addEventListener("pointercancel", up);
  });
}

function bdFit() { if (bd.lay && !$("bd").hidden) panFit(bd, $("bd-stage"), bd.lay.bounds, 1); }
// After every pan or zoom: zoomed-out cards show their summary, the overview follows, and cards that came on screen
// load their log now instead of at the next poll.
let bdLoadTimer = 0;
function bdMoved() {
  $("bd").classList.toggle("far", bd.view.k < BD.far);
  bdMiniView();
  clearTimeout(bdLoadTimer);
  bdLoadTimer = setTimeout(() => { for (const c of bdLive()) if (!c.loaded) { c.loaded = true; ckPoll(c); } }, 250);
}
// The cards ckTick polls: on screen, zoomed in, in an open frame.
function bdLive() {
  if (!$("bd") || $("bd").hidden || bd.view.k < BD.far) return [];
  const st = $("bd-stage"), v = bd.view, W = st.clientWidth, H = st.clientHeight;
  return [...bd.wraps].filter(([, w]) => {
    if (w.hidden) return false;
    const x = v.x + parseFloat(w.style.left) * v.k, y = v.y + parseFloat(w.style.top) * v.k;
    return x < W && y < H && x + BD.w * v.k > 0 && y + BD.h * v.k > 0;
  }).map(([k]) => ck.cards.get(k)).filter(Boolean);
}

// The overview in the corner: frames and cards as small boxes, the screen as a frame.
function bdMini() {
  const L = bd.lay, box = $("bd-mini-in"), W = 180, H = 120;
  const k = Math.min(W / L.bounds.w, H / L.bounds.h);
  bd.mini = { k, ox: (W - L.bounds.w * k) / 2, oy: (H - L.bounds.h * k) / 2 };
  const at = (x, y, w, h, cls) => { const d = el("div", cls); Object.assign(d.style, { left: `${bd.mini.ox + (x - L.bounds.x) * k}px`, top: `${bd.mini.oy + (y - L.bounds.y) * k}px`, width: `${Math.max(2, w * k)}px`, height: `${Math.max(2, h * k)}px` }); return d; };
  box.replaceChildren(...L.frames.map((f) => at(f.x, f.y, f.w, f.h, "bd-mini-frame")),
    ...[...bd.wraps.values()].filter((w) => !w.hidden).map((w) => at(parseFloat(w.style.left), parseFloat(w.style.top), BD.w, BD.h, `bd-mini-card ${w.classList.contains("needs") ? "needs" : w.classList.contains("running") ? "running" : ""}`)));
  bdMiniView();
}
function bdMiniView() {
  const L = bd.lay, m = bd.mini, st = $("bd-stage"), v = bd.view, r = $("bd-mini-view"); if (!L || !m) return;
  const x = (-v.x / v.k - L.bounds.x) * m.k + m.ox, y = (-v.y / v.k - L.bounds.y) * m.k + m.oy;
  Object.assign(r.style, { left: `${x}px`, top: `${y}px`, width: `${st.clientWidth / v.k * m.k}px`, height: `${st.clientHeight / v.k * m.k}px` });
}
for (const b of $("chats-mode").querySelectorAll("button")) b.onclick = () => go(b.dataset.mode === "board" ? "#/chats?view=board" : "#/chats");
