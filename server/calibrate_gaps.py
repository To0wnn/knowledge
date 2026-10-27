#!/usr/bin/env python3
"""Measure the knowledge-gap thresholds (WEAK_SCORE, DOUBT_SCORE, MIN_COVERAGE in server.py) against the live server:
questions the knowledge answers must not count as a gap, plausible questions it cannot answer should. Re-run after
changing the embedding model, the thresholds or a lot of the knowledge. The questions are the deployment's own, kept
outside this repo: JSON {"answerable": [...], "unanswerable": [...]} (20+ each). Server: KNOWLEDGE_URL.
Uses /semantic (not logged as a gap). Standard library only.
  python3 calibrate_gaps.py SET.json [token-file]   (default token ~/.config/knowledge-server/report.token)
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = os.environ.get("KNOWLEDGE_URL", "http://localhost:8766").rstrip("/")


def ask(q: str, token: str) -> dict:
    req = urllib.request.Request(f"{SERVER}/semantic?" + urllib.parse.urlencode({"q": q}),
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sets = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    answerable, unanswerable = sets["answerable"], sets["unanswerable"]
    token = Path(sys.argv[2] if len(sys.argv) > 2 else Path.home() / ".config/knowledge-server/report.token").read_text().strip()
    if "chunks" not in (status := ask("x", token)["status"]):
        sys.exit(f"semantic index not ready: {status}")
    false_alarms = [q for q in answerable if ask(q, token)["gap"]]
    missed = [q for q in unanswerable if not ask(q, token)["gap"]]
    print(f"gaps caught: {len(unanswerable) - len(missed)} of {len(unanswerable)} (missed: {missed})")
    print(f"false alarms: {len(false_alarms)} of {len(answerable)} {false_alarms}")
    return 1 if false_alarms else 0  # a false alarm buries the real gaps; a missed gap only hides one


if __name__ == "__main__":
    sys.exit(main())
