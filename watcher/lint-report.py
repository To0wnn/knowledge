#!/usr/bin/env python3
"""Weekly knowledge lint report to Matrix: only findings that are new since the last run, so known items do not
repeat every week. Silent when nothing is new. Runs on the VM from lint-report.timer.

Config: ~/.config/knowledge-server/report.token (read-only device token), the Matrix bot token from the vault
(`secret MATRIX_BOT_TOKEN`), room and homeserver below. State: ~/.local/state/knowledge-lint-report.json.
  --init     store the current findings as the baseline without sending
  --dry-run  print the message instead of sending it
Standard library only.
"""
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = "https://knowledge.to0wnn.com"
MATRIX = "http://<lan-ip>:8450"
ROOM = "!<room-id>:to0wnn"  # Homelab Alerts
TOKEN = Path.home() / ".config/knowledge-server/report.token"
STATE = Path.home() / ".local/state/knowledge-lint-report.json"
SHOW = 20


def findings() -> set[str]:
    req = urllib.request.Request(f"{SERVER}/ui/api/lint", headers={"Authorization": f"Bearer {TOKEN.read_text().strip()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        report = json.load(r)
    return {f"{check}: {item}" for check, items in report.items() for item in items}


def message(new: set[str], total: int, resolved: int) -> str:
    lines = [f"Knowledge lint: {len(new)} new finding(s) since last week ({total} open, {resolved} resolved)."]
    lines += [f"- {f}" for f in sorted(new)[:SHOW]]
    if len(new) > SHOW:
        lines.append(f"- … and {len(new) - SHOW} more")
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
    current = findings()
    previous = set(json.loads(STATE.read_text())["findings"]) if STATE.exists() else set()
    new, resolved = diff(previous, current)
    if "--init" not in sys.argv and new:
        text = message(new, len(current), resolved)
        print(text) if "--dry-run" in sys.argv else send(text)
    if "--dry-run" not in sys.argv:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"findings": sorted(current), "at": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
    print(f"{len(current)} open, {len(new)} new, {resolved} resolved")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        new, resolved = diff({"a: 1", "b: 2"}, {"b: 2", "c: 3"})
        assert new == {"c: 3"} and resolved == 1
        assert message({"x: y"}, 3, 1).startswith("Knowledge lint: 1 new finding(s) since last week (3 open, 1 resolved).")
        print("ok")
    else:
        main()
