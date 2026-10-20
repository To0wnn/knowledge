// Knowledge admin: device tokens, people (project access) and projects. Admins only (the server checks every call).
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const api = (path, body) => fetch(path, {
  method: body ? "POST" : "GET", credentials: "same-origin",
  headers: { "X-Knowledge-UI": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
  body: body ? JSON.stringify(body) : undefined,
}).then(async (r) => { const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j; });

let state = null;

function showError(e) { $("error").textContent = e ? String(e.message || e) : ""; $("error").hidden = !e; }

async function run(fn) {
  showError(null);
  try { await fn(); await load(); } catch (e) { showError(e); }
}

function renderTokens() {
  $("tokens").replaceChildren(...state.tokens.map((t) => {
    const tr = el("tr");
    const access = t.watch ? "watcher" : t.write ? "read + write" : "read";
    const scope = (t.roots && t.roots.length ? t.roots.join(", ") : "all projects") + (t.redact ? " · secrets masked" : "");
    tr.append(el("td", "mono", t.name), el("td", "", access), el("td", "", scope), el("td", "muted", t.created || "—"));
    const td = el("td"), b = el("button", "ghost danger", "Revoke");
    b.onclick = () => confirm(`Revoke ${t.name}? Devices using it stop working right away.`) &&
      run(() => api("/ui/api/admin/token", { name: t.name, revoke: true }));
    td.append(b); tr.append(td);
    return tr;
  }));
  $("t-roots").replaceChildren(...state.roots.map((r) => {
    const l = el("label", "check"), c = el("input"); c.type = "checkbox"; c.value = r;
    l.append(c, " " + r); return l;
  }));
}

function renderUsers() {
  const projects = state.projects.map((p) => p.name);
  const head = el("tr"); head.append(el("th", "", "Person"), ...projects.map((p) => el("th", "", p)), el("th"));
  $("users-head").replaceChildren(head);
  const rows = state.admins.map((a) => {
    const tr = el("tr"); tr.append(el("td", "mono", a), ...projects.map(() => el("td", "muted", "admin")), el("td"));
    return tr;
  });
  for (const [name, u] of Object.entries(state.users).sort()) {
    const tr = el("tr"); tr.append(el("td", "mono", name));
    const selects = projects.map((p) => {
      const s = el("select"); s.setAttribute("aria-label", `${name}: ${p}`);
      for (const m of ["none", "read", "write"]) { const o = el("option", "", m); o.value = m; s.append(o); }
      s.value = (u.projects || {})[p] || "none";
      const td = el("td"); td.append(s); tr.append(td);
      return [p, s];
    });
    const td = el("td", "actions");
    const save = el("button", "", "Save"), del = el("button", "ghost danger", "Remove");
    save.onclick = () => run(() => api("/ui/api/admin/user", { username: name, projects: Object.fromEntries(selects.map(([p, s]) => [p, s.value])) }));
    del.onclick = () => confirm(`Remove ${name}? They lose access right away.`) && run(() => api("/ui/api/admin/user", { username: name, delete: true }));
    td.append(save, del); tr.append(td);
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

$("token-form").onsubmit = (e) => {
  e.preventDefault();
  run(async () => {
    const res = await api("/ui/api/admin/token", {
      name: $("t-name").value.trim(), write: $("t-write").checked, redact: $("t-redact").checked,
      roots: [...$("t-roots").querySelectorAll("input:checked")].map((c) => c.value),
    });
    $("new-token-name").textContent = res.name;
    $("new-token-value").textContent = res.token;
    $("new-token").hidden = false;
    e.target.reset();
  });
};
$("copy-token").onclick = async () => {
  try { await navigator.clipboard.writeText($("new-token-value").textContent); $("copy-token").textContent = "Copied"; }
  catch { getSelection().selectAllChildren($("new-token-value")); } // no clipboard access: select it for Ctrl+C
};
$("user-form").onsubmit = (e) => {
  e.preventDefault();
  run(() => api("/ui/api/admin/user", { username: $("u-name").value.trim(), projects: {} }).then(() => e.target.reset()));
};
$("project-form").onsubmit = (e) => {
  e.preventDefault();
  run(() => api("/ui/api/admin/project", { name: $("p-name").value.trim() }).then(() => e.target.reset()));
};

load().catch(showError);
