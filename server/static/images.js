// The images of one chat in a tab of their own (/ui/images?machine=&id=&file=<id>/<name>, opened from an image in the
// chat): scroll through them, zoom and pan, close the tab. The chat's events say which images it holds.
const query = new URLSearchParams(location.search), machine = query.get("machine") || "", chat = query.get("id") || "";
const REF = /⟦file:([0-9a-f]{12}\/[^⟧]+)⟧/g, IMAGE = /\.(png|jpe?g|gif|webp)$/i, ZOOM_MAX = 8;
const $ = (id) => document.getElementById(id), enc = encodeURIComponent;
const stage = $("iv-stage"), img = $("iv-img"), thumbs = $("iv-thumbs");
const fileUrl = (ref) => `/ui/api/chat/file?machine=${enc(machine)}&file=${enc(ref)}`;
const nameOf = (ref) => ref.slice(ref.indexOf("/") + 1);
let files = [], current = "", after = 0;
let s = 1, x = 0, y = 0, fit = 1;

// What the chat shows as an image: the owner's attachments and the tool lines, subagents' too; each file once.
function collect(events) {
  for (const e of events) {
    after = Math.max(after, e.n);
    if (e.kind !== "user" && e.kind !== "tool") continue;
    for (const m of (e.text || "").matchAll(REF)) if (IMAGE.test(m[1]) && !files.includes(m[1])) files.push(m[1]);
  }
}
async function load() {
  try {
    const res = await fetch(`/ui/api/chat?machine=${enc(machine)}&id=${enc(chat)}&after=${after}&peek=1`);
    if (res.ok) collect((await res.json()).events);
  } catch { /* offline: the images known so far stay */ }
  renderThumbs();
}

function renderThumbs() {
  if (thumbs.children.length !== files.length) {
    thumbs.replaceChildren(...files.map((ref) => {
      const b = document.createElement("button"), t = document.createElement("img");
      b.type = "button"; b.className = "iv-thumb"; b.title = nameOf(ref); b.onclick = () => show(ref);
      t.src = fileUrl(ref); t.alt = nameOf(ref); t.loading = "lazy"; t.draggable = false;
      b.append(t);
      return b;
    }));
  }
  thumbs.hidden = files.length < 2;
  const i = files.indexOf(current);
  [...thumbs.children].forEach((b, n) => (n === i ? b.setAttribute("aria-current", "true") : b.removeAttribute("aria-current")));
  thumbs.children[i]?.scrollIntoView({ inline: "center", block: "nearest" });
  $("iv-count").textContent = files.length > 1 ? `${i + 1} / ${files.length}` : "";
  $("iv-prev").hidden = $("iv-next").hidden = files.length < 2;
  $("iv-prev").disabled = i <= 0;
  $("iv-next").disabled = i >= files.length - 1;
}

function show(ref) {
  if (!ref) return;
  current = ref;
  query.set("file", ref);
  history.replaceState(null, "", `?${query}`); // no new history entry: a tab with one entry may close itself
  $("iv-msg").hidden = true;
  img.hidden = false;
  img.alt = $("iv-name").textContent = document.title = nameOf(ref);
  img.src = fileUrl(ref);
  renderThumbs();
  const i = files.indexOf(ref);
  for (const near of [files[i - 1], files[i + 1]]) if (near) new Image().src = fileUrl(near);
}
const step = (by) => show(files[files.indexOf(current) + by]);

function paint() {
  const W = stage.clientWidth, H = stage.clientHeight, w = img.naturalWidth * s, h = img.naturalHeight * s;
  x = w <= W ? (W - w) / 2 : Math.min(0, Math.max(W - w, x)); // small: centred; large: no empty side beside it
  y = h <= H ? (H - h) / 2 : Math.min(0, Math.max(H - h, y));
  img.style.transform = `translate(${x}px, ${y}px) scale(${s})`;
  $("iv-pct").textContent = `${Math.round(s * 100)}%`;
  stage.classList.toggle("zoomed", zoomed());
  $("iv-out").disabled = !zoomed();
  $("iv-in").disabled = s >= ZOOM_MAX;
}
const zoomed = () => s > fit * 1.001;
function fitView() {
  if (!img.naturalWidth) return;
  fit = Math.min(1, stage.clientWidth / img.naturalWidth, stage.clientHeight / img.naturalHeight);
  s = fit;
  paint();
}
function zoomAt(to, px = stage.clientWidth / 2, py = stage.clientHeight / 2) { // the point under (px, py) stays there
  const next = Math.max(fit, Math.min(ZOOM_MAX, to));
  x = px - (px - x) * (next / s);
  y = py - (py - y) * (next / s);
  s = next;
  paint();
}
img.onload = fitView;
img.onerror = () => {
  img.hidden = true;
  $("iv-msg").hidden = false;
  $("iv-msg").textContent = "This image is no longer here (chat files are kept 30 days).";
};
new ResizeObserver(() => {
  if (!img.naturalWidth) return;
  const wasZoomed = zoomed();
  fit = Math.min(1, stage.clientWidth / img.naturalWidth, stage.clientHeight / img.naturalHeight);
  if (wasZoomed) paint(); else fitView();
}).observe(stage);

// Wheel: Ctrl (also how a trackpad reports a pinch) zooms, a zoomed image pans, else on to the next image. One flick
// of a trackpad keeps sending events for a while: it moves one image.
let wheelSum = 0, wheelLast = 0, wheelHeld = false;
stage.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = stage.getBoundingClientRect();
  if (e.ctrlKey || e.metaKey) return zoomAt(s * Math.exp(-e.deltaY * 0.004), e.clientX - rect.left, e.clientY - rect.top);
  if (zoomed()) { x -= e.deltaX; y -= e.deltaY; return paint(); }
  const now = e.timeStamp, quiet = now - wheelLast > 150;
  wheelLast = now;
  if (quiet) { wheelHeld = false; wheelSum = 0; }
  if (wheelHeld) return;
  wheelSum += Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
  if (Math.abs(wheelSum) >= 40) { step(Math.sign(wheelSum)); wheelHeld = true; }
}, { passive: false });
thumbs.addEventListener("wheel", (e) => { // a plain mouse wheel moves the strip sideways
  if (!e.deltaY) return;
  e.preventDefault();
  thumbs.scrollLeft += e.deltaY;
}, { passive: false });

// Pointers: drag pans a zoomed image, two fingers pinch, a swipe on a fitted image goes on, a double tap or click zooms.
const pointers = new Map();
let pinch = 0, downAt = null, lastTap = 0;
stage.addEventListener("pointerdown", (e) => {
  if (e.target.closest("button")) return;
  stage.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, [e.clientX, e.clientY]);
  downAt = pointers.size === 1 ? [e.clientX, e.clientY] : null;
  pinch = 0;
});
stage.addEventListener("pointermove", (e) => {
  const was = pointers.get(e.pointerId);
  if (!was) return;
  pointers.set(e.pointerId, [e.clientX, e.clientY]);
  if (pointers.size === 2) {
    const [a, b] = [...pointers.values()], gap = Math.hypot(a[0] - b[0], a[1] - b[1]), rect = stage.getBoundingClientRect();
    if (pinch) zoomAt(s * (gap / pinch), (a[0] + b[0]) / 2 - rect.left, (a[1] + b[1]) / 2 - rect.top);
    pinch = gap;
  } else if (zoomed()) {
    x += e.clientX - was[0];
    y += e.clientY - was[1];
    paint();
  }
});
function pointerEnd(e) {
  if (!pointers.delete(e.pointerId) || !downAt || e.type === "pointercancel") return;
  const dx = e.clientX - downAt[0], dy = e.clientY - downAt[1];
  if (Math.hypot(dx, dy) < 10) {
    if (e.timeStamp - lastTap < 300) {
      const rect = stage.getBoundingClientRect();
      if (zoomed()) fitView(); else zoomAt(Math.max(1, fit * 2), e.clientX - rect.left, e.clientY - rect.top);
      lastTap = 0;
    } else lastTap = e.timeStamp;
  } else if (e.pointerType === "touch" && !zoomed() && Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) step(dx < 0 ? 1 : -1);
}
stage.addEventListener("pointerup", pointerEnd);
stage.addEventListener("pointercancel", pointerEnd);

function closeTab() { // not "close": a global function by that name would replace window.close
  window.close();
  // A browser may refuse (a tab that went elsewhere first, an app window): then back to the chat.
  setTimeout(() => location.replace(machine && chat ? `/ui#/chats/${enc(machine)}/${enc(chat)}` : "/ui"), 200);
}
$("iv-close").onclick = closeTab;
$("iv-prev").onclick = () => step(-1);
$("iv-next").onclick = () => step(1);
$("iv-in").onclick = () => zoomAt(s * 1.5);
$("iv-out").onclick = () => zoomAt(s / 1.5);
$("iv-pct").onclick = fitView;
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return; // the browser's own shortcuts
  const act = { Escape: closeTab, ArrowLeft: () => step(-1), ArrowRight: () => step(1), ArrowUp: () => step(-1), ArrowDown: () => step(1),
    Home: () => show(files[0]), End: () => show(files[files.length - 1]),
    "+": () => zoomAt(s * 1.5), "=": () => zoomAt(s * 1.5), "-": () => zoomAt(s / 1.5), 0: fitView }[e.key];
  if (act) { e.preventDefault(); act(); }
});
window.addEventListener("focus", load); // back from the chat: the images the agent looked at meanwhile

(async () => {
  const start = query.get("file") || "";
  await load();
  if (start && !files.includes(start)) files.unshift(start); // older than what the chat keeps: still shown
  show(start || files[files.length - 1]);
  if (!files.length) {
    img.hidden = true;
    $("iv-msg").hidden = false;
    $("iv-msg").textContent = "This chat has no images.";
    renderThumbs();
  }
})();
