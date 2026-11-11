// The Agents views (#/inbox, #/chats, Tasks/Machines cards), split out of app.js: it shares app.js's global scope
// ($, el, api, go, me ...) and loads right after it (index.html).

// ---------------------------------------------------------------- Agents: what needs you, what runs, and your chats
// Chats with agents on your pcs: one card per chat (pc, agent, folder), the answer streaming in. The server only
// relays: the pc's companion pulls each message and runs one turn of the agent (plugin/hooks/chat.py). Everything is
// text (textContent): nothing an agent prints becomes markup. The view is #/chats (shell.js; #/inbox is the same):
// #/chats/<chat id> is one chat (its card is keyed by pc and chat id).
const AGENT_MARK = { claude: "C", codex: "X", agy: "A" };
const ck = { cards: new Map(), machines: [], chats: [], board: [], view: "", timer: 0, summaryAt: 0, doneOpen: false, focus: "", hit: "", boardAt: 0, filter: "", waits: new Map() };
const ckKey = (c) => `${c.machine}/${c.id}`;
const ckBusy = (s) => s === "running" || s === "queued";
const FORCE_BUSY = "This chat is working: force archive stops its turn, drops its queued messages and skips the wrap-up. Go on?";
const ckShort = (path) => (path || "").split(/[\\/]/).filter(Boolean).pop() || path;
const ckScope = (x) => !agentProject || x.project === agentProject; // the header's project filter

// Polling of the pcs, chats and board runs while an Agents view shows; the router turns it on and off.
function ckRun(on) { clearTimeout(ck.timer); if (on) { ck.summaryAt = 0; ckTick(); } }

// The bar and the board: the pcs, the chats and the board every ~5 s on the board (10 s in a chat); the open chat
// every 1.5 s while it runs, else 4 s (15 s in a tab out of view). Only the chats on screen are polled (the open one, or the grid's cards on
// screen, mission.js bdLive): showing a chat's log is what marks it read.
async function ckTick() {
  if ($("cockpit").hidden) return;
  let live = [];
  try {
    if (Date.now() - ck.summaryAt > (ck.view ? 10000 : 5000)) await ckSummary();
    live = [ck.cards.get(ck.view), ...bdLive()].filter(Boolean);
    await Promise.all(live.map(ckPoll));
  } catch (e) { $("ck-list").title = e.message; if (!ck.ready) { ck.error = e.message; ckList(); } }
  clearTimeout(ck.timer);
  ck.timer = setTimeout(ckTick, document.hidden ? 15000 : live.some((c) => ckBusy(c.meta.status)) ? 1500 : 4000); // out of view: enough for the badge and a notification
}
// Back in view: catch up now, which also marks the open chat read.
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("cockpit").hidden) { clearTimeout(ck.timer); ckTick(); } });
async function ckSummary() {
  ck.summaryAt = Date.now();
  const [ms, cs, bd] = await Promise.all([api("/ui/api/machines"), api("/ui/api/chats"), api("/ui/api/board").catch(() => null)]);
  ck.machines = ms.machines.filter((m) => m.own && !m.old); ck.ready = true; ck.error = "";
  // someone else's pcs shared with you for a project to "use": new chats there too (Machines: Share, on the owner's side)
  ck.shared = ms.machines.filter((m) => !m.own && !m.old && Object.values(m.shared || {}).some((x) => x === "use" || x === "yolo"));
  for (const m of ck.machines) for (const x of m.actions || []) { // a Merge queued from a card: say how it went on the pc
    const what = ck.waits.get(x.id);
    if (what && (x.status === "done" || x.status === "failed")) { ck.waits.delete(x.id); toast(`${what}: ${x.result || x.status}`, x.status === "failed"); }
  }
  ck.chats = cs.chats;
  if (bd) { ck.board = bd.items; ck.boardAt = Date.now(); inboxCount(ck.board); } // a failed board keeps the last one: an empty one would notify it all again
  const now = Date.now() / 1000;
  let live = ck.chats.filter((c) => ckBusy(c.status)).length;
  const worst = {}; // per agent its fullest usage window over the pcs
  for (const m of ck.machines) for (const [a, ag] of Object.entries(m.agents || {})) {
    live += (ag.workers || []).filter((w) => w.status === "running").length + (ag.sessions || []).filter((s) => !s.worker && now - s.at < 600).length;
    if (ag.limit == null || !(ag.resets > now)) continue;
    const had = worst[a], wins = [...(had?.windows || []), ...(ag.windows || [])];
    // usage belongs to the account, not the pc: per window the fullest reading of any pc (one with an older plugin sends none)
    const merged = Object.values(wins.reduce((by, w) => ((!by[w[1]] || w[0] > by[w[1]][0]) && (by[w[1]] = w), by), {}));
    worst[a] = { ...(had && had.limit >= ag.limit ? had : { ...ag, agent: a, label: m.label }), windows: merged };
  }
  $("ck-live-n").textContent = live; $("ck-live").classList.toggle("on", live > 0);
  // one chip per agent with a known limit, each window: "Claude 5h 12% · 7d 82%"; amber from 80%, red from 95% (the
  // fullest window); when each resets in the tooltip
  const when = (t) => new Date(t * 1000).toLocaleString("en-GB", t - now > 86400 ? { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" }).replace(",", "");
  const chips = Object.values(worst).sort((a, b) => b.limit - a.limit).map((w) => {
    const c = el("span", `chip${w.limit >= 95 ? " bad" : w.limit >= 80 ? " warn" : ""}`, `${(AGENT_NAME[w.agent] || w.agent).replace(" Code", "")} ${usageText(w)}`);
    const wins = usageWindows(w).length ? usageWindows(w) : [[w.limit, "limit", w.resets]];
    c.title = `${AGENT_NAME[w.agent] || w.agent}, usage limit used:\n` + wins.map(([p, n, r]) => `${n}: ${Math.round(p)}%${r ? `, resets ${when(r)}` : ""}`).join("\n");
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
  const last = store.get("kn_last_chat"); // the list keeps the chat last open (until its × closes it); a phone starts on the list
  if (!ck.view && shellRoute.view === "chats" && !shellRoute.arg && !bdOn() && matchMedia("(min-width: 700px)").matches
      && last && ck.chats.some((x) => x.id === last && !x.closed)) return go(`#/chats/${encodeURIComponent(last)}`, true);
  if (solo() && !ck.view) ck.view = shellRoute.arg; // its chat was dropped above: below it comes back or the tab becomes the app, never an empty page
  const opened = ck.view && !ck.cards.has(ck.view);
  if (opened) { // a route's chat id (or an older pc/id key) once the chats are known
    const sub = /^([0-9a-f]{12})\/sub\/(.+)$/.exec(ck.view), sc = sub && ck.chats.find((x) => x.id === sub[1]);
    const c = !sub && ck.chats.find((x) => x.id === ck.view || ckKey(x) === ck.view), hit = !sub && ckFindWorker(ck.view);
    if (sc) ck.view = ckKey(ckSubCard(sc, sub[2]).meta);
    else if (c) ck.view = ckKey(ckCard(c).meta);
    else if (hit) ck.view = ckKey(ckWorkerCard(hit.m, hit.w).meta);
    else { // gone or never there: the list, without a dead chat in the URL
      ck.view = "";
      if (shellRoute.view === "chats" && shellRoute.arg) { document.body.classList.remove("solo"); history.replaceState(history.state, "", "#/chats"); shellRoute = { ...shellRoute, arg: "" }; lastRoute.chats = "#/chats"; }
      store.set("kn_last_chat", ""); // a link to a chat that is gone lands on the list, not on another chat
    }
  }
  ckList(); ckLayout();
  if (opened && !touch) ck.cards.get(ck.view)?.input.focus({ preventScroll: true }); // a link opened on this chat: as applyRoute does for a card it knows
}
// The Agents view's list, grouped by what you have to do: Needs you (each card says why), Review (branches to merge,
// finished workers), Working, Done today (folded to one line), then the earlier chats (the filter and Archived narrow
// those; the API calls an archived chat closed). A dot and a word say a chat's state (not colour alone). Every card
// has its actions right there.
const CK_STATE = { needs: "needs you", running: "running", idle: "idle", closed: "archived" };
const CK_SECS = [["needs", "Needs you"], ["review", "Review"], ["running", "Working"], ["open", "Queued"], ["done", "Done today"]];
const ckSec = (i) => (i.kind === "review" ? "review" : i.col); // Review: a branch with work to merge; a finished worker is done
function ckState(c) { // an approval waiting in a running turn needs you too
  const needs = (ck.board || []).some((i) => i.col === "needs" && i.machine === c.machine && (i.kind === "chat" ? i.id : i.kind === "ask" ? i.chat : "") === c.id);
  return needs ? "needs" : ckBusy(c.status) ? "running" : c.closed ? "closed" : "idle";
}
function ckSubRows(c) { // a chat's subagents, each a chat of its own, under it: the running ones; the finished fold away
  const all = (c?.sub_list || []).slice().reverse(), run = all.filter((s) => s.status === "running"), rest = all.filter((s) => s.status !== "running");
  const open = ck.subsOpen?.has(ckKey(c)), shown = [...run, ...(open ? rest : [])];
  const rows = shown.map((s) => {
    const sl = el("li", "ck-subrow"), sb = el("button", `ck-chat ck-subchat ${s.status === "running" ? "running" : "idle"}`); sb.type = "button";
    sb.dataset.row = `sub:${ckKey(c)}~${s.id}`;
    sb.append(el("span", "ck-dot", ""), el("span", "ck-chat-title", `↳ ${s.label}`), el("small", null, `subagent · ${s.status}`));
    if (ck.view === `${ckKey(c)}~${s.id}`) sb.setAttribute("aria-current", "true");
    sb.onclick = () => go(`#/chats/${encodeURIComponent(c.id)}/sub/${encodeURIComponent(s.id)}`);
    sl.append(sb); return sl;
  });
  if (rest.length) { // the finished ones on request (the server keeps a chat's newest 20)
    const li = el("li", "ck-subrow"), more = el("button", "btn small quiet ck-submore", open ? "Hide finished subagents" : `+${rest.length} finished subagent${rest.length > 1 ? "s" : ""}`);
    more.type = "button";
    more.onclick = (e) => { e.stopPropagation(); ck.subsOpen = ck.subsOpen || new Set(); ck.subsOpen[open ? "delete" : "add"](ckKey(c)); ckList(); };
    li.append(more); rows.push(li);
  }
  return rows;
}
const ckOnlinePc = () => ck.machines.some((m) => m.online && m.companion);
const ckPc = (id) => ck.machines.find((m) => m.id === id) || (ck.shared || []).find((m) => m.id === id); // own, else shared with you
const ckLinked = (m) => (m.companion?.folders?.linked || []).filter((f) => m.own || ["use", "yolo"].includes(m.shared?.[f.project]));
// Getting started, instead of an empty list: the help page's install line for this pc's system (help.html#install),
// then linking a folder and the first chat.
const INSTALL_CMD = {
  linux: "curl -fsSL https://raw.githubusercontent.com/To0wnn/knowledge/main/install.sh | sh",
  windows: "irm https://raw.githubusercontent.com/To0wnn/knowledge/main/install.ps1 | iex",
};
function ckGetStarted() {
  const os = store.get("kn_help_os") || (/Windows/.test(navigator.userAgent) ? "windows" : "linux");
  const card = el("section", "card ck-start"), h = el("h2", null, "Get started"); h.id = "ck-start-h";
  card.setAttribute("aria-labelledby", h.id);
  const cmd = el("div", "cmd"), pre = el("pre", null, INSTALL_CMD[os] || INSTALL_CMD.linux), copy = actionButton("Copy", async () => {
    try { await navigator.clipboard.writeText(pre.textContent); copy.textContent = "Copied"; }
    catch { getSelection().selectAllChildren(pre); copy.textContent = "Selected"; } // no clipboard access
    setTimeout(() => (copy.textContent = "Copy"), 1500);
  }, "btn small copy");
  cmd.append(pre, copy);
  const help = el("a", null, "Help: Install"); help.href = "/ui/help#install";
  const step = (title, ...body) => { const li = el("li"); li.append(el("b", null, title), ...body); return li; };
  const ol = el("ol");
  ol.append(
    step("Install on your pc", `Run the setup in a ${os === "windows" ? "PowerShell" : "terminal"}; it asks for this server's address (${location.origin}) and your token, and offers the companion: say yes, chats run through it.`, cmd,
      el("span", "muted", "Other systems and what it needs: "), help, "."),
    step("Link a project folder", "On the companion page (Folders), under Machines here, or by asking your agent: sessions in that folder get the project."),
    step("Start a chat", "Once your pc shows online here, + New chat talks to Claude Code, Codex or agy in that folder."));
  card.append(h, el("p", "muted", "None of your pcs is online with a companion yet. Three steps:"), ol);
  return card;
}
function ckList() {
  const box = $("ck-list"), hist = $("ck-history"), side = $("ck-side");
  // a half-typed reply or reason, or an open menu, outlives the refresh: the list waits until it is sent or closed
  if (side.querySelector(".ck-reply, .ck-reject, .ck-snooze[open]") || selecting(side)) return;
  const had = side.contains(document.activeElement) && document.activeElement.dataset.row;
  const items = (ck.board || []).filter((i) => ckScope(i) && !(i.kind === "chat" && i.col === "done")); // a read chat is an earlier chat
  const pcs = ckOnlinePc(), fresh = ck.ready && !pcs && !ck.chats.length;
  side.querySelector(".ck-sec-earlier").hidden = !ck.ready || fresh; // nothing to filter yet
  if (!ck.ready) { box.replaceChildren(ck.error ? errorState(`Chats did not load: ${ck.error}`, () => { ck.error = ""; ckList(); ckRun(true); }) : loadingState()); return; }
  box.replaceChildren();
  if (!pcs && !items.length) box.append(ck.start ||= ckGetStarted()); // kept: a refresh would reset Copy and a selection
  const elsewhere = (ck.board || []).filter((i) => i.col === "needs" && !ckScope(i)).length; // a project filter never hides what needs you without saying so
  if (elsewhere) {
    const b = el("button", "btn small ck-elsewhere", `${elsewhere} more need${elsewhere > 1 ? "" : "s"} you in other projects: show all`);
    b.type = "button"; b.onclick = () => switchProject(ALL); box.append(b); // the one picker: the sidebar shows All too
  }
  for (const [id, name] of pcs || items.length ? CK_SECS : []) {
    const list = items.filter((i) => ckSec(i) === id);
    if (!list.length && id !== "needs") continue;
    const sec = el("section", `ck-sec ck-sec-${id}${list.length ? " has-items" : ""}`), head = el("h2", "ck-sec-head");
    sec.setAttribute("aria-label", `${name}: ${list.length}`);
    if (id === "done") { // one line ("9 done today") that unfolds
      const fold = el("button", "ck-done-toggle", `${list.length} done today`); fold.type = "button"; fold.dataset.row = "done";
      fold.setAttribute("aria-expanded", String(ck.doneOpen));
      fold.onclick = () => { ck.doneOpen = !ck.doneOpen; ckList(); $("ck-list").querySelector(".ck-done-toggle")?.focus(); };
      head.append(fold);
    } else head.append(el("span", null, name), el("span", "ck-count", String(list.length)));
    sec.append(head);
    if (!list.length) sec.append(el("p", "ck-none", "Nothing waits for you."));
    if (id !== "done" || ck.doneOpen) sec.append(...ckBunches(list));
    box.append(sec);
  }
  const inList = new Set(items.filter((i) => i.kind === "chat").map((i) => `${i.machine}/${i.id}`));
  const words = ck.filter.toLowerCase().split(/\s+/).filter(Boolean), closed = $("ck-closed").getAttribute("aria-pressed") === "true";
  const rows = ck.chats.filter((c) => ckScope(c) && !inList.has(ckKey(c)) && !!c.closed === closed
      && words.every((w) => [c.title, c.label, c.folder, AGENT_NAME[c.agent] || c.agent].join(" ").toLowerCase().includes(w)))
    .sort((a, b) => (b.updated || 0) - (a.updated || 0));
  hist.replaceChildren();
  for (const c of rows.slice(0, 60)) {
    const st = ckState(c), li = el("li"), b = el("a", `ck-chat ${st}`), out = (ck.board || []).find((i) => i.kind === "chat" && ckKey(i) === ckKey(c))?.outcome;
    const who = c.rights === "view" || c.guest ? ` · by ${c.by || "?"}` : ""; // someone else's chat, or a guest's on your pc
    const meta = `${CK_STATE[st]} · ${AGENT_NAME[c.agent] || c.agent} · ${c.label}${who} · ${ckShort(c.folder)} · ${ago(c.updated)}`;
    b.href = `#/chats/${encodeURIComponent(c.id)}?solo=1`; b.dataset.row = `chat:${ckKey(c)}`; // a click opens it here; Ctrl, Cmd or the middle button in a tab of its own
    b.append(el("span", "ck-dot", ""), el("span", "ck-chat-title", c.title || "(no message yet)"), el("small", null, out || meta));
    if (out) b.title = meta;
    if (ckKey(c) === ck.view) b.setAttribute("aria-current", "true");
    b.onclick = (e) => { if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return; e.preventDefault(); ckShow(c); };
    li.append(b); hist.append(li, ...ckSubRows(c));
  }
  $("ck-earlier-n").textContent = rows.length;
  if (!hist.childNodes.length) hist.append(!ck.chats.length && pcs ? el("li", null, emptyState("No chats yet: talk to an agent on one of your pcs.", actionButton("New chat", () => $("ck-new").click(), "btn small primary")))
    : el("li", "muted ck-none", !ck.chats.length ? "No chats yet." : words.length ? "No chat matches." : closed ? "No archived chats." : "No earlier chats here."));
  if (had) side.querySelector(`[data-row="${CSS.escape(had)}"]`)?.focus({ preventScroll: true }); // the refresh keeps your place
  const n = items.filter((i) => i.col === "needs").length, asks = items.filter((i) => i.kind === "ask").length;
  const said = (n ? `${n} item${n > 1 ? "s" : ""} need${n > 1 ? "" : "s"} you` : "Nothing needs you") + (asks ? `, ${asks} approval${asks > 1 ? "s" : ""} waiting` : "");
  if ($("ck-needs-live").textContent !== said) $("ck-needs-live").textContent = said;
  ckFocus();
}
let ckFilterTimer;
$("ck-filter").oninput = () => { clearTimeout(ckFilterTimer); ckFilterTimer = setTimeout(() => { ck.filter = $("ck-filter").value; ckList(); }, 120); };
$("ck-closed").onclick = () => { $("ck-closed").setAttribute("aria-pressed", String($("ck-closed").getAttribute("aria-pressed") !== "true")); ckList(); };
$("ck-side").addEventListener("keydown", (e) => { // up and down go through the rows; Enter opens one (it is a button)
  if ((e.key !== "ArrowDown" && e.key !== "ArrowUp") || !e.target.dataset.row) return;
  const rows = [...$("ck-side").querySelectorAll("[data-row]")].filter((r) => r.offsetParent), at = rows.indexOf(e.target);
  e.preventDefault(); rows[Math.max(0, Math.min(rows.length - 1, at + (e.key === "ArrowDown" ? 1 : -1)))]?.focus();
});
// Open one chat (its route), or go back from it when `meta` is empty.
function ckShow(meta) {
  if (!meta) return solo() ? ckCloseTab() : goBack("#/chats");
  go(`#/chats/${encodeURIComponent(ckCard(meta).meta.id)}`);
}
// The pill over the end of a chat you scrolled up in; it also says when more came in below.
function ckJump(card) {
  if (!card.jump || !card.log.clientHeight) return; // not on screen: nothing to measure, keep what it said
  const far = card.log.scrollHeight - card.log.scrollTop - card.log.clientHeight >= 80;
  if (!far) card.fresh = false;
  card.jump.hidden = !far;
  card.jumpText.textContent = card.fresh ? "↓ New messages" : "↓ Jump to bottom";
}
// Find in a chat (its button or Ctrl+F): the whole chat is loaded first (the older parts too), then every hit is marked
// with the CSS Custom Highlight API (the log's own nodes stay as they are); Enter and Shift+Enter go from hit to hit, a
// folded block with the hit opens, Esc closes. Outside a chat the key keeps the browser's own find.
function ckFindBar() {
  const bar = el("div", "ck-find"), q = el("input"), count = el("span", "muted ck-find-n"), prev = el("button", "btn icon small quiet", "↑"), next = el("button", "btn icon small quiet", "↓"), close = el("button", "btn icon small quiet", "×");
  bar.hidden = true; bar.setAttribute("role", "search");
  q.type = "search"; q.placeholder = "Find in this chat"; q.setAttribute("aria-label", "Find in this chat");
  count.setAttribute("aria-live", "polite");
  for (const [b, label] of [[prev, "Previous hit (Shift+Enter)"], [next, "Next hit (Enter)"], [close, "Close (Esc)"]]) { b.type = "button"; b.title = label; b.setAttribute("aria-label", label); }
  bar.append(q, count, prev, next, close);
  return { bar, q, count, prev, next, close, hits: [], at: -1 };
}
function ckFindOpen(card) {
  const f = card.find;
  if (!f.wired) { // once per card: the card exists by now
    f.wired = true;
    let timer = 0;
    f.q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => ckFindRun(card), 200); });
    f.q.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); ckFindStep(card, e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); ckFindClose(card); }
    });
    f.prev.onclick = () => ckFindStep(card, -1); f.next.onclick = () => ckFindStep(card, 1); f.close.onclick = () => ckFindClose(card);
    new MutationObserver(() => { f.dirty = true; }).observe(card.log, { childList: true, subtree: true, characterData: true });
  }
  f.bar.hidden = false; f.q.focus(); f.q.select();
  if (f.q.value) ckFindRun(card);
}
function ckFindClose(card) {
  const f = card.find;
  f.bar.hidden = true; f.hits = []; f.at = -1; ckFindPaint(card);
  (card.input && !card.form.hidden ? card.input : f.btn).focus();
}
async function ckFindRun(card, keep = false) {
  const f = card.find, word = f.q.value.trim().toLowerCase(), seq = (f.seq = (f.seq || 0) + 1);
  if (!word) { f.hits = []; f.at = -1; f.count.textContent = ""; return ckFindPaint(card); }
  for (let i = 0; card.older && i < 200; i++) { // the whole chat: a hit in an older part must be found too
    f.count.textContent = "Loading the earlier part…";
    const was = card.first; await ckOlder(card);
    if (seq !== f.seq) return; // typed on meanwhile
    if (card.first === was) break; // it did not load (an error, said in a toast)
  }
  const hits = [], walk = document.createTreeWalker(card.log, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (n.parentElement?.closest(".ck-older")) continue;
    const t = n.data.toLowerCase();
    for (let i = t.indexOf(word); i >= 0; i = t.indexOf(word, i + word.length)) { const r = new Range(); r.setStart(n, i); r.setEnd(n, i + word.length); hits.push(r); }
  }
  const old = keep && f.hits[f.at];
  f.hits = hits; f.dirty = false;
  // start at the newest hit (the end of the chat is where you are); keep the place when the log grew
  f.at = old ? Math.max(0, hits.findIndex((r) => r.startContainer === old.startContainer && r.startOffset === old.startOffset)) : hits.length - 1;
  ckFindPaint(card, true);
}
function ckFindStep(card, by) {
  const f = card.find;
  if (f.dirty) return ckFindRun(card, true).then(() => ckFindStep(card, by)); // new output came in: count again first
  if (!f.hits.length) return;
  f.at = (f.at + by + f.hits.length) % f.hits.length;
  ckFindPaint(card, true);
}
function ckFindPaint(card, show = false) {
  const f = card.find, hl = window.CSS?.highlights;
  f.count.textContent = f.q.value.trim() && !f.bar.hidden ? (f.hits.length ? `${f.at + 1} of ${f.hits.length}` : "No hits") : "";
  if (hl) { // one set per page: this card's hits only while its bar is open
    hl.set("ck-find", new Highlight(...f.hits)); hl.set("ck-find-now", new Highlight(...(f.hits[f.at] ? [f.hits[f.at]] : [])));
  }
  const r = f.hits[f.at];
  if (!show || !r) return;
  for (let d = r.startContainer.parentElement?.closest("details"); d && card.log.contains(d); d = d.parentElement?.closest("details")) d.open = true;
  const box = r.getBoundingClientRect(), log = card.log.getBoundingClientRect();
  if (box.top < log.top + 40 || box.bottom > log.bottom - 40) card.log.scrollTop += box.top - log.top - log.height / 3;
  card.stickToBottom = false;
}
document.addEventListener("keydown", (e) => {
  if (!keyHit(e, "find")) return;
  const root = document.activeElement?.closest?.(".ck-card") || document.querySelector(".ck-card:hover") || (document.body.classList.contains("in-chat") ? ck.cards.get(ck.view)?.root : null);
  const card = root && [...ck.cards.values()].find((c) => c.root === root);
  if (!card?.find) return;
  e.preventDefault(); ckFindOpen(card);
});
// The jump key (Ctrl+End): the chat you are in or point at. With no pill to press, the key keeps its own meaning (end of the text box).
document.addEventListener("keydown", (e) => {
  if (!keyHit(e, "jump")) return;
  const root = document.activeElement?.closest?.(".ck-card") || document.querySelector(".ck-card:hover") || ck.cards.get(ck.view)?.root;
  const pill = root?.querySelector(":scope > .ck-jump:not([hidden]) button");
  if (pill?.offsetParent) { e.preventDefault(); pill.click(); }
});
// × of a chat alone in its tab: the tab closes; a browser that refuses gets the app in this tab instead.
function ckCloseTab() {
  window.close();
  setTimeout(() => go(startRoute(), true), 200);
}
// What is typed but not sent, per person and chat, in this browser (so a reload, a closed tab or the chat's other tab
// keeps it; the newest 50; sign out clears them, nav.js; a session that just expires does not, so the next load drops
// what is older than 7 days: it can hold a secret). Files are not kept: only their names would be.
const CK_DRAFTS = "kn_drafts", ckDraftKey = (card) => `${me.user}/${ckKey(card.meta)}`;
function ckDrafts() { try { return JSON.parse(store.get(CK_DRAFTS) || "{}") || {}; } catch { return {}; } } // a damaged entry must not break a chat
(function ckDraftsExpire(now = Date.now()) {
  const kept = Object.entries(ckDrafts()).map(([k, d]) => [k, { ...d, at: d?.at || now }]).filter(([, d]) => now - d.at < 7 * 864e5); // without a date (older code): from today
  if (kept.length || store.get(CK_DRAFTS)) store.set(CK_DRAFTS, JSON.stringify(Object.fromEntries(kept)));
})();
function ckDraftKeep(card) {
  const all = ckDrafts(), k = ckDraftKey(card), text = card.input.value;
  if (text.trim()) all[k] = { text, at: Date.now() }; else delete all[k];
  store.set(CK_DRAFTS, JSON.stringify(Object.fromEntries(Object.entries(all).sort((a, b) => b[1].at - a[1].at).slice(0, 50))));
}
function ckDraftShow(card) {
  card.input.value = ckDrafts()[ckDraftKey(card)]?.text || "";
  card.grow(); ckPaint(card);
}
// Typed or sent in the chat's other tab: follow, but never under someone's fingers.
addEventListener("storage", (e) => {
  if (e.key === CK_DRAFTS) for (const c of ck.cards.values()) if (document.activeElement !== c.input || !document.hasFocus()) ckDraftShow(c);
});
// The send key (Settings); a phone's keyboard has no Shift+Enter, so its Enter stays a new line.
const ckSendKey = () => (!touch && { enter: "Enter", shift: "Shift+Enter" }[me.prefs?.send_key]) || "Ctrl+Enter";
const ckKeyEl = (action, cls, text) => { const s = el("span", cls, text); s.dataset.shortcut = action; return s; };
function ckSendHints(card) {
  const hint = card.input.dataset.hint; // a subagent's and a worker's box name the key; a chat's says it in its label
  if (hint) card.input.placeholder = `${hint} (${ckSendKey()} sends)`;
  card.input.setAttribute("aria-label", ckSendKey() === "Enter" ? "Message (Enter sends, Shift+Enter a new line)" : `Message (Enter a new line, ${ckSendKey()} sends)`);
  keyLabels(card.root);
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
  ckPick.textContent = !ck.ready ? "" : ck.chats.length ? "Pick a chat on the left, or start a new one." : ckOnlinePc()
    ? "No chats yet: start one with + New chat, with Claude Code, Codex or agy in a folder linked to a project on one of your pcs."
    : "None of your pcs is online with a companion right now. Chats run on a pc through its knowledge companion (plugin 0.16 or newer).";
  if ($("ck-canvas").firstChild !== show || $("ck-canvas").childNodes.length !== 1) $("ck-canvas").replaceChildren(show);
  document.body.classList.toggle("in-chat", !!card && !$("cockpit").hidden && $("cockpit").dataset.mode === "chats");
  if (card) {
    ckScrollBottom(card, card.savedScrollTop == null);
    card.x.title = solo() ? "Close this tab" : "Back to the list (the chat stays under Earlier)"; card.x.setAttribute("aria-label", solo() ? "Close this tab" : "Back to the list");
  }
  paintTitle();
  bdShow(); // the grid (mission.js) instead of the list, on #/chats on a desktop
}

// Items that say the same thing (eleven tasks waiting for the same missing pc) fold into one group with a count.
// A task past its date, or a person's, is never folded: each needs its own answer.
const ckSame = (i) => (i.kind === "ask" || i.kind === "chat" || i.closable ? "" : `${i.kind}|${i.col}|${i.kind === "worker" ? `${i.status}|${i.why || (i.offline_since ? "offline" : "")}` : i.line}`);
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
// #/inbox/<key> (the Matrix links, an alias of #/chats): once the board is in, scroll to that item and mark it; a
// chat's item opens its chat. An unknown key leaves the plain list. ck.hit keeps the mark across the refreshes.
function ckFocus() {
  const key = ck.focus;
  if (!key || !ck.boardAt || ck.boardAt < (ck.focusAt || 0)) return; // a board older than the link: wait for the fresh one
  ck.focus = "";
  const i = (ck.board || []).find((x) => x.key === key);
  if (i && (i.kind === "chat" || i.kind === "ask")) return go(`#/chats/${encodeURIComponent(i.kind === "ask" ? i.chat : i.id)}`, true);
  if (!i) { // gone or dismissed: the plain list
    ck.hit = "";
    for (const c of $("ck-list").querySelectorAll(".ck-item.hit")) c.classList.remove("hit");
    return;
  }
  ck.hit = key;
  if (!ckScope(i)) { ck.focus = key; return switchProject(ALL); } // filtered away: show all (the sidebar too), then find it
  if (ckSec(i) === "done" && !ck.doneOpen) { ck.doneOpen = true; ck.focus = key; return ckList(); }
  const card = [...$("ck-list").querySelectorAll(".ck-item")].find((c) => c.dataset.key === key);
  if (!card) return;
  card.closest("details")?.setAttribute("open", "");
  for (const c of $("ck-list").querySelectorAll(".ck-item.hit")) c.classList.remove("hit");
  card.classList.add("hit");
  card.scrollIntoView({ block: "center", behavior: calm ? "auto" : "smooth" });
  card.querySelector(".ck-item-main").focus({ preventScroll: true });
}
async function ckDo(path, body) {
  try { await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
  catch (e) { toast(e.message); return false; }
  finally { ck.summaryAt = 0; ckTick(); }
  return true;
}
function ckItem(i) {
  const card = el("article", `ck-item ${i.col}${i.key && i.key === ck.hit ? " hit" : ""}${i.result ? " result" : ""}`), main = el("button", "ck-item-main"), top = el("span", "ck-item-top");
  main.type = "button"; card.dataset.key = main.dataset.row = i.key || "";
  const chat = i.kind === "chat" || i.kind === "ask" ? ck.chats.find((c) => ckKey(c) === `${i.machine}/${i.kind === "ask" ? i.chat : i.id}`) : null;
  if (i.kind === "chat" && chat && ckKey(chat) === ck.view) main.setAttribute("aria-current", "true");
  top.append(el("span", "ck-mark", AGENT_MARK[i.agent] || (i.owner || "?")[0].toUpperCase()), el("strong", null, i.kind === "ask" || i.kind === "terminal" ? ckUnbidi(i.title) : i.title));
  if (i.overdue) top.append(el("span", "chip bad", "overdue"));
  if (i.subs) { const s = el("span", "chip live", `${i.subs} subagent${i.subs > 1 ? "s" : ""}`); s.title = "Subagents of this chat running now"; top.append(s); }
  // a chat's second line is the agent's last sentence when there is one; why it needs you moves up to the first
  const why = i.kind === "chat" && i.outcome && i.col === "needs" ? (i.line.startsWith("replied: ") ? "answered, not read" : i.line) : "";
  const where = [why, i.kind === "review" ? `review · ${AGENT_NAME[i.agent] || i.agent}` : i.kind === "worker" ? "worker" : i.kind === "terminal" ? "terminal" : i.kind === "task" ? (i.result ? "result" : `task for ${i.owner}`) : i.kind === "ask" ? "approval" : "", i.label, ckShort(i.folder),
    agentProject ? "" : shown(i.project || ""), i.due && !i.result ? `due ${i.due}` : "", i.at ? ago(i.at) : ""];
  main.append(top, el("span", "ck-item-where", where.filter(Boolean).join(" · ")), el("span", "ck-item-line", (i.kind === "chat" && i.outcome) || i.line));
  if (i.kind === "worker" && i.why && !i.line.includes(i.why)) main.append(el("span", "ck-item-why", i.why)); // the line says why and "pc offline since"
  if (i.progress) main.append(workerProgress(i.progress));
  if (i.kind === "review") main.append(ckReview(i));
  card.append(main);
  main.title = i.kind === "terminal" ? "Answer it in that terminal" : i.kind === "chat" || i.kind === "ask" ? "Open this chat" : i.kind === "task" || i.kind === "review" ? "Open its line in the memo" : "Follow this worker";
  main.onclick = () => {
    if (i.kind === "terminal") return; // nothing to open here: it is answered in that terminal
    if (i.kind === "chat") return ckShow(chat || i);
    if (i.kind === "ask") return ckShow(chat);
    if (i.kind === "task") { return openNote(i.path, { find: [i.id.toLowerCase()] }); }
    if (i.kind === "review") return openNote(i.path, { find: [`${i.task.toLowerCase()}-done`] });
    return ckFollow(i); // a worker opens as a chat nobody types in: its transcript, steer, take over
  };
  const acts = el("div", "ck-item-acts"), act = (text, title, fn, cls = "btn small quiet") => { const b = el("button", cls, text); b.type = "button"; b.title = title; b.onclick = fn; acts.append(b); return b; };
  const later = []; // Snooze, Dismiss and Archive: behind one ⋯ menu, so the row's answer stands out
  if (i.git) acts.append(ckGitPill(i));
  if (i.approve) act("Approve", "Let this task start" + (i.approve_warning ? `\n\n${i.approve_warning}` : ""),
    () => (!i.approve_warning || confirm(`${i.approve_warning}\n\nApprove ${i.id} anyway?`)) && ckDo("/ui/api/task/approve", { project: i.project, id: i.id }));
  if (i.kind === "ask") { // the board shows a short title: Allow only when that title is the whole request
    for (const [d, text, title] of ckAskButtons(i).filter(([d]) => i.full !== false || d === "deny")) act(text, title, () => ckDo("/ui/api/chat/approve", { machine: i.machine, id: i.chat, ask: i.id, decision: d }));
    if (i.full === false) act("Open chat", "The request is longer than the list shows: read it whole in its chat", () => main.click());
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
    act("Reject", i.chat ? "Keep the branch, take the card off the list" : "Keep the branch, take the card off the list; optionally send it back to the agent", () => ckReject(card, i));
  }
  if (i.closable && i.can_close) { // past its date or a person's: the three answers, right here
    act("Finished", "It is done: remove the line from its memo (its history keeps it)", () => ckDo("/ui/api/task/done", { path: i.path, line: i.task_line }), "btn small primary");
    act("+7 days", "Still wanted: move its date a week from today", () => ckDo("/ui/api/task/extend", { project: i.project, id: i.id, days: 7 }));
    act("Cancel", "Not wanted: its requester gets it back as a result", () => {
      const why = prompt(`Cancel ${i.id}? Why (optional):`, "");
      if (why !== null) ckDo("/ui/api/task/cancel", { project: i.project, id: i.id, reason: why });
    }, "btn small quiet risky");
  }
  if (i.result) act("Open memo", "Read the whole line in its memo; remove it there (or under Tasks) once read", () => main.click());
  if (i.kind === "chat" && i.col === "needs") act("Reply", "Answer in one line without opening the chat (Enter sends, Esc closes)", () => ckReply(card, i), "btn small primary");
  if (ckSec(i) === "needs" && i.kind !== "ask") { // an approval is answered, not put off: its turn waits on it
    // Off the list for a while: it comes back at `until` (Unix seconds), or sooner when it changes.
    const tomorrow = () => { const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(8, 0, 0, 0); return d.getTime() / 1000; };
    for (const [text, until] of [["Snooze 1 hour", () => Date.now() / 1000 + 3600], ["Snooze until tomorrow 08:00", tomorrow]])
      later.push([text, "Take it off the list for a while", () => ckDo("/ui/api/board/dismiss", { key: i.key, sig: i.sig, until: Math.round(until()) })]);
    later.push(["Dismiss", "Take it off the list until it changes again", () => ckDo("/ui/api/board/dismiss", { key: i.key, sig: i.sig })]);
  }
  if (i.kind === "task" && !i.result && i.can_cancel) later.push(["Assign…", "Give it to another agent or person (owner@pc pins it to one pc)", () => {
    const to = (prompt(`${i.id} goes to (Claude, Codex, Gemini or a person's name; add @<pc> to pin it):`, i.owner || "") || "").trim();
    if (to) { const [owner, device = ""] = to.split("@"); ckDo("/ui/api/task/assign", { project: i.project, id: i.id, owner: owner.trim(), device: device.trim() }); }
  }]);
  if (i.result && me.writable?.includes(i.project) && i.line_text) later.push(["Remove (read)", "Take the result line out of its memo (its history keeps it)",
    () => ckDo("/ui/api/task/done", { path: i.path, line: i.line_text })]);
  if (i.kind === "task" && i.col === "open" && i.can_cancel) later.push(["Cancel task", "Not wanted: its requester gets it back as a result", () => {
    const why = prompt(`Cancel ${i.id}? Why (optional):`, "");
    if (why !== null) ckDo("/ui/api/task/cancel", { project: i.project, id: i.id, reason: why });
  }]);
  if (i.kind === "chat" && !ckBusy(i.status)) later.push(["Archive", "The agent records it in the memos, then it goes off the list (under Earlier, Archived); a new message opens it again",
    () => ckDo("/ui/api/chat/close", { machine: i.machine, id: i.id, closed: true })]);
  if (i.kind === "chat") later.push(["Force archive", "Close right away without wrap-up (e.g. when quota is exhausted or agent is stuck)",
    () => (!ckBusy(i.status) || confirm(FORCE_BUSY)) && ckDo("/ui/api/chat/close", { machine: i.machine, id: i.id, closed: true, force: true, record: false })]);
  if (i.kind === "chat" && ckBusy(i.status)) act("Stop", "Stop this turn", () => ckDo("/ui/api/chat/stop", { machine: i.machine, id: i.id }));
  if (later.length) acts.append(ckMore(later, i.title));
  if (acts.childNodes.length) card.append(acts);
  if (i.kind === "chat" && chat?.sub_list?.length) card.append(ckItemSubs(chat));
  return card;
}
// A row's less frequent actions in a small menu (⋯); Esc closes it and gives the focus back.
function ckMore(list, what) {
  const menu = el("details", "menu ck-snooze ck-rowmore"), sum = el("summary", "btn small quiet", "⋯"), box = el("div", "menu-list");
  sum.title = list.map(([t]) => t.replace(/ .*/, "")).filter((t, j, a) => a.indexOf(t) === j).join(", "); sum.setAttribute("aria-label", `More actions for ${what}`);
  for (const [text, title, fn] of list) {
    const b = el("button", null, text); b.type = "button"; b.title = title;
    b.onclick = () => { menu.open = false; fn(); };
    box.append(b);
  }
  menu.onkeydown = (e) => { if (e.key === "Escape") { e.stopPropagation(); menu.open = false; sum.focus(); } };
  menu.append(sum, box);
  return menu;
}
// A row's subagents in the list: folded to one line "N running" that unfolds (six of them made a phone row a page tall).
function ckItemSubs(chat) {
  const subs = el("ol", "ck-history ck-subs"), k = ckKey(chat), n = chat.sub_list.filter((s) => s.status === "running").length;
  if (!n) { subs.append(...ckSubRows(chat)); return subs; } // only finished ones: ckSubRows folds them to one line already
  const open = !!ck.itemSubsOpen?.has(k), li = el("li", "ck-subrow"), b = el("button", "btn small quiet ck-subfold", `${n} subagent${n > 1 ? "s" : ""} running ${open ? "▾" : "▸"}`);
  b.type = "button"; b.dataset.row = `subs:${k}`; b.setAttribute("aria-expanded", String(open));
  b.onclick = (e) => { e.stopPropagation(); ck.itemSubsOpen = ck.itemSubsOpen || new Set(); ck.itemSubsOpen[open ? "delete" : "add"](k); ckList(); };
  li.append(b); subs.append(li, ...(open ? ckSubRows(chat) : []));
  return subs;
}
// One line to a chat's agent from the list: Enter sends it as the chat's next message, Esc closes without sending.
function ckReply(card, i) {
  const old = card.querySelector(".ck-reply");
  if (old) { old.remove(); return ckList(); }
  const form = el("form", "ck-reply"), input = el("input", "small");
  input.placeholder = "Reply…"; input.maxLength = 20000; input.setAttribute("aria-label", `Reply to ${i.title}`);
  const shut = () => { form.remove(); card.querySelector(".ck-item-main")?.focus(); ckList(); };
  input.onkeydown = (e) => { if (e.key === "Escape" && !e.isComposing) { e.preventDefault(); e.stopPropagation(); shut(); } };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || input.disabled) return;
    input.disabled = true;
    if (await ckDo("/ui/api/chat/send", { machine: i.machine, id: i.id, text })) { toast(`Sent to ${i.title}`, false); form.remove(); }
    else { input.disabled = false; input.focus(); } // refused (the pc offline): the text stays
  };
  form.append(input); card.append(form); input.focus();
}

// A finished worker's branch, for review: what the pc counted, what the worker said it did, left and tested. The
// server runs nothing of it; Merge is a fixed action of that pc's companion, which refuses a dirty or other checkout.
function ckReview(i) {
  const box = el("span", "progress ck-review"), row = (k, v) => { const r = el("span", "ck-review-row"); r.append(el("b", null, k), " ", v); box.append(r); };
  if (i.commit) row("last commit", i.commit);
  if (i.done) row("done", i.done);
  if (i.left) row("left", i.left);
  if (i.uncommitted) row("uncommitted", `${i.uncommitted} file${i.uncommitted === 1 ? "" : "s"} never committed: not part of a merge, the worktree stays until they are gone`);
  if (!i.chat) row("tests", i.tests || "not reported by the worker");
  if (i.blocked) { const r = el("span", "ck-review-row ck-item-why"); r.append(el("b", null, "merge by hand"), " ", i.blocked); box.append(r); }
  if (i.files.length) box.append(el("code", "progress-log", i.files.join("\n") + (i.nfiles > i.files.length ? `\n… ${i.nfiles - i.files.length} more` : "")));
  return box;
}
async function ckMerge(i) {
  if (!confirm(`Merge ${i.branch} into ${i.into} on ${i.label}?\n\nIts companion runs git merge --no-ff in the main checkout, only when that is on ${i.into} and has no uncommitted changes. A conflict is aborted, nothing is pushed.`)) return;
  try {
    const { id } = await api("/ui/api/machines/action", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ machine: i.machine, action: "merge", args: { project: i.project, id: i.id, ...(i.chat ? { chat: true } : {}) } }) });
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
  reason.placeholder = i.chat ? "Why (optional)" : reason.placeholder;
  form.append(reason, ...(i.chat ? [] : [lab]), go);
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (back.checked && !reason.value.trim()) return toast("Sending it back needs a reason: the agent only gets that line.");
    go.disabled = true;
    try {
      await api("/ui/api/review/reject", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: i.key, reason: reason.value, send_back: back.checked }) });
      toast(back.checked ? `Sent ${i.task} back to ${AGENT_NAME[i.agent] || i.agent}` : `Review of ${i.task} rejected; the branch stays`, false);
    } catch (err) { toast(err.message); go.disabled = false; return; }
    form.remove(); // the list waits while a reason is being typed
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
  $("ck-diff-where").textContent = `${i.label} · ${i.chat ? i.branch : i.kind === "worker" || i.kind === "review" ? `worker ${i.id}` : i.folder}${i.git ? ` · ${ckGitText(i.git)}` : ""}`;
  say("muted", `Asking ${i.label}…`);
  if (!$("ck-diff-dialog").open) $("ck-diff-dialog").showModal();
  try {
    const { id } = await api("/ui/api/git/diff", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(!i.chat && (i.kind === "worker" || i.kind === "review") ? { machine: i.machine, worker: `${i.project}/${i.id}` } : { machine: i.machine, chat: i.id }) });
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

const CK_CMDS = Object.assign(new Map(), { seq: 0 }); // "machine/agent" -> {at, list: Promise}; seq: a box's list id
function ckCard(meta) {
  const k = ckKey(meta);
  if (ck.cards.has(k)) return ck.cards.get(k);
  const root = el("article", "ck-card"), head = el("header", "ck-head"), title = el("div", "ck-title");
  const name = el("strong"), where = el("span"), state = el("span", "ck-state"), x = el("button", "btn icon small quiet ck-x", "×");
  x.type = "button"; x.title = "Back to the list (the chat stays under Earlier)"; x.setAttribute("aria-label", "Back to the list");
  title.append(name, where);
  const git = el("span", "ck-git-slot"), open = el("a", "btn icon small quiet ck-x bd-only", "↗"); // on a grid card: the chat alone in a tab
  open.href = `#/chats/${encodeURIComponent(meta.id)}?solo=1`; open.target = "_blank"; open.rel = "noopener"; open.title = "Open in a tab of its own"; open.setAttribute("aria-label", "Open in a tab of its own");
  const home = el("a", "ck-home", "Knowledge"); home.href = "/ui"; home.title = "Knowledge: the home page"; // only a chat alone in its tab shows it
  const mark = el("span", "ck-mark", AGENT_MARK[meta.agent] || "+"), close = el("button", "btn small quiet ck-close", "Archive");
  const tok = el("span", "chip ck-tok"); tok.hidden = true;
  close.type = "button"; close.hidden = true;
  const headMeta = el("div", "ck-meta"); headMeta.append(git, tok); // its own line on a phone; inline on a wide screen
  const findBtn = el("button", "btn icon small quiet ck-findbtn"); findBtn.type = "button"; findBtn.innerHTML = CK_ICON.find;
  findBtn.dataset.shortcut = "find"; findBtn.title = "Find in this chat ({key})"; findBtn.setAttribute("aria-label", "Find in this chat");
  head.append(home, el("span", "ck-dot bd-only"), mark, title, headMeta, state, findBtn, close, open, x);
  const log = el("div", "ck-log"), form = el("form", "ck-form"), input = el("textarea"), btns = el("div", "ck-btns");
  log.setAttribute("aria-live", "polite"); log.setAttribute("role", "log");
  log.addEventListener("click", (e) => {
    const a = e.target.closest("a"); if (!a) return;
    const href = a.getAttribute("href") || "";
    if (/^https?:/.test(href)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
  });
  log.addEventListener("scroll", () => {
    if (log.scrollTop < 300 && card.older) ckOlder(card); // near the top of a long chat: the part before
    const dist = log.scrollHeight - log.scrollTop - log.clientHeight;
    card.stickToBottom = dist < 80;
    card.savedScrollTop = log.scrollTop;
    ckJump(card);
  }, { passive: true });
  // the distance to the end also changes without a scroll: an image loads, a block unfolds, the card gets another size
  const sized = new ResizeObserver(() => ckJump(card)); sized.observe(log);
  const mut = new MutationObserver((changes) => { for (const c of changes) for (const n of c.addedNodes) if (n.nodeType === 1) sized.observe(n); });
  mut.observe(log, { childList: true });
  const jump = el("div", "ck-jump"), jumpBtn = el("button", "btn small ck-jump-btn"), jumpText = el("span");
  jump.hidden = true; jumpBtn.type = "button";
  jumpBtn.append(jumpText, ckKeyEl("jump", "muted ck-jump-key", "{key}")); jump.append(jumpBtn);
  jumpBtn.onmousedown = (e) => e.preventDefault(); // a click leaves the focus in the text box
  jumpBtn.onclick = () => {
    if (document.activeElement === jumpBtn) input.focus(); // the button hides: reached by keyboard, the focus must land somewhere
    ckScrollBottom(card, true);
  };
  input.rows = 1; input.placeholder = "Message the agent…";
  const stop = el("button", "btn small ck-stop", "Stop"), send = el("button", "btn small primary ck-send", "Send"), note = el("p", "ck-note");
  // a live agent at work: Queue (after this turn) and Force (stop the step, then this) in a small menu beside Send
  const more = el("details", "menu up ck-more"), moreSum = el("summary", "btn small primary ck-send-more"), moreList = el("div", "menu-list");
  moreSum.title = "Queue or Force"; moreSum.setAttribute("aria-label", "Queue or Force this message");
  const queueBtn = el("button", "ck-queue"), forceBtn = el("button", "ck-forcebtn");
  queueBtn.append("Queue", ckKeyEl("queue", "muted", "after this turn · {key}")); forceBtn.append("Force", ckKeyEl("force", "muted", "stop the step, then this · {key}"));
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
  mic.type = "button"; mic.hidden = !ckMicOk(); mic.dataset.shortcut = "speak"; mic.title = "Speak (click to start and stop, or hold; {key}). The text lands in the box; you send it."; mic.setAttribute("aria-label", "Speak a message");
  // one box: the files, the text, and a bar under it (attach and model left; microphone, Stop and Send right)
  const box = el("div", "ck-box"), bar = el("div", "ck-bar"), barL = el("div", "ck-bar-l"), sendGroup = el("span", "ck-sendgroup");
  stop.type = "button"; send.type = "submit"; sendGroup.append(send, more);
  // a new chat only: its own git worktree and branch (chat/<id>) on the pc, so it never works in your checkout
  const branch = el("button", "btn small quiet ck-branch", "Own branch"); branch.type = "button"; branch.hidden = true;
  branch.title = "Work in its own git worktree on branch chat/<id>, merged from a Review card once closed. Left alone, a chat gets this by itself when another chat works in the folder or it has uncommitted files; pressed and released, it stays in the checkout";
  branch.setAttribute("aria-pressed", "false");
  branch.onclick = () => { card.worktree = !card.worktree; branch.setAttribute("aria-pressed", String(card.worktree)); };
  barL.append(attachBtn, filePick, picks, branch); btns.append(mic, stop, sendGroup); bar.append(barL, btns);
  box.append(files, input, bar); form.append(box, note);
  box.addEventListener("click", (e) => { if (e.target === box || e.target === bar || e.target === barL) input.focus(); });
  const asks = el("div", "ck-asks"); asks.hidden = true; asks.setAttribute("aria-live", "assertive");
  const subbar = el("div", "ck-subbar"); subbar.hidden = true; subbar.setAttribute("aria-label", "Subagents running now");
  const find = ckFindBar(); find.btn = findBtn;
  root.append(head, find.bar, log, jump, subbar, asks, form);
  const card = { meta: { ...meta }, x, openTab: open, root, find, name, where, git, state, log, jump, jumpText, asks, form, input, stop, send, note, picks, mark, close, tok, subbar, queueBtn, forceBtn, more, branch, filesBox: files, files: [], mode: "", pick: null, after: 0, online: meta.online !== false, stickToBottom: true, savedScrollTop: null };
  card.sized = sized; card.mut = mut;
  findBtn.onclick = () => (find.bar.hidden ? ckFindOpen(card) : ckFindClose(card));
  open.onclick = (e) => { if (me.prefs?.grid_open === "here" && !e.ctrlKey && !e.metaKey && !e.shiftKey) { e.preventDefault(); ckShow(card.meta); } }; // Settings
  ckSendHints(card);
  x.onclick = () => {
    if (card.meta.draft && bdOn()) { card.input.value = ""; ckDraftKeep(card); ckDrop(ckKey(CK_DRAFT)); return bdRender(); } // a draft on the grid: gone
    if (!solo()) store.set("kn_last_chat", ""); ckShow(null); // closed on purpose: the list stays empty
  };
  const cmds = el("ul", "ck-cmds"); cmds.hidden = true; cmds.id = `ck-cmds-${++CK_CMDS.seq}`; cmds.setAttribute("role", "listbox"); cmds.setAttribute("aria-label", "Slash commands");
  box.prepend(cmds); card.cmds = { list: cmds, items: [], sel: 0 };
  input.setAttribute("aria-autocomplete", "list"); input.setAttribute("aria-controls", cmds.id);
  input.addEventListener("keydown", (e) => { // the command list open: its keys first (registered before the box's own)
    if (cmds.hidden || e.isComposing || e.keyCode === 229) return; // 229: an Enter that ends a composition (Safari)
    const c = card.cmds, n = c.items.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { if (!n) return; e.preventDefault(); ckCmdSel(card, (c.sel + (e.key === "ArrowDown" ? 1 : n - 1)) % n); }
    else if ((e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.altKey) || e.key === "Tab") { if (!n || e.shiftKey) return; e.preventDefault(); ckCmdPick(card, c.items[c.sel]); }
    else if (e.key === "Escape") ckCmdClose(card);
    else return;
    e.stopImmediatePropagation();
  });
  input.addEventListener("keydown", (e) => {
    // Esc as its ×, also from this box, where an opened chat starts (the text stays as a draft; an attached file would not; the grid has its own Esc)
    if (e.key === "Escape" && !e.isComposing && document.body.classList.contains("in-chat") && !card.files.length && !document.querySelector("details[open], dialog[open]")) { e.preventDefault(); return x.click(); }
    // Ctrl/Cmd+Enter presses the button (Send, Queue or Force), and so does the send key of Settings; a phone's Enter stays a new line
    if (keyHit(e, "speak") && !(e.metaKey && e.key === " ") && !mic.hidden) { e.preventDefault(); return mic.toggle(); }
    if (keyHit(e, "queue") && !e.isComposing && !queueBtn.hidden) { e.preventDefault(); return queueBtn.click(); } // queue
    if (keyHit(e, "force") && !e.isComposing && !forceBtn.hidden) { e.preventDefault(); return forceBtn.click(); }
    if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return; // 229: Enter that ends a composition (Safari)
    const bare = ckSendKey() !== "Ctrl+Enter" && keyCombo(e) === ckSendKey();
    if (!bare && ((!e.ctrlKey && !e.metaKey) || e.altKey)) return; // Ctrl+Alt is AltGr on Windows: it must not send into the running turn
    e.preventDefault();
    if (!bare || input.value.trim() || card.files.length) form.requestSubmit(); // a stray Enter on an empty box must not Force: that stops the agent's step
  });
  const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 220)}px`; }; // the box grows with the text
  card.grow = grow;
  input.addEventListener("input", () => { grow(); ckPaint(card); ckDraftKeep(card); ckCmdShow(card); });
  input.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== input) ckCmdClose(card); }, 150)); // after a click on an item; back in the box by then, it stays // typing turns Force back into Queue
  queueMicrotask(() => { if (card.input.value === "") ckDraftShow(card); }); // after the caller's own setup of the card (a worker's meta, say)
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
        const pick = card.pick?.touched ? { model: card.pick.id, effort: card.pick.effort, ...(card.pick.agent !== card.meta.agent ? { agent: card.pick.agent } : {}) } : {};
        // mode: Send into the turn (a live agent) or after it (others); Queue or Force from their buttons
        await api("/ui/api/chat/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id, text, mode, ...pick }) });
        Object.assign(card.meta, pick); card.pick = null;
      }
      input.value = ""; input.style.height = ""; ckCmdClose(card); ckDraftKeep(card); card.files = []; ckFilesPaint(card); if (!card.worker) card.meta.status = "queued";
    } catch (err) { note.className = "ck-note bad"; note.textContent = err.message; }
    ckPaint(card); await ckPoll(card); clearTimeout(ck.timer); ck.timer = setTimeout(ckTick, 1500);
  };
  close.onclick = async (e) => { // archive: the agent records the chat in the memos, then it goes off the board and the list (Archived); a new message opens it again
    if (!e?.force && Date.now() - (close.forcedAt || 0) < 1000) return; // the click that ends a long press
    const closed = !card.meta.closed;
    const force = closed && (Boolean(e?.shiftKey) || Boolean(e?.force) || Boolean(card.failedWrap));
    if (force && ckBusy(card.meta.status) && !confirm(FORCE_BUSY)) return;
    try {
      const r = await api("/ui/api/chat/close", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id, closed, ...(force ? { force: true, record: false } : {}) }) });
      ck.summaryAt = 0;
      if (r.recording) { // the agent records the chat in the memos first; the server closes it when that turn ends
        toast("Recording this chat in the memos first; it is archived when the agent is done", false);
        card.meta.status = "queued"; ckPaint(card); await ckPoll(card); clearTimeout(ck.timer); ck.timer = setTimeout(ckTick, 1500); return;
      }
      card.meta.closed = closed;
      card.failedWrap = false;
      if (closed) {
        toast(force ? "Force archived without wrap-up" : "Archived: under Archived in the list; a new message opens it again", false);
        if (store.get("kn_last_chat") === card.meta.id) store.set("kn_last_chat", "");
        if (bdOn()) bdRender(); else ckShow(null); // the grid: only its card goes
      } else ckPaint(card);
      ckTick();
    } catch (err) { note.className = "ck-note bad"; note.textContent = err.message; }
  };
  // right-click or long press on Archive: a small menu whose Force archive is a second deliberate step
  // (iOS fires no contextmenu on a long press, hence the timer); Esc or a tap elsewhere closes it (popover)
  const forceClose = (e) => {
    e?.preventDefault?.();
    if (card.meta.closed || Date.now() - (close.forcedAt || 0) < 1000) return;
    close.forcedAt = Date.now();
    document.querySelector(".ck-forcepop")?.remove();
    const pop = el("div", "menu-list ck-forcepop"), go = el("button", "ck-force-go", "Force archive");
    pop.popover = "auto"; pop.tabIndex = -1; go.type = "button";
    pop.append(el("p", "ck-forcepop-why", "Close now, without the agent recording this chat in the memos."), go);
    go.onclick = () => { pop.hidePopover(); close.onclick({ force: true }); };
    pop.onkeydown = (k) => { if (k.key === "Escape") k.stopPropagation(); }; // the popover closes itself; the chat stays open
    pop.addEventListener("toggle", (t) => { if (t.newState === "closed") pop.remove(); });
    document.body.append(pop);
    const r = close.getBoundingClientRect();
    pop.style.top = `${Math.min(r.bottom + 6, innerHeight - 160)}px`;
    pop.style.left = `${Math.max(8, Math.min(r.right - 260, innerWidth - 268))}px`;
    pop.showPopover(); pop.focus(); // not the button: Enter does not archive
  };
  close.addEventListener("contextmenu", forceClose);
  close.addEventListener("pointerdown", (e) => { if (e.pointerType === "touch") close.hold = setTimeout(forceClose, 500); });
  for (const ev of ["pointerup", "pointercancel", "pointerleave"]) close.addEventListener(ev, () => clearTimeout(close.hold));
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
// Settings (#/settings): the person's own choices, kept on the server (/ui/api/me/prefs): the model a new chat starts
// with ("" = the one used last, from the models the pcs online now offer), the start page, how the grid opens a chat and groups its chats,
// the send key, the shortcuts. Theme and notifications are per browser. Only what use cannot teach belongs here (docs/web-ui.md).
// Ctrl+Enter always sends and these belong to the browser's text editing: a shortcut there would take them away.
const KEY_TAKEN = ["Ctrl+Enter", "Ctrl+A", "Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+Y", "Ctrl+Z"];
// Why this key cannot do this action, or "". A digit or arrow counts for the whole family: Alt+1 is taken by Alt+#.
function keyRefusal(action, combo) {
  const fam = keyFamily(combo), last = (k) => k.split("+").at(-1), want = last(keyDefault(action));
  if (!/\b(Ctrl|Alt)\+/.test(combo)) return "Use Ctrl or Alt with it";
  if (/Ctrl\+Alt\+/.test(combo)) return "Ctrl+Alt is AltGr on some keyboards: it types characters";
  if (last(combo) === "Dead") return "That key types an accent here: take another";
  if (/^(#|Arrow)$/.test(want) ? last(fam) !== want : fam !== combo) return /^(#|Arrow)$/.test(want) ? `This one needs ${want === "#" ? "a digit" : "an arrow"}` : "Digits and arrows are taken";
  if (KEY_TAKEN.includes(combo)) return `${combo} stays what it is`;
  const other = KEY_ACTIONS.find((a) => a[0] !== action && keyOf(a[0]) === fam);
  return other ? `${combo} is already: ${other[2]}` : "";
}
async function setKeysSave(action, combo) {
  const keys = { [action]: combo === keyDefault(action) ? null : combo };
  try {
    me.prefs = (await api("/ui/api/me/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keys }) })).prefs;
    keyPrefs = me.prefs.keys || {}; keyLabels();
    for (const c of ck.cards.values()) ckSendHints(c);
    setKeysShow();
    const saved = $("set-keys-saved"); saved.textContent = "Saved"; clearTimeout(saved.timer); saved.timer = setTimeout(() => (saved.textContent = ""), 2000);
  } catch (err) { toast(err.message, true); }
}
function setKeysShow() {
  $("set-keys").hidden = touch; // no keyboard to press them on
  $("set-keys-list").replaceChildren(...KEY_ACTIONS.map(([action, dflt, label, agents]) => {
    const row = el("div", `set-key${agents ? " ag-on" : ""}`), pick = el("button", "btn small", keyShow(action)), undo = el("button", "btn small quiet", "Default");
    pick.type = undo.type = "button"; pick.dataset.action = action; pick.setAttribute("aria-label", `${label}: ${keyShow(action)}. Press to change`);
    undo.hidden = keyOf(action) === dflt; undo.onclick = () => { const why = keyRefusal(action, dflt); if (why) toast(why, true); else setKeysSave(action, dflt); };
    pick.onclick = () => {
      pick.textContent = "Press the keys…";
      const stop = () => { removeEventListener("keydown", listen, true); pick.onblur = null; pick.textContent = keyShow(action); };
      const listen = (e) => { // capture: before the page's own keys
        e.preventDefault(); e.stopPropagation();
        if (/^(Control|Alt|AltGraph|Shift|Meta)$/.test(e.key) || e.isComposing) return;
        if (e.key === "Escape") return stop();
        const combo = keyFamily(keyCombo(e)), why = keyRefusal(action, combo);
        if (why) return toast(why, true);
        stop(); setKeysSave(action, combo);
      };
      addEventListener("keydown", listen, true);
      pick.onblur = stop; pick.focus();
    };
    row.append(el("span", null, label), pick, undo);
    return row;
  }));
}
async function loadSettings() {
  if (!ck.machines.length) await ckSummary().catch(() => {});
  const cat = ckCatalog(ck.machines.filter((m) => m.online));
  const key = (x) => `${x.agent}/${x.id}`;
  const setupRolePick = (selId, effId, now, emptyLabel = "auto (the agent's setting)") => {
    const sel = $(selId), eff = $(effId);
    if (!sel || !eff) return () => null;
    sel.replaceChildren(new Option(emptyLabel, ""));
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
    return () => {
      if (!sel.value) return null;
      const [agent, ...id] = sel.value.split("/");
      return { agent, id: id.join("/"), effort: eff.value };
    };
  };
  const getLead = setupRolePick("set-model", "set-effort", me.prefs?.chat_default, "The one I used last");
  const getReader = setupRolePick("set-reader-model", "set-reader-effort", me.prefs?.chat_reader);
  const getReview = setupRolePick("set-review-model", "set-review-effort", me.prefs?.chat_review);
  for (const [id, key, dflt] of [["set-start", "start_page", "chats"], ["set-grid", "grid_open", "tab"], ["set-group", "grid_group", "project"], ["set-send", "send_key", "ctrl"], ["set-live-typing", "live_typing", "on"]]) {
    const pick = $(id); pick.value = me.prefs?.[key] || dflt;
    pick.onchange = async () => {
      try {
        me.prefs = (await api("/ui/api/me/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [key]: pick.value }) })).prefs;
        for (const c of ck.cards.values()) ckSendHints(c);
        $("set-use-saved").textContent = "Saved"; clearTimeout(pick.savedTimer); pick.savedTimer = setTimeout(() => ($("set-use-saved").textContent = ""), 2000);
      } catch (err) { pick.value = me.prefs?.[key] || dflt; toast(err.message, true); }
    };
  }
  $("set-start").querySelector('[value="chats"]').hidden = agentsOff();
  $("set-start").querySelector('[value="tasks"]').hidden = !agentsOff(); // with Agents, tasks are there
  const mx = me.matrix; // null for a non-admin: only admins get the server's messages
  $("set-matrix").hidden = !mx;
  if (mx) {
    // filled with what is used now; the server's own value is saved as "" so a later .env change still reaches you
    $("set-matrix-url").value = me.prefs?.matrix_url || mx.url; $("set-matrix-url").placeholder = "https://matrix.example.org";
    $("set-matrix-room").value = me.prefs?.matrix_room || mx.room; $("set-matrix-room").placeholder = "!abc123:example.org";
    const tokenHint = () => ($("set-matrix-token").placeholder = me.matrix.own_token ? "saved (type to replace)" : "the server's bot"); tokenHint();
    $("set-matrix-token").value = ""; $("set-matrix-token-clear").checked = false; $("set-matrix-token-clear").hidden = !mx.own_token;
    $("set-matrix").onsubmit = async (e) => {
      e.preventDefault();
      const own = (v, dflt) => (v === dflt ? "" : v), url = $("set-matrix-url").value.trim().replace(/\/+$/, "");
      const body = { matrix_url: own(url, mx.url.replace(/\/+$/, "")), matrix_room: own($("set-matrix-room").value.trim(), mx.room) }, tok = $("set-matrix-token").value.trim();
      if (tok) body.matrix_token = tok; else if ($("set-matrix-token-clear").checked) body.matrix_token = "";
      try {
        me.prefs = (await api("/ui/api/me/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).prefs;
        if ("matrix_token" in body) me.matrix.own_token = !!tok;
        $("set-matrix-token").value = ""; $("set-matrix-token-clear").checked = false; $("set-matrix-token-clear").hidden = !me.matrix.own_token; tokenHint();
        $("set-matrix-saved").textContent = "Saved"; clearTimeout(e.target.savedTimer); e.target.savedTimer = setTimeout(() => ($("set-matrix-saved").textContent = ""), 2000);
      } catch (err) { toast(err.message, true); }
    };
    $("set-matrix-test").onclick = async () => {
      try { await api("/ui/api/me/matrix_test", { method: "POST" }); toast("Test message sent", false); } catch (err) { toast(err.message, true); }
    };
  }
  setKeysShow();
  $("set-theme").onchange = () => setTheme($("set-theme").value);
  showTheme();
  const on = $("set-notify-on"), note = $("set-notify-note");
  on.checked = notifyOn(); on.disabled = !window.Notification;
  if (!window.Notification) note.textContent = "This browser has no notifications (on a phone: Matrix, admin page Settings: needs_per_hour).";
  on.onchange = async () => {
    if (on.checked && Notification.permission !== "granted") on.checked = (await Notification.requestPermission()) === "granted";
    if (!on.checked && Notification.permission === "denied") toast("Notifications are blocked for this site in the browser settings", true);
    store.set("kn_notify", on.checked ? "1" : "0");
    $("set-notify-saved").textContent = "Saved"; clearTimeout(on.savedTimer); on.savedTimer = setTimeout(() => ($("set-notify-saved").textContent = ""), 2000);
  };
  $("set-chat").onsubmit = async (e) => {
    e.preventDefault();
    const chat_default = getLead(), chat_reader = getReader(), chat_review = getReview();
    try {
      const res = await api("/ui/api/me/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chat_default, chat_reader, chat_review }) });
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
  card.input.dataset.hint = "Message this subagent (its main agent passes it on)…"; ckSendHints(card);
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
function ckDrop(k) {
  const c = ck.cards.get(k);
  if (c) { c.sized?.disconnect(); c.mut?.disconnect(); }
  ck.cards.delete(k); if (ck.view === k) ck.view = "";
}
const CK_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]; // the server's EFFORTS, in order
// The model picker: one chip under the box ("Opus · high ▾"), a panel with the models grouped by family and the chosen
// model's effort levels. Picking a model sets its own default effort; the agent (Claude Code, Codex, agy) follows from
// the model. In a running chat another agent's model continues the chat there (when its own limit is used up): the
// server hands the new agent the chat so far as text, and it starts its own session.
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
  const draft = card.meta.draft, mine = ckPc(card.meta.machine);
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
      b.setAttribute("aria-pressed", String(m === x));
      b.title = other ? `${AGENT_NAME[m.agent] || m.agent}: continue this chat there (it gets the chat so far and starts its own session)` : m.id;
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
// Esc closes the model panel and the Snooze menu (a click outside: nav.js, for every menu); a click or Esc drops the
// mark a link put on an item.
function ckUnfold(target) {
  if (!target) for (const d of document.querySelectorAll("details.ck-model[open], details.ck-snooze[open]")) d.open = false;
  const hit = document.querySelector("#ck-list .ck-item.hit");
  if (hit && (!target || !hit.contains(target))) { hit.classList.remove("hit"); ck.hit = ""; }
}
document.addEventListener("click", (e) => ckUnfold(e.target));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !document.querySelector("dialog[open]")) ckUnfold(null); });

// A new chat is a draft in the chat pane (#/chats/new): an empty box with the model chip and the folder; the chat is
// made on the first message. The project is the sidebar's; the pc follows from the model and the folder.
const CK_DRAFT = { machine: "draft", id: "new", draft: true, agent: "", title: "", folder: "", label: "", status: "idle" };
function ckDraftPcs() { // own and shared pcs online with a companion that reports models and has a linked folder in the project
  return [...ck.machines, ...(ck.shared || [])].filter((m) => m.online && (m.companion?.agents || []).length
    && ckLinked(m).some((f) => !agentProject || f.project === agentProject));
}
function ckDraftFolders(card) { // [{machine, label, path, project}] that can run the chosen model
  const x = ckModelOf(card), pcs = ckDraftPcs().filter((m) => x ? x.pcs.includes(m.id) : (m.companion.agents || []).includes(card.pick?.agent));
  return pcs.flatMap((m) => ckLinked(m).filter((f) => !agentProject || f.project === agentProject)
    .map((f) => ({ machine: m.id, label: m.own ? m.label : `${m.label} (shared)`, path: f.path, project: f.project })));
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
  const [machine, folder] = (card.where_ || "").split("\n"), m = ckPc(machine);
  const project = ckDraftFolders(card).find((f) => `${f.machine}\n${f.path}` === card.where_)?.project ?? agentProject ?? ""; // its frame on the grid
  card.meta = { ...card.meta, machine: "draft", agent: card.pick?.agent || "", folder: folder || "", label: m?.label || "", target: machine || "", project };
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
    else { card.note.className = "ck-note bad"; card.note.textContent = `${ckPc(machine)?.label || "That pc"} has no folder linked to ${agentProject ? shown(agentProject) : "a project"}: pick another project in the sidebar, or link one under Machines.`; }
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
    const res = await api("/ui/api/chats", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine, agent: p.agent, folder, ...(card.worktree === undefined ? {} : { worktree: card.worktree }) }) });
    const m = ckPc(machine);
    let failed = "";
    try {
      await api("/ui/api/chat/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine, id: res.chat.id, text, model: p.id, effort: p.effort }) });
      store.set("kn_chat_pick", JSON.stringify({ agent: p.agent, id: p.id, effort: p.effort, machine, folder }));
    } catch (err) { failed = err.message; } // the chat exists now: go there with the text kept, never make another
    card.input.value = ""; ckDraftKeep(card); ckDrop(ckKey(CK_DRAFT)); ck.summaryAt = 0;
    const chat = ckCard({ ...res.chat, ...(failed ? {} : { model: p.id, effort: p.effort, status: "queued" }), machine, label: m?.label || machine, online: true });
    if (failed) { chat.input.value = text; ckDraftKeep(chat); chat.note.className = "ck-note bad"; chat.note.textContent = failed; }
    if (bdOn()) { // on the grid the chat stays a card there; the next summary brings it from the server
      const raised = Date.now() / 1000; // your own new chat is the one move that is not held
      ck.chats.unshift({ ...chat.meta, raised }); bd.held.set(ckKey(chat.meta), raised); if (bd.part) bdPartMove(ckKey(chat.meta), bd.part); bdRender();
      if (!touch) chat.input.focus();
    } else go(`#/chats/${encodeURIComponent(chat.meta.id)}`, true); // Back from it goes to the list, not to an empty draft
    ckTick();
  } finally { card.starting = false; }
}
// A chat without a title yet (its first turn still running) goes by its first message.
const ckTitle = (m, card) => m.title || card?.log.querySelector(".ck-msg.user")?.textContent.trim().slice(0, 60) || "New chat";
// Slash commands: "/" at the start of the box lists what the chat's agent offers on its pc (its skills and commands,
// reported by the companion from the agent itself); a pick fills the box and the agent runs the text its own way.
function ckCmdList(card) {
  const machine = card.meta.draft ? (card.where_ || "").split("\n")[0] : card.meta.machine;
  const agent = card.meta.draft ? card.pick?.agent : card.meta.agent;
  if (!machine || !agent || card.worker || card.meta.parent) return Promise.resolve(null); // a worker or subagent: not its own box
  const k = `${machine}/${agent}`, hit = CK_CMDS.get(k);
  if (hit && Date.now() - hit.at < 60000) return hit.list;
  const list = api(`/ui/api/chat/commands?machine=${encodeURIComponent(machine)}&agent=${encodeURIComponent(agent)}`).then((r) => Object.assign(r.commands || [], { version: r.version || "" })).catch(() => { CK_CMDS.delete(k); return []; });
  CK_CMDS.set(k, { at: Date.now(), list });
  return list;
}
async function ckCmdShow(card) {
  const m = /^\/(\S*)$/.exec(card.input.value), c = card.cmds;
  if (!m) return ckCmdClose(card);
  const all = await ckCmdList(card);
  if (all === null || !/^\/\S*$/.test(card.input.value)) return ckCmdClose(card); // typed on meanwhile
  const q = m[1].toLowerCase(), name = (x) => x.name.toLowerCase();
  c.items = [...all.filter((x) => name(x).startsWith(q)), ...all.filter((x) => !name(x).startsWith(q) && name(x).includes(q))].slice(0, 50);
  c.list.replaceChildren();
  c.items.forEach((x, i) => {
    const li = el("li", "pal-opt"); li.id = `${c.list.id}-${i}`; li.setAttribute("role", "option");
    li.append(el("span", "pal-label", `/${x.name}${x.hint ? ` ${x.hint}` : ""}`), el("small", null, x.description));
    li.onmousedown = (e) => e.preventDefault(); // the focus stays in the box
    li.onclick = () => ckCmdPick(card, x);
    c.list.append(li);
  });
  if (!c.items.length) { const li = el("li", "muted ck-cmds-none", all.length ? "No command matches."
    : all.version && versionNewer("0.43.0", all.version) ? `This pc runs plugin ${all.version}; its commands show from 0.43.0 on: update it under Machines, then start a chat there.`
    : "No commands known yet: they show once this agent has started a chat on this pc."); li.setAttribute("role", "presentation"); c.list.append(li); }
  c.list.hidden = false;
  ckCmdSel(card, 0);
}
function ckCmdSel(card, i) {
  const c = card.cmds; c.sel = i;
  [...c.list.querySelectorAll("[role=option]")].forEach((li, j) => li.setAttribute("aria-selected", String(j === i)));
  const on = c.list.querySelector(`[aria-selected="true"]`);
  if (on) { card.input.setAttribute("aria-activedescendant", on.id); on.scrollIntoView({ block: "nearest" }); } else card.input.removeAttribute("aria-activedescendant");
}
function ckCmdPick(card, x) {
  card.input.value = `/${x.name} `; ckCmdClose(card); card.input.focus(); card.grow(); ckDraftKeep(card); ckPaint(card);
}
function ckCmdClose(card) {
  if (!card.cmds || card.cmds.list.hidden) return;
  card.cmds.list.hidden = true; card.cmds.items = [];
  card.input.removeAttribute("aria-activedescendant");
}

function ckPaint(card) {
  if (card.worker) return ckWorkerPaint(card);
  if (card.meta.parent) return; // a subagent's chat: painted by its poll (ckSubPoll)
  const m = card.meta, busy = ckBusy(m.status);
  setText(card.mark, AGENT_MARK[m.agent] || "+");
  if (m.draft) {
    setText(card.name, "New chat");
    setText(card.where, m.folder ? `${AGENT_NAME[m.agent] || m.agent} · ${m.label} · ${ckShort(m.folder)}` : "");
    card.where.title = m.folder || ""; card.state.hidden = true; card.stop.hidden = true; card.send.textContent = "Send"; card.send.disabled = !!card.starting;
    card.branch.hidden = false;
    if (!card.note.classList.contains("bad")) card.note.textContent = !ckDraftPcs().length
      ? (agentProject ? `No pc of yours is online with a folder linked to ${shown(agentProject)}.` : "None of your pcs is online with a companion and a linked folder.")
      : "Runs on your pc in the project's folder, with a worker's permissions there (YOLO only where you turned it on).";
    return;
  }
  card.state.hidden = false;
  const before = Object.entries(m.used_by || {}); // agents this chat ran in before (continued elsewhere): each its own count
  card.tok.hidden = !m.used && !m.ctx && !before.length; // the conversation's tokens: its context now and all it used
  setText(card.tok, [m.ctx && `${ckNum(m.ctx)} context`, m.used && `${ckNum(m.used)} used${before.length ? ` (${AGENT_NAME[m.agent] || m.agent})` : ""}`,
    ...before.map(([a, n]) => `${ckNum(n)} (${AGENT_NAME[a] || a})`)].filter(Boolean).join(" · "));
  card.tok.title = "Context: what the agent sent for its last step. Used: every token of this chat so far, cache reads included"
    + (before.length ? "; per agent, since the chat continued in another one." : ".");
  card.close.hidden = busy && !card.failedWrap;
  if (card.failedWrap && !m.closed) {
    card.close.textContent = "Force archive";
    card.close.className = "btn small bad ck-close";
    card.close.title = "Wrap-up failed: click to force archive without wrap-up (or Shift+click)";
  } else {
    card.close.textContent = m.closed ? "Open again" : "Archive";
    card.close.className = "btn small quiet ck-close";
    card.close.title = m.closed ? "Back in the list" : "The agent records this chat in the memos, then it goes off the list (under Earlier, Archived); Shift-click to force archive without wrap-up; right-click or long press for a menu with it";
  }
  setText(card.name, ckTitle(m, card));
  card.branch.hidden = true;
  setText(card.where, `${AGENT_NAME[m.agent] || m.agent} · ${m.label} · ${ckShort(m.folder)}${m.project ? ` (${m.project})` : ""}${m.worktree ? ` · branch chat/${m.id}` : ""}`);
  card.where.title = m.folder || "";
  const item = (ck.board || []).find((i) => i.kind === "chat" && `${i.machine}/${i.id}` === ckKey(m));
  if (card.git.dataset.v !== JSON.stringify(item?.git || null)) {
    card.git.dataset.v = JSON.stringify(item?.git || null); card.git.replaceChildren(...(item?.git ? [ckGitPill(item)] : []));
  }
  card.state.className = `ck-state ${!card.online ? "offline" : busy ? "running" : ""}`;
  setText(card.state, !card.online ? "pc offline" : m.status === "queued" ? "waiting for the pc" : busy ? "working" : "ready");
  card.root.classList.toggle("running", busy);
  // while the agent answers, a message waits in line (the server keeps up to 5) and goes in when this turn ends
  // with messages waiting and nothing typed the button forces: the turn stops and the first waiting message goes in
  const force = busy && m.status === "running" && m.waiting > 0 && !card.input.value.trim();
  const live = ckLive(m), typed = !!card.input.value.trim();
  card.stop.hidden = !busy; card.send.disabled = !card.online; card.send.classList.toggle("force", force);
  card.queueBtn.hidden = card.forceBtn.hidden = card.more.hidden = !(live && busy && typed); // a live agent at work: three ways in
  card.send.classList.toggle("split", !card.more.hidden);
  setText(card.send, force ? "Force" : busy && !live ? "Queue" : "Send");
  card.send.title = force ? `Send the first of ${m.waiting} waiting message${m.waiting > 1 ? "s" : ""} into the turn${live ? ": its step stops and it goes in" : ": the agent reads it after its next step"} (Stop ends the turn)`
    : busy && live ? (m.agent === "agy" ? `Right after this turn, in the same agy (it takes no message mid-turn) (${ckSendKey()})` : `Into this turn: the agent takes it in as it works (${ckSendKey()})`)
    : busy ? "The agent gets this when its current turn ends" : "";
  if (!card.note.classList.contains("bad") || busy) {
    card.note.className = "ck-note";
    setText(card.note, !card.online ? `${m.label} is offline${card.at ? ` since ${ago(card.at)}` : ""}: a chat needs its companion running.` : "");
  }
}
// A chat on a pc shared with you: Join (both steer it, it stays theirs) or Take over (it becomes yours); Leave when you
// joined. One small bar above the box, redrawn only when what it offers changes.
function ckShareBar(card, ro) {
  const me_ = me.user, joined = (card.meta.joined || []).includes(me_);
  const want = ro && card.meta.takeover ? "offer" : joined ? "joined" : "";
  if (want === (card.shareWant || "")) return;
  card.shareWant = want; card.shareBar?.remove(); card.shareBar = null;
  if (!want) return;
  const bar = el("div", "ck-sharebar"), post = (path, body, then) => async () => {
    try { await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id, ...body }) }); }
    catch (e) { toast(e.message); return; }
    then?.(); ck.summaryAt = 0; ckPoll(card);
  };
  const btn = (text, title, fn, cls = "btn small") => { const b = el("button", cls, text); b.type = "button"; b.title = title; b.onclick = fn; bar.append(b); };
  if (want === "offer") {
    btn("Join", "Work in this chat together: you both steer it and every message names its writer; it stays theirs", post("/ui/api/chat/join", {}), "btn small primary");
    btn("Take over", "Make this chat yours: the same conversation goes on here, on that pc, under your share; its owner still sees it",
      () => confirm(`Take over "${card.meta.title || "this chat"}" from ${card.meta.by || "its owner"}?\n\nIts running turn stops; the conversation goes on as yours on ${card.meta.label}, with your share's settings. When the share ends it goes back.`)
        && post("/ui/api/chat/take", {})());
  } else {
    bar.append(el("span", "muted", `You are in ${card.meta.by || "their"}'s chat`));
    btn("Leave", "Stop steering this chat; you can still read it", post("/ui/api/chat/join", { leave: true }));
  }
  card.shareBar = bar; card.form.prepend(bar);
}
function ckTypingLine(card) { // "<name> is typing…" from the others in this chat, and what they type when they share it
  const who = card.meta.typing || [], drafts = card.meta.drafts || {};
  const text = who.length ? `${who.join(", ")} ${who.length > 1 ? "are" : "is"} typing…` : "";
  if (!card.typingEl) {
    card.typingEl = el("div", "ck-typing muted"); card.typingEl.setAttribute("aria-live", "polite");
    card.draftsEl = el("div", "ck-drafts"); card.form.prepend(card.draftsEl, card.typingEl);
  }
  if (card.typingEl.textContent !== text) card.typingEl.textContent = text;
  card.typingEl.hidden = !text;
  const sig = JSON.stringify(drafts);
  if (card.draftsSig !== sig) { // live: the other one's box as they type it (Settings: Show what I type)
    card.draftsSig = sig;
    card.draftsEl.replaceChildren(...Object.entries(drafts).map(([u, t]) => {
      const d = el("div", "ck-draft-live"); d.append(el("span", "ck-by", u), el("span", null, t.length > 600 ? `…${t.slice(-600)}` : t)); return d;
    }));
  }
  card.draftsEl.hidden = !Object.keys(drafts).length;
  if (!card.typingHooked) { // say you type, only in a chat someone else may see: with the text at most every second, else every 3 s
    card.typingHooked = true;
    const send = () => {
      card.typedAt = Date.now(); clearTimeout(card.typeTimer);
      const live = (me.prefs?.live_typing || "on") === "on";
      api("/ui/api/chat/typing", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id, ...(live ? { text: card.input.value } : {}) }) }).catch(() => {});
    };
    card.input.addEventListener("input", () => {
      if (!card.meta.watched || card.meta.rights === "view" || card.meta.draft) return;
      const gap = (me.prefs?.live_typing || "on") === "on" ? 1000 : 3000, wait = gap - (Date.now() - (card.typedAt || 0));
      clearTimeout(card.typeTimer);
      if (wait <= 0) send(); else if (gap === 1000) card.typeTimer = setTimeout(send, wait); // the last words go too
    });
  }
}
async function ckPoll(card) {
  if (!card) return;
  if (card.worker) return ckWorkerPoll(card);
  if (card.meta.parent) return ckSubPoll(card);
  if (card.meta.draft) return ckPicker(card), ckDraftWhere(card);
  let res;
  // only the chat opened full and in view counts as read; a card glanced at on the board, or a tab in the background, stays under Needs you
  const peek = ck.view !== ckKey(card.meta) || document.hidden ? "&peek=1" : "";
  // a long chat opens on its newest part; the rest loads when you scroll up (ckOlder)
  const tail = card.after ? "" : `&tail=${CK_PAGE}`;
  try { res = await api(`/ui/api/chat?machine=${card.meta.machine}&id=${card.meta.id}&after=${card.after}${tail}${peek}`); }
  catch (e) { card.note.className = "ck-note bad"; card.note.textContent = e.message; return; }
  if (!card.after) { card.older = res.older; card.first = res.events[0]?.n || 0; ckOlderMark(card); }
  card.meta = { ...card.meta, ...res.chat, label: res.label }; card.online = res.online; card.at = res.at;
  const ro = card.meta.rights === "view"; // a chat of someone else on a pc shared with you: read only, unless you take it over
  if (ro !== !!card.readOnly) {
    card.readOnly = ro; card.input.disabled = ro;
    card.input.placeholder = ro ? "Read only: this chat is someone else's." + (card.meta.takeover ? " Take over to go on in it yourself." : "") : "Message the agent…";
    if (!ro) ckSendHints(card);
  }
  ckShareBar(card, ro);
  ckTypingLine(card);
  ckPicker(card);
  ckEvents(card, res);
  if (!card.meta.subs) ckSubsEnded(card); // the server knows none runs (its turn ended, or it was lost): no block says running
  ckSubbar(card); // its background shells come with the chat's meta
  ckAsks(card, res.asks || [], (res.now || Date.now() / 1000) - Date.now() / 1000);
  ckPaint(card);
}
const CK_ICON = { // inline, in the text colour
  find: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/></svg>',
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
// longer than a moment, letting go stops (push to talk). While the browser asks for the microphone (the first time a
// prompt) presses are ignored, so no second recorder starts; a release in that time stops right after the start.
const ckMicOk = () => !!(me.stt && window.MediaRecorder && navigator.mediaDevices?.getUserMedia);
function ckMic(card, btn) {
  let rec = null, timer = null, starting = false, stopSoon = false;
  let held = 0, startX = 0, startY = 0, holdTimer = null, isHold = false, cancelled = false;
  const label = () => { const s = Math.round((Date.now() - rec.t0) / 1000); btn.textContent = `● ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; if (s >= 300) stop(); };
  const say = (msg) => { card.note.className = "ck-note bad"; card.note.textContent = msg; };
  async function start() {
    if (starting || rec) return;
    let stream;
    starting = true; stopSoon = false;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { return say(`Microphone: ${e.name === "NotAllowedError" ? "not allowed (site settings, or the server's Permissions-Policy: docs/voice.md)" : e.message}`); }
    finally { starting = false; }
    rec = new MediaRecorder(stream); rec.t0 = Date.now(); const parts = [];
    rec.ondataavailable = (e) => e.data.size && parts.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop()); clearInterval(timer);
      const wasRec = rec; rec = null;
      btn.classList.remove("rec");
      if (wasRec?.discarded || (Date.now() - wasRec.t0 < 600 && !parts.some((p) => p.size > 2000))) {
        btn.innerHTML = CK_ICON.mic; btn.disabled = false; return;
      }
      btn.textContent = "…"; btn.disabled = true;
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
    if (stopSoon) stop();
  }
  const stop = () => { if (starting) stopSoon = true; else if (rec && rec.state === "recording") rec.stop(); };
  btn.toggle = () => (starting ? undefined : rec ? stop() : start());
  btn.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || starting) return;
    if (rec) { e.preventDefault(); stop(); return; }
    startX = e.clientX; startY = e.clientY; cancelled = false; isHold = false; held = Date.now();
    if (e.pointerType !== "touch") {
      e.preventDefault();
      holdTimer = setTimeout(() => { isHold = true; start(); }, 300);
    } else {
      holdTimer = setTimeout(() => {
        if (!cancelled && !rec) {
          isHold = true; start();
          try { navigator.vibrate?.(40); } catch (_) {}
        }
      }, 350);
    }
  });
  btn.addEventListener("pointermove", (e) => {
    if (!held || cancelled) return;
    if (Math.hypot(e.clientX - startX, e.clientY - startY) > 10) {
      cancelled = true; clearTimeout(holdTimer);
      if (isHold && rec) { rec.discarded = true; stop(); }
    }
  });
  const abort = () => {
    clearTimeout(holdTimer); cancelled = true;
    if (isHold && rec) { rec.discarded = true; stop(); }
    held = 0;
  };
  btn.addEventListener("pointercancel", abort);
  btn.addEventListener("pointerup", (e) => {
    clearTimeout(holdTimer);
    if (cancelled || !held) { held = 0; return; }
    const duration = Date.now() - held; held = 0;
    if (isHold) { stop(); return; }
    if (e.pointerType === "touch" && duration < 120) return;
    btn.toggle();
  });
  btn.addEventListener("click", (e) => { if (e.detail === 0) btn.toggle(); }); // the keyboard (Enter/Space on the button)
}
// A message of the owner, or a tool line naming an image the agent looked at: its text, each file a thumbnail
// (images) or a download link.
function ckUserMsg(card, text, box = el("div", "ck-msg user"), by = "") {
  let at = 0;
  if (by && by !== me.user) box.append(el("span", "ck-by", by)); // someone else wrote it: a chat two people share
  for (const m of text.matchAll(CK_REF)) {
    box.append(text.slice(at, m.index)); at = m.index + m[0].length;
    const a = el("a", "ck-file"), url = `/ui/api/chat/file?machine=${encodeURIComponent(card.meta.machine)}&file=${encodeURIComponent(`${m[1]}/${m[2]}`)}`;
    a.href = CK_IMG.test(m[2]) ? `/ui/images?machine=${encodeURIComponent(card.meta.machine)}&id=${encodeURIComponent(card.meta.parent || card.meta.id)}&file=${encodeURIComponent(`${m[1]}/${m[2]}`)}` : url;
    a.target = "_blank"; a.rel = "noopener";
    if (CK_IMG.test(m[2])) { const img = el("img", "ck-img"); img.src = url; img.alt = m[2]; img.loading = "lazy"; a.append(img); }
    else { a.textContent = m[2]; a.download = m[2]; }
    box.append(a);
  }
  box.append(text.slice(at));
  return box;
}
function ckLine(card, e) {
  if (e.kind === "tool" && e.text.includes("⟦file:")) return ckUserMsg(card, e.text, el("div", "ck-line tool"));
  const line = el("div", `ck-line ${e.kind}`, e.text);
  if (e.kind === "info" && (e.text || "").startsWith("Not archived:")) {
    card.failedWrap = true;
    const btn = el("button", "btn small bad", "Force archive");
    btn.type = "button";
    btn.style.marginLeft = "8px";
    btn.title = "Archive immediately without recording turn (e.g. when quota is exhausted)";
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api("/ui/api/chat/close", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ machine: card.meta.machine, id: card.meta.id, closed: true, force: true, record: false }),
        });
        toast("Force archived without wrap-up", false);
        card.meta.closed = true;
        card.failedWrap = false;
        if (store.get("kn_last_chat") === card.meta.id) store.set("kn_last_chat", "");
        if (bdOn()) bdRender(); else ckShow(null);
        ckTick();
      } catch (err) {
        toast(err.message, true);
        btn.disabled = false;
      }
    };
    line.append(btn);
  }
  return line;
}
// New events into a card's log: text as sanitized Markdown, the rest as lines; never one twice.
const CK_PAGE = 200; // events a chat opens with, and loads per step when you scroll up
function ckEvents(card, res) {
  if (res.trimmed && !card.after && !res.older && res.events.length) card.log.append(el("p", "ck-line info", "Older output was trimmed (the chat keeps its newest part)."));
  const near = card.stickToBottom !== false || (card.log.scrollHeight - card.log.scrollTop - card.log.clientHeight < 120);
  const dirty = new Set(card.held ? [card.held] : []); // a long answer comes in many pieces: its Markdown is rendered once, at the end
  card.held = null;
  for (const e of res.events) {
    if (e.n <= card.after) continue; // two polls crossed (board + chat): never append an event twice
    card.after = Math.max(card.after, e.n);
    const last = card.log.lastElementChild;
    if (e.sub) { ckSub(card, e); continue; }
    if (e.kind === "text") { // Markdown, sanitized as a note is (DOMPurify): an agent's output never becomes script
      const msg = last && last.classList.contains("agent") ? last : card.log.appendChild(el("div", "ck-msg agent md"));
      msg.dataset.raw = (msg.dataset.raw || "") + e.text;
      dirty.add(msg);
    } else {
      if (e.kind === "user") card.failedWrap = false; // a new message or a retried Archive: only the newest wrap-up counts
      card.log.append(e.kind === "user" ? ckUserMsg(card, e.text, undefined, e.by) : ckLine(card, e));
    }
  }
  for (const msg of dirty) if (selecting(msg)) card.held = msg; else msg.innerHTML = renderMd(msg.dataset.raw.replace(/^\n+/, "")); // text being selected waits for the next poll
  if (res.events.length) {
    const hasUser = res.events.some((e) => e.kind === "user");
    if (near || hasUser) ckScrollBottom(card, hasUser);
    if (!hasUser && card.stickToBottom === false) { card.fresh = true; ckJump(card); }
  }
}

// Scrolling up in a long chat: the CK_PAGE events before the oldest shown are rendered apart (their own subagent
// blocks) and put on top, the view kept where it was.
function ckOlderMark(card) {
  let m = card.log.querySelector(":scope > .ck-older");
  if (!card.older) return m?.remove();
  if (!m) {
    m = el("button", "btn small quiet ck-older", "Earlier messages"); m.type = "button";
    m.onclick = () => ckOlder(card); card.log.prepend(m);
  }
}
async function ckOlder(card) {
  if (!card.older || card.loadingOlder || !card.first) return;
  card.loadingOlder = true;
  try {
    const res = await api(`/ui/api/chat?machine=${card.meta.machine}&id=${card.meta.id}&before=${card.first}&tail=${CK_PAGE}&peek=1`);
    const part = el("div"), shadow = { ...card, log: part, subs: new Map(), subbar: el("div"), after: 0, stickToBottom: false, jump: null };
    ckEvents(shadow, { ...res, trimmed: false });
    const log = card.log, keep = log.scrollHeight - log.scrollTop, mark = log.querySelector(":scope > .ck-older");
    (mark ? mark.after.bind(mark) : log.prepend.bind(log))(...part.childNodes);
    log.scrollTop = log.scrollHeight - keep; // the message you were reading stays in place
    card.older = res.older; card.first = res.events[0]?.n || card.first;
    if (!card.older && res.trimmed) log.prepend(el("p", "ck-line info", "Older output was trimmed (the chat keeps its newest part)."));
    ckOlderMark(card);
  } catch (e) { toast(e.message); }
  finally { card.loadingOlder = false; }
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
  else b.tools.append(ckLine(card, e));
  const status = e.kind === "sub" ? e.status || "running" : b.status || "running";
  b.status = status; b.d.classList.toggle("running", status === "running");
  b.st.className = `chip${status === "failed" ? " bad" : status === "done" ? "" : " live"}`;
  b.st.textContent = status === "running" ? "running" : status;
  ckSubbar(card);
  function report(x, ev) { x.report.innerHTML = renderMd(ev.text); }
}
const ckLive = (m) => (ckPc(m.machine)?.companion?.live || []).includes(m.agent); // its pc runs this agent live
function ckSubsEnded(card) {
  for (const b of (card.subs || new Map()).values()) {
    if (b.status !== "running") continue;
    b.status = "ended"; b.d.classList.remove("running"); b.st.className = "chip"; b.st.textContent = "ended";
    b.st.title = "It did not report its end in this chat (its turn ended or was cut off)";
  }
  ckSubbar(card);
}
const ckNum = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));
// The subagents running now, pinned above the box: the log scrolls on, these stay; a click opens its block. Folded so
// the log keeps the room: a phone shows one line "N running ▸", a wide screen two lines and "+N" for the rest.
const ckPhone = () => matchMedia("(max-width: 699.98px)").matches;
function ckSubbar(card) {
  const run = [...(card.subs || new Map()).values()].filter((b) => b.status === "running");
  // the agent's shells left running in the background (Claude Code), named by what they run; a click shows its output
  const list = card.meta.bg_list || [];
  if (card.shell && !list.some((s) => s.id === card.shell)) ckShellClose(card); // it ended
  const shells = list.map((s) => {
    const x = el("button", "ck-subpin ck-shellpin"); x.type = "button"; x.setAttribute("aria-expanded", String(card.shell === s.id));
    x.append(el("span", "ck-dot live"), el("span", "ck-sub-label", `shell: ${s.label}`));
    x.title = "A shell the agent left running in the background: show what it prints";
    x.onclick = () => { if (card.shell === s.id) ckShellClose(card); else ckShellOpen(card, s.id); ckSubbar(card); };
    return x;
  });
  const pins = [...shells, ...run.map((b) => {
    const x = el("button", "ck-subpin"); x.type = "button";
    x.append(el("span", "ck-dot live"), el("span", "ck-sub-label", b.label.textContent), ...(b.tokens ? [el("small", null, `${ckNum(b.tokens)} tokens`)] : []));
    x.title = "Open this subagent's block";
    x.onclick = () => { b.d.open = true; b.d.scrollIntoView({ block: "nearest", behavior: "smooth" }); };
    return x;
  })];
  const n = pins.length, open = !!card.subbarOpen, phone = ckPhone(), had = document.activeElement?.classList.contains("ck-subfold") && card.subbar.contains(document.activeElement);
  card.subbar.hidden = !n;
  const fold = el("button", "btn small quiet ck-subfold"); fold.type = "button"; fold.setAttribute("aria-expanded", String(open));
  fold.title = pins.map((x) => x.querySelector(".ck-sub-label").textContent).join("\n");
  fold.onclick = () => { card.subbarOpen = !open; ckSubbar(card); };
  if (phone) { fold.textContent = `${n} running ${open ? "▾" : "▸"}`; for (const x of pins) x.hidden = !open; card.subbar.replaceChildren(fold, ...pins); }
  else {
    card.subbar.replaceChildren(...pins);
    if (open) { fold.textContent = "Show less"; card.subbar.append(fold); }
    else if (n > 1 && card.subbar.offsetParent) { // what wraps past the second line goes behind +N
      const tops = [...new Set(pins.map((x) => x.offsetTop))].sort((a, b) => a - b), cut = tops[1] ?? tops[0];
      let shown = pins.filter((x) => x.offsetTop <= cut).length;
      if (shown < n) {
        for (const x of pins.slice(shown)) x.hidden = true;
        fold.textContent = `+${n - shown}`; card.subbar.append(fold);
        while (shown > 1 && fold.offsetTop > cut) { pins[--shown].hidden = true; fold.textContent = `+${n - shown}`; } // +N must fit too
      }
    }
  }
  card.subbar.classList.toggle("open", open);
  if (card.shellOut) { card.shellOut.hidden = phone && !open; card.subbar.append(card.shellOut); }
  if (had && fold.isConnected) fold.focus({ preventScroll: true });
}
// A background shell's output, live: asking keeps it watched, so the pc's companion sends the file's tail (~2 s).
function ckShellOpen(card, id) {
  ckShellClose(card);
  const out = card.shellOut = el("pre", "progress-log ck-shell-out", "Waiting for the pc to send this shell's output…");
  card.shell = id;
  const tick = async () => {
    if (card.shellOut !== out) return;
    if (!card.subbar.isConnected) { ckShellClose(card); return; } // the chat was left
    let r; try { r = await api(`/ui/api/chat/shell?machine=${card.meta.machine}&id=${card.meta.id}&shell=${encodeURIComponent(id)}`); }
    catch (e) { r = { error: e.message }; }
    if (card.shellOut !== out) return;
    if (r.error) out.textContent = r.error;
    else if (!r.online) out.textContent = "The pc is offline.";
    else if (r.at) {
      const end = out.scrollHeight - out.scrollTop - out.clientHeight < 8; // at the bottom: stay there as it grows
      out.textContent = (r.size > r.text.length ? "…\n" : "") + (ckTerm(r.text) || "(no output yet)");
      if (end) out.scrollTop = out.scrollHeight;
    }
    card.shellTimer = setTimeout(tick, 2000);
  };
  tick();
}
// What a terminal would show: colour codes dropped, and a line a progress bar redraws with \r only as it was last drawn.
const ckTerm = (t) => t.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").split("\n").map((l) => l.replace(/\r+$/, "").split("\r").pop()).join("\n");
function ckShellClose(card) { clearTimeout(card.shellTimer); card.shell = card.shellOut = null; }

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
  const sig = JSON.stringify([w.status, w.chat, !!w.review, !!w.git, card.online, !!w.checkpoint, !!w.merged, w.ahead]);
  if (card.acts.dataset.sig !== sig) {
    card.acts.dataset.sig = sig; card.acts.replaceChildren();
    const act = (text, title, fn) => { const b = el("button", "btn small", text); b.type = "button"; b.title = title; b.onclick = fn; card.acts.append(b); };
    if (card.online && run) act("Stop", "Stop this worker (this pc keeps its lease until it runs out)", () => ckWorkerAct(card, "stop_worker"));
    if (card.online && w.status === "failed") act("Restart", "Start this worker again now", () => ckWorkerAct(card, "restart"));
    if (w.review || w.git) act("Review", "The changes on its branch, read on that pc", () => ckDiff({ kind: "worker", machine: m.machine, project: w.project, id: w.id, title: w.id, label: m.label }));
    if (card.online && !w.chat && w.status !== "merged") act("Take over", "Stop the worker and go on with its conversation as a chat on that pc", () => ckWorkerAct(card, "takeover"));
    if (w.chat) act("Open chat", "The chat this worker became", () => go(`#/chats/${encodeURIComponent(w.chat)}`));
    if (!run && !w.merged && w.checkpoint) act("Roll back", "Put its worktree back as it was before its last turn (a checkpoint of now is kept)", () => confirm(`Roll ${w.id} back to before its last turn?`) && ckWorkerAct(card, "rollback"));
    if (card.online && !run && !(w.ahead > 0)) act("Archive", "Take it off the lists; its branch and worktree stay", () => ckWorkerAct(card, "archive_worker"));
  }
  card.form.hidden = !live;
  card.stop.hidden = true; card.send.disabled = !live;
  card.input.dataset.hint = "Steer the worker… (it reads this after its next step)"; ckSendHints(card);
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
function ckNewChat(machine = "") {
  if (machine || !bdOn()) { ck.draftFor = machine; return go("#/chats/new"); } // a draft in the chat pane (ckDraft)
  const card = ck.cards.get(ckKey(CK_DRAFT)) || ckDraft(), shut = bdShut(); // on the grid: a card on top of its project
  if (shut.includes(card.meta.project)) store.set("kn_board", JSON.stringify({ shut: shut.filter((p) => p !== card.meta.project) }));
  bdRender();
  if (touch) card.root.scrollIntoView({ block: "nearest" }); else card.input.focus(); // a phone would pop its keyboard up
}
$("ck-new").onclick = async () => { if (!ck.machines.length) await ckSummary().catch(() => {}); ckNewChat(); };

