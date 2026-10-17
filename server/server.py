"""Kennis server: live, shared access to the homelab docs and Claude memory for Claude Code on any device.

The files on the VM stay the single source of truth (VM sessions keep using them natively). Other
devices reach them through this MCP server over HTTPS via the `kennis` plugin. Design choices that keep
it from breaking:
  - stateless streamable HTTP + JSON responses: no MCP sessions that expire or get lost on a restart;
  - a static bearer token per device (no login that times out); revoke a device by removing its token;
  - responses are gzip-compressed by the server when the client accepts it;
  - writes are serialized per file and atomic; `kennis_edit` replaces one unique snippet (concurrent
    edits elsewhere in the file survive), `kennis_write` refuses when the file changed since it was read.

Config: ~/.config/kennis-server/tokens.json  {"<device>": {"token": "...", "write": true|false}}
Run:    uv run server.py   (systemd user unit kennis-server.service)
"""
import asyncio
import fnmatch
import hashlib
import hmac
import json
import os
import re
import tempfile
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
    "memory": Path(os.environ.get("KENNIS_MEMORY", HOME / ".claude/projects/-home-to0wnn-homelab/memory")),
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
_text_cache: dict[Path, tuple[int, int, str | None]] = {}


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


def cached_text(p: Path) -> str | None:
    """File text, re-read only when size or mtime changed (every write changes mtime, so results stay live)."""
    st = p.stat()
    hit = _text_cache.get(p)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        text = None
    _text_cache[p] = (st.st_mtime_ns, st.st_size, text)
    return text


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
        "Shared homelab knowledge of Anthony (To0wnn): docs in homelab/ (00_OVERZICHT.md ... 14_*.md, tools/), "
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
    rels = [p.relative_to(ROOTS[root]).as_posix() for p in shared_files(root)]
    hits = [f"{root}/{r}" for r in rels if fnmatch.fnmatch(r, pattern)]
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
        rx = re.compile(pattern, re.I)
    except re.error as e:
        raise ToolError(f"invalid regex: {e}")
    out = []
    for r in ([root] if root else list(ROOTS)):
        for p in shared_files(r):
            text = cached_text(p)
            if text is None:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    out.append(f"{r}/{p.relative_to(ROOTS[r]).as_posix()}:{n}: {line[:300]}")
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


def load_tokens() -> dict:
    return json.loads(TOKENS_FILE.read_text())


class BearerAuth:
    """ASGI middleware: every request needs 'Authorization: Bearer <device token>'. Tokens are re-read per request,
    so adding or revoking a device needs no restart."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/health":
            return await PlainTextResponse("ok")(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        given = auth[7:] if auth.lower().startswith("bearer ") else ""
        for name, cfg in load_tokens().items():
            if given and hmac.compare_digest(given, cfg["token"]):
                device.set({"name": name, "write": bool(cfg.get("write"))})
                return await self.app(scope, receive, send)
        return await JSONResponse({"error": "invalid or missing token"}, status_code=401)(scope, receive, send)


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
    uvicorn.run(build_app(), host=os.environ.get("KENNIS_BIND", "0.0.0.0"), port=int(os.environ.get("KENNIS_PORT", "8766")),
                log_level="warning", proxy_headers=True, forwarded_allow_ips="<lan-ip>")
