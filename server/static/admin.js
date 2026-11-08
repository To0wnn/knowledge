// Knowledge admin: device tokens, people (project access) and projects. The server checks every call; a person who
// is not an admin gets only their own tokens and projects (state.admin false) and sees only those two tabs.
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const api = (path, body) => fetch(path, {
  method: body ? "POST" : "GET", credentials: "same-origin",
  headers: { "X-Knowledge-UI": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
  body: body ? JSON.stringify(body) : undefined,
}).then(async (r) => {
  const text = await r.text();
  let j;
  try { j = JSON.parse(text); } catch { j = { error: serverText(r, text) }; } // a proxy error page is no JSON
  if (!r.ok) throw new Error(j.error || serverText(r, ""));
  return j;
});
// What the server said when its answer is no JSON: its own short plain-text message, else the HTTP status.
function serverText(r, text) {
  const t = text.trim();
  return t && t.length < 300 && !t.startsWith("<") ? `${r.status}: ${t}` : `${r.status} ${r.statusText || "no answer"}${r.status >= 502 ? ": the server may be restarting" : ""}`;
}
// A short message at the bottom ("Saved"); errors stay in the bar at the top (#error).
function toast(text) {
  const t = el("div", "toast", text);
  t.onclick = () => t.remove(); $("toasts").append(t);
  setTimeout(() => t.remove(), 3000);
}
// Tokens the VM itself uses: revoking one breaks the live sync, the weekly report or the VM sessions.
const SYSTEM = new Set(["vm-watcher", "vm-sessions", "vm-lint-report"]);
const openClients = new Set();  // tokens whose client rows are unfolded; kept across re-renders

let state = null;
const ago = (sec) => { if (!sec) return "never"; const d = Date.now() / 1000 - sec;
  return d < 900 ? "in the last 15 min" : d < 3600 ? `${Math.round(d / 60)} min ago` : d < 86400 ? `${Math.round(d / 3600)} h ago` : day(sec); }; // as the app: a date past a day

// One empty state for every table (the app's .empty): a single cell over all columns.
const emptyRow = (cols, text) => { const tr = el("tr"), td = el("td", "empty", text); td.colSpan = cols; tr.append(td); return tr; };

function showError(e) { $("error").textContent = e ? String(e.message || e) : ""; $("error").hidden = !e; }

// One action at a time: the clicked control is disabled until the server answered, then the page reloads its state.
// A change of access or a setting says "Saved" once the server took it.
async function run(fn, control, saved) {
  showError(null);
  if (control) control.disabled = true;
  try { await fn(); if (saved) toast(saved); await load(); return true; } catch (e) { showError(e); return false; } finally { if (control) control.disabled = false; }
}

function scopeText(t) {
  if (!t.roots || !t.roots.length) return "all projects";
  const names = state.projects.filter((p) => t.roots.includes(p.docs) || t.roots.includes(p.memory)).map((p) => p.name);
  return [...names, ...(t.roots.includes("claude") ? ["shared skills"] : [])].join(", ");
}

// Shared skills per token: a switch that shows the state, only where the server allows it (else why not).
function skillsWhyNot(t) {
  if (!state.admin || t.owner) return "only for the admin's own devices";
  if (t.watch) return "the VM watcher needs no skills";
  if (!t.write) return "read-only token";
  if (t.redact) return "masks secrets";
  if (t.roots && t.roots.length && !t.roots.includes("claude")) return "does not see the shared skills";
  return "";
}
// An admin-made token counts as the admin (its machines, its task lines) only when marked as the admin's own device.
function accessCell(t, access) {
  const td = el("td", "", access);
  if (!t.watch) td.append(el("br"), agentsSwitch(t));
  if (!state.admin || t.owner) return td;
  const l = el("label", "check"), c = el("input"); c.type = "checkbox"; c.checked = !!t.self;
  c.setAttribute("aria-label", `${t.name} is my own device`);
  l.title = "My own device: counts as me for its machines and task lines. Never tick it for a friend's or a PA's token.";
  c.onchange = () => {
    if (!confirm(c.checked ? `Mark ${t.name} as your own device? Its machines and task lines then count as yours.` : `${t.name} is not your own device: its machines and task lines stop counting as yours. Go on?`)) { c.checked = !c.checked; return; }
    run(() => api("/ui/api/admin/token", { name: t.name, update: true, self: c.checked }), c, `Saved: ${t.name} ${c.checked ? "is" : "is not"} your own device`);
  };
  l.append(c, " own device"); td.append(el("br"), l); return td;
}
// Agents (remote control): whether the pc behind this token may be steered from the web UI (chats, workers, remote
// actions, approvals). Off, the server refuses all of it for that token; a token from before the switch counts as on.
function agentsSwitch(t) {
  const on = t.agents !== false, l = el("label", "check"), c = el("input"); c.type = "checkbox"; c.setAttribute("role", "switch"); c.checked = on;
  c.setAttribute("aria-label", `Agents (remote control) for ${t.name}`);
  l.title = "Agents (remote control): chats, workers, remote actions and approvals for the pc behind this token. Off: the web UI cannot steer it (memory and MCP keep working).";
  c.onchange = () => {
    if (!confirm(c.checked ? `Allow remote control for ${t.name}? The web UI may then start chats and workers and run actions on the pc behind it.` : `Turn remote control off for ${t.name}? The web UI can no longer steer that pc; memory and MCP keep working.`)) { c.checked = !c.checked; return; }
    run(() => api("/ui/api/admin/token", { name: t.name, update: true, agents: c.checked }), c, `Saved: remote control ${c.checked ? "on" : "off"} for ${t.name}`);
  };
  l.append(c, " agents"); return l;
}
function skillsCell(t) {
  const td = el("td"), why = skillsWhyNot(t);
  if (why) { const x = el("span", "muted", "—"); x.title = `Not possible: ${why}`; td.append(x); return td; }
  const l = el("label", "check"), c = el("input"); c.type = "checkbox"; c.setAttribute("role", "switch"); c.checked = !!t.skills;
  c.setAttribute("aria-label", `Shared skills for ${t.name}`);
  l.title = t.skills ? "On: the device keeps the shared skills in ~/.claude/skills and may add or change them"
                     : "Off: switch on to give this device the shared skills at its next session start";
  c.onchange = () => {
    if (!c.checked && !confirm(`Switch shared skills off for ${t.name}? At its next sync the device removes the skills it got from here; skills it made itself and did not send yet stay.`)) {
      c.checked = true; return;
    }
    run(() => api("/ui/api/admin/token", { name: t.name, update: true, skills: c.checked }), c, `Saved: shared skills ${c.checked ? "on" : "off"} for ${t.name}`);
  };
  l.append(c, " " + (t.skills ? "on" : "off")); td.append(l); return td;
}

// "behind" when older than the newest; "no URL set" when the device names no server: a plugin before 0.14.2 leans on
// its built-in address and loses the server at its next update, a newer Claude plugin found the server elsewhere but
// has no MCP address. Plugins before 0.10.7 do not say, so they cannot count as safe yet.
// Codex and agy also report their own daily update check, so a device stuck behind says why.
const pluginText = (p) => `${p.v}${p.behind ? " · behind" : ""}${p.checked ? ` · last check ${ago(p.checked)}` : ""}`
  + `${p.update_error ? ` · update failed: ${p.update_error}` : ""}${p.default_url ? " · no URL set" : p.default_url === null ? " · URL not reported" : ""}`;

// The plugin version per agent this token reported at its last session start.
function pluginCell(t, open) {
  const td = el("td", "muted wrap");
  const own = new Set(open ? t.clients.map((c) => c.agent) : []);  // unfolded, those show it in their own rows
  const rest = (t.plugin || []).filter((p) => !own.has(p.agent));
  if (!rest.length) { td.textContent = "–"; return td; }
  // one short line; every agent's version, check and session start on hover
  const bad = rest.filter((p) => p.behind || p.update_error).length, versions = [...new Set(rest.map((p) => p.v))];
  const v = el("span", bad ? "plugin-v unused" : "plugin-v", bad ? `${bad} behind` : versions.length === 1 ? `${versions[0]} · current` : `${rest.length} current`);
  v.title = rest.map((p) => `${p.agent} ${pluginText(p)} · session start ${ago(p.at)}`).join("\n");
  v.tabIndex = 0; v.setAttribute("aria-label", `Plugin: ${v.title}`); // the details reach the keyboard and a screen reader too
  td.append(v);
  return td;
}

// One row per session under its token, in the columns that fit: agent under Name, when under Last used, the
// plugin version (recorded per token and agent, not per session) under Plugin.
function clientRows(t) {
  const plugin = Object.fromEntries((t.plugin || []).map((p) => [p.agent, p]));
  return [...(t.clients || [])].sort((a, b) => b.at - a.at).map((c, i, all) => {
    const tr = el("tr", i === all.length - 1 ? "client last" : "client");
    const id = el("td", "mono", c.session); id.colSpan = 2;
    const p = plugin[c.agent];
    tr.append(el("td", "mono", `↳ ${c.agent}`), id, el("td", c.wake ? "" : "muted", c.wake ? "wakeable" : "not wakeable"),
      el("td", "", c.project || "–"), el("td", "", ago(c.at)),
      el("td", p && p.behind ? "unused" : "", p ? pluginText(p) : "–"));
    const td = el("td"), b = el("button", "btn small quiet risky", "Forget");
    b.setAttribute("aria-label", `Forget ${c.agent} session ${c.session}`);
    b.title = "Remove from this list; a session still in use comes back at its next prompt";
    b.onclick = () => run(() => api("/ui/api/admin/client", { name: t.name, session: c.session }), b);
    td.append(b);
    tr.append(td);
    return tr;
  });
}

function renderTokens() {
  $("tokens").replaceChildren(...state.tokens.flatMap((t) => {
    const n = (t.clients || []).length, open = n && openClients.has(t.name);
    const tr = el("tr", open ? "has-clients" : "");
    const access = t.watch ? "VM watcher" : t.write ? "read + write" : "read";
    const name = el("td", "mono", t.name);
    if (SYSTEM.has(t.name) || t.watch) name.append(el("span", "muted", " · used by the VM"));
    if (n) {
      const fold = el("button", "btn small quiet fold", `${open ? "▾" : "▸"} ${n} client${n === 1 ? "" : "s"}`);
      fold.setAttribute("aria-expanded", String(Boolean(open)));
      fold.onclick = () => { openClients.has(t.name) ? openClients.delete(t.name) : openClients.add(t.name); renderTokens(); };
      name.append(" ", fold);
    }
    tr.append(name, accessCell(t, access), el("td", "wrap", scopeText(t) + (t.redact ? " · secrets masked" : "")), skillsCell(t),
      el("td", "muted", t.created ? `${day(t.created)}${t.by ? " · " + t.by : ""}` : "before the admin page"),
      el("td", t.last_used ? "muted" : "unused", t.last_used ? ago(t.last_used) : "never used"), pluginCell(t, open));
    const td = el("td", "token-acts"), b = el("button", "btn small quiet risky", "Revoke");
    b.setAttribute("aria-label", `Revoke ${t.name}`);
    b.onclick = () => {
      const warn = SYSTEM.has(t.name) || t.watch ? " The VM uses this token: live updates, the weekly report or the VM sessions stop." : "";
      if (confirm(`Revoke ${t.name}? Devices using it stop working right away.${warn}`)) run(() => api("/ui/api/admin/token", { name: t.name, revoke: true }), b);
    };
    td.append(b); tr.append(td);
    // the phone shows each token as a card: every cell its own line with its column name (app.css, under 600 px)
    ["", "Access", "Scope", "Shared skills", "Created", "Last used", "Plugin", ""].forEach((l, i) => { if (l) tr.children[i].dataset.label = l; });
    return [tr, ...(open ? clientRows(t) : [])];
  }));
  // Rebuilt after every action: keep what was ticked. "All projects" is explicit, the server refuses an empty pick.
  const ticked = new Set([...$("t-roots").querySelectorAll("input:checked")].map((c) => c.value));
  const boxes = state.projects.map((p) => [p.name, [p.docs, p.memory]])
    .concat(state.admin ? [["shared skills (claude/)", ["claude"]], ["All projects (everything, now and later)", ["*"]]] : []);
  $("t-roots").replaceChildren(...boxes.map(([label, roots]) => {
    const l = el("label", "check"), c = el("input"); c.type = "checkbox"; c.value = roots.join(",");
    c.checked = ticked.has(c.value); l.append(c, " " + label); return l;
  }));
}

function renderUsers() {
  const projects = state.projects.map((p) => p.name);
  const head = el("tr"); head.append(el("th", "", "Person"), ...projects.map((p) => el("th", "", p)), el("th", "", "Actions"));
  $("users-head").replaceChildren(head);
  const rows = state.admins.map((a) => {
    const tr = el("tr"); tr.append(el("td", "mono", a), ...projects.map(() => el("td", "muted", "admin")), el("td", "muted", "server config"));
    return tr;
  });
  // People who signed in without access first: they are waiting for an answer.
  const entries = Object.entries(state.users).sort(([a, ua], [b, ub]) =>
    (Object.keys(ua.projects || {}).length > 0) - (Object.keys(ub.projects || {}).length > 0) || a.localeCompare(b));
  for (const [name, u] of entries) {
    const tr = el("tr");
    if (!Object.keys(u.projects || {}).length) tr.className = "pending";
    const who = el("td", "mono", name);
    if (u.first_seen) who.title = `first signed in ${stamp(new Date(u.first_seen * 1000))}`;
    tr.append(who);
    const selects = projects.map((p) => {
      const s = el("select"); s.setAttribute("aria-label", `${name}: ${p}`);
      for (const m of ["none", "read", "write"]) { const o = el("option", "", m); o.value = m; s.append(o); }
      s.value = (u.projects || {})[p] || "none";
      const td = el("td"); td.append(s); tr.append(td);
      return [p, s];
    });
    // Saved shortly after the last change, without rebuilding the table: arrow keys on a closed select fire a change
    // per step, and a rebuild would drop the focus. On an error the table reloads, so it shows what is stored.
    for (const [, s] of selects) {
      s.onchange = () => {
        clearTimeout(s.saveTimer);
        s.saveTimer = setTimeout(async () => {
          showError(null);
          try {
            await api("/ui/api/admin/user", { username: name, projects: Object.fromEntries(selects.map(([p, x]) => [p, x.value])) });
            tr.classList.toggle("pending", selects.every(([, x]) => x.value === "none"));
            toast(`Saved: access of ${name}`);
          } catch (e) { showError(e); load().catch(showError); }
        }, 700);
      };
    }
    const td = el("td", "actions"), del = el("button", "btn small danger", "Remove");
    del.setAttribute("aria-label", `Remove ${name}`);
    del.onclick = () => confirm(`Remove ${name}? They lose access right away.`) && run(() => api("/ui/api/admin/user", { username: name, delete: true }), del);
    td.append(del); tr.append(td);
    rows.push(tr);
  }
  $("users").replaceChildren(...rows);
}

// What the knowledge holds of a project (its indexed files), as KB or MB.
const size = (b) => b < 1e6 ? `${Math.max(1, Math.round(b / 1e3))} KB` : `${(b / 1e6).toFixed(1)} MB`;

// The name the web UI shows for a project; saved when the field loses focus or on Enter. Empty = the project name.
function labelCell(p) {
  const td = el("td"), input = el("input");
  input.type = "text"; input.value = p.label || p.name; input.setAttribute("aria-label", `Display name of ${p.name}`); input.maxLength = 40;
  input.title = `Shown in the web UI; roots, tokens and linked folders keep the name ${p.name}`;
  input.onkeydown = (e) => { if (e.key === "Enter") input.blur(); };
  input.onchange = () => run(() => api("/ui/api/admin/project-label", { name: p.name, label: input.value.trim() }), input, `Saved: display name of ${p.name}`);
  td.append(input);
  if (p.label) td.append(el("div", "muted mono", p.name));
  return td;
}

// Whose new task lines may start without approval; empty = the default (the project's owner and their tokens, admins
// and tokens an admin made). Saved like the display name.
function writersCell(p) {
  const td = el("td"), input = el("input");
  input.type = "text"; input.value = (p.task_writers || []).join(", "); input.placeholder = "default";
  input.setAttribute("aria-label", `Trusted task writers of ${p.name}`);
  input.title = "Token names or users, comma-separated. Empty: the project's owner" + (p.owner ? ` (${p.owner})` : "") + " and their tokens, admins and tokens an admin made";
  input.onkeydown = (e) => { if (e.key === "Enter") input.blur(); };
  input.onchange = () => { const w = input.value.split(",").map((x) => x.trim()).filter(Boolean);
    run(() => api("/ui/api/admin/task-writers", { name: p.name, writers: w.length ? w : null }), input, `Saved: task writers of ${p.name}`); };
  td.append(input);
  return td;
}

function renderProjects() {
  if (!state.admin) {
    $("project-rows").replaceChildren(...state.projects.map((p) => { const tr = el("tr");
      tr.append(el("td", "", p.name), el("td", "mono", p.docs + "/"), el("td", "mono", p.memory + "/"), el("td", "num", "—"), el("td", "num", "—"),
        el("td", "", p.mode === "write" ? "you: read + write" : "you: read"), el("td", "muted", "—"), el("td", "muted", p.builtin ? "VM (built in)" : "server"));
      return tr; }));
    return;
  }
  const files = Object.fromEntries(state.system.index.map((r) => [r.root, r.files]));
  const bytes = Object.fromEntries(state.system.index.map((r) => [r.root, r.bytes || 0]));
  $("project-rows").replaceChildren(...state.projects.map((p) => {
    const tr = el("tr"), mine = (roots) => !roots || !roots.length || roots.includes(p.docs) || roots.includes(p.memory);
    const tokens = state.tokens.filter((t) => mine(t.roots)).length;
    const people = state.admins.length + Object.values(state.users).filter((u) => (u.projects || {})[p.name] && u.projects[p.name] !== "none").length;
    tr.append(labelCell(p), el("td", "mono", p.docs + "/"), el("td", "mono", p.memory + "/"),
      el("td", "num", `${files[p.memory] ?? 0} memory · ${files[p.docs] ?? 0} docs`),
      el("td", "num", size((bytes[p.memory] ?? 0) + (bytes[p.docs] ?? 0))),
      el("td", "", `${tokens} token${tokens === 1 ? "" : "s"} · ${people} ${people === 1 ? "person" : "people"}`),
      writersCell(p), el("td", "muted", p.builtin ? "VM (built in)" : "server"));
    return tr;
  }));
}

// External skills: one row per skill from GitHub, with its state and the actions the server allows.
function renderExternal() {
  const rows = Object.entries(state.external || {});
  $("external").replaceChildren(...(rows.length ? rows.map(([name, x]) => {
    const tr = el("tr"), src = el("a", "mono", `${x.owner}/${x.repo}/${x.path}`), act = el("td", "actions");
    src.href = x.url; src.rel = "noopener noreferrer"; src.target = "_blank";
    const status = el("td", x.status === "ok" ? "" : "alert", x.status + (x.note ? `: ${x.note}` : ""));
    const button = (label, action, cls) => { const b = el("button", cls || "btn small", label); b.type = "button";
      b.setAttribute("aria-label", `${label} ${name}`);
      b.onclick = () => (action !== "remove" || confirm(`Remove the skill ${name} from every device?`)) &&
        run(() => api("/ui/api/admin/external", { name, action }), b);
      return b; };
    act.append(button("Check now", "check"), ...(x.status === "waiting" ? [button("Approve", "approve", "btn small primary")] : []), button("Remove", "remove", "btn small danger"));
    const srcTd = el("td"); srcTd.append(src);
    tr.append(el("td", "", name), srcTd, el("td", "mono", x.commit.slice(0, 7) + (x.pending ? ` → ${x.pending.slice(0, 7)}` : "")),
      el("td", "", x.licence), status, el("td", "", ago(x.checked)), act);
    return tr;
  }) : [emptyRow(7, "No external skills yet.")]));
  const waiting = rows.filter(([, x]) => x.status !== "ok").length;
  $("n-external").textContent = waiting ? `${waiting} !` : rows.length || "";
  $("n-external").classList.toggle("alert", !!waiting);
}
// Tools & skills: one Add for both kinds. A GitHub skill folder is a shared skill at once; anything else becomes a
// request that is drafted right away (a task line for an agent), as any person's request would be after Draft it.
const SKILL_FOLDER = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/(tree|blob)\/\S+$/;
$("tool-add-form").onsubmit = (e) => {
  e.preventDefault();
  const what = $("tool-what").value.trim();
  run(async () => {
    if (SKILL_FOLDER.test(what)) return api("/ui/api/admin/external", { url: what });
    const { id } = await api("/ui/api/tools/request", { what, why: "" });
    await api("/ui/api/admin/tools/request", { id, decision: "draft" });
  }, e.target.querySelector("button"), SKILL_FOLDER.test(what) ? "Added as a shared skill" : "Drafting: a task line for an agent").then((ok) => ok && e.target.reset());
};
const REQUEST_STATE = { waiting: "New request", drafting: "Being drafted", added: "Added", declined: "Declined" };
// Requests of people, newest first: the open ones with Draft it, Mark added and Decline; the answered ones folded below.
async function renderRequests() {
  let res;
  try { res = await api("/ui/api/tools/requests"); } catch (e) { $("tool-requests").replaceChildren(el("p", "error", e.message)); return; }
  const open = res.requests.filter((r) => r.status === "waiting" || r.status === "drafting"), done = res.requests.filter((r) => !open.includes(r));
  const row = (r) => {
    const li = el("li", "req"), text = el("div"), acts = el("div", "actions");
    text.append(el("strong", null, r.what), el("span", "muted", ` · ${r.by} ${ago(r.at)}${r.why ? `: "${r.why}"` : ""}`));
    if (r.task) text.append(el("span", "muted", ` · task ${r.task}`));
    if (r.reason) text.append(el("span", "muted", ` · ${r.reason}`));
    const tag = el("span", `pill ${r.status === "waiting" ? "warn" : r.status === "added" ? "ok" : ""}`, REQUEST_STATE[r.status] || r.status);
    const decide = (label, decision, cls, ask) => { const b = el("button", cls, label); b.type = "button";
      b.onclick = () => { const reason = ask ? prompt(ask) : ""; if (ask && reason === null) return;
        run(() => api("/ui/api/admin/tools/request", { id: r.id, decision, reason }), b); }; return b; };
    if (r.status === "waiting") acts.append(decide(SKILL_FOLDER.test(r.what) ? "Add as shared skill" : "Draft it", "draft", "btn small primary", res.draft || SKILL_FOLDER.test(r.what) ? "" : null));
    if (r.status !== "added" && r.status !== "declined") acts.append(decide("Mark added", "added", "btn small", ""), decide("Decline…", "decline", "btn small quiet", `Why decline "${r.what}"? ${r.by} sees the reason.`));
    li.append(tag, text, acts);
    return li;
  };
  const box = [];
  if (open.length) { const ul = el("ul", "req-list"); ul.append(...open.map(row)); box.push(ul); }
  else box.push(el("p", "muted", "No requests waiting."));
  if (!res.draft) box.push(el("p", "muted small", "Draft it needs KNOWLEDGE_TOOLS_MEMO in local.env: the memo a draft task goes into."));
  if (done.length) { const d = el("details"), ul = el("ul", "req-list"); d.append(el("summary", null, `Answered (${done.length})`)); ul.append(...done.map(row)); d.append(ul); box.push(d); }
  $("tool-requests").replaceChildren(...box);
  const waiting = open.filter((r) => r.status === "waiting").length, ext = Object.values(state.external || {}).filter((x) => x.status !== "ok").length;
  $("n-external").textContent = waiting + ext ? `${waiting + ext} !` : ""; $("n-external").classList.toggle("alert", !!(waiting + ext));
}
// The tools the plugin offers, as the pcs report them: name, kind, what it does, on how many of your own pcs.
async function renderCatalog() {
  let res;
  try { res = await api("/ui/api/machines"); } catch (e) { $("tool-catalog").replaceChildren(el("p", "error", e.message)); return; }
  const tools = {}, own = res.machines.filter((m) => m.mine && m.companion);
  for (const m of res.machines) for (const [id, t] of Object.entries(m.companion?.tools || {})) tools[id] = tools[id] || t;
  const ids = Object.keys(tools).sort((x, y) => tools[x].name.localeCompare(tools[y].name));
  if (!ids.length) { $("tool-catalog").replaceChildren(el("p", "muted", "No pc reported its tools yet (plugin 0.41.0 or newer).")); return; }
  const table = el("table"), body = el("tbody"), head = el("thead"), hr = el("tr");
  for (const h of ["Name", "Kind", "What it does", "Installed on"]) hr.append(el("th", null, h));
  head.append(hr);
  for (const id of ids) {
    const t = tools[id], tr = el("tr"), n = own.filter((m) => m.companion.tools?.[id]?.installed).length, link = el("a", null, `${n} of ${own.length} of your pcs`);
    link.href = "/ui#/machines";
    const on = el("td"); on.append(link);
    tr.append(el("td", null, t.name), el("td", "muted", t.kind || ""), el("td", "muted", t.description || ""), on);
    body.append(tr);
  }
  table.append(head, body);
  const wrap = el("div", "table-wrap"); wrap.append(table);
  $("tool-catalog").replaceChildren(wrap);
}

// Settings: a number field per server setting (range and meaning from the server), plus the extra admins.
function renderSettings() {
  const info = state.setting_info, cur = state.settings;
  $("settings-fields").replaceChildren(...Object.entries(info).map(([k, i]) => {
    const wrap = el("div", "setting"), id = `s-${k}`, label = el("label", "", i.label), input = el("input");
    label.htmlFor = id; input.id = id; input.name = k; input.type = "number"; input.required = true;
    input.min = i.min; input.max = i.max; input.step = i.float ? "0.01" : "1"; input.value = cur[k];
    wrap.append(label, input);
    if (i.help) { const h = el("p", "muted setting-help", `${i.help} Range ${i.min}–${i.max}.`); h.id = `${id}-help`; input.setAttribute("aria-describedby", h.id); wrap.append(h); }
    return wrap;
  }));
  $("s-admins").value = cur.extra_admins.join("\n");
  $("s-config-admins").textContent = `Always admin (server config): ${state.config_admins.join(", ") || "nobody"}.`;
}

// One list for the whole server: per role and agent the advice, or the admin's own pick. A pc follows only after Apply on Machines.
function renderModels() {
  const m = state.models; $("models-box").hidden = !m;
  if (!m) return;
  $("models-note").textContent = `Scores are right answers on our reading and review test${m.measured_on ? `, last measured ${m.measured_on}` : ""}. Pick here, then Apply per pc below; nothing else changes a pc. ${m.note}`;
  const name = (c) => `${c.model}${c.effort ? `, ${c.effort}` : ""}${c.score ? ` (${c.score})` : " (not measured)"}`;
  $("models-rows").replaceChildren(...m.roles.flatMap((r) => Object.entries(r.agents).map(([a, v]) => {
    const wrap = el("div", "setting"), id = `m-${r.key}-${a}`, label = el("label", "", `${r.role} · ${a}`), sel = el("select");
    label.htmlFor = id; sel.id = id; sel.name = id;
    const mine = m.chosen[r.key]?.[a], opt = (value, text) => { const o = el("option", "", text); o.value = value; sel.append(o); return o; };
    opt("", v.advised.length ? `Advice: ${name(v.advised[0])}` : "Advice: none");
    const seen = new Set();
    for (const c of v.advised) { seen.add(c.model); opt(JSON.stringify({ model: c.model, effort: c.effort || "" }), name(c)); }
    for (const id2 of v.offered) if (!seen.has(id2)) opt(JSON.stringify({ model: id2, effort: "" }), `${id2} (not measured)`);
    if (mine) {
      const value = JSON.stringify({ model: mine.model, effort: mine.effort || "" });
      if (![...sel.options].some((o) => o.value === value)) opt(value, `${mine.model} (no pc offers it now)`);
      sel.value = value;
    }
    sel.onchange = () => run(() => api("/ui/api/admin/model-choice", { role: r.key, agent: a, ...(sel.value ? JSON.parse(sel.value) : {}) }), sel, "Saved: Apply it per pc below");
    wrap.append(label, sel);
    return wrap;
  })));
  $("models-pcs").replaceChildren(...m.pcs.map((pc) => {
    const row = el("p", "setting-help"), plain = (d) => `${d.agent} ${d.setting.replace("_", " ")}: ${d.now || "not set"} → ${d.wants}`;
    row.append(el("strong", "", pc.label), ` · ${pc.online ? "online" : "offline, runs when it is back"} · `,
      pc.differs.length ? pc.differs.map(plain).join("; ") : pc.unknown.length ? `not reported yet (${pc.unknown.join(", ")})` : "matches");
    if (pc.missing.length) row.append(` · not offered there: ${pc.missing.join(", ")}`);
    if (pc.differs.length || pc.unknown.length) {
      const b = el("button", "btn small", "Apply"); b.type = "button"; b.title = "Write these into the agents' settings on this pc; new sessions use them"; 
      b.onclick = () => run(async () => {
        await api("/ui/api/machines/action", { machine: pc.id, action: "apply_models", args: {} });
        for (let i = 0; i < 12 && pc.online; i++) { await new Promise((r) => setTimeout(r, 3000)); await load(); if (state.models.pcs.find((x) => x.id === pc.id)?.last?.status !== "waiting") break; }
      }, b, "");
      row.append(" ", b);
    }
    if (pc.last) row.append(el("span", pc.last.status === "failed" ? "error" : "muted", ` Last Apply: ${pc.last.status}${pc.last.result ? `: ${pc.last.result}` : ""}`));
    return row;
  }));
  $("models-unmeasured").textContent = m.unmeasured.length ? `Not measured yet: ${m.unmeasured.map((u) => `${u.model} (${u.agent}, on ${u.pcs.join(", ")})`).join("; ")}.` : "Every model a pc offers has been measured.";
}

// One date style on the whole page: "21 Oct 09:26", the year only when it is not this year.
const stamp = (d) => d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  ...(d.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}) });
const day = (when) => new Date(typeof when === "number" ? when * 1000 : `${when}T12:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }); // "1 Nov 2026"
const when = (sec) => sec ? `${stamp(new Date(sec * 1000))} (${ago(sec)})` : "—";
function renderSystem() {
  const sys = state.system, row = (...cells) => { const tr = el("tr"); tr.append(...cells.map((c) => el("td", "", c))); return tr; };
  $("system-head").textContent = `Release ${sys.release} · running since ${when(sys.started)}.`;
  $("background").replaceChildren(...sys.background.map((b) => row(b.name, b.what, when(b.last), b.detail)));
  $("index-roots").replaceChildren(...sys.index.map((r) => { const tr = row(r.root, r.path, String(r.files), String(r.mb)); tr.children[1].className = "mono"; return tr; }));
  $("config").replaceChildren(...sys.config.map((c) => row(c.name, c.value, c.where)));
  $("audit").replaceChildren(...(sys.audit.length ? sys.audit.map((line) => { // "2026-10-21T08:56:45 device action path"
    const [t, who, action, ...rest] = line.split(" "), tr = row(stamp(new Date(t)), who, action, rest.join(" "));
    tr.children[3].className = "mono"; return tr;
  }) : [emptyRow(4, "No activity yet.")]));
  $("gaps").replaceChildren(...(sys.gaps.length ? sys.gaps.map((g) => {
    const tr = row(stamp(new Date(g.last)), String(g.count), g.who.join(", "), `${g.query} (${g.tool}${g.score ? ` ${g.score}` : ""})`);
    tr.children[3].className = "mono"; return tr;
  }) : [emptyRow(4, "No searches without a result.")]));
  $("lessons").replaceChildren(...(sys.lessons.length ? sys.lessons.map((l) =>
    row(stamp(new Date(l.at)), `${l.device} (${l.project})`, l.status, `${l.what} — prevent: ${l.prevent}${l.note ? ` [${l.note}]` : ""}`)
  ) : [emptyRow(4, "No lessons yet.")]));
  for (const k of ["background", "index-roots", "config", "audit", "gaps", "lessons"]) $(`n-${k}`).textContent = sys[{ "index-roots": "index" }[k] || k].length || "";
}

let loaded = false;
async function load() {
  state = await api("/ui/api/admin");
  if (!state.admin) { // a person: own tokens and projects, within the limits the admin set
    const l = state.limits;
    $("crumb").textContent = "My tokens & projects"; document.title = "My tokens & projects · Knowledge";
    for (const sec of ["people", "external", "settings", "system"]) document.querySelector(`[data-sec=${sec}]`).hidden = true;
    for (const id of ["t-watch", "t-skills", "t-self"]) $(id).closest("label").style.display = "none"; // .check sets display, which beats [hidden]
    document.querySelectorAll(".admin-only").forEach((x) => (x.hidden = true));
    $("t-roots").previousElementSibling.textContent = "Projects (tick at least one)";
    $("limits-tokens").textContent = `You may hold ${l.tokens} tokens, for the projects you have access to. Revoke one you no longer use.`;
    $("limits-projects").textContent = `You may create ${l.projects} projects (${l.created} made so far), up to ${l.project_mb} MB each. The admin is told about every new project and token.`;
    renderTokens(); renderProjects();
    $("n-tokens").textContent = state.tokens.length; $("n-projects").textContent = state.projects.length;
    return;
  }
  renderTokens(); renderUsers(); renderProjects(); renderSystem(); renderExternal(); renderRequests(); renderCatalog();
  $("undo-box").hidden = false;
  $("undo-names").replaceChildren(...state.tokens.map((t) => { const o = el("option"); o.value = t.name; return o; }));
  if (!loaded) { loaded = true; applyKind(); } // later reloads keep what is being filled in
  $("n-tokens").textContent = state.tokens.length; $("n-projects").textContent = state.projects.length;
  const users = Object.values(state.users), waiting = users.filter((u) => !Object.keys(u.projects || {}).length).length; // asks for access
  $("n-people").textContent = waiting ? `${waiting} new` : $("users").rows.length; // config admins are rows too
  $("n-people").classList.toggle("alert", !!waiting);
  if (!$("settings-form").contains(document.activeElement)) renderSettings(); // don't overwrite what is being typed
  if (!$("models-box").contains(document.activeElement)) renderModels();
}

$("settings-form").onsubmit = (e) => {
  e.preventDefault();
  const body = Object.fromEntries([...$("settings-fields").querySelectorAll("input")].map((i) => [i.name, Number(i.value)]));
  body.extra_admins = $("s-admins").value.split("\n").map((x) => x.trim()).filter(Boolean);
  run(() => api("/ui/api/admin/settings", body), e.target.querySelector("button"), "Saved: settings").then((ok) => ok && renderSettings());
};

function showNewToken(res) {
  $("new-token-name").textContent = res.name;
  $("new-token-value").textContent = res.token;
  // The setup steps per client and OS are on the help page; here only the vault line for a device of the admin's own.
  const label = `KNOWLEDGE_TOKEN_${res.name.toUpperCase().replace(/[^A-Z0-9]/g, "_")}`;
  $("new-token-setup").textContent = ["read -rs TOKEN", `printf %s "$TOKEN" | secret set ${label}`].join("\n");
  $("new-token-agent").textContent = `/usr/bin/python3 ~/knowledge/clients/knowledge-agent.py install codex ${label}`;
  // The setup asks for the server address; with KNOWLEDGE_URL set it offers this one (install.sh/.ps1 pass the env on).
  const url = res.url || location.origin, raw = "https://raw.githubusercontent.com/To0wnn/knowledge/main";
  $("new-token-url").textContent = url;
  $("new-token-sh").textContent = `curl -fsSL ${raw}/install.sh | KNOWLEDGE_URL='${url}' sh`;
  $("new-token-ps").textContent = `$env:KNOWLEDGE_URL='${url}'; irm ${raw}/install.ps1 | iex`;
  tokenCopied = false;
  $("new-token").hidden = false;
  $("copy-token").textContent = "Copy token";
  document.querySelectorAll(".copy-line").forEach((b) => (b.textContent = "Copy"));
  $("new-token").scrollIntoView({ block: "center" });
}

$("token-form").onsubmit = (e) => {
  e.preventDefault();
  const button = e.target.querySelector("button[type=submit]");
  let res;
  run(async () => {
    res = await api("/ui/api/admin/token", {
      name: $("t-name").value.trim(), write: $("t-write").checked, redact: $("t-redact").checked, watch: $("t-watch").checked,
      skills: $("t-skills").checked, self: $("t-self").checked,
      all: !!$("t-roots").querySelector('input[value="*"]:checked'),
      roots: [...$("t-roots").querySelectorAll("input:checked")].flatMap((c) => c.value.split(",")).filter((r) => r !== "*"),
    });
  }, button).then(() => { if (res) { e.target.reset(); applyKind(); $("new-token-box").open = false; showNewToken(res); } }); // shown once: even if the reload after it failed
};
let tokenCopied = false; // "Done, hide it" asks first while the token was never copied: it is shown only once
async function copyFrom(src, button) {
  try { await navigator.clipboard.writeText(src.textContent); button.textContent = "Copied"; return true; }
  catch { getSelection().selectAllChildren(src); button.textContent = "Selected: press Ctrl+C"; return false; } // no clipboard access
}
$("copy-token").onclick = async () => { tokenCopied = await copyFrom($("new-token-value"), $("copy-token")) || tokenCopied; };
document.addEventListener("copy", () => { if (getSelection().toString().includes($("new-token-value").textContent || "\0")) tokenCopied = true; });
document.querySelectorAll(".copy-line").forEach((b) => (b.onclick = () => copyFrom($(b.dataset.src), b)));
// Undo what one device changed: first the list, then only what that list offered.
function showUndo(rows, device, hours, applied) {
  const offered = rows.filter((r) => r.action && !r.done);
  $("undo-rows").replaceChildren(...(rows.length ? rows : [{ path: "No changes by this device in that time." }]).map((r) =>
    el("li", r.action || r.done ? "" : "muted", r.path + (r.done ? " · undone" : r.skip ? ` · left alone: ${r.skip}` : r.action === "remove" ? " · made by this device: goes to the trash" : r.action ? " · back to the version before" : ""))));
  $("undo-apply").hidden = applied || !offered.length;
  $("undo-apply").onclick = () => {
    if (!confirm(`Undo ${offered.length} file(s) changed by ${device}?`)) return;
    run(async () => showUndo((await api("/ui/api/admin/undo-device", { device, hours, apply: true, paths: offered.map((r) => r.path) })).rows, device, hours, true), $("undo-apply"), "Undone");
  };
}
$("undo-form").onsubmit = (ev) => {
  ev.preventDefault();
  const device = $("undo-device").value.trim(), hours = Number($("undo-hours").value);
  run(async () => showUndo((await api("/ui/api/admin/undo-device", { device, hours })).rows, device, hours), ev.submitter);
};
$("hide-token").onclick = () => {
  if (!tokenCopied && !confirm("The token was not copied, and it is not shown again. Hide it anyway?")) return;
  $("new-token").hidden = true; $("new-token-value").textContent = ""; $("new-token-setup").textContent = "";
};

// "What is this token for?" sets the switches under Fine-tune; changing a switch there by hand clears the choice.
const KINDS = { own: { write: true, redact: false, skills: true, all: true }, guest: { write: true, redact: true, skills: false, all: false },
  reader: { write: false, redact: true, skills: false, all: false } };
function applyKind() {
  const k = KINDS[document.querySelector('input[name="t-kind"]:checked')?.value];
  if (!k) return;
  $("t-write").checked = k.write; $("t-redact").checked = k.redact; $("t-watch").checked = false;
  $("t-skills").checked = k.skills && state.admin; $("t-self").checked = k.skills && state.admin; // "own" = my device
  const all = $("t-roots").querySelector('input[value="*"]');
  if (all) all.checked = k.all;
  checkRules();
}
// Shared skills only where the server allows them; say why instead of refusing after the click.
function checkRules() {
  const ticked = [...$("t-roots").querySelectorAll("input:checked")].map((c) => c.value);
  const why = !$("t-write").checked ? "they need 'May write memos'" : $("t-redact").checked ? "not together with masked secrets"
    : !ticked.some((v) => v === "*" || v === "claude") ? "tick 'shared skills (claude/)' or 'All projects'" : "";
  $("t-skills").disabled = !!why;
  if (why) $("t-skills").checked = false;
  $("t-skills-why").textContent = why && state.admin ? `Shared skills are off: ${why}.` : "";
}
document.querySelectorAll('input[name="t-kind"]').forEach((r) => (r.onchange = applyKind));
for (const id of ["t-write", "t-redact", "t-skills", "t-watch", "t-self"]) {
  $(id).addEventListener("change", () => { document.querySelectorAll('input[name="t-kind"]').forEach((r) => (r.checked = false)); checkRules(); });
}
$("t-roots").addEventListener("change", checkRules);
$("user-form").onsubmit = (e) => {
  e.preventDefault();
  run(() => api("/ui/api/admin/user", { username: $("u-name").value.trim(), projects: {}, add: true }), e.target.querySelector("button"))
    .then((ok) => ok && e.target.reset());
};
$("project-form").onsubmit = (e) => {
  e.preventDefault();
  run(() => api("/ui/api/admin/project", { name: $("p-name").value.trim() }), e.target.querySelector("button"))
    .then((ok) => ok && e.target.reset());
};

// One section at a time, chosen in the nav; the hash keeps it on reload (/ui/admin#settings). A part of System has a
// hash of its own (#gaps, #audit …): it opens System with that part unfolded and in view.
const tabs = [...document.querySelectorAll(".admin-nav [role=tab]")];
function showSection() {
  const want = location.hash.slice(1), sub = want && document.getElementById(`sub-${want}`);
  const sec = sub ? "system" : tabs.some((t) => t.dataset.sec === want && !t.hidden) ? want : "tokens";
  for (const t of tabs) {
    const on = t.dataset.sec === sec;
    t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1;
    $(`sec-${t.dataset.sec}`).hidden = !on;
    if (on) { $("title").textContent = t.firstChild.textContent.trim(); t.scrollIntoView({ block: "nearest", inline: "nearest" }); } // the bar scrolls on a phone
  }
  if (sub && !tabs.find((t) => t.dataset.sec === "system").hidden) { sub.open = true; sub.scrollIntoView({ block: "start" }); }
}
tabs.forEach((t) => {
  t.onclick = () => { history.replaceState(null, "", `#${t.dataset.sec}`); showSection(); };
  t.onkeydown = (e) => { // a tablist: Left and Right go through the visible tabs
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const on = tabs.filter((x) => !x.hidden), n = on[(on.indexOf(t) + (e.key === "ArrowRight" ? 1 : on.length - 1)) % on.length];
    e.preventDefault(); n.focus(); n.click();
  };
});
addEventListener("hashchange", showSection);
showSection();

load().then(showSection).catch(showError); // after load: a person's hidden tabs are known
