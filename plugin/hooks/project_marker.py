#!/usr/bin/env python3
"""The link between a local folder and a knowledge project: a file `.knowledge-project` holding the project name, in
the project's root folder. The session start (Claude Code, Codex, agy) asks the server for that project, and docs-sync
mirrors the folder's Markdown into it. The knowledge-init skill writes it.

  project_marker.py <folder>   print the project name the folder is linked to (nothing when it is not linked)
Standard library only, works on Windows.
"""
import sys
from pathlib import Path

MARKER = ".knowledge-project"


def project_root(start: Path):
    """(folder, project name) of the nearest folder at or above `start` that holds the marker, else (None, None)."""
    for d in [start, *start.parents]:
        marker = d / MARKER
        if marker.is_file():
            raw = marker.read_bytes()  # PowerShell's "echo a350 > file" writes UTF-16, Set-Content -Encoding utf8 a BOM
            text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", errors="replace")
            name = text.strip().splitlines()[:1]
            return d, (name[0].strip() if name else "")
    return None, None


if __name__ == "__main__":
    if sys.argv[1:2]:
        try:
            print(project_root(Path(sys.argv[1]).resolve())[1] or "")
        except OSError:
            pass  # an unreadable folder is an unlinked one; never break the session start
