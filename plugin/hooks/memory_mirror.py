#!/usr/bin/env python3
"""Offline copy of this device's memory on the knowledge server, so a session can read and extend it while the server
is down; what was written offline goes up at the next sync.

  memory_mirror.py                      SessionStart/SessionEnd hook: start a sync in the background and return at once
  memory_mirror.py --now                sync in the foreground: push local changes, then fetch what changed on the server
  memory_mirror.py offline KEY [PART]   the context for a session that cannot reach the server (cached + offline notice)

The copy lives in ~/.knowledge/mirror/<memory root>/ (KNOWLEDGE_MIRROR overrides). Only files whose content differs
from the last sync travel: the device sends the versions it has, the server only what it has newer, and the device
only what it changed itself. A memo changed on both sides is never overwritten: the server keeps the offline text
next to it (<name>.offline-<time>.md) and knowledge_lint asks to merge. Standard library only, Linux/macOS/Windows.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MIRROR = Path(os.environ.get("KNOWLEDGE_MIRROR") or Path.home() / ".knowledge" / "mirror")
STATE, LOCK = MIRROR / ".state.json", MIRROR / ".lock"


def option(name: str, default: str = "") -> str:  # same lookup as docs-sync.py: plugin option, else KNOWLEDGE_*
    return os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name.upper()}", os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name}")) \
        or os.environ.get(f"KNOWLEDGE_{name.upper()}", default)


def version(data: bytes) -> str:  # the server's file version
    return hashlib.sha256(data).hexdigest()[:16]


def post(path: str, payload: dict) -> dict:
    url = option("url", "https://knowledge.to0wnn.com").rstrip("/") + path
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {option('token')}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def local_path(rel: str) -> Path:
    parts = rel.split("/")
    if not rel.endswith(".md") or any(p in ("", ".", "..") or ":" in p or "\\" in p for p in parts):
        raise ValueError(f"unsafe path from server: {rel}")
    return MIRROR.joinpath(*parts)


def write_atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)  # bytes: no newline translation on Windows, so versions match the server's
    os.replace(tmp, path)


def local_files(roots: list) -> dict:
    out = {}
    for root in roots:
        for p in (MIRROR / root).rglob("*.md"):
            out[p.relative_to(MIRROR).as_posix()] = p.read_bytes()
    return out


def sync() -> str:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"files": {}, "roots": []}
    known = state["files"]  # rel -> version both sides had at the last sync
    # 1. Up: what this device wrote or changed since the last sync (a deleted local file is not a deletion: it returns).
    mine = {rel: data for rel, data in local_files(state["roots"]).items() if version(data) != known.get(rel)}
    keep, conflicts, report = set(), set(), []
    if mine:
        res = post("/mirror/push", {"files": {rel: {"text": data.decode("utf-8", "replace"), "base": known.get(rel, "")}
                                              for rel, data in mine.items()}})
        known.update(res["written"])
        for rel, data in mine.items():
            if rel not in res["written"] and rel not in res["conflicts"] and not any(r.startswith(rel + ":") for r in res["refused"]):
                known[rel] = version(data)  # unchanged: the server already had exactly this
        keep = {r.split(":", 1)[0] for r in res["refused"]}  # refused (e.g. a secret): stays local, tried again next time
        conflicts = set(res["conflicts"])  # the offline text is safe on the server now, as the .offline- copy
        report += [f"conflict: {rel} -> {copy}" for rel, copy in res["conflicts"].items()] + [f"refused: {r}" for r in res["refused"]]
    # 2. Down: only what differs from the versions this device has.
    res = post("/mirror", {"have": {rel: v for rel, v in known.items() if local_path(rel).exists()}})  # a lost file comes back
    for rel, text in res["files"].items():
        if rel not in keep:
            write_atomic(local_path(rel), text.encode())
            known[rel] = res["versions"][rel]
    for rel in [r for r in known if r not in res["versions"]]:  # deleted on the server
        p = local_path(rel)
        if p.exists() and (version(p.read_bytes()) == known[rel] or rel in conflicts):
            p.unlink()
        known.pop(rel)
    write_atomic(STATE, json.dumps({"files": known, "roots": res["roots"], "synced": time.strftime("%d %b %Y %H:%M")}).encode())
    return "\n".join(report)


def sync_locked():
    MIRROR.mkdir(parents=True, exist_ok=True)
    try:
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime > 600:
            LOCK.unlink()  # a sync that died
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return  # another session is syncing
    os.close(fd)
    try:
        report = sync()
        if report:
            (MIRROR / "last-sync.log").write_text(report + "\n", encoding="utf-8")
    except Exception as e:  # offline or server down: the copy stays as it is, the next session tries again
        (MIRROR / "last-sync.log").write_text(f"sync failed {time.strftime('%d %b %Y %H:%M')}: {e}\n", encoding="utf-8")
    finally:
        LOCK.unlink(missing_ok=True)


def start_background():
    """Never slows a session start: the sync runs detached, with the same environment (URL and token)."""
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--now"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def save_context(key: str, text: str, part: str = "all"):
    """The startup context of the last session that reached the server, per project folder (key)."""
    try:
        write_atomic(MIRROR / f".context-{key}-{part}", text.encode())
    except OSError:
        pass


def offline_context(key: str, part: str = "all") -> str:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    cached = MIRROR / f".context-{key}-{part}"
    text = cached.read_text(encoding="utf-8") if cached.exists() else ""
    if part not in ("all", "1"):
        return text
    if not state.get("roots"):
        return "knowledge server unreachable and no offline copy on this device: shared memory and docs were not loaded."
    where = ", ".join(str(MIRROR / r) for r in state["roots"])
    return ("===== knowledge server unreachable: offline mode =====\n"
            f"The memory is the offline copy in {where} (last synced {state.get('synced', '?')}); the knowledge_* tools do "
            "not work now. Read memos there with your file tools, and save new or changed memos there by the house rules "
            "(frontmatter, ## Stand / ## Keuzes, one index line in that MEMORY.md). They are uploaded at the next session "
            "start once the server is back. Do not delete files there. Context of the last online session:\n\n" + text)


if __name__ == "__main__":
    if sys.argv[1:2] == ["offline"]:
        sys.stdout.write(offline_context(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "all"))
    elif "--now" in sys.argv:
        sync_locked()
    elif "--selftest" in sys.argv:
        for bad in ("../x.md", "a/../../x.md", "C:/x.md", "a\\b.md", "a/b.txt"):
            try:
                local_path(bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        assert local_path("g-memory/archief/x.md") == MIRROR / "g-memory" / "archief" / "x.md"
        print("ok")
    else:
        try:
            sys.stdin.read()  # the hook input is not needed
            start_background()
        except Exception as e:  # never fail the session
            print(f"knowledge mirror skipped: {e}", file=sys.stderr)
