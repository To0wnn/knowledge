#!/usr/bin/env python3
"""Which shared instruction files (claude/CLAUDE.md, house_rules.md, dev_rules.md) this agent already loads from its
own instruction file, so the start context does not repeat them. A file counts only when its whole text stands in that
instruction file (Claude Code: ~/.claude/CLAUDE.md with its @~/.claude/gedeeld/ imports; Codex: ~/.codex/AGENTS.md;
agy: ~/.gemini/GEMINI.md, which the owner's watcher writes from the same files). The server leaves a part out only when
the hash matches its own copy, so a stale or edited local copy gets the full context. Only names and hashes leave the
machine, never the text.

  native_rules.py <claude|codex|agy>   print name:sha256 pairs, comma-separated (nothing when none)
Standard library only, works on Windows.
"""
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

IMPORT = re.compile(r"^@~/\.claude/gedeeld/([\w.-]+\.md)[ \t]*$", re.M)  # the server's and the watcher's syntax
FRONT = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)


def flat(text: str) -> str:
    return " ".join(text.split())  # line ends and wrapping differ between the copies, the words do not


def digest(text: str) -> str:
    return hashlib.sha256(flat(text).encode("utf-8")).hexdigest()


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def expand(text: str, shared: Path) -> str:
    """An import line replaced by the shared file it names, as Claude Code reads it; a missing file keeps the line."""
    return IMPORT.sub(lambda m: FRONT.sub("", read(shared / m.group(1)), count=1) or m.group(0), text)


# Codex and agy cut a long instruction file (agy: 24 KB after includes, Codex: project_doc_max_bytes 32 KiB): past this
# the tail may never reach the model, so nothing is claimed and the start context brings the rules itself.
MAX_BYTES = {"codex": 28_000, "agy": 20_000}  # with some margin below those caps


def own_file(agent: str):
    home = Path.home()
    return {"claude": Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "CLAUDE.md",
            "codex": Path(os.environ.get("CODEX_HOME") or home / ".codex") / "AGENTS.md",
            "agy": home / ".gemini" / "GEMINI.md"}.get(agent)


def claude_excludes() -> bool:
    """True when a settings file Claude Code reads names claudeMdExcludes: then it may skip an imported rule file that
    still stands in CLAUDE.md (tested 14 Nov 2026: house_rules.md was gone), so Claude claims nothing. A --settings flag
    on the command line is checked in claude_flags."""
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    files = [cfg / "settings.json", cfg / "settings.local.json", project / ".claude" / "settings.json",
             project / ".claude" / "settings.local.json", Path("/etc/claude-code/managed-settings.json"),
             Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
             Path(os.environ.get("ProgramData") or "C:/ProgramData") / "ClaudeCode" / "managed-settings.json",
             Path(os.environ.get("ProgramFiles") or "C:/Program Files") / "ClaudeCode" / "managed-settings.json"]
    return any("claudeMdExcludes" in read(f) for f in files) or claude_flags()


def ancestors(limit: int = 8) -> list:
    """The command lines of the processes above this hook (Claude Code itself is one of them): /proc on Linux, ps on
    macOS. Windows: none (no cheap way to read another process's arguments); shortcut: a --settings flag with
    claudeMdExcludes on Windows stays unseen, upgrade if anything of ours ever starts Claude that way."""
    pid, out, top = os.getppid(), [], int(os.environ.get("KNOWLEDGE_NATIVE_TOP") or 0)  # top: the self-test's own process
    for _ in range(limit):
        if pid <= 1 or pid == top:
            break
        try:
            if Path(f"/proc/{pid}/cmdline").exists():
                args = [a.decode("utf-8", "replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
                ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
            elif sys.platform == "darwin":
                ppid_s, _, line = subprocess.run(["ps", "-o", "ppid=", "-o", "args=", "-p", str(pid)], capture_output=True,
                                                  text=True, timeout=2).stdout.strip().partition(" ")
                ppid, args = int(ppid_s), line.split()
            else:
                break
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            break
        out.append(args)
        if args and Path(args[0]).name.lower() in ("claude", "claude.exe"):
            break  # Claude Code itself: only its flags count, not those of whatever started it
        pid = ppid
    return out


def claude_flags() -> bool:
    """claudeMdExcludes in a --settings flag of a process above this hook: inline JSON or a settings file it names."""
    for args in ancestors():
        if any("claudeMdExcludes" in a for a in args):
            return True
        for i, a in enumerate(args):
            path = args[i + 1] if a == "--settings" and i + 1 < len(args) else a[len("--settings="):] if a.startswith("--settings=") else ""
            if path and "claudeMdExcludes" in read(Path(path)):
                return True
    return False


def shared_text(path: Path, shared: Path) -> str:
    """The text a shared file adds to an instruction file: without frontmatter, CLAUDE.md with its imports."""
    body = FRONT.sub("", read(path), count=1)
    return expand(body, shared) if path.name == "CLAUDE.md" else body


def codex_skips(raw: str) -> bool:
    """Codex reads AGENTS.override.md instead of AGENTS.md when it is not empty, and stops adding instruction files at
    project_doc_max_bytes (32 KiB unless set): in config.toml (any profile) or as -c on a process above the hook."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if read(home / "AGENTS.override.md").strip():
        return True
    limits = [int(n) for n in re.findall(r"^\s*project_doc_max_bytes\s*=\s*(\d+)", read(home / "config.toml"), re.M)]
    if limits and min(limits) < len(raw.encode("utf-8")):
        return True
    return any("project_doc_max_bytes" in a for args in ancestors() for a in args)


AGY_BUDGET = 60_000  # bytes: agy shares 20k tokens among all always-on rules and turns the largest into a mere pointer


def agy_rule_bytes(cwd: str) -> int:
    """The always-on rule files agy loads: its global ones, config/rules, plugin rules, and AGENTS.md / GEMINI.md /
    .agents/ from the folder up to the repository root (https://antigravity.google/docs/rules)."""
    home, files = Path.home(), []
    files += [home / ".gemini" / n for n in ("AGENTS.md", "GEMINI.md", "config/AGENTS.md", "config/GEMINI.md")]
    files += sorted((home / ".gemini" / "config" / "rules").glob("*.md")) + sorted(home.glob(".gemini/config/plugins/*/rules/*.md"))
    d = Path(cwd).resolve()
    while True:
        files += [d / n for n in ("AGENTS.md", "GEMINI.md", ".agents/AGENTS.md", ".agents/GEMINI.md")]
        files += sorted((d / ".agents" / "rules").glob("*.md"))
        if (d / ".git").exists() or d == d.parent:
            break
        d = d.parent
    return sum(f.stat().st_size for f in files if f.is_file())


def native(agent: str, shared: Path = None, cwd: str = "") -> str:
    shared = shared or Path.home() / ".claude" / "gedeeld"
    path = own_file(agent)
    raw = read(path) if path else ""
    if (agent in MAX_BYTES and len(raw.encode("utf-8")) > MAX_BYTES[agent] or agent == "claude" and claude_excludes()
            or agent == "codex" and codex_skips(raw)
            or agent == "agy" and agy_rule_bytes(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()) > AGY_BUDGET):
        return ""
    own = flat(expand(raw, shared))
    if not own:
        return ""
    found = []
    for p in sorted(shared.glob("*.md")):
        text = shared_text(p, shared)
        if flat(text) and flat(text) in own:
            found.append(f"{p.name}:{digest(text)}")
    return ",".join(found)


def self_test():
    import tempfile
    global ancestors
    real, ancestors = ancestors, lambda limit=8: []  # whatever started this test (a shell naming the word) does not count
    with tempfile.TemporaryDirectory() as t:
        home = Path(t)
        shared = home / ".claude" / "gedeeld"
        shared.mkdir(parents=True)
        (shared / "house_rules.md").write_text("---\nname: x\n---\n# House rules\n1. Search first.\n", encoding="utf-8")
        (shared / "dev_rules.md").write_text("# Dev\n1. Read first.\n", encoding="utf-8")
        (shared / "CLAUDE.md").write_text("# Me\n\n@~/.claude/gedeeld/house_rules.md\n", encoding="utf-8")
        os.environ.update({"CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
                           "HOME": str(home), "USERPROFILE": str(home)})  # never the real ~/.gemini
        (home / ".claude" / "CLAUDE.md").write_text("Local line.\n@~/.claude/gedeeld/house_rules.md\n", encoding="utf-8")
        got = dict(x.split(":") for x in native("claude", shared).split(","))
        assert set(got) == {"house_rules.md"} and got["house_rules.md"] == digest("# House rules\n1. Search first.\n"), got
        (home / ".claude" / "settings.json").write_text('{"claudeMdExcludes": ["**/house_rules.md"]}', encoding="utf-8")
        assert native("claude", shared) == "", "Claude may skip an import: claim nothing"
        (home / ".claude" / "settings.json").unlink()
        if sys.platform != "win32":  # a --settings flag on a process above the hook, inline or as a file
            me = [sys.executable, __file__, "claude"]
            env = {**os.environ, "PYTHONPATH": "", "KNOWLEDGE_NATIVE_TOP": str(os.getpid())}
            run = lambda *flag: subprocess.run(["sh", "-c", 'exec "$@"', "sh", *me] if not flag else
                                               ["sh", "-c", '"$0" "$1" "$2"', *me, *flag], capture_output=True, text=True, env=env).stdout.strip()
            assert run() != "", "no flag: claimed"
            assert run("--settings", '{"claudeMdExcludes": ["x"]}') == ""
            (home / "s.json").write_text('{"claudeMdExcludes": ["x"]}', encoding="utf-8")
            assert run("--settings", str(home / "s.json")) == "" and run(f"--settings={home / 's.json'}") == ""
        (home / ".codex").mkdir()  # the watcher's generated file: CLAUDE.md with its imports in place, other wrapping
        (home / ".codex" / "AGENTS.md").write_text("<!-- Generated -->\n# Me\n\n# House rules\n1.   Search first.\r\n", encoding="utf-8")
        assert set(dict(x.split(":") for x in native("codex", shared).split(","))) == {"CLAUDE.md", "house_rules.md"}
        (home / ".codex" / "AGENTS.md").write_text("# Me\n\n# House rules\n1. Search first, edited.\n", encoding="utf-8")
        assert native("codex", shared) == "", "an edited copy is not the shared text"
        assert native("agy", shared) == "" and native("nobody", shared) == ""  # no file: nothing claimed
        (home / ".codex" / "AGENTS.md").write_text("# Me\n\n# House rules\n1. Search first.\n" + "x" * 30_000, encoding="utf-8")
        assert native("codex", shared) == "", "a file the agent may cut off claims nothing"
        good = "<!-- Generated -->\n# Me\n\n# House rules\n1. Search first.\n"
        (home / ".codex" / "AGENTS.md").write_text(good, encoding="utf-8")
        assert native("codex", shared) != ""
        (home / ".codex" / "AGENTS.override.md").write_text("Use French.\n", encoding="utf-8")
        assert native("codex", shared) == "", "Codex reads the override instead"
        (home / ".codex" / "AGENTS.override.md").write_text("  \n", encoding="utf-8")
        assert native("codex", shared) != "", "an empty override is skipped by Codex too"
        (home / ".codex" / "config.toml").write_text("[profiles.x]\nproject_doc_max_bytes = 10\n", encoding="utf-8")
        assert native("codex", shared) == "", "a limit below the file cuts it"
        (home / ".codex" / "config.toml").write_text("project_doc_max_bytes = 65536\n", encoding="utf-8")
        assert native("codex", shared) != ""
        (home / ".gemini").mkdir()
        (home / ".gemini" / "GEMINI.md").write_text(good, encoding="utf-8")
        repo = home / "repo"
        (repo / ".git").mkdir(parents=True)
        assert native("agy", shared, str(repo)) != ""
        (repo / "AGENTS.md").write_text("x" * 70_000, encoding="utf-8")
        assert native("agy", shared, str(repo)) == "", "past agy's shared rule budget the largest file may become a pointer"
    ancestors = real
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    elif len(sys.argv) == 2:
        print(native(sys.argv[1]))
