#!/usr/bin/env python3
"""Keep the VM's agy on the latest knowledge plugin. agy has no plugin update command and no auto-update, so this
compares the installed version with the one on GitHub and reruns `knowledge-agent.py install agy` when they differ.
Silent when up to date or updated; a failure goes to Matrix (same room as the lint report). Runs from
agy-update.timer.

Config: the agy token from the vault (`secret KNOWLEDGE_TOKEN_AGY`), Matrix as in lint-report.py.
  --self-test  run the checks of this file
Standard library only.
"""
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LATEST = "https://raw.githubusercontent.com/To0wnn/knowledge/main/plugin/.claude-plugin/plugin.json"  # agy reads this one
INSTALLER = HERE.parent / "clients" / "knowledge-agent.py"


def installed(listing: str) -> str | None:
    """Version of the knowledge plugin in `agy plugin list --json` output (None = not installed)."""
    start = listing.find("[")
    plugins = json.loads(listing[start:]) if start >= 0 else []
    return next((p.get("version") for p in plugins if p.get("name") == "knowledge"), None)


def agy_version() -> str | None:
    out = subprocess.run(["agy", "plugin", "list", "--json"], capture_output=True, text=True, timeout=60, check=True)
    return installed(out.stdout)


def latest() -> str:
    with urllib.request.urlopen(LATEST, timeout=20) as r:
        return json.load(r)["version"]


def main() -> None:
    have, want = agy_version(), latest()
    if have == want:
        return print(f"agy knowledge plugin {have}: up to date")
    token = subprocess.run(["secret", "KNOWLEDGE_TOKEN_AGY"], capture_output=True, text=True, check=True).stdout.strip()
    run = subprocess.run([sys.executable, str(INSTALLER), "install", "agy"], capture_output=True, text=True, timeout=300,
                         env={**os.environ, "KNOWLEDGE_TOKEN": token}, stdin=subprocess.DEVNULL)
    now = agy_version()
    if run.returncode != 0 or now != want:
        raise RuntimeError(f"install agy left {now} (wanted {want}, exit {run.returncode}): {(run.stdout + run.stderr)[-400:]}")
    print(f"agy knowledge plugin {have} -> {now}")


def send(text: str) -> None:
    spec = importlib.util.spec_from_file_location("lint_report", HERE / "lint-report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.send(text)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        assert installed('note\n[{"name": "x", "version": "1"}, {"name": "knowledge", "version": "0.9.4"}]') == "0.9.4"
        assert installed('[{"name": "x", "version": "1"}]') is None and installed("no plugins") is None
        print("ok")
    else:
        try:
            main()
        except Exception as e:  # fail loudly: a silent daily job is worse than a noisy one
            send(f"agy knowledge plugin update failed: {type(e).__name__}: {e}")
            raise
