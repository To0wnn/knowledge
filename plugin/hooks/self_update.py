#!/usr/bin/env python3
"""Auto-update of the knowledge plugin for Codex and agy, the counterpart of auto_update.py for Claude Code: neither
agent updates a plugin by itself (agy has no update command at all). At session start, at most once a day, the plugin
compares its own version with the one on GitHub and, when that is newer, updates itself in the background with the
agent's own CLI; the new version loads at the next start. The session context says it once when auto-update is on,
after an update, and after a failed one. Off: KNOWLEDGE_AUTO_UPDATE=0.

  self_update.py run <codex|agy> [script]   the background update (started by notice()); "script" = the setup
                                              without a plugin (clients/knowledge-agent.py): sync, then install
  self_update.py sync <checkout>   bring the setup's git checkout to GitHub's main (also after a rewritten history)
  self_update.py --self-test       the checks of this file
Standard library only.
"""
from __future__ import annotations  # X | None on Python 3.9 (macOS's /usr/bin/python3)

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import token_store
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge") / "auto-update.json"  # next to the offline copy (memory_mirror.py)
LATEST = os.environ.get("KNOWLEDGE_PLUGIN_LATEST",
                        "https://raw.githubusercontent.com/To0wnn/knowledge/main/plugin/.claude-plugin/plugin.json")
AGY_PLUGIN = os.environ.get("KNOWLEDGE_AGY_PLUGIN", "https://github.com/To0wnn/knowledge/tree/main/plugin")
AGY_DIR = Path.home() / ".gemini" / "config" / "plugins" / "knowledge"
HOOK_WAIT = 35  # seconds; agy's session-start hook runs at most 30
INSTALL_TIME = 300  # seconds; longer than `agy plugin install` takes
HEALING = "knowledge plugin: agy's plugin folder is empty after a failed update; reinstalling it in the background."
DAY = 86400


def commands(agent: str, mode: str = "plugin") -> list[list[str]]:
    if mode == "script":  # the checkout this file runs from: sync it, then let the setup update itself
        repo = PLUGIN.parent
        return [[sys.executable, "-X", "utf8", __file__, "sync", str(repo)],
                [sys.executable, "-X", "utf8", str(repo / "clients" / "knowledge-agent.py"), "install", agent]]
    if agent == "claude":  # only the companion's button: Claude Code updates by itself otherwise
        claude = shutil.which("claude") or "claude"
        return [[claude, "plugin", "marketplace", "update", "knowledge"], [claude, "plugin", "update", "knowledge@knowledge"]]
    if agent == "codex":
        codex = shutil.which("codex") or "codex"
        return [[codex, "plugin", "marketplace", "upgrade", "knowledge"], [codex, "plugin", "add", "knowledge@knowledge"]]
    return [[shutil.which("agy") or "agy", "plugin", "install", AGY_PLUGIN]]


def sync(repo: Path) -> None:
    """A rewritten history (force-push) cannot fast-forward and a pull would merge two histories with conflicts in every
    file. The checkout is only a copy of the repository, so it is reset to GitHub's main, but only when nothing tracked in
    it is changed; its old HEAD stays as branch knowledge-before-update-<old sha> (one per lost state, so none is overwritten).
    reset --keep, not --hard: it refuses to overwrite a file that git does not track."""
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300, stdin=subprocess.DEVNULL)
    if (repo / ".git" / "MERGE_HEAD").exists():  # a manual pull that stopped with conflicts: undo it first
        git("merge", "--abort")
    if git("pull", "--ff-only", "-q").returncode == 0:
        return
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise RuntimeError(f"{repo} has local changes: keep them elsewhere, then: git -C {repo} reset --hard origin/main")
    for step in (("fetch", "-q"), ("branch", "-f", "knowledge-before-update-" + git("rev-parse", "--short=8", "HEAD").stdout.strip(), "HEAD"),
                 ("reset", "-q", "--keep", "@{u}")):
        p = git(*step)
        if p.returncode:
            raise RuntimeError(f"git {step[0]}: {(p.stdout + p.stderr).strip()[-300:]}")


def version(text: str) -> tuple:
    return tuple(int(p) for p in json.loads(text)["version"].split("."))


def own() -> tuple:
    for manifest in (".claude-plugin", ".codex-plugin"):
        path = PLUGIN / manifest / "plugin.json"
        if path.exists():
            return version(path.read_text(encoding="utf-8"))
    return ()


def latest() -> tuple:
    if "://" not in LATEST:  # a file, in the tests
        return version(Path(LATEST).read_text(encoding="utf-8"))
    with urllib.request.urlopen(LATEST, timeout=15) as r:
        return version(r.read().decode("utf-8"))


def load() -> dict:
    try:
        return json.loads(token_store.read_settled(STATE))
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f"{STATE.name}.{os.getpid()}.{threading.get_ident()}.tmp")  # own name: another writer's half file never moves into place
    tmp.write_text(json.dumps(state), encoding="utf-8")
    token_store.replace_durable(tmp, STATE)


def agy_manifest(folder: Path) -> bool:
    return (folder / ".claude-plugin" / "plugin.json").exists()


def agy_wiped() -> bool:
    """agy's plugin folder lost its manifest in a failed update: an older version of this file left it so and noted the
    failure, which no hook could say any more. Without that note it stays, as an interrupted uninstall looks the same and
    is the person's choice. Only a folder that stayed so for a while (an install empties it for a moment), and not on
    the setup without a plugin, where a plugin would run every hook twice."""
    state = load()
    if not state.get("agy:wiped") and "auto-update" not in state.get("agy:result", ""):
        return False
    try:
        if agy_manifest(AGY_DIR) or time.time() - AGY_DIR.stat().st_mtime < INSTALL_TIME:
            return False
    except OSError:  # no plugin folder: no plugin
        return False
    try:
        return "knowledge-agent.py" not in (AGY_DIR.parent.parent / "hooks.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False


def heal_agy(now: float | None = None, spawn=None) -> bool:
    """From a hook of another agent or the companion, as agy has no hooks left: reinstall, at most once a day."""
    if os.environ.get("KNOWLEDGE_AUTO_UPDATE") == "0" or not shutil.which("agy") or not agy_wiped():
        return False
    now, state = now or time.time(), load()
    if now - state.get("agy:healed", -DAY) < DAY:
        return False
    state.pop("agy:result", None)  # the old failure: from here the line says how this attempt went
    save({**state, "agy:healed": now, "agy:healing": True, "agy:wiped": True})
    (spawn or start)("agy")
    return True


def heal_result() -> str:
    """How the reinstall went, once, for whoever comes first: agy may have no session start to say it in."""
    state = load()
    if not state.get("agy:healing") or "agy:result" not in state:
        return ""
    del state["agy:healing"]
    line = state.pop("agy:result")
    save(state)
    return line


def notice(agent: str, now: float | None = None, spawn=None, mode: str = "plugin") -> str:
    """Session start: the line(s) for the context; starts the background update when a check is due."""
    if os.environ.get("KNOWLEDGE_AUTO_UPDATE") == "0":
        return ""
    healing = agent != "agy" and heal_agy(now, spawn)
    healed = heal_result() if agent != "agy" and not healing else ""
    now, state, lines = now or time.time(), load(), [HEALING] if healing else [healed] if healed else []
    key = f"{agent}:"
    if not state.get(key + "announced"):
        lines.append(f"knowledge plugin: auto-update is on for {agent}, so new versions arrive by themselves "
                     "(turn it off with KNOWLEDGE_AUTO_UPDATE=0).")
        state[key + "announced"] = True
    result = state.pop(key + "result", None)
    if result:
        lines.append(result)
    if now - state.get(key + "checked", -DAY) >= DAY:  # never checked: due now
        state[key + "checked"] = now  # before starting: two sessions at once start one update
        (spawn or start)(agent, mode) if not spawn else spawn(agent)
    save(state)
    return "\n".join(lines)


def start(agent: str, mode: str = "plugin") -> None:
    kw = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP (as workers.WIN_FLAGS): DETACHED gave git and the agent CLIs a window each
    # cwd: agy runs its hooks in the plugin folder, and on Windows a process standing in a folder stops its removal
    subprocess.Popen([sys.executable, "-X", "utf8", __file__, "run", agent, mode], cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def record(agent: str, error: str = "") -> None:
    """The last check and its failure ('' = fine): the session start sends it with the plugin version, so the admin
    page shows why a device stays behind."""
    state = load()
    state[f"{agent}:check"] = {"at": time.time(), "error": error[-200:]}
    save(state)


def keep_agy() -> Path | None:
    """A copy of agy's plugin: `agy plugin install` deletes the old folder before it copies, and a process standing in that
    folder on Windows stops it halfway, with the plugin gone and no hook left to say so or to try again."""
    keep = STATE.with_name("agy-plugin-before-update")
    if not agy_manifest(AGY_DIR):  # nothing to save, and an earlier copy may be the only good one left
        return keep if agy_manifest(keep) else None
    shutil.rmtree(keep, ignore_errors=True)
    shutil.copytree(AGY_DIR, keep, ignore=shutil.ignore_patterns("__pycache__"))
    return keep


def lock_agy() -> Path | None:
    """One keep, install and restore at a time, also across processes: a second one would copy the half-deleted folder
    and put that back. None when another runs; a lock left by a crash counts for INSTALL_TIME + HOOK_WAIT only."""
    lock = STATE.with_name("agy-update.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime < INSTALL_TIME + HOOK_WAIT:
                    return None
                lock.unlink()
            except OSError:
                return None
    return None


def restore_agy(keep: Path | None) -> str:
    """Back after a failed install, over what is left; the line to add when that fails too."""
    if not keep:
        return ""
    try:
        shutil.copytree(keep, AGY_DIR, dirs_exist_ok=True)
    except OSError as e:
        return f" The plugin could not be put back ({e}); its copy is in {keep}."
    shutil.rmtree(keep, ignore_errors=True)
    return ""


def run(agent: str, mode: str = "plugin", quiet: bool = False, have: tuple | None = None) -> str | None:
    """Update when GitHub has a newer version; the line to tell, or None (up to date). quiet: a failed version check
    (offline) says nothing, it is tried again tomorrow; the background run at session start uses that. have: the
    version of that agent's plugin, for a caller running from another copy (the companion); default this copy."""
    have = own() if have is None else have
    if agent == "agy" and mode == "plugin" and agy_wiped():
        have = (0,)
    try:
        want = latest()
    except Exception as e:
        record(agent, f"version check failed: {e}")
        return None if quiet else f"knowledge plugin: could not check for a new {agent} plugin ({type(e).__name__}: {e})"
    record(agent)
    if not have or want <= have:
        return None
    new = ".".join(map(str, want))
    if agent != "agy" or mode != "plugin":
        return install(agent, mode, new, None)
    lock = lock_agy()
    if not lock:
        return f"knowledge plugin: an update of {agent} is already running."
    try:
        if quiet and os.name == "nt":
            time.sleep(HOOK_WAIT)  # the hook that started this stands in the plugin folder until it ends
        try:
            keep = keep_agy()
        except OSError as e:
            record(agent, f"no copy of the plugin: {e}")
            return (f"knowledge plugin: the auto-update to {new} did not start, no copy of the plugin could be made ({e}); "
                    f"update by hand: python3 knowledge-agent.py install {agent}")
        return install(agent, mode, new, keep)
    finally:
        lock.unlink(missing_ok=True)


def install(agent: str, mode: str, new: str, keep: Path | None) -> str:
    """The update commands; on a failure the kept copy comes back first, whatever happens to the bookkeeping after."""
    fail = None
    for cmd in commands(agent, mode):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                               stdin=subprocess.DEVNULL)
            out = (p.stdout + p.stderr).strip()
            ok = p.returncode == 0 or "already" in out.lower() or "not configured as a git" in out.lower()  # a local checkout, as the setup
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, out = False, str(e)
        if not ok:
            fail = f"{' '.join(cmd[1:])}: {out}"
            break
    if not fail and agent == "agy" and mode == "plugin" and not agy_manifest(AGY_DIR):
        fail = f"{' '.join(commands(agent, mode)[0][1:])}: the plugin is not there after the install: {out}"  # "already" also matches Windows' "Cannot create a file when that file already exists"
    if fail:
        note = restore_agy(keep)
        record(agent, fail[-200:])
        return (f"knowledge plugin: the auto-update to {new} failed ({fail[-260:]}); "
                f"update by hand: python3 knowledge-agent.py install {agent}.{note}")
    if keep:
        shutil.rmtree(keep, ignore_errors=True)
    if agent == "agy":
        state = load()
        if state.pop("agy:wiped", None):
            save(state)
    return f"knowledge plugin: updated to {new} for {agent}; it loads at the next start."


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"] and sys.argv[2:3] and sys.argv[2] in ("codex", "agy"):
        msg = run(sys.argv[2], (sys.argv[3:4] or ["plugin"])[0], quiet=True)
        if msg:
            state = load()
            state[f"{sys.argv[2]}:result"] = msg
            save(state)
    elif sys.argv[1:2] == ["sync"] and sys.argv[2:3]:
        try:
            sync(Path(sys.argv[2]))
        except RuntimeError as e:
            sys.exit(str(e))
    elif "--self-test" in sys.argv:
        import tempfile
        STATE = Path(tempfile.mkdtemp()) / "auto-update.json"
        AGY_DIR = STATE.parent / "no-agy"
        started = []
        first = notice("agy", now=1000.0, spawn=started.append)
        assert "auto-update is on for agy" in first and started == ["agy"], first
        assert notice("agy", now=2000.0, spawn=started.append) == "" and started == ["agy"], "said once, checked once a day"
        assert notice("agy", now=1000.0 + DAY, spawn=started.append) == "" and started == ["agy", "agy"], "next day: check again"
        real_read, tries = Path.read_text, []
        Path.read_text = lambda *a, **k: real_read(*a, **k) if tries.append(1) or len(tries) > 2 else (_ for _ in ()).throw(PermissionError("mid replace"))
        try:
            assert load() and len(tries) == 3, "a read during a replace on Windows waits, it does not look like no state"
        finally:
            Path.read_text = real_read
        save({**load(), "agy:result": "knowledge plugin: updated to 9.9.9 for agy; it loads at the next start."})
        assert "updated to 9.9.9" in notice("agy", now=1000.0 + DAY, spawn=started.append), "the result is said"
        assert notice("agy", now=1000.0 + DAY, spawn=started.append) == "", "and said once"
        assert "auto-update is on for codex" in notice("codex", now=1.0, spawn=started.append), "per agent"
        os.environ["KNOWLEDGE_AUTO_UPDATE"] = "0"
        assert notice("agy", now=9e9, spawn=started.append) == "" and started[-1] == "codex", "off: nothing"
        assert version('{"version": "0.10.0"}') > version('{"version": "0.9.5"}'), "numeric, not text"
        script = commands("agy", "script")
        assert script[0][-2] == "sync" and script[1][-2:] == ["install", "agy"], "script setup: sync, then install"
        assert commands("agy")[0][1:3] == ["plugin", "install"] and commands("codex")[1][-1] == "knowledge@knowledge"
        assert commands("claude")[0][1:4] == ["plugin", "marketplace", "update"] and commands("claude")[1][-1] == "knowledge@knowledge"
        LATEST = str(STATE.parent / "missing.json")  # the version check fails: said when asked, silent in the background
        assert run("agy", quiet=True) is None and "version check failed" in load()["agy:check"]["error"]
        assert "could not check" in run("agy"), "the companion's update says it failed, never 'up to date'"
        popen, seen = subprocess.Popen, {}
        subprocess.Popen = lambda *a, **kw: seen.update(kw)
        start("agy")
        subprocess.Popen = popen
        assert seen["cwd"] == str(Path.home()), "the updater does not stand in the plugin folder it replaces"
        AGY_DIR = STATE.parent / "config" / "plugins" / "agy"  # the install deletes the folder's contents, then fails, as on Windows
        (AGY_DIR / "hooks").mkdir(parents=True)
        (AGY_DIR / "hooks" / "a.py").write_text("x", encoding="utf-8")
        (AGY_DIR / ".claude-plugin").mkdir()
        (AGY_DIR / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        LATEST = str(STATE.parent / "new.json")
        Path(LATEST).write_text('{"version": "99.0.0"}', encoding="utf-8")
        wipe = f"import shutil,sys; shutil.rmtree(r'{AGY_DIR}'); sys.exit('being used by another process')"
        commands = lambda agent, mode="plugin": [[sys.executable, "-c", wipe]]
        msg = run("agy")
        assert "failed" in msg and "being used" in msg and (AGY_DIR / "hooks" / "a.py").read_text(encoding="utf-8") == "x", msg
        assert not STATE.with_name("agy-plugin-before-update").exists(), "the copy goes once it is back"
        commands = lambda agent, mode="plugin": [[sys.executable, "-c", ""]]
        assert "updated to 99.0.0" in run("agy") and not STATE.with_name("agy-plugin-before-update").exists()
        del os.environ["KNOWLEDGE_AUTO_UPDATE"]
        (AGY_DIR.parent.parent / "hooks.json").unlink(missing_ok=True)
        which, shutil.which = shutil.which, lambda name: "/x/" + name
        started, note = [], "knowledge plugin: the auto-update to 9.9.9 failed (being used)"
        manifest = AGY_DIR / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text('{"version": "1.0.0"}', encoding="utf-8")
        save({"agy:result": note})
        assert not agy_wiped() and not heal_agy(spawn=started.append), "a plugin in place"
        shutil.rmtree(manifest.parent)
        assert not agy_wiped(), "just emptied: an install may be running"
        os.utime(AGY_DIR, (1, 1))
        save({})
        assert not agy_wiped(), "no failed update noted: an interrupted uninstall stays the person's choice"
        save({"agy:result": note})
        assert agy_wiped() and heal_agy(spawn=started.append) and started == ["agy"]
        assert "agy:result" not in load() and not heal_result(), "the old failure is not told as this attempt's"
        assert not heal_agy(spawn=started.append) and started == ["agy"], "once a day"
        put = f"import pathlib; p = pathlib.Path(r'{manifest}'); p.parent.mkdir(exist_ok=True); p.write_text('{{}}')"
        commands = lambda agent, mode="plugin": [[sys.executable, "-c", put]]
        assert "updated to 99.0.0" in run("agy", have=(99, 0, 0)), "wiped: a newer own copy does not count"
        assert "agy:wiped" not in load(), "healed: an uninstall later is a choice again"
        shutil.rmtree(manifest.parent)
        commands = lambda agent, mode="plugin": [[sys.executable, "-c", ""]]
        msg = run("agy")
        assert "not there after the install" in msg, msg
        commands = lambda agent, mode="plugin": [[sys.executable, "-c", "import sys; sys.exit('file already exists')"]]
        assert "not there after the install" in run("agy"), "already counts only with a plugin in place"
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text("{}", encoding="utf-8")
        keep = keep_agy()
        shutil.rmtree(manifest.parent)
        assert keep_agy() == keep and agy_manifest(keep), "a folder without a plugin never replaces the good copy"
        lock = lock_agy()
        assert lock and lock_agy() is None and "already running" in run("agy"), "one at a time"
        os.utime(lock, (1, 1))
        assert lock_agy() == lock, "a lock left by a crash ends"
        lock.unlink()
        assert not lock.exists()
        shutil.rmtree(keep)
        os.utime(AGY_DIR, (1, 1))
        save({"agy:wiped": True})
        (AGY_DIR.parent.parent / "hooks.json").write_text('{"knowledge": "knowledge-agent.py"}', encoding="utf-8")
        assert not agy_wiped(), "the setup without a plugin: no plugin"
        (AGY_DIR.parent.parent / "hooks.json").unlink()
        assert agy_wiped()
        save({"agy:healed": 2e9, "agy:healing": True, "agy:result": "agy back", "codex:announced": True, "codex:checked": 2e9})
        assert notice("codex", now=2e9, spawn=lambda a: None) == "agy back" and "agy:healing" not in load(), "the result, once"
        shutil.rmtree(AGY_DIR)
        assert not agy_wiped(), "uninstalled: stays so"
        shutil.which = which
        assert run("codex", have=(99, 0, 0)) is None and "updated to 99.0.0" in run("codex", have=(0, 1, 0)), "the agent's version counts"
        print("ok")
    else:
        sys.exit(__doc__)
