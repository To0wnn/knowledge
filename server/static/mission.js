// The chat grid (Agents, #/chats on a desktop). Uses app.js globals: $, el, api, agentProject, shown, store, ck*; go and
// shellRoute from shell.js.

// ---------------------------------------------------------------- Chat grid (#/chats on a desktop)
// 1000 px and up; narrower keeps the list. One card per chat, tiled to fill the screen like terminal panes, inside one
// foldable frame per project, the one last active on top (folding is kept in localStorage; while you look at the grid, by bdHold), or Settings: all in one. A card is the chat itself (ckCard). Only cards on screen
// are polled (ckTick). ?view=grid is an older link to the same view; ?view=list is the attention list (#/inbox links).
const BG = { minW: 480, minH: 380, gap: 16, pad: 16, head: 46 };
const bd = { lay: [], wraps: new Map(), frames: new Map(), held: new Map(), part: (() => { try { const v = sessionStorage.getItem("kn_part"); return v === null ? null : Number(v) || 0; } catch { return null; } })() }; // null: part 1 with a limit, else all (bdRender)
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
  if (n > 1 || bdPerPart()) parts[key] = n; // with a limit a chat without a part is new: the overflow would place it again
  store.set("kn_parts", JSON.stringify(parts));
}
// Settings: at most this many chats per part (0: no limit, parts only by hand).
const bdPerPart = () => (me.prefs?.grid_per_part === "off" ? 0 : Number(me.prefs?.grid_per_part) || 6);
// The overflow, pure (ui_smoke checks it): each open chat no part holds yet goes to the lowest part with room, the oldest
// first, and is stored there like a move by hand, so it stays when others close (a chat never jumps between windows).
function bdFlow(chats, parts, max) {
  const out = { ...parts }, count = {};
  for (const c of chats) if (out[ckKey(c)]) count[out[ckKey(c)]] = (count[out[ckKey(c)]] || 0) + 1;
  for (const c of chats.filter((x) => !out[ckKey(x)]).sort((a, b) => (a.created || 0) - (b.created || 0))) {
    let n = 1;
    while ((count[n] || 0) >= max) n++;
    out[ckKey(c)] = n; count[n] = (count[n] || 0) + 1;
  }
  return out;
}
function bdPartFill() {
  const max = bdPerPart();
  if (!max) return;
  const parts = bdParts(), next = bdFlow(ck.chats.filter((c) => ckState(c) !== "closed"), parts, max);
  if (Object.keys(next).length !== Object.keys(parts).length) store.set("kn_parts", JSON.stringify(next));
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

// What needs you but has no chat card on the grid (reviews, tasks to approve, results to read, an approval of a chat
// in another part): the Needs-you strip on top of the grid shows it, the same cards and answers as the list.
function bdNeeds() {
  const cards = new Set(bdChats().map(({ c }) => ckKey(c)));
  return (ck.board || []).filter((i) => i.col === "needs" && i.kind !== "worker" && ckScope(i) // workers: their own strip
    && !cards.has(`${i.machine}/${i.kind === "chat" ? i.id : i.kind === "ask" ? i.chat : ""}`));
}
// The header link between grid and list. On the grid it counts what needs you in other projects: the strip shows the rest.
function bdLink() {
  const on = bdOn(), a = $("ck-view");
  bdPartBar(on);
  const n = on ? (ck.board || []).filter((i) => i.col === "needs" && !ckInProject(i) && ckYours(i)).length : 0;
  a.href = on ? "#/chats?view=list" : "#/chats";
  a.replaceChildren(on ? "List" : "Grid", ...(n ? [el("span", "n", String(n))] : []));
  a.title = on ? "Review, workers, tasks, earlier and archived chats" + (n ? `; ${n} in other projects need${n > 1 ? "" : "s"} you` : "") : "Back to the grid";
  if (n) a.setAttribute("aria-label", `List, ${n} in other projects need${n > 1 ? "" : "s"} you`); else a.removeAttribute("aria-label");
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
  bdPartFill();
  if (bd.part == null) bd.part = bdPerPart() ? 1 : 0; // a new window: the first part, as the limit means
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
  // workers (a chat nobody types in) that run, or stopped and need you: one strip of compact cards (progress, Follow, Stop);
  // rebuilt each render, which is fine: nothing is typed in them
  const workers = (ck.board || []).filter((i) => i.kind === "worker" && i.col !== "done" && ckScope(i));
  bd.workers ||= el("div", "bd-workers");
  bd.workers.replaceChildren(...workers.map(ckItem));
  // what needs you first, on top of everything: answered right here (an answer being typed or a menu open keeps it as it is)
  const needs = bdNeeds();
  bd.needs ||= el("section", "bd-needs");
  if (!bd.needs.querySelector(".ck-answer, .ck-reply, details[open]") && !selecting(bd.needs)) {
    const had = bd.needs.contains(document.activeElement) && document.activeElement.dataset.row; // the redraw keeps your place
    const h = el("h2", "bd-needs-head", `Needs you · ${needs.length}`);
    const away = (ck.board || []).filter((i) => i.col === "needs" && !ckInProject(i) && ckYours(i)).length; // as the list says it: never hidden without a word
    if (away) { const b = el("button", "btn small ck-elsewhere", `${away} more in other projects: show all`); b.type = "button"; b.onclick = () => switchProject(ALL); h.append(" ", b); }
    h.append(" ", ckScopeSwitch());
    bd.needs.setAttribute("aria-label", `Needs you: ${needs.length}`);
    bd.needs.replaceChildren(h, ...ckBunches(needs));
    if (had) bd.needs.querySelector(`[data-row="${CSS.escape(had)}"]`)?.focus({ preventScroll: true });
  }
  const strip = needs.length || ck.scope?.others || ck.chats.some((c) => !c.closed && ckTheirs(c)) || (ck.board || []).some((i) => i.col === "needs" && !ckInProject(i) && ckYours(i)); // the switch and "other projects" stay in reach
  const order = [...(strip ? [bd.needs] : []), ...(queued.length ? [bd.queue] : []), ...(workers.length ? [bd.workers] : [])];
  if (!strip) bd.needs.remove();
  if (!queued.length) bd.queue.remove();
  if (!workers.length) bd.workers.remove();
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
