// The help page (/ui/help, and inline on the public /setup page): client tabs, the OS switch, copy buttons.
"use strict";
for (const e of document.querySelectorAll('.server-url')) e.textContent = location.origin;
const tabs = [...document.querySelectorAll('.client-tabs [role="tab"]')], oses = [...document.querySelectorAll('.os [role="radio"]')];
let client = 'claude', os = /Windows/.test(navigator.userAgent) ? 'windows' : 'linux';
try { os = localStorage.getItem('kn_help_os') || os; } catch (e) {}
function render(push) {
  for (const t of tabs) {
    const on = t.getAttribute('aria-controls') === client;
    t.setAttribute('aria-selected', String(on));
    document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
  }
  for (const b of oses) b.setAttribute('aria-checked', String(b.dataset.os === os));
  for (const e of document.querySelectorAll('.wrap [data-os]:not([role])')) e.hidden = e.dataset.os !== os;
  if (push) { try { history.replaceState(null, '', '#' + client + '-' + os); localStorage.setItem('kn_help_os', os); } catch (e) {} }
}
tabs.forEach((t, i) => {
  t.addEventListener('click', () => { client = t.getAttribute('aria-controls'); render(true); });
  t.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    n.focus(); n.click();
  });
});
oses.forEach((b) => b.addEventListener('click', () => { os = b.dataset.os; render(true); }));
function fromHash() { // #codex-windows, also #codex; a link from the admin page may change it while the page is open
  const [c, o] = location.hash.slice(1).split('-');
  if (['claude', 'agy', 'codex'].includes(c)) client = c;
  if (['linux', 'windows'].includes(o)) os = o;
  render(false);
}
window.addEventListener('hashchange', fromHash);
fromHash();
document.querySelectorAll('.copy').forEach((b) => b.addEventListener('click', async () => {
  const pre = b.parentElement.querySelector('pre');
  try { await navigator.clipboard.writeText(pre.textContent); b.textContent = 'Copied'; }
  catch (e) { const r = document.createRange(); r.selectNodeContents(pre); const s = getSelection(); s.removeAllRanges(); s.addRange(r); b.textContent = 'Selected'; }
  setTimeout(() => (b.textContent = 'Copy'), 1500);
}));
