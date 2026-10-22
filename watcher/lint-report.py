#!/usr/bin/env python3
"""Weekly knowledge lint report to Matrix: only findings that are new since the last run, so known items do not
repeat every week, plus every week the memos most overdue for a check (review_due, oldest first) so a few get
checked each week. Silent when there is neither. Runs on the VM from lint-report.timer.

Config: ~/.config/knowledge-server/report.token (read-only device token), the Matrix bot token from the vault
(`secret MATRIX_BOT_TOKEN`), room and homeserver below. State: ~/.local/state/knowledge-lint-report.json.
  --init     store the current findings as the baseline without sending
  --dry-run  print the message instead of sending it
Standard library only.
"""
import json
import re
import subprocess
import sys
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = "https://knowledge.to0wnn.com"
MATRIX = os.environ["KNOWLEDGE_MATRIX_URL"]  # from local.env via the unit (EnvironmentFile); missing = fail loudly
ROOM = os.environ["KNOWLEDGE_MATRIX_ROOM"]
TOKEN = Path.home() / ".config/knowledge-server/report.token"
STATE = Path.home() / ".local/state/knowledge-lint-report.json"
SHOW = 20
DUE = 5  # memos to check per week
COMPOSE = Path(__file__).resolve().parent.parent / "server/compose.yaml"  # the memory dirs the server serves
PROJECTS_DIR = Path.home() / ".claude/projects"


def findings() -> set[str]:
    req = urllib.request.Request(f"{SERVER}/lint", headers={"Authorization": f"Bearer {TOKEN.read_text().strip()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        report = json.load(r)
    return {f"{check}: {item}" for check, items in report.items() for item in items}


def unserved(projects_dir: Path = PROJECTS_DIR, compose: Path = COMPOSE) -> set[str]:
    """Claude Code memory dirs on the VM that hold memos but are not served by the knowledge server: a new working
    folder got its own memory. Fix: add it as a project, or point the folder's autoMemoryDirectory at an existing one."""
    served = set(re.findall(r'device: ":(/[^"]+/memory)"', compose.read_text()))
    out = set()
    for mem in sorted(projects_dir.glob("*/memory")):
        memos = [f for f in mem.glob("*.md") if f.name != "MEMORY.md"]
        if memos and str(mem) not in served:
            out.add(f"unserved_memory: {mem} ({len(memos)} memos)")
    return out


def due(current: set[str]) -> list[str]:
    """The DUE review_due findings with the oldest check date ("…: not checked since 2026-08-01 (…)")."""
    items = [f.removeprefix("review_due: ") for f in current if f.startswith("review_due: ")]
    return sorted(items, key=lambda i: (m := re.search(r"since (\S+)", i)) and m.group(1) or "")[:DUE]  # never = first


def overdue(current: set[str]) -> list[str]:
    """Open items past their 'open tot <date>', listed every week until they are done or moved."""
    return sorted(f.removeprefix("open_overdue: ") for f in current if f.startswith("open_overdue: "))


def message(new: set[str], total: int, resolved: int, check: list[str] = (), late: list[str] = ()) -> str:
    lines = [f"Knowledge lint: {len(new)} new finding(s) since last week ({total} open, {resolved} resolved)."]
    lines += [f"- {f}" for f in sorted(new)[:SHOW]]
    if len(new) > SHOW:
        lines.append(f"- … and {len(new) - SHOW} more")
    if late:
        lines.append(f"Overdue open items ({len(late)}): do them or move the date:")
        lines += [f"- {o}" for o in late]
    if check:
        lines.append(f"Check these {len(check)} on the live system this week, then mark them (gecontroleerd <date>):")
        lines += [f"- {c}" for c in check]
    lines.append(f"Details: {SERVER}/ui (tab Lint) or knowledge_lint.")
    return "\n".join(lines)


def send(text: str) -> None:
    token = subprocess.run(["secret", "MATRIX_BOT_TOKEN"], capture_output=True, text=True, check=True).stdout.strip()
    url = f"{MATRIX}/_matrix/client/v3/rooms/{urllib.parse.quote(ROOM)}/send/m.room.message/lint-{time.time_ns()}"
    req = urllib.request.Request(url, method="PUT", data=json.dumps({"msgtype": "m.text", "body": text}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status != 200:
            raise RuntimeError(f"Matrix answered {r.status}")


def diff(previous: set[str], current: set[str]) -> tuple[set[str], int]:
    return current - previous, len(previous - current)


def main() -> None:
    current = findings() | unserved()
    previous = set(json.loads(STATE.read_text())["findings"]) if STATE.exists() else set()
    new, resolved = diff(previous, current)
    new = {f for f in new if not f.startswith(("review_due: ", "open_overdue: "))}  # those are listed every week below
    if "--init" not in sys.argv and (new or due(current) or overdue(current)):
        text = message(new, len(current), resolved, due(current), overdue(current))
        print(text) if "--dry-run" in sys.argv else send(text)
    if "--dry-run" not in sys.argv:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"findings": sorted(current), "at": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
    print(f"{len(current)} open, {len(new)} new, {resolved} resolved")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        new, resolved = diff({"a: 1", "b: 2"}, {"b: 2", "c: 3"})
        assert new == {"c: 3"} and resolved == 1
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "p/-home-a/memory").mkdir(parents=True); (t / "p/-home-a/memory/x.md").write_text("x")
            (t / "p/-home-b/memory").mkdir(parents=True); (t / "p/-home-b/memory/MEMORY.md").write_text("")
            (t / "p/-home-c/memory").mkdir(parents=True); (t / "p/-home-c/memory/y.md").write_text("y")
            (t / "compose.yaml").write_text(f'device: ":{t}/p/-home-c/memory"')
            assert unserved(t / "p", t / "compose.yaml") == {f"unserved_memory: {t}/p/-home-a/memory (1 memos)"}
        cur = {f"review_due: m{i}.md: not checked since 2026-0{i}-01 (1 addresses/versions)" for i in range(1, 8)} | {"x: y"}
        assert due(cur) == [f"m{i}.md: not checked since 2026-0{i}-01 (1 addresses/versions)" for i in range(1, 6)], due(cur)
        assert due(cur | {"review_due: n.md: never checked (no date) (1 addresses/versions)"})[0].startswith("n.md"), "never first"
        assert "- m1.md: not checked since 2026-01-01" in message(set(), 8, 0, due(cur))
        assert overdue({"open_overdue: d.md: due 2026-10-21: x", "x: y"}) == ["d.md: due 2026-10-21: x"]
        assert "Overdue open items (1)" in message(set(), 2, 0, late=["d.md: due 2026-10-21: x"])
        assert message({"x: y"}, 3, 1).startswith("Knowledge lint: 1 new finding(s) since last week (3 open, 1 resolved).")
        print("ok")
    else:
        try:
            main()
        except Exception as e:  # fail loudly: a silent weekly job is worse than a noisy one
            if "--dry-run" not in sys.argv:
                send(f"Knowledge lint report failed: {type(e).__name__}: {e}")
            raise
