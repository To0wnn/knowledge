// Command palette: Ctrl/Cmd+K, the sidebar's "Search or jump" and the phone's search icon open it; / opens it for notes
// only. One list of notes (titles at once from the map data, then text and meaning from the server), chats, tasks,
// views and actions; recent picks first while the box is empty. Keyboard: Up/Down, Enter, Esc (the dialog's own). The
// box is a combobox over a listbox (aria-activedescendant), so a screen reader hears the highlighted item.
// Items are plain data ({kind, id, label, hint, …}); palGo() runs one, so a recent item works after a reload too.
// Uses app.js and shell.js globals (data, byId, ck, me, project, api, el, store, shown, go, openNote, toggleRail, …).
"use strict";

const pal = { items: [], sel: 0, notesOnly: false, seq: 0, timer: 0, tasks: [], server: null };
const PAL_VIEWS = [["chats", "Agents"], ["tasks", "Tasks"], ["machines", "Machines"], ["notes", "Notes"],
  ["map", "Map"], ["changes", "Changes"], ["health", "Health"], ["thispc", "This pc"], ["admin", "Admin"], ["help", "Help"]];
const PAL_ACTIONS = [["new-chat", "New chat…", "new_chat"], ["theme", "Toggle theme"], ["rail", "Collapse or expand the sidebar", "rail"]];
const palKey = (it) => `${it.kind}:${it.id}`;

function openPalette(notesOnly = false) {
  pal.notesOnly = notesOnly; pal.server = null; pal.sel = 0;
  $("pal-q").value = "";
  $("pal-q").placeholder = notesOnly ? "Search notes…" : "Search notes, chats, tasks, or type a command…";
  if (!$("palette").open) $("palette").showModal();
  $("pal-q").focus();
  palRender();
  if (!notesOnly) api("/ui/api/tasks").then((r) => { pal.tasks = r.tasks.filter((t) => !t.done); if ($("palette").open) palRender(true); }).catch(() => {});
}

function palRecent() { try { return JSON.parse(store.get("kn_pal_recent") || "[]"); } catch { return []; } }

// Run one item: close first, so a view it opens gets the focus.
function palGo(it) {
  $("palette").close();
  store.set("kn_pal_recent", JSON.stringify([it, ...palRecent().filter((x) => palKey(x) !== palKey(it))].slice(0, 8)));
  if (it.kind === "note") return openNote(it.id, it.find ? { find: it.find } : {});
  if (it.kind === "search") return go(`#/notes?q=${encodeURIComponent(it.id)}`);
  if (it.kind === "chat") return go(`#/chats/${encodeURIComponent(it.id)}`);
  if (it.kind === "task") return openNote(it.path, { find: [it.id.toLowerCase()] });
  if (it.kind === "view") return ["admin", "help"].includes(it.id) ? location.assign(`/ui/${it.id}`) : go(`#/${it.id}`);
  const act = { "new-chat": () => { go("#/chats"); $("ck-new").onclick(); }, theme: () => document.querySelector(".theme-btn").click(),
    rail: toggleRail }[it.id];
  if (act) act();
}

// A snippet as plain text: no **, `, # or [text](link) markup in a one-line hint.
const palPlain = (t) => String(t || "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/\[\[([^\]]+)\]\]/g, "$1").replace(/(\*{1,3}|`)([^*`]+)\1/g, "$2")
  .replace(/^\s*(#+|[-*>]|\d+\.)\s+/, "").replace(/\s+/g, " ").trim();
// Where a note lives, short: its project and folder ("homelab · memory"), so two notes with one title tell apart.
const palWhere = (path) => `${shown(projectOf(path))} · ${path.split("/").slice(0, -1).join("/") || "/"}`;
const noteItem = (n, hint, find) => ({ kind: "note", id: n.id, label: noteName(n.id).title, hint: hint ?? `${palWhere(n.id)}${n.cluster ? ` · ${n.cluster}` : ""}`, ...(find ? { find } : {}) });

// The groups for the current text: [[heading, items]], empty groups left out.
function palGroups(q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean), has = (t) => words.every((w) => t.toLowerCase().includes(w));
  const views = PAL_VIEWS.filter(([id]) => (id !== "health" || me.admin) && (id !== "thispc" || !$("nav-thispc").hidden) && !(agentsOff() && AGENT_VIEWS.includes(id)) && !(id === "tasks" && !agentsOff()))
    .map(([id, label]) => ({ kind: "view", id, label: id === "admin" && !me.admin ? "My tokens & projects" : label, hint: "Go to" }));
  const actions = PAL_ACTIONS.filter(([id]) => !(agentsOff() && id === "new-chat")).map(([id, label, key]) => ({ kind: "action", id, label, hint: key ? keyOf(key) : "" }));
  if (!words.length) {
    const recent = palRecent().filter((x) => !pal.notesOnly || x.kind === "note");
    return [["Recent", recent], ...(pal.notesOnly ? [] : [["Go to", views], ["Actions", actions]])];
  }
  const seen = new Set();
  const notes = data.nodes.filter((n) => has(`${n.label} ${n.id}`))
    .sort((a, b) => (inProject(b.project) - inProject(a.project)) || (b.degree || 0) - (a.degree || 0)).slice(0, 6).map((n) => noteItem(n));
  notes.forEach((x) => seen.add(x.id));
  const text = [];
  if (pal.server?.q === q) {
    for (const h of pal.server.text) if (!seen.has(h.path) && seen.add(h.path)) text.push(noteItem({ id: h.path }, `${palWhere(h.path)} · ${palPlain(h.hits[0]?.text)}`, words));
    for (const h of pal.server.sem) if (!seen.has(h.path) && seen.add(h.path)) text.push(noteItem({ id: h.path }, `${palWhere(h.path)} · similar · ${palPlain(h.text)}`));
  }
  const all = { kind: "search", id: q, label: `Search all notes for “${q}”`, hint: "Enter on the page" };
  if (pal.notesOnly) return [["Notes", notes], ["Text and meaning", text.slice(0, 8)], ["", [all]]];
  const chats = ck.chats.filter((c) => has(`${c.title || ""} ${c.folder || ""} ${c.label || ""}`)).slice(0, 5)
    .map((c) => ({ kind: "chat", id: c.id, label: c.title || "(no message yet)", hint: `chat · ${c.label} · ${ckShort(c.folder)}` }));
  const tasks = pal.tasks.filter((t) => has(`${t.id} ${t.owner} ${t.text}`)).slice(0, 5)
    .map((t) => ({ kind: "task", id: t.id, path: t.path, label: `${t.id} · ${t.owner}`, hint: palPlain(t.text) }));
  return [["Notes", notes], ["Text and meaning", text.slice(0, 6)], ["Chats", chats], ["Tasks", tasks],
    ["Go to", views.filter((v) => has(v.label))], ["Actions", actions.filter((a) => has(a.label))], ["", [all]]];
}

// keep = keep the item the user moved to when the list grows (server results arriving); else the best match leads
function palRender(keep = false) {
  const q = $("pal-q").value.trim(), was = keep && pal.moved && pal.items[pal.sel] ? palKey(pal.items[pal.sel]) : "";
  const list = $("pal-list"); list.replaceChildren(); pal.items = [];
  const seen = new Set(); // one row per note: a recent pick or a title hit is not listed again under text
  for (const [head, all] of palGroups(q)) {
    const items = all.filter((it) => !seen.has(palKey(it)) && seen.add(palKey(it)));
    if (!items.length) continue;
    if (head) { const h = el("li", "pal-grp", head); h.setAttribute("role", "presentation"); list.append(h); }
    for (const it of items) {
      const i = pal.items.length, li = el("li", "pal-opt");
      li.id = `pal-o-${i}`; li.setAttribute("role", "option");
      li.append(el("span", "pal-label", it.kind === "note" ? noteName(it.id).title : it.label)); // a recent pick saved before titles were used if (it.hint) li.append(el("small", null, it.hint));
      li.onmousemove = () => { if (pal.sel !== i) { pal.moved = true; palSelect(i); } };
      li.onclick = () => palGo(it);
      list.append(li); pal.items.push(it);
    }
  }
  if (!pal.items.length) { const li = el("li"); li.setAttribute("role", "presentation"); li.append(emptyState(q ? "Nothing found." : "Type to search.")); list.append(li); }
  const at = was ? pal.items.findIndex((x) => palKey(x) === was) : -1;
  palSelect(at >= 0 ? at : 0);
}

function palSelect(i) {
  pal.sel = i;
  for (const li of $("pal-list").querySelectorAll(".pal-opt")) li.setAttribute("aria-selected", String(li.id === `pal-o-${i}`));
  const cur = $(`pal-o-${i}`);
  if (cur) { $("pal-q").setAttribute("aria-activedescendant", cur.id); cur.scrollIntoView({ block: "nearest" }); }
  else $("pal-q").removeAttribute("aria-activedescendant");
}

$("pal-q").addEventListener("input", () => {
  pal.moved = false;
  palRender();
  clearTimeout(pal.timer);
  const q = $("pal-q").value.trim();
  if (q.length < 2) return;
  pal.timer = setTimeout(async () => { // text and meaning: one round trip each, the newest query wins
    const seq = ++pal.seq, enc = encodeURIComponent(q);
    // a query that matches one of the palette's own views or actions found what it looked for: no knowledge gap
    const own = pal.items.some((i) => i.kind === "view" || i.kind === "action") ? "&gap=0" : "";
    try {
      const [t, s] = await Promise.all([api(`/ui/api/search?q=${enc}&project=${project}`),
        api(`/ui/api/semantic?q=${enc}&project=${project}${own}`).catch(() => ({ hits: [] }))]);
      if (seq !== pal.seq || !$("palette").open) return;
      pal.server = { q, text: t.notes.slice(0, 6), sem: s.hits.slice(0, 6) };
      palRender(true);
    } catch { /* the titles stay; the search page shows the error */ }
  }, 200);
});
$("pal-q").addEventListener("keydown", (e) => {
  const n = pal.items.length;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (n) pal.moved = true, palSelect((pal.sel + (e.key === "ArrowDown" ? 1 : n - 1)) % n); }
  else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); if (pal.items[pal.sel]) palGo(pal.items[pal.sel]); }
});
$("pal-close").onclick = () => $("palette").close();
$("palette").addEventListener("click", (e) => { if (e.target === $("palette")) $("palette").close(); }); // the backdrop
