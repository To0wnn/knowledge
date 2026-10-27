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
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
STATE = Path.home() / ".knowledge" / "auto-update.json"  # next to the offline copy (memory_mirror.py)
LATEST = os.environ.get("KNOWLEDGE_PLUGIN_LATEST",
                        "https://raw.githubusercontent.com/To0wnn/knowledge/main/plugin/.claude-plugin/plugin.json")
AGY_PLUGIN = os.environ.get("KNOWLEDGE_AGY_PLUGIN", "https://github.com/To0wnn/knowledge/tree/main/plugin")
DAY = 86400


def commands(agent: str, mode: str = "plugin") -> list[list[str]]:
    if mode == "script":  # the checkout this file runs from: sync it, then let the setup update itself
        repo = PLUGIN.parent
        return [[sys.executable, "-X", "utf8", __file__, "sync", str(repo)],
                [sys.executable, "-X", "utf8", str(repo / "clients" / "knowledge-agent.py"), "install", agent]]
    if agent == "codex":
        codex = shutil.which("codex") or "codex"
        return [[codex, "plugin", "marketplace", "upgrade", "knowledge"], [codex, "plugin", "add", "knowledge@knowledge"]]
    return [[shutil.which("agy") or "agy", "plugin", "install", AGY_PLUGIN]]


def sync(repo: Path) -> None:
    """A rewritten history (force-push) cannot fast-forward and a pull would merge two histories with conflicts in every
    file. The checkout is only a copy of the repository, so it is reset to GitHub's main, but only when nothing tracked in
    it is changed; its old HEAD stays as branch knowledge-before-update."""
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300, stdin=subprocess.DEVNULL)
    if (repo / ".git" / "MERGE_HEAD").exists():  # a manual pull that stopped with conflicts: undo it first
        git("merge", "--abort")
    if git("pull", "--ff-only", "-q").returncode == 0:
        return
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise RuntimeError(f"{repo} has local changes: keep them elsewhere, then: git -C {repo} reset --hard origin/main")
    for step in (("fetch", "-q"), ("branch", "-f", "knowledge-before-update", "HEAD"), ("reset", "-q", "--hard", "@{u}")):
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
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE)


def notice(agent: str, now: float | None = None, spawn=None, mode: str = "plugin") -> str:
    """Session start: the line(s) for the context; starts the background update when a check is due."""
    if os.environ.get("KNOWLEDGE_AUTO_UPDATE") == "0":
        return ""
    now, state, lines = now or time.time(), load(), []
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
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}  # detached
    subprocess.Popen([sys.executable, "-X", "utf8", __file__, "run", agent, mode], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def run(agent: str, mode: str = "plugin") -> str | None:
    """Update when GitHub has a newer version; the line for the next session context, or None (nothing to say)."""
    have = own()
    try:
        want = latest()
    except Exception:  # offline: try again tomorrow, nothing to report
        return None
    if not have or want <= have:
        return None
    new = ".".join(map(str, want))
    for cmd in commands(agent, mode):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                               stdin=subprocess.DEVNULL)
            out = (p.stdout + p.stderr).strip()
            ok = p.returncode == 0 or "already" in out.lower() or "not configured as a git" in out.lower()  # a local checkout, as the setup
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, out = False, str(e)
        if not ok:
            return (f"knowledge plugin: the auto-update to {new} failed ({' '.join(cmd[1:])}: {out[-200:]}); "
                    f"update by hand: python3 knowledge-agent.py install {agent}")
    return f"knowledge plugin: updated to {new} for {agent}; it loads at the next start."


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"] and sys.argv[2:3] and sys.argv[2] in ("codex", "agy"):
        msg = run(sys.argv[2], (sys.argv[3:4] or ["plugin"])[0])
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
        started = []
        first = notice("agy", now=1000.0, spawn=started.append)
        assert "auto-update is on for agy" in first and started == ["agy"], first
        assert notice("agy", now=2000.0, spawn=started.append) == "" and started == ["agy"], "said once, checked once a day"
        assert notice("agy", now=1000.0 + DAY, spawn=started.append) == "" and started == ["agy", "agy"], "next day: check again"
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
        print("ok")
    else:
        sys.exit(__doc__)
