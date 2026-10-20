// Knowledge admin: device tokens, people (project access) and projects. Admins only (the server checks every call).
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

let state = null;

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

function renderTokens() {
  $("tokens").replaceChildren(...state.tokens.map((t) => {
    const tr = el("tr");
    const access = t.watch ? "VM watcher" : t.write ? "read + write" : "read";
    const name = el("td", "mono", t.name);
    if (SYSTEM.has(t.name) || t.watch) name.append(el("span", "muted", " · used by the VM"));
    tr.append(name, el("td", "", access), el("td", "", scopeText(t) + (t.redact ? " · secrets masked" : "")),
      el("td", "muted", t.created ? `${t.created}${t.by ? " · " + t.by : ""}` : "before the admin page"));
    const td = el("td"), b = el("button", "ghost danger", "Revoke");
    b.setAttribute("aria-label", `Revoke ${t.name}`);
    b.onclick = () => {
      const warn = SYSTEM.has(t.name) || t.watch ? " The VM uses this token: live updates, the weekly report or the VM sessions stop." : "";
      if (confirm(`Revoke ${t.name}? Devices using it stop working right away.${warn}`)) run(() => api("/ui/api/admin/token", { name: t.name, revoke: true }), b);
    };
    td.append(b); tr.append(td);
    return tr;
  }));
  const boxes = state.projects.map((p) => [p.name, [p.docs, p.memory]]).concat([["shared skills (claude/)", ["claude"]]]);
  $("t-roots").replaceChildren(...boxes.map(([label, roots]) => {
    const l = el("label", "check"), c = el("input"); c.type = "checkbox"; c.value = roots.join(",");
    l.append(c, " " + label); return l;
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
    if (u.first_seen) who.title = `first signed in ${new Date(u.first_seen * 1000).toLocaleString()}`;
    tr.append(who);
    const selects = projects.map((p) => {
      const s = el("select"); s.setAttribute("aria-label", `${name}: ${p}`);
      for (const m of ["none", "read", "write"]) { const o = el("option", "", m); o.value = m; s.append(o); }
      s.value = (u.projects || {})[p] || "none";
      const td = el("td"); td.append(s); tr.append(td);
      return [p, s];
    });
    // A change is saved right away; the table reloads from the server, so what you see is what is stored.
    for (const [, s] of selects) {
      s.onchange = () => run(() => api("/ui/api/admin/user", { username: name, projects: Object.fromEntries(selects.map(([p, x]) => [p, x.value])) }), s);
    }
    const td = el("td", "actions"), del = el("button", "ghost danger", "Remove");
    del.setAttribute("aria-label", `Remove ${name}`);
    del.onclick = () => confirm(`Remove ${name}? They lose access right away.`) && run(() => api("/ui/api/admin/user", { username: name, delete: true }), del);
    td.append(del); tr.append(td);
    rows.push(tr);
  }
  $("users").replaceChildren(...rows);
}

function renderProjects() {
  $("projects").replaceChildren(...state.projects.map((p) => {
    const tr = el("tr");
    tr.append(el("td", "", p.name), el("td", "mono", p.docs + "/"), el("td", "mono", p.memory + "/"),
      el("td", "muted", p.builtin ? "VM (built in)" : "server"));
    return tr;
  }));
}

async function load() {
  state = await api("/ui/api/admin");
  renderTokens(); renderUsers(); renderProjects();
}

function showNewToken(res) {
  $("new-token-name").textContent = res.name;
  $("new-token-value").textContent = res.token;
  // The same steps as README "Install on a device": the token goes in over stdin, so it stays out of shell history.
  const label = `KNOWLEDGE_TOKEN_${res.name.toUpperCase().replace(/[^A-Z0-9]/g, "_")}`;
  $("new-token-setup").textContent = [
    "read -rs TOKEN   # paste the token, then Enter (keeps it out of the shell history)",
    `printf %s "$TOKEN" | secret set ${label}   # on the VM: keep it in the vault`,
    "claude plugin marketplace add to0wnn@<lan-ip>:/home/to0wnn/knowledge   # or the GitHub URL",
    "claude plugin install knowledge@knowledge --scope user",
    `printf '{"url":"${location.origin}","token":"%s"}' "$TOKEN" | claude plugin configure knowledge@knowledge --values-stdin`,
  ].join("\n");
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
      roots: [...$("t-roots").querySelectorAll("input:checked")].flatMap((c) => c.value.split(",")),
    });
  }, button).then((ok) => { if (ok) { e.target.reset(); showNewToken(res); } });
};
$("copy-token").onclick = async () => {
  try { await navigator.clipboard.writeText($("new-token-value").textContent); $("copy-token").textContent = "Copied"; }
  catch { getSelection().selectAllChildren($("new-token-value")); } // no clipboard access: select it for Ctrl+C
};
$("hide-token").onclick = () => { $("new-token").hidden = true; $("new-token-value").textContent = ""; $("new-token-setup").textContent = ""; };
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

load().catch(showError);
