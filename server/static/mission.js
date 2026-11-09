// Mission map (Tasks > Flow, #/tasks?view=flow): the task lines between agents as a graph, always laid out automatically.
// Nodes = tasks (real <button>s), one horizontal lane per owner (agent or person), edges = "start after <ID>",
// hand-overs, -cont continuations and -done results (SVG). Left to right by dependency: a small layered (Sugiyama)
// layout, missionLayout(), a pure function the UI smoke test checks. A phone gets the chains per lane as a list.
// The chat grid (Agents, #/chats) lives at the end; it has no pan or zoom.
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
      <button type="button" class="btn small" id="ms-all" aria-pressed="false" title="Also results handed back more than a day ago">Older results</button>
      <span class="ck-gap"></span>
      <span class="ms-count muted" id="ms-count" aria-live="polite"></span>
      <button type="button" class="btn small" id="ms-fit" data-shortcut="fit" title="Fit the map to the screen ({key})">Fit</button>
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
  keyLabels(root);
  $("ms-fit").onclick = msFit;
  $("ms-all").onclick = () => { ms.all = !ms.all; $("ms-all").setAttribute("aria-pressed", ms.all); ms.fitted = false; ms.data = null; msLoad(); };
  panInit($("ms-stage"), ms);
  window.addEventListener("keydown", (e) => { // capture: before the page's own keys
    if (root.hidden || /input|textarea|select/i.test(document.activeElement.tagName)) return;
    if (e.key === "Escape" && !e.altKey && !e.ctrlKey && !e.metaKey && !$("ms-panel").hidden) { e.preventDefault(); e.stopPropagation(); msClosePanel(); }
    else if (keyHit(e, "fit")) { e.preventDefault(); e.stopPropagation(); msFit(); }
  }, true);
  matchMedia("(max-width: 640px)").addEventListener("change", () => msRender());
}

function openMission(on) {
  if (!ms.root) msBuild();
  ms.root.hidden = !on; clearTimeout(ms.timer);
  if (!on) return;
  ms.project = agentProject; ms.fitted = false; ms.data = null; $("ms-count").textContent = "Loading…";
  msLoad();
}

async function msLoad() {
  clearTimeout(ms.timer);
  if (ms.root.hidden) return;
  try {
    const q = new URLSearchParams({ project: ms.project, ...(ms.all ? { all: "1" } : {}) });
    const g = await api(`/ui/api/mission?${q}`);
    if (JSON.stringify(g) !== JSON.stringify(ms.data)) { ms.data = g; msRender(); } // a re-render would drop the focus
  } catch (e) {
    $("ms-count").textContent = e.message;
    if (!ms.data) { $("ms-stage").hidden = true; $("ms-list").hidden = false; $("ms-list").replaceChildren(errorState(`The flow did not load: ${e.message}`, msLoad)); }
  }
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
  $("ms-legend").hidden = !g.nodes.length; // a key to an empty graph explains nothing
  $("ms-all").hidden = !g.nodes.length && !ms.all; // one "show" on an empty flow: the other projects
  if (!g.nodes.length) {
    $("ms-world").replaceChildren();
    $("ms-list").replaceChildren(emptyState(`No task lines${ms.project ? ` in ${shown(ms.project)}` : ""}: the flow of work agents hand each other shows here.`,
      ...(ms.project ? [actionButton("Show all projects", () => switchProject(ALL))] : [])));
    $("ms-list").firstChild.classList.add("ms-empty"); // the smoke test waits for it
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

// Pan and zoom of the mission map: wheel zooms at the pointer (a touchpad pinch arrives as a
// wheel with ctrlKey), a drag pans, two fingers pinch. The world is one transformed layer, so nodes stay real
// elements. `s` holds view {x, y, k}, pointers, dragged, pinch.
function panApply(s) { const v = s.view; s.world().style.transform = `translate(${v.x}px, ${v.y}px) scale(${v.k})`; }
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
function panInit(st, s) {
  st.addEventListener("wheel", (e) => {
    e.preventDefault(); const r = st.getBoundingClientRect();
    panZoom(s, s.view.k * Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : e.ctrlKey ? 0.01 : 0.0015)), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  st.addEventListener("pointerdown", (e) => {
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
    if (n.approve_warning) ok.title += `\n\n${n.approve_warning}`;
    ok.onclick = async () => {
      if (n.approve_warning && !confirm(`${n.approve_warning}\n\nApprove ${n.id} anyway?`)) return;
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


// ---------------------------------------------------------------- Chat grid (#/chats on a desktop)
// 1000 px and up; narrower keeps the list. One card per chat, tiled to fill the screen like terminal panes, inside one
// foldable frame per project, the one last active on top (folding is kept in localStorage; while you look at the grid, by bdHold), or Settings: all in one. A card is the chat itself (ckCard). Only cards on screen
// are polled (ckTick). ?view=grid is an older link to the same view; ?view=list is the attention list (#/inbox links).
const BG = { minW: 480, minH: 380, gap: 16, pad: 16, head: 46 };
const bd = { lay: [], wraps: new Map(), frames: new Map(), held: new Map(), part: (() => { try { return Number(sessionStorage.getItem("kn_part")) || 0; } catch { return 0; } })() };
const bdWide = matchMedia("(min-width: 1000px)");
bdWide.addEventListener("change", () => ckLayout());
const bdOn = () => shellRoute.view === "chats" && !shellRoute.arg && shellRoute.q.get("view") !== "list" && bdWide.matches;
// Grid columns, pure (ui_smoke checks it): as many as fit at minW, then balanced so rows are even (4 -> 2x2, 5 -> 3+2).
function gridCols(n, width, s = BG) {
  const max = Math.max(1, Math.floor((width - 2 * s.pad + s.gap) / (s.minW + s.gap)));
  return n <= 1 ? 1 : Math.ceil(n / Math.ceil(n / Math.min(n, max)));
}
// Grid sizes from the stage: columns from the biggest open project, rows share the height (two at most on screen).
function bdGridSize() {
  const st = $("bd-stage"), open = bd.lay.filter((f) => !f.shut), one = bd.lay.length < 2 && !bd.lay[0]?.shut; // a folded project keeps its title: else nothing unfolds it
  const cols = gridCols(Math.max(1, ...open.map((f) => f.n)), st.clientWidth);
  const rows = open.reduce((a, f) => a + Math.ceil(f.n / cols), 0), fit = Math.max(1, Math.min(rows, 2));
  const heads = one ? 0 : bd.lay.length * BG.head;
  const h = Math.max(BG.minH, Math.floor((st.clientHeight - 2 * BG.pad - (fit - 1) * BG.gap - (rows <= 2 ? heads : 0)) / fit));
  $("bd").classList.toggle("one", one);
  $("bd").style.setProperty("--bg-cols", String(cols)); $("bd").style.setProperty("--bg-row", `${h}px`);
}
function bdShut() {
  try {
    const b = JSON.parse(store.get("kn_board") || "{}") || {}; // a damaged entry must not break every render
    return Array.isArray(b.shut) ? b.shut : [];
  } catch { return []; }
}
// The frames, pure (ui_smoke checks it): chats come latest first (bdChats), so a project's newest chat puts it in its
// place and the project last active is on top. Settings can put all chats in one frame, which has no heading to fold.
function bdProjects(chats, shut = [], together = false) {
  if (together) return chats.length ? [{ project: "", n: chats.length, shut: false }] : [];
  return [...new Set(chats.map((c) => c.project || ""))].map((p) => ({ project: p, n: chats.filter((c) => (c.project || "") === p).length, shut: shut.includes(p) }));
}

// Parts, for a grid over several windows: a chat belongs to one part (kept per browser, so all its windows agree; none =
// part 1) and a window shows one (per tab; 0 = all). A part is not a slice of the sorted list: then a finished chat would
// jump to another window, and each window's own hold would show a chat twice or not at all. So sorting stays inside a part.
function bdParts() { try { return JSON.parse(store.get("kn_parts") || "{}") || {}; } catch { return {}; } } // a damaged entry must not break every render
const bdPartOf = (c, parts = bdParts()) => parts[ckKey(c)] || 1;
function bdPartShow(n) {
  const draft = ck.cards.get(ckKey(CK_DRAFT));
  if (draft && !draft.starting && !draft.input.value.trim() && !draft.files.length) ckDrop(ckKey(CK_DRAFT)); // an empty new chat stays behind; one with text goes along
  bd.part = n;
  try { sessionStorage.setItem("kn_part", String(n)); } catch { /* blocked: this tab still shows it */ }
  bdSettle(); bdRender(); // another part's order is news to nobody here: no pill for it
  $("bd-stage").scrollTop = 0; $("bd-stage").focus({ preventScroll: true }); bdLoadSoon();
  if (n && !bdChats().length) $("ck-new").onclick(); // an empty part is there to start something
}
function bdPartMove(key, n) {
  const known = new Set(ck.chats.map(ckKey)), parts = Object.fromEntries(Object.entries(bdParts()).filter(([k]) => known.has(k) && k !== key));
  if (n > 1) parts[key] = n;
  store.set("kn_parts", JSON.stringify(parts));
}
addEventListener("storage", (e) => { if (e.key === "kn_parts") bdRender(); }); // moved in another window
// Alt+0-9 show a part; Alt+Shift+1-9 moves the card you are in or point at there (Settings can change both).
function bdPartKey(e) {
  const show = keyHit(e, "digits");
  if (!show && !keyHit(e, "digits_move")) return false;
  const n = Number(keyCombo(e).at(-1));
  if (show) { if (!n) switchProject(ALL); bdPartShow(n); return true; } // 0 is everything, as it is off the grid
  const w = document.activeElement?.closest?.(".bd-card") || document.querySelector(".bd-card:hover");
  const key = [...bd.wraps].find(([, x]) => x === w)?.[0];
  if (!n || !key || ck.cards.get(key)?.meta.draft) return false;
  bdPartMove(key, n); bdRender();
  if (!w.isConnected) $("bd-stage").focus({ preventScroll: true });
  return true;
}
// The card keys go to: the one you are in, else the last one you were in, else the first.
function bdCardNow() {
  const world = $("bd-world");
  return document.activeElement?.closest?.(".bd-card") || (bd.last && world?.querySelector(`.bd-card[data-id="${bd.last}"]:not([hidden])`))
    || world?.querySelector(".bd-card:not([hidden]):has(.ck-form:not([hidden]) textarea:not(:disabled))");
}
// Arrows: to the box of the nearest card that way. Out of no card they first land on the card keys would go to.
function bdArrow(e) {
  const [dx, dy] = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key] || [];
  if (dx === undefined) return false;
  const mid = (w) => { const r = w.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; };
  let to = bdCardNow();
  if (to && document.activeElement.closest(".bd-card")) {
    const [x, y] = mid(to);
    const away = [...bd.wraps.values()].filter((w) => !w.hidden && w !== to).map((w) => { const [a, b] = mid(w); return { w, along: (a - x) * dx + (b - y) * dy, side: Math.abs((a - x) * dy) + Math.abs((b - y) * dx) }; });
    to = away.filter((c) => c.along > 1).sort((p, q) => p.along + 2 * p.side - q.along - 2 * q.side)[0]?.w;
  }
  if (!to) return true; // nothing that way: stay
  const box = to.querySelector(".ck-form textarea");
  (box?.offsetParent && !box.disabled ? box : to).focus({ preventScroll: true });
  to.scrollIntoView({ block: "nearest" });
  return true;
}
// The buttons beside List, once there is more than one part; each counts what needs you there, on no card in this window.
function bdPartBar(on) {
  const box = $("bd-parts"), parts = bdParts(), live = on ? ck.chats.filter((c) => ckScope(c) && ckState(c) !== "closed") : [];
  const top = Math.max(bd.part, ...live.map((c) => bdPartOf(c, parts)));
  const list = on && top > 1 ? Array.from({ length: top + 1 }, (_, n) => ({ n, needs: n ? live.filter((c) => bdPartOf(c, parts) === n && ckState(c) === "needs").length : 0 })) : [];
  const sig = JSON.stringify([bd.part, list, keyOf("digits"), keyOf("digits_move")]);
  if (box.dataset.sig === sig) return; // a rebuild would drop the focus from a button
  box.dataset.sig = sig;
  box.replaceChildren(...list.map(({ n, needs }) => {
    const b = el("button", "btn small quiet", n ? String(n) : "All"); b.type = "button";
    b.title = n ? `Part ${n} (${keyOf("digits").replace("#", n)}). ${keyOf("digits_move").replace("#", n)} moves a card here` : `All parts (${keyOf("digits").replace("#", 0)})`;
    b.setAttribute("aria-label", `${n ? `Part ${n}` : "All parts"}${needs ? `, ${needs} need${needs > 1 ? "" : "s"} you` : ""}`);
    b.setAttribute("aria-pressed", String(n === bd.part));
    if (needs) b.append(el("span", "n", String(needs)));
    b.onclick = () => bdPartShow(n);
    return b;
  }));
}

function bdBuild() {
  const root = el("section", "bd"); root.id = "bd"; root.hidden = true; root.setAttribute("aria-label", "Chats");
  root.innerHTML = `<button type="button" class="btn small ck-jump-btn bd-sort" id="bd-sort" title="Put the chats that finished or need you on top" hidden></button><div class="bd-stage" id="bd-stage" tabindex="0" aria-label="Chats. Tab goes through them"><div class="bd-world" id="bd-world"></div></div>`;
  $("cockpit").append(root);
  const st = $("bd-stage");
  $("bd-sort").onclick = () => { bdSettle(); bdRender(); st.scrollTop = 0; st.focus({ preventScroll: true }); };
  new ResizeObserver(bdGridSize).observe(st);
  $("bd-world").addEventListener("focusin", (e) => { bd.last = e.target.closest(".bd-card")?.dataset.id || bd.last; });
  st.addEventListener("scroll", bdLoadSoon, { passive: true }); // cards scrolled into view load now
  root.addEventListener("keydown", (e) => { // Esc leaves a card (also its text box) for the grid
    if (e.key === "Escape" && !e.isComposing && document.activeElement.closest(".bd-card")) { e.preventDefault(); e.stopPropagation(); st.focus(); }
  });
}

// The header link between grid and list. On the grid it counts what needs you but has no card here: the sidebar's badge
// counts those too, and only the list shows them (reviews, tasks, workers, other projects).
function bdLink() {
  const on = bdOn(), a = $("ck-view"), cards = new Set(on ? bdChats().map(({ c }) => ckKey(c)) : []);
  bdPartBar(on);
  const n = on ? (ck.board || []).filter((i) => i.col === "needs" && !cards.has(`${i.machine}/${i.kind === "chat" ? i.id : i.kind === "ask" ? i.chat : ""}`)).length : 0;
  a.href = on ? "#/chats?view=list" : "#/chats";
  a.replaceChildren(on ? "List" : "Grid", ...(n ? [el("span", "n", String(n))] : []));
  a.title = on ? "Review, workers, tasks, earlier and archived chats" : "Back to the grid";
  if (n) a.setAttribute("aria-label", `List, ${n} need${n > 1 ? "" : "s"} you`); else a.removeAttribute("aria-label");
}

// Shown or hidden by the route (ckLayout); the cockpit hides the list while it shows.
function bdShow() {
  const on = bdOn();
  bdLink();
  if (!on && !$("bd")) return;
  if (!$("bd")) bdBuild();
  $("bd").hidden = !on; $("cockpit").classList.toggle("board", on);
  if (on) bdRender();
}

// The chats in grid order: per project the one that last finished (or asks an approval) first. A card keeps its place
// otherwise: a new message, its events or reading it never move it; its state shows on the card. Unless `live`, a finish
// counts only once it is settled (bdHold).
function bdChats(live = false) {
  const rank = (c) => (live ? null : bd.held.get(ckKey(c))) ?? c.raised ?? 0;
  const draft = ck.cards.get(ckKey(CK_DRAFT))?.meta, parts = bdParts(); // + New chat on the grid: on top, so its project's frame is too
  return [...(draft ? [draft] : []), ...ck.chats].filter(ckScope).map((c) => ({ c, st: ckState(c) })).filter((x) => x.st !== "closed" && (x.c.draft || !bd.part || bdPartOf(x.c, parts) === bd.part)) // closed: under the list's Closed only
    .sort((a, b) => !!b.c.draft - !!a.c.draft || rank(b.c) - rank(a.c));
}

const bdSettle = () => { bd.held = new Map(ck.chats.map((c) => [ckKey(c), c.raised || 0])); };
// A grid you look at holds still, or you lose the line you read: a chat that finishes, or one new from elsewhere (it comes
// in below), moves only by the pill or while the tab is hidden. The pill counts them, also what no scrolled-off card can show.
function bdHold(together) {
  const low = Math.min(...bd.held.values()), project = ({ c }) => c.project || "";
  ck.chats.forEach((c, i) => { if (!bd.held.has(ckKey(c))) bd.held.set(ckKey(c), low - 1 - i); });
  const seq = (rows) => (together ? rows : [...new Set(rows.map(project))].flatMap((p) => rows.filter((r) => project(r) === p))).map(({ c }) => ckKey(c)).join();
  const rows = bdChats(), moved = rows.filter(({ c }) => !c.draft && (c.raised || 0) > bd.held.get(ckKey(c)));
  const still = !document.hidden && low !== Infinity && moved.length && seq(rows) !== seq(bdChats(true));
  if (!still) bdSettle();
  const needs = moved.filter(({ st }) => st === "needs").length, pill = $("bd-sort");
  pill.hidden = !still; pill.classList.toggle("hot", !!needs);
  pill.textContent = needs ? `↑ ${needs} need${needs > 1 ? "" : "s"} you` : `↑ ${moved.length} updated`;
}

// A re-insert resets a node's focus and scroll position: you may be typing or reading in the card that moves.
function bdMove(world, n, before) {
  const active = n.contains(document.activeElement) ? document.activeElement : null, log = n.querySelector(".ck-log"), top = log?.scrollTop;
  world.insertBefore(n, before);
  if (log) log.scrollTop = top;
  active?.focus({ preventScroll: true });
}

// Frames and cards are kept per project and per chat: a re-render (every summary) moves them, never rebuilds them, so
// the focus and a half-typed message stay.
function bdRender() {
  if (!$("bd") || $("bd").hidden) return;
  bdLink();
  const together = me.prefs?.grid_group === "together";
  bdHold(together);
  const rows = bdChats(), shut = bdShut(), world = $("bd-world");
  const frames = bd.lay = bdProjects(rows.map(({ c }) => c), shut, together);
  // Whatever moves, the card you are in stays where it is on screen (the stage scrolls along below).
  const here = document.activeElement?.closest?.(".bd-card"), hereTop = here?.getBoundingClientRect().top;
  const keep = new Set(), back = [];
  for (const f of frames) {
    let box = bd.frames.get(f.project);
    if (!box) {
      box = el("section", "bd-frame"); const t = el("button", "bd-frame-title"); t.type = "button";
      t.append(el("span", "bd-chev", "▾"), el("strong"), el("span", "ck-count"), el("span", "badge hot"));
      t.onclick = () => { const now = bdShut(); store.set("kn_board", JSON.stringify({ shut: now.includes(f.project) ? now.filter((x) => x !== f.project) : [...now, f.project] })); bdRender(); };
      box.append(t); bd.frames.set(f.project, box);
    }
    const t = box.firstChild, name = f.project ? shown(f.project) : "no project";
    t.children[1].textContent = name; t.children[2].textContent = String(f.n);
    // folded away, its cards cannot show that one waits on you
    const needs = f.shut ? rows.filter(({ c, st }) => st === "needs" && (c.project || "") === f.project).length : 0;
    t.children[3].textContent = needs ? `${needs} need${needs > 1 ? "" : "s"} you` : ""; t.children[3].hidden = !needs;
    t.setAttribute("aria-expanded", String(!f.shut)); t.title = f.shut ? `Show the chats of ${name}` : `Fold ${name} away`;
    box.setAttribute("aria-label", `${name}: ${f.n} chat${f.n === 1 ? "" : "s"}`);
    box.classList.toggle("shut", f.shut);
    keep.add(box);
  }
  for (const [p, box] of bd.frames) if (!keep.has(box)) { box.remove(); bd.frames.delete(p); }
  // the queued tasks in scope: one line of chips on top of the grid (a project with tasks but no chat has no frame);
  // a chip opens the task in the list
  const queued = (ck.board || []).filter((i) => i.kind === "task" && i.col === "open" && ckScope(i));
  bd.queue ||= el("div", "bd-queue");
  bd.queue.replaceChildren(el("span", "muted", `Queued ${queued.length}:`), ...queued.map((i) => {
    const b = el("a", "chip", agentProject ? i.id : `${shown(i.project)} · ${i.id}`); b.href = `#/inbox/${encodeURIComponent(i.key)}`;
    b.title = `${i.title}\n${i.line}`;
    return b;
  }));
  const order = queued.length ? [bd.queue] : [];
  if (!queued.length) bd.queue.remove();
  for (const f of frames) {
    order.push(bd.frames.get(f.project));
    for (const { c, st } of rows.filter(({ c }) => together || (c.project || "") === f.project)) {
      const card = ckCard(c), k = ckKey(c);
      let w = bd.wraps.get(k);
      if (!w) {
        w = el("article", "bd-card"); w.tabIndex = 0; w.dataset.id = c.id;
        bd.wraps.set(k, w); back.push(card);
        w.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === w) { e.preventDefault(); if (card.meta.draft) card.input.focus(); else card.openTab.click(); } });
      }
      if (card.root.parentNode !== w) w.replaceChildren(card.root); // back from the full chat (ckLayout moved it there); a new draft replaces an old one
      w.className = `bd-card ${st}${c.draft ? " draft" : ""}`; w.hidden = f.shut;
      w.setAttribute("aria-label", `${ckTitle(c, card)}: ${CK_STATE[st]}, ${AGENT_NAME[c.agent] || c.agent} on ${c.label}, ${ckShort(c.folder)}. Enter opens it in a tab of its own, Esc goes back to the grid`);
      order.push(w);
    }
  }
  for (const [k, w] of bd.wraps) if (!ck.cards.has(k) || !order.includes(w)) { w.remove(); bd.wraps.delete(k); }
  // DOM order = layout order, so Tab walks project by project; only moved nodes are touched
  order.forEach((n, i) => { if (world.children[i] !== n) bdMove(world, n, world.children[i] || null); });
  if (!rows.length) world.replaceChildren(...order, el("p", "bd-none muted", bd.part ? `No chats in part ${bd.part}${agentProject ? " of this project" : ""}: start one with + New chat, or ${keyOf("digits_move").replace("#", bd.part)} on a card elsewhere moves it here.` : ck.chats.length ? "No chats in this project." : "No chats yet: start one with + New chat."));
  else world.querySelector(".bd-none")?.remove();
  bdGridSize();
  for (const card of back) ckScrollBottom(card, card.savedScrollTop == null); // back from another part: taken off the page, its log forgot where it was
  if (here?.isConnected) $("bd-stage").scrollTop += here.getBoundingClientRect().top - hereTop;
}

let bdLoadTimer = 0;
function bdLoadSoon() {
  clearTimeout(bdLoadTimer);
  bdLoadTimer = setTimeout(() => { for (const c of bdLive()) if (!c.loaded) { c.loaded = true; ckPoll(c); } }, 250);
}
// The cards ckTick polls: in an open frame, their box meeting the stage's (the grid scrolls).
function bdLive() {
  if (!$("bd") || $("bd").hidden) return [];
  const s = $("bd-stage").getBoundingClientRect();
  return [...bd.wraps].filter(([, w]) => { if (w.hidden) return false; const r = w.getBoundingClientRect(); return r.bottom > s.top && r.top < s.bottom; })
    .map(([k]) => ck.cards.get(k)).filter(Boolean);
}
