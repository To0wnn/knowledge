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
  const j = await r.json().catch(() => ({ error: `${r.status} ${r.statusText}` })); // a proxy error page is no JSON
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
});
// Tokens the VM itself uses: revoking one breaks the live sync, the weekly report or the VM sessions.
const SYSTEM = new Set(["vm-watcher", "vm-sessions", "vm-lint-report"]);
const openClients = new Set();  // tokens whose client rows are unfolded; kept across re-renders

let state = null;
const ago = (sec) => { if (!sec) return "never"; const d = Date.now() / 1000 - sec;
  return d < 900 ? "in the last 15 min" : d < 3600 ? `${Math.round(d / 60)} min ago` : d < 86400 ? `${Math.round(d / 3600)} h ago` : `${Math.round(d / 86400)} days ago`; };

function showError(e) { $("error").textContent = e ? String(e.message || e) : ""; $("error").hidden = !e; }

// One action at a time: the clicked control is disabled until the server answered, then the page reloads its state.
async function run(fn, control) {
  showError(null);
  if (control) control.disabled = true;
  try { await fn(); await load(); return true; } catch (e) { showError(e); return false; } finally { if (control) control.disabled = false; }
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
    run(() => api("/ui/api/admin/token", { name: t.name, update: true, skills: c.checked }), c);
  };
  l.append(c, " " + (t.skills ? "on" : "off")); td.append(l); return td;
}

// "behind" when older than the newest; "default URL" when the device names no server (that default will go);
// plugins before 0.10.7 do not say, so they cannot count as safe yet.
const pluginText = (p) => `${p.v}${p.behind ? " (behind)" : ""}${p.default_url ? " · default URL" : p.default_url === null ? " · URL not reported" : ""}`;

// The plugin version per agent this token reported at its last session start.
function pluginCell(t, open) {
  const td = el("td", "muted");
  const own = new Set(open ? t.clients.map((c) => c.agent) : []);  // unfolded, those show it in their own rows
  const rest = (t.plugin || []).filter((p) => !own.has(p.agent));
  if (!rest.length) { td.textContent = "–"; return td; }
  rest.forEach((p, i) => {
    if (i) td.append(el("br"));
    const v = el("span", p.behind ? "plugin-v unused" : "plugin-v", `${p.agent} ${pluginText(p)}`);
    v.title = `session start ${ago(p.at)}`;  // the time is in Last used and the client rows; here it only wrapped
    td.append(v);
  });
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
    const td = el("td"), b = el("button", "ghost danger", "Forget");
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
      const fold = el("button", "ghost fold", `${open ? "▾" : "▸"} ${n} client${n === 1 ? "" : "s"}`);
      fold.setAttribute("aria-expanded", String(Boolean(open)));
      fold.onclick = () => { openClients.has(t.name) ? openClients.delete(t.name) : openClients.add(t.name); renderTokens(); };
      name.append(" ", fold);
    }
    tr.append(name, el("td", "", access), el("td", "", scopeText(t) + (t.redact ? " · secrets masked" : "")), skillsCell(t),
      el("td", "muted", t.created ? `${day(t.created)}${t.by ? " · " + t.by : ""}` : "before the admin page"),
      el("td", t.last_used ? "muted" : "unused", t.last_used ? ago(t.last_used) : "never used"), pluginCell(t, open));
    const td = el("td"), b = el("button", "ghost danger", "Revoke");
    b.setAttribute("aria-label", `Revoke ${t.name}`);
    b.onclick = () => {
      const warn = SYSTEM.has(t.name) || t.watch ? " The VM uses this token: live updates, the weekly report or the VM sessions stop." : "";
      if (confirm(`Revoke ${t.name}? Devices using it stop working right away.${warn}`)) run(() => api("/ui/api/admin/token", { name: t.name, revoke: true }), b);
    };
    td.append(b); tr.append(td);
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
          } catch (e) { showError(e); load().catch(showError); }
        }, 700);
      };
    }
    const td = el("td", "actions"), del = el("button", "ghost danger", "Remove");
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
  input.onchange = () => run(() => api("/ui/api/admin/project-label", { name: p.name, label: input.value.trim() }), input);
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
    run(() => api("/ui/api/admin/task-writers", { name: p.name, writers: w.length ? w : null }), input); };
  td.append(input);
  return td;
}

function renderProjects() {
  if (!state.admin) {
    $("projects").replaceChildren(...state.projects.map((p) => { const tr = el("tr");
      tr.append(el("td", "", p.name), el("td", "mono", p.docs + "/"), el("td", "mono", p.memory + "/"), el("td", "num", "—"), el("td", "num", "—"),
        el("td", "", p.mode === "write" ? "you: read + write" : "you: read"), el("td", "muted", "—"), el("td", "muted", p.builtin ? "VM (built in)" : "server"));
      return tr; }));
    return;
  }
  const files = Object.fromEntries(state.system.index.map((r) => [r.root, r.files]));
  const bytes = Object.fromEntries(state.system.index.map((r) => [r.root, r.bytes || 0]));
  $("projects").replaceChildren(...state.projects.map((p) => {
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
    const button = (label, action, cls) => { const b = el("button", cls || "ghost", label); b.type = "button";
      b.setAttribute("aria-label", `${label} ${name}`);
      b.onclick = () => (action !== "remove" || confirm(`Remove the skill ${name} from every device?`)) &&
        run(() => api("/ui/api/admin/external", { name, action }), b);
      return b; };
    act.append(button("Check now", "check"), ...(x.status === "waiting" ? [button("Approve", "approve", "")] : []), button("Remove", "remove", "ghost danger"));
    const srcTd = el("td"); srcTd.append(src);
    tr.append(el("td", "", name), srcTd, el("td", "mono", x.commit.slice(0, 7) + (x.pending ? ` → ${x.pending.slice(0, 7)}` : "")),
      el("td", "", x.licence), status, el("td", "", ago(x.checked)), act);
    return tr;
  }) : [(() => { const tr = el("tr"), td = el("td", "muted", "No external skills yet."); td.colSpan = 7; tr.append(td); return tr; })()]));
  const waiting = rows.filter(([, x]) => x.status !== "ok").length;
  $("n-external").textContent = waiting ? `${waiting} !` : rows.length || "";
  $("n-external").classList.toggle("alert", !!waiting);
}
$("external-form").onsubmit = (e) => {
  e.preventDefault();
  run(() => api("/ui/api/admin/external", { url: $("x-url").value.trim() }), e.target.querySelector("button"))
    .then((ok) => ok && e.target.reset());
};

// Settings: a number field per server setting (range and meaning from the server), plus the extra admins.
function renderSettings() {
  const info = state.setting_info, cur = state.settings;
  $("settings-fields").replaceChildren(...Object.entries(info).map(([k, i]) => {
    const wrap = el("div", "setting"), id = `s-${k}`, label = el("label", "", i.label), input = el("input");
    label.htmlFor = id; input.id = id; input.name = k; input.type = "number"; input.required = true;
    input.min = i.min; input.max = i.max; input.step = i.float ? "0.01" : "1"; input.value = cur[k];
    wrap.append(label, input); return wrap;
  }));
  $("s-admins").value = cur.extra_admins.join("\n");
  $("s-config-admins").textContent = `Always admin (server config): ${state.config_admins.join(", ") || "nobody"}.`;
}

// One date style on the whole page: "21 Oct 09:26", the year only when it is not this year.
const stamp = (d) => d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  ...(d.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}) });
const day = (iso) => new Date(`${iso}T12:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
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
  }) : [row("—", "", "", "")]));
  $("gaps").replaceChildren(...(sys.gaps.length ? sys.gaps.map((g) => {
    const tr = row(stamp(new Date(g.last)), String(g.count), g.who.join(", "), `${g.query} (${g.tool}${g.score ? ` ${g.score}` : ""})`);
    tr.children[3].className = "mono"; return tr;
  }) : [row("—", "", "", "")]));
  $("lessons").replaceChildren(...(sys.lessons.length ? sys.lessons.map((l) =>
    row(stamp(new Date(l.at)), `${l.device} (${l.project})`, l.status, `${l.what} — prevent: ${l.prevent}${l.note ? ` [${l.note}]` : ""}`)
  ) : [row("—", "", "", "")]));
}

let loaded = false;
async function load() {
  state = await api("/ui/api/admin");
  if (!state.admin) { // a person: own tokens and projects, within the limits the admin set
    const l = state.limits;
    document.querySelector(".brand").textContent = `Knowledge · ${state.user}`;
    for (const sec of ["people", "external", "settings", "system"]) document.querySelector(`[data-sec=${sec}]`).hidden = true;
    for (const id of ["t-watch", "t-skills"]) $(id).closest("label").style.display = "none"; // .check sets display, which beats [hidden]
    document.querySelectorAll(".admin-only").forEach((x) => (x.hidden = true));
    $("t-roots").previousElementSibling.textContent = "Projects (tick at least one)";
    $("limits-tokens").textContent = `You may hold ${l.tokens} tokens, for the projects you have access to. Revoke one you no longer use.`;
    $("limits-projects").textContent = `You may create ${l.projects} projects (${l.created} made so far), up to ${l.project_mb} MB each. The admin is told about every new project and token.`;
    renderTokens(); renderProjects();
    $("n-tokens").textContent = state.tokens.length; $("n-projects").textContent = state.projects.length;
    return;
  }
  renderTokens(); renderUsers(); renderProjects(); renderSystem(); renderExternal();
  if (!loaded) { loaded = true; applyKind(); } // later reloads keep what is being filled in
  $("n-tokens").textContent = state.tokens.length; $("n-projects").textContent = state.projects.length;
  const users = Object.values(state.users), waiting = users.filter((u) => !Object.keys(u.projects || {}).length).length; // asks for access
  $("n-people").textContent = waiting ? `${waiting} new` : $("users").rows.length; // config admins are rows too
  $("n-people").classList.toggle("alert", !!waiting);
  if (!$("settings-form").contains(document.activeElement)) renderSettings(); // don't overwrite what is being typed
}

$("settings-form").onsubmit = (e) => {
  e.preventDefault();
  const body = Object.fromEntries([...$("settings-fields").querySelectorAll("input")].map((i) => [i.name, Number(i.value)]));
  body.extra_admins = $("s-admins").value.split("\n").map((x) => x.trim()).filter(Boolean);
  run(() => api("/ui/api/admin/settings", body), e.target.querySelector("button")).then((ok) => {
    if (ok) { renderSettings(); $("settings-saved").textContent = "Saved."; setTimeout(() => ($("settings-saved").textContent = ""), 3000); }
  });
};

function showNewToken(res) {
  $("new-token-name").textContent = res.name;
  $("new-token-value").textContent = res.token;
  // The setup steps per client and OS are on the help page; here only the vault line for a device of the admin's own.
  const label = `KNOWLEDGE_TOKEN_${res.name.toUpperCase().replace(/[^A-Z0-9]/g, "_")}`;
  $("new-token-setup").textContent = ["read -rs TOKEN", `printf %s "$TOKEN" | secret set ${label}`].join("\n");
  $("new-token-agent").textContent = `/usr/bin/python3 ~/knowledge/clients/knowledge-agent.py install codex ${label}`;
  $("new-token").hidden = false;
  $("copy-token").textContent = "Copy token";
  $("new-token").scrollIntoView({ block: "center" });
}

$("token-form").onsubmit = (e) => {
  e.preventDefault();
  const button = e.target.querySelector("button[type=submit]");
  let res;
  run(async () => {
    res = await api("/ui/api/admin/token", {
      name: $("t-name").value.trim(), write: $("t-write").checked, redact: $("t-redact").checked, watch: $("t-watch").checked,
      skills: $("t-skills").checked,
      all: !!$("t-roots").querySelector('input[value="*"]:checked'),
      roots: [...$("t-roots").querySelectorAll("input:checked")].flatMap((c) => c.value.split(",")).filter((r) => r !== "*"),
    });
  }, button).then(() => { if (res) { e.target.reset(); applyKind(); $("new-token-box").open = false; showNewToken(res); } }); // shown once: even if the reload after it failed
};
$("copy-token").onclick = async () => {
  try { await navigator.clipboard.writeText($("new-token-value").textContent); $("copy-token").textContent = "Copied"; }
  catch { getSelection().selectAllChildren($("new-token-value")); } // no clipboard access: select it for Ctrl+C
};
$("hide-token").onclick = () => { $("new-token").hidden = true; $("new-token-value").textContent = ""; $("new-token-setup").textContent = ""; };

// "What is this token for?" sets the switches under Fine-tune; changing a switch there by hand clears the choice.
const KINDS = { own: { write: true, redact: false, skills: true, all: true }, guest: { write: true, redact: true, skills: false, all: false },
  reader: { write: false, redact: true, skills: false, all: false } };
function applyKind() {
  const k = KINDS[document.querySelector('input[name="t-kind"]:checked')?.value];
  if (!k) return;
  $("t-write").checked = k.write; $("t-redact").checked = k.redact; $("t-watch").checked = false;
  $("t-skills").checked = k.skills && state.admin;
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
for (const id of ["t-write", "t-redact", "t-skills", "t-watch"]) {
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

// One section at a time, chosen in the nav; the hash keeps it on reload (/ui/admin#settings).
const tabs = [...document.querySelectorAll(".admin-nav [role=tab]")];
function showSection() {
  const want = location.hash.slice(1), sec = tabs.some((t) => t.dataset.sec === want && !t.hidden) ? want : "tokens";
  for (const t of tabs) {
    t.setAttribute("aria-selected", String(t.dataset.sec === sec));
    $(`sec-${t.dataset.sec}`).hidden = t.dataset.sec !== sec;
  }
}
for (const t of tabs) t.onclick = () => { history.replaceState(null, "", `#${t.dataset.sec}`); showSection(); };
addEventListener("hashchange", showSection);
showSection();

load().then(showSection).catch(showError); // after load: a person's hidden tabs are known
