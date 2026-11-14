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


def shared_text(path: Path, shared: Path) -> str:
    """The text a shared file adds to an instruction file: without frontmatter, CLAUDE.md with its imports."""
    body = FRONT.sub("", read(path), count=1)
    return expand(body, shared) if path.name == "CLAUDE.md" else body


def native(agent: str, shared: Path = None) -> str:
    shared = shared or Path.home() / ".claude" / "gedeeld"
    path = own_file(agent)
    raw = read(path) if path else ""
    if agent in MAX_BYTES and len(raw.encode("utf-8")) > MAX_BYTES[agent]:
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
        (home / ".codex").mkdir()  # the watcher's generated file: CLAUDE.md with its imports in place, other wrapping
        (home / ".codex" / "AGENTS.md").write_text("<!-- Generated -->\n# Me\n\n# House rules\n1.   Search first.\r\n", encoding="utf-8")
        assert set(dict(x.split(":") for x in native("codex", shared).split(","))) == {"CLAUDE.md", "house_rules.md"}
        (home / ".codex" / "AGENTS.md").write_text("# Me\n\n# House rules\n1. Search first, edited.\n", encoding="utf-8")
        assert native("codex", shared) == "", "an edited copy is not the shared text"
        assert native("agy", shared) == "" and native("nobody", shared) == ""  # no file: nothing claimed
        (home / ".codex" / "AGENTS.md").write_text("# Me\n\n# House rules\n1. Search first.\n" + "x" * 30_000, encoding="utf-8")
        assert native("codex", shared) == "", "a file the agent may cut off claims nothing"
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    elif len(sys.argv) == 2:
        print(native(sys.argv[1]))
