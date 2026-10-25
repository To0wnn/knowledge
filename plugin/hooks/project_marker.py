#!/usr/bin/env python3
"""The link between a local folder and a knowledge project: a file `.knowledge-project` holding the project name, in
the project's root folder. The session start (Claude Code, Codex, agy) asks the server for that project, and docs-sync
mirrors the folder's Markdown into it. The knowledge-init skill writes it.

A linked git worktree (Codex and agy make them per chat, e.g. under ~/.codex/worktrees/) counts as its main checkout:
same project, and docs come only from the main checkout, so a stale worktree never overwrites them.

  project_marker.py <folder>          print the project name the folder is linked to (nothing when it is not linked)
  project_marker.py notice <folder>   print the session-start notice for a worktree (nothing elsewhere)
Standard library only, works on Windows.
"""
import sys
from pathlib import Path

MARKER = ".knowledge-project"


def worktree(start: Path):
    """(worktree root, main checkout) when `start` is inside a linked git worktree, else None."""
    for d in [start, *start.parents]:
        git = d / ".git"
        if git.is_dir():
            return None  # a main checkout
        if git.is_file():
            line = git.read_text(encoding="utf-8", errors="replace").strip()
            if not line.startswith("gitdir:"):
                return None
            gitdir = (d / line[len("gitdir:"):].strip()).resolve()
            common = gitdir / "commondir"
            if not common.is_file():
                return None  # a submodule, not a worktree
            return d, (gitdir / common.read_text(encoding="utf-8").strip()).resolve().parent
    return None


def project_root(start: Path):
    """(folder, project name) of the nearest folder at or above `start` that holds the marker, else (None, None).
    Inside a linked worktree the search starts at its main checkout."""
    wt = worktree(start)
    for d in ([wt[1], *wt[1].parents] if wt else [start, *start.parents]):
        marker = d / MARKER
        if marker.is_file():
            raw = marker.read_bytes()  # PowerShell's "echo a350 > file" writes UTF-16, Set-Content -Encoding utf8 a BOM
            text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", errors="replace")
            name = text.strip().splitlines()[:1]
            return d, (name[0].strip() if name else "")
    return None, None


def notice(start: Path) -> str:
    """Tell an agent that runs in a separate checkout, so it does not finish there unseen."""
    wt = worktree(start)
    if not wt:
        return ""
    return (f"knowledge: this session runs in a separate git worktree ({wt[0]}) of {wt[1]}. Other agents and the user "
            f"work in the main checkout and do not see these changes until they are merged. Follow the project's rule on "
            f"worktrees in its memory; before you finish, merge or hand the work off and say where it is. Docs are "
            f"mirrored from the main checkout only.")


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if args[:1] == ["notice"] and args[1:2]:
            if text := notice(Path(args[1]).resolve()):
                print(text)
        elif args[:1]:
            print(project_root(Path(args[0]).resolve())[1] or "")
    except OSError:
        pass  # an unreadable folder is an unlinked one; never break the session start
