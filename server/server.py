"""Kennis server: live, shared access to the homelab docs and Claude memory for Claude Code on any device.

The files on the VM stay the single source of truth (VM sessions keep using them natively). Other
devices reach them through this MCP server over HTTPS via the `kennis` plugin. Design choices that keep
it from breaking:
  - stateless streamable HTTP + JSON responses: no MCP sessions that expire or get lost on a restart;
  - a static bearer token per device (no login that times out); revoke a device by removing its token;
  - responses are gzip-compressed by the server when the client accepts it;
  - writes are serialized per file and atomic; `kennis_edit` replaces one unique snippet (concurrent
    edits elsewhere in the file survive), `kennis_write` refuses when the file changed since it was read.

Config: tokens.json with hashes only: {"<device>": {"sha256": "<hex of sha256(token)>", "write": bool, "watch": bool}}
Run:    uv run server.py   (systemd user unit kennis-server.service)
"""
import asyncio
import fnmatch
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextvars import ContextVar
from pathlib import Path, PurePosixPath

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # anticipated refusals: the client sees the message
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

HOME = Path.home()
# Where the shared trees are mounted. Option A (now): the files stay on the VM and are NFS-mounted into the
# container on forge. Option B (later): move the files to forge and only change these mounts.
ROOTS = {
    "memory": Path(os.environ.get("KENNIS_MEMORY", HOME / ".claude/projects/-home-user-homelab/memory")),
    "homelab": Path(os.environ.get("KENNIS_HOMELAB", HOME / "homelab")),
    "claude": Path(os.environ.get("KENNIS_CLAUDE", HOME / ".claude/gedeeld")),
}
# The "claude" root is a dedicated share (CLAUDE.md, skills/, agents/), never all of ~/.claude (credentials).
CLAUDE_READABLE = ("CLAUDE.md", "skills/*", "agents/*")
WRITABLE = ("memory/*.md", "memory/archief/*.md", "homelab/*.md")
SKIP_PARTS = {".git", "__pycache__", "node_modules", ".venv", ".cache"}
SKIP_NAMES = re.compile(r"(\.bak|\.vault_pass$|\.env$|credentials|\.pem$|\.key$|id_ed25519|id_rsa)")
MAX_BYTES = 2_000_000
TOKENS_FILE = Path(os.environ.get("KENNIS_TOKENS", HOME / ".config/kennis-server/tokens.json"))
AUDIT = Path(os.environ.get("KENNIS_AUDIT", HOME / ".local/state/kennis-server/audit.log"))

device: ContextVar[dict] = ContextVar("device", default={})
_locks: dict[str, asyncio.Lock] = {}


def resolve(path: str) -> tuple[Path, str]:
    """'memory/x.md' -> absolute path inside its root; rejects traversal and hidden secrets."""
    root, _, rest = path.strip().lstrip("/").partition("/")
    if root not in ROOTS or not rest:
        raise ToolError(f"path must start with one of {sorted(ROOTS)}/, e.g. memory/MEMORY.md")
    base = ROOTS[root].resolve()
    full = (base / rest).resolve()
    if base not in full.parents and full != base:
        raise ToolError("path escapes its root")
    rel = full.relative_to(base).as_posix()
    if set(Path(rel).parts) & SKIP_PARTS or SKIP_NAMES.search(rel):
        raise ToolError("path not shared")
    if root == "claude" and not any(fnmatch.fnmatch(rel, p) for p in CLAUDE_READABLE):
        raise ToolError("only CLAUDE.md, skills/, agents/ and hooks/ are shared from ~/.claude")
    return full, f"{root}/{rel}"


def version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


BINARY = {".zip", ".gz", ".tgz", ".xz", ".7z", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
          ".bin", ".img", ".iso", ".so", ".pyc", ".whl", ".mp4", ".mkv", ".db", ".sqlite", ".woff", ".woff2", ".ttf"}
RESCAN_SECONDS = int(os.environ.get("KENNIS_RESCAN", "60"))


def shared_files(root: str):
    """Walk a root, pruning skipped directories while walking (the homelab tree has ~21k files, ~1.3k shared)."""
    base = ROOTS[root]
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_PARTS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix.lower() in BINARY:
                continue
            try:
                resolve(f"{root}/{p.relative_to(base).as_posix()}")
                if p.stat().st_size > MAX_BYTES:
                    continue
            except (ToolError, OSError):
                continue
            yield p


class Index:
    """All shared text in RAM (a few MB), so list and search never touch NFS.

    Kept fresh without polling: the watcher on the VM (inotify) posts changed paths to /invalidate, and writes
    through this server update it directly. A cheap rescan (stat only, re-read on size/mtime change) every
    RESCAN_SECONDS is the safety net for a missed event. Re-reading uses open(), which makes NFS revalidate
    (close-to-open), so the 1 s attribute cache does not delay an invalidated file.
    """

    def __init__(self):
        # rel -> (mtime_ns, size, text, lowercase text). Case-insensitive search on the lowercase copy without re.I is
        # ~12x faster than re.I on the original (8 ms vs 98 ms over 24 MB, measured 18 Oct).
        self.files: dict[str, tuple[int, int, str, str | None]] = {}
        self.lock = threading.Lock()

    def refresh(self, rel: str):
        try:
            full, rel = resolve(rel)
            with open(full, "rb") as f:
                st = os.fstat(f.fileno())
                if st.st_size > MAX_BYTES or full.suffix.lower() in BINARY:
                    raise OSError("not indexed")
                text = f.read().decode("utf-8")
        except (ToolError, OSError, UnicodeDecodeError):
            with self.lock:
                self.files.pop(rel, None)
            return
        low = text.lower()
        with self.lock:
            # A few Unicode characters change length when lowercased; then positions would not line up.
            self.files[rel] = (st.st_mtime_ns, st.st_size, text, low if len(low) == len(text) else None)

    def rescan(self):
        seen = set()
        for root in ROOTS:
            for p in shared_files(root):
                rel = f"{root}/{p.relative_to(ROOTS[root]).as_posix()}"
                seen.add(rel)
                try:
                    st = p.stat()
                except OSError:
                    continue
                hit = self.files.get(rel)
                if not hit or hit[0] != st.st_mtime_ns or hit[1] != st.st_size:
                    self.refresh(rel)
        with self.lock:
            for rel in set(self.files) - seen:
                del self.files[rel]

    def loop(self):
        while True:
            time.sleep(RESCAN_SECONDS)
            try:
                self.rescan()
            except Exception as e:  # never let the safety net die
                print(f"rescan failed: {e}", flush=True)

    def items(self, root: str = ""):
        with self.lock:
            snap = sorted(self.files.items())
        return [(rel, v[2], v[3]) for rel, v in snap if not root or rel.startswith(root + "/")]


index = Index()


def check_writable(rel: str):
    if not device.get().get("write"):
        raise ToolError("this device token is read-only")
    # PurePosixPath.match: "*" stays within one directory level (fnmatch's "*" would also match "sub/x.md").
    if not any(PurePosixPath(rel).match(p) for p in WRITABLE):
        raise ToolError(f"writes are allowed only for {', '.join(WRITABLE)}")


def audit(action: str, rel: str):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {device.get().get('name', '?')} {action} {rel}\n")


def atomic_write(full: Path, data: bytes):
    full.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=full.parent, prefix=".kennis-")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, full.stat().st_mode & 0o777 if full.exists() else 0o644)  # mkstemp would leave new files 0600
    os.replace(tmp, full)


server = MCPServer(
    name="kennis",
    instructions=(
        "Shared homelab knowledge of the owner: docs in homelab/ (00_OVERZICHT.md ... 14_*.md, tools/), "
        "Claude memory in memory/ (index memory/MEMORY.md, one fact per file), shared instructions in claude/CLAUDE.md "
        "and claude/skills/. This replaces local auto-memory on this device: read memory files here, and save new "
        "memories here (new file + one index line in memory/MEMORY.md). Prefer kennis_edit over kennis_write."
    ),
)


@server.tool()
def kennis_list(root: str = "memory", pattern: str = "*") -> str:
    """List shared files under a root (memory, homelab or claude), optionally filtered by a glob on the relative path."""
    if root not in ROOTS:
        raise ToolError(f"root must be one of {sorted(ROOTS)}")
    hits = [rel for rel, _, _ in index.items(root) if fnmatch.fnmatch(rel.split("/", 1)[1], pattern)]
    return "\n".join(hits[:2000]) or "(no files)"


@server.tool()
def kennis_read(path: str, offset: int = 1, limit: int = 600) -> str:
    """Read a shared text file, e.g. memory/MEMORY.md or homelab/05_SERVICES.md. Returns lines offset..offset+limit-1
    with line numbers, plus the file version needed by kennis_write."""
    full, rel = resolve(path)
    data = full.read_bytes()
    lines = data.decode("utf-8", errors="replace").splitlines()
    start = max(offset, 1)
    chunk = lines[start - 1:start - 1 + limit]
    body = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start))
    more = f"\n[... {len(lines) - (start - 1 + len(chunk))} more lines, use offset]" if start - 1 + len(chunk) < len(lines) else ""
    return f"{rel} (version {version(data)}, {len(lines)} lines)\n{body}{more}"


@server.tool()
def kennis_search(pattern: str, root: str = "", max_results: int = 60) -> str:
    """Case-insensitive regex search over shared files (all roots, or one root). Returns path:line: text."""
    try:
        rx_i = re.compile(pattern, re.I | re.M)
        # Fast path: lowercase pattern on the lowercase copy. Not when the pattern has uppercase escapes (\S, \W, \D,
        # \B, \A, \Z), whose meaning would change by lowercasing.
        rx_low = None if re.search(r"\\[A-Z]", pattern) else re.compile(pattern.lower(), re.M)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}")
    out = []
    for rel, text, low in index.items(root):
        hay, rx = (low, rx_low) if rx_low is not None and low is not None else (text, rx_i)
        # One C-level regex pass over the whole file (re.M keeps ^/$ per line); at most one hit per line.
        last, lineno, pos = -1, 1, 0
        for m in rx.finditer(hay):
            start = text.rfind("\n", 0, m.start()) + 1
            if start == last:
                continue
            lineno += text.count("\n", pos, start)
            pos, last = start, start
            end = text.find("\n", start)
            out.append(f"{rel}:{lineno}: {text[start:end if end >= 0 else None][:300]}")
            if len(out) >= max_results:
                return "\n".join(out) + "\n[max_results reached]"
    return "\n".join(out) or "(no matches)"


@server.tool()
async def kennis_edit(path: str, old_string: str, new_string: str) -> str:
    """Replace one unique snippet in a shared file (like the Edit tool). Edits by other sessions elsewhere in the
    file are kept. Fails if old_string is missing or not unique: read the file again and retry."""
    full, rel = resolve(path)
    check_writable(rel)
    async with _locks.setdefault(rel, asyncio.Lock()):
        text = full.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count != 1:
            raise ToolError(f"old_string found {count} times in {rel}; it must occur exactly once (re-read the file)")
        new = text.replace(old_string, new_string, 1).encode()
        atomic_write(full, new)
        index.refresh(rel)
    audit("edit", rel)
    return f"edited {rel} (version {version(new)})"


@server.tool()
async def kennis_write(path: str, content: str, base_version: str = "") -> str:
    """Create or replace a whole shared file. For an existing file pass base_version from kennis_read; it is refused
    when another session changed the file since then. Leave base_version empty only to create a new file."""
    full, rel = resolve(path)
    check_writable(rel)
    async with _locks.setdefault(rel, asyncio.Lock()):
        if full.exists():
            current = version(full.read_bytes())
            if base_version != current:
                raise ToolError(f"{rel} changed since you read it (now {current}); read it again and redo your change")
        elif base_version:
            raise ToolError(f"{rel} does not exist; create it with an empty base_version")
        data = content.encode()
        atomic_write(full, data)
        index.refresh(rel)
    audit("write", rel)
    return f"wrote {rel} (version {version(data)})"


PART_CHARS = 9500  # Claude Code caps each SessionStart hook output at 10,000 characters


def context_parts() -> list[str]:
    """Startup context for the plugin's SessionStart hooks: what VM sessions get loaded natively, split on line
    boundaries into parts that each fit one hook output, so nothing is cut off."""
    text = [
        "===== kennis plugin =====\nThe memory and docs below live on the kennis MCP server (the VM), not on this "
        "machine; other sessions on other devices use the same files live. Read memory files with "
        "kennis_read('memory/<file>'), search with kennis_search, and save or update memories there (kennis_edit, or "
        "kennis_write for a new file, plus one index line in memory/MEMORY.md) instead of local files. Docs: "
        "homelab/<file>; skills: claude/skills/<name>/SKILL.md."
    ]
    for rel in ("claude/CLAUDE.md", "homelab/CLAUDE.md", "memory/MEMORY.md"):
        full, _ = resolve(rel)
        if full.exists():
            text.append(f"===== {rel} (via kennis server) =====\n{full.read_text(encoding='utf-8')}")
    parts, cur = [], ""
    for line in "\n\n".join(text).splitlines(keepends=True):
        if len(cur) + len(line) > PART_CHARS:
            parts.append(cur)
            cur = ""
        cur += line
    return parts + [cur]


_tokens_cache: tuple[int, int, dict] = (0, 0, {})


def load_tokens() -> dict:
    """sha256(token) -> (device name, config). The file holds only hashes: {"<device>": {"sha256": "<hex>",
    "write": bool, "watch": bool}}, so a leaked tokens.json gives no usable token. Re-read when it changes, so adding
    or revoking a device needs no restart."""
    global _tokens_cache
    st = TOKENS_FILE.stat()
    if (st.st_mtime_ns, st.st_size) != _tokens_cache[:2]:
        by_hash = {cfg["sha256"]: (name, cfg) for name, cfg in json.loads(TOKENS_FILE.read_text()).items()}
        _tokens_cache = (st.st_mtime_ns, st.st_size, by_hash)
    return _tokens_cache[2]


class BearerAuth:
    """ASGI middleware: every request needs 'Authorization: Bearer <device token>', matched by its SHA-256 hash."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/health":
            return await PlainTextResponse("ok")(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        given = auth[7:] if auth.lower().startswith("bearer ") else ""
        hit = load_tokens().get(hashlib.sha256(given.encode()).hexdigest()) if given else None
        if hit:
            name, cfg = hit
            device.set({"name": name, "write": bool(cfg.get("write")), "watch": bool(cfg.get("watch"))})
            return await self.app(scope, receive, send)
        return await JSONResponse({"error": "invalid or missing token"}, status_code=401)(scope, receive, send)


@server.custom_route("/invalidate", methods=["POST"])
async def invalidate_route(request: Request):
    """Called by the inotify watcher on the VM with the paths that changed there: {"paths": ["memory/x.md", ...]}."""
    if not device.get().get("watch"):
        return JSONResponse({"error": "token may not invalidate"}, status_code=403)
    paths = (await request.json()).get("paths", [])[:5000]
    await asyncio.to_thread(lambda: [index.refresh(p) for p in paths])
    return JSONResponse({"refreshed": len(paths)})


@server.custom_route("/context/{part}", methods=["GET"])
async def context_route(request: Request):
    parts = context_parts()
    n = int(request.path_params["part"])
    if not 1 <= n <= len(parts):
        return PlainTextResponse("")  # the plugin asks for a fixed number of parts; extra ones are empty
    head = f"[kennis context part {n}/{len(parts)}]\n"
    return PlainTextResponse(head + parts[n - 1])


def build_app():
    hosts = os.environ.get("KENNIS_ALLOWED_HOSTS", "kennis.to0wnn.com,<lan-ip>:8766,127.0.0.1:8766").split(",")
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                                     allowed_origins=[f"https://{h}" for h in hosts]),
    )
    # gzip in the server itself, so responses are compressed on every path (also direct :8766 without Traefik).
    return BearerAuth(GZipMiddleware(app, minimum_size=1024))


if __name__ == "__main__":
    t0 = time.time()
    index.rescan()
    print(f"index: {len(index.files)} files in {time.time() - t0:.1f}s", flush=True)
    threading.Thread(target=index.loop, daemon=True).start()
    uvicorn.run(build_app(), host=os.environ.get("KENNIS_BIND", "0.0.0.0"), port=int(os.environ.get("KENNIS_PORT", "8766")),
                log_level="warning", proxy_headers=True, forwarded_allow_ips="<lan-ip>")
