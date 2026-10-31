#!/usr/bin/env python3
"""Offline copy of this device's memory on the knowledge server, so a session can read and extend it while the server
is down; what was written offline goes up at the next sync.

  memory_mirror.py                      SessionStart/SessionEnd hook: start a sync in the background and return at once
  memory_mirror.py --now                sync in the foreground: push local changes, then fetch what changed on the server
  memory_mirror.py offline KEY [PART]   the context for a session that cannot reach the server (cached + offline notice)
  memory_mirror.py edited               PostToolUse hook: sync in the background when the edit was a skill or a memo here

The shared skills (root "claude", only for a device that may change them) live in ~/.claude/skills/<name>/ instead, so
Claude Code loads them as real skills in every project and picks up changes live; a skill made there goes up the same
way. Symlinked skill folders (installed by something else), synced/ (claude.ai) and dot-folders stay local.

The copy lives in ~/.knowledge/mirror/<memory root>/ (KNOWLEDGE_MIRROR overrides). Only files whose content differs
from the last sync travel: the device sends the versions it has, the server only what it has newer, and the device
only what it changed itself. A memo changed on both sides is never overwritten: the server keeps the offline text
next to it (<name>.offline-<time>.md) and knowledge_lint asks to merge. Standard library only, Linux/macOS/Windows.
"""
import hashlib
import json
import token_store
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MIRROR = Path(os.environ.get("KNOWLEDGE_MIRROR") or Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge") / "mirror")
STATE, LOCK = MIRROR / ".state.json", MIRROR / ".lock"
SKILLS = Path(os.environ.get("KNOWLEDGE_SKILLS") or Path.home() / ".claude" / "skills")
MAX_REMOVALS = 5  # more skills or memos gone from the server in one sync looks like an accident: hold and say so
SKILL_EXT = {".md", ".py", ".sh", ".json", ".txt", ".yaml", ".yml", ".toml", ".js", ".html", ".css", ".xml", ".xsd"}  # = server


def option(name: str, default: str = "") -> str:
    if name == "token":  # one lookup order for every hook and the MCP header: token_store.py
        return token_store.token()  # same lookup as docs-sync.py: plugin option, else KNOWLEDGE_*
    return os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name.upper()}", os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name}")) \
        or os.environ.get(f"KNOWLEDGE_{name.upper()}", default)


def version(data: bytes) -> str:  # the server's file version
    return hashlib.sha256(data).hexdigest()[:16]


def post(path: str, payload: dict) -> dict:
    url = token_store.url() + path
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {option('token')}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def local_path(rel: str) -> Path:
    parts = rel.split("/")
    if any(p in ("", ".", "..") or ":" in p or "\\" in p for p in parts):
        raise ValueError(f"unsafe path from server: {rel}")
    if parts[0] == "claude":
        if parts[1:2] != ["skills"] or len(parts) < 4 or parts[2] == "synced" or any(p.startswith(".") for p in parts) \
                or Path(rel).suffix.lower() not in SKILL_EXT:
            raise ValueError(f"unsafe path from server: {rel}")
        path = SKILLS
        for part in parts[2:]:  # never through a linked folder: that leads into someone else's files (a repo, /usr/share)
            path = path / part
            if linked(path):
                raise ValueError(f"linked folder, stays local: {rel}")
        return path
    if not rel.endswith(".md"):
        raise ValueError(f"unsafe path from server: {rel}")
    return MIRROR.joinpath(*parts)


def target(rel: str):
    """local_path, or None for a path this device must leave alone (a linked skill folder, an unsafe name)."""
    try:
        return local_path(rel)
    except ValueError:
        return None


def write_atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)  # bytes: no newline translation on Windows, so versions match the server's
    os.replace(tmp, path)


def linked(p: Path) -> bool:
    return p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())  # a Windows junction is no symlink


def local_skills() -> dict:
    out = {}
    for d in sorted(SKILLS.iterdir()) if SKILLS.is_dir() else ():
        if linked(d) or not d.is_dir() or d.name == "synced" or d.name.startswith("."):
            continue
        for p in d.rglob("*"):
            rel = p.relative_to(SKILLS)
            if p.is_file() and not linked(p) and p.suffix.lower() in SKILL_EXT and not any(x.startswith(".") for x in rel.parts):
                out["claude/skills/" + rel.as_posix()] = p.read_bytes()
    return out


def local_files(roots: list) -> dict:
    out = local_skills() if "claude" in roots else {}
    for root in [r for r in roots if r != "claude"]:
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
        same = res.get("unchanged_files")  # a server before 21 Oct 2026 only counts them
        for rel, data in mine.items():
            listed = rel in same if same is not None else rel not in res["written"] and rel not in res["conflicts"] \
                and not any(r.startswith(rel + ":") for r in res["refused"])
            if listed:
                known[rel] = version(data)  # unchanged: the server already had exactly this
        keep = {r.split(":", 1)[0] for r in res["refused"]}  # refused (e.g. a secret): stays local, tried again next time
        conflicts = set(res["conflicts"])  # the offline text is safe on the server now, as the .offline- copy
        report += [f"conflict: {rel} -> {copy}" for rel, copy in res["conflicts"].items()] + [f"refused: {r}" for r in res["refused"]]
    # 2. Down: only what differs from the versions this device has.
    res = post("/mirror", {"have": {rel: v for rel, v in known.items() if (p := target(rel)) and p.exists()}})  # a lost file comes back
    for rel, text in res["files"].items():
        p = target(rel)
        if rel in keep or not p:
            continue
        if rel not in known and rel not in conflicts and p.exists() and p.read_bytes() != text.encode():
            continue  # here before the sync knew it (skills just switched on): it goes up next time, as a conflict copy
        write_atomic(p, text.encode())
        known[rel] = res["versions"][rel]
    gone = [r for r in known if r not in res["versions"]]  # deleted on the server
    # A root the server no longer gives this device (skills switched off) goes on purpose; within a root it still
    # gives, many removals at once (an emptied folder on the VM) are held back until someone confirms them.
    units = {"/".join(r.split("/")[:3]) if r.startswith("claude/") else r
             for r in gone if r.split("/", 1)[0] in res["roots"]}
    if len(units) > MAX_REMOVALS and not os.environ.get("KNOWLEDGE_ALLOW_REMOVALS"):
        report.append(f"held back: the server no longer has {len(units)} items ({', '.join(sorted(units)[:6])}...); they stay "
                      "here. Removed on purpose? Delete them here too, or run memory_mirror.py --now once with "
                      "KNOWLEDGE_ALLOW_REMOVALS=1. By mistake? Restore them with knowledge_history.")
        gone = [r for r in gone if r.split("/", 1)[0] not in res["roots"]]
    for rel in gone:
        p = target(rel)
        if not p:
            known.pop(rel)
            continue
        if p.exists() and (version(p.read_bytes()) == known[rel] or rel in conflicts):
            p.unlink()
            while rel.startswith("claude/") and p.parent != SKILLS and not any(p.parent.iterdir()):
                p = p.parent  # a removed skill leaves no empty folder behind
                p.rmdir()
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
        log = MIRROR / "last-sync.log"  # only what needs a look: conflicts, refusals, a failed sync
        log.write_text(report + "\n", encoding="utf-8") if report else log.unlink(missing_ok=True)
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


def sync_notice() -> str:
    """What the last sync left to look at (conflict, refusal, failure), for the next session's context: fail loudly."""
    log = MIRROR / "last-sync.log"
    try:
        text = log.read_text(encoding="utf-8").strip() if log.exists() else ""
    except OSError:
        return ""
    return f"knowledge offline copy, last sync: {text[:400]}" if text else ""


def offline_context(key: str, part: str = "all") -> str:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    cached = MIRROR / f".context-{key}-{part}"
    text = cached.read_text(encoding="utf-8") if cached.exists() else ""
    if part not in ("all", "1"):
        return text
    if not state.get("roots"):
        return "knowledge server unreachable and no offline copy on this device: shared memory and docs were not loaded."
    where = ", ".join(str(MIRROR / r) for r in state["roots"] if r != "claude")
    return ("===== knowledge server unreachable: offline mode =====\n"
            f"The memory is the offline copy in {where} (last synced {state.get('synced', '?')}); the knowledge_* tools do "
            "not work now. Read memos there with your file tools, and save new or changed memos there by the house rules "
            "(frontmatter, ## Stand / ## Keuzes, one index line in that MEMORY.md). They are uploaded at the next session "
            "start once the server is back. Do not delete files there." + (f" {sync_notice()}" if sync_notice() else "")
            + " Context of the last online session:\n\n" + text)


if __name__ == "__main__":
    if sys.argv[1:2] == ["offline"]:  # bytes: a Windows pipe is cp1252 and the context has → and ✅
        sys.stdout.buffer.write(offline_context(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "all").encode("utf-8"))
    elif sys.argv[1:2] == ["notice"]:
        sys.stdout.buffer.write(sync_notice().encode("utf-8"))
    elif sys.argv[1:2] == ["edited"]:
        try:
            path = Path((json.load(sys.stdin).get("tool_input") or {}).get("file_path") or "").resolve()
            if any(base.resolve() in path.parents for base in (SKILLS, MIRROR)):
                start_background()
        except Exception as e:  # never fail the session
            print(f"knowledge mirror skipped: {e}", file=sys.stderr)
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
        assert local_path("claude/skills/s/scripts/a.py") == SKILLS / "s" / "scripts" / "a.py"
        for bad in ("claude/CLAUDE.md", "claude/skills/synced/x/SKILL.md", "claude/skills/s/.x/a.md", "claude/skills/s/a.bin", "g/a.py"):
            try:
                local_path(bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        print("ok")
    else:
        try:
            sys.stdin.read()  # the hook input is not needed
            start_background()
        except Exception as e:  # never fail the session
            print(f"knowledge mirror skipped: {e}", file=sys.stderr)
