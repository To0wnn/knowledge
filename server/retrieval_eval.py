#!/usr/bin/env python3
"""Measure how well search by meaning finds the note that answers a real question, against the live server.

The question set is the deployment's own (real questions its users asked, each with the file(s) that answer it), kept
outside this repo: JSON list of {"q", "expected": ["memory/x.md", ...]}. Reports hit@1, hit@5 and MRR over the distinct
files in /semantic's answer, plus the text size of the top 5 (what an agent reads). Re-run before and after a change
to search, the embedding model or a lot of the knowledge; --save appends the result to a history file next to the set.
Uses /semantic (not logged as a gap). Standard library only.
  python3 retrieval_eval.py SET.json [token-file] [--save]   (default token ~/.config/knowledge-server/report.token)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = os.environ.get("KNOWLEDGE_URL", "http://localhost:8766").rstrip("/")


def ranks(paths: list[str], expected: list[str]) -> int | None:
    """1-based rank of the first expected file among the distinct files, or None."""
    seen = list(dict.fromkeys(paths))
    return next((i + 1 for i, p in enumerate(seen) if p in expected), None)


def score(results: list[tuple[int | None, int]]) -> dict:
    n = len(results)
    return {"n": n, "hit1": round(sum(r == 1 for r, _ in results) / n, 3),
            "hit5": round(sum(r is not None and r <= 5 for r, _ in results) / n, 3),
            "mrr": round(sum(1 / r for r, _ in results if r) / n, 3),
            "top5_chars": round(sum(c for _, c in results) / n)}


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--save"]
    items = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    token = Path(args[1] if len(args) > 1 else Path.home() / ".config/knowledge-server/report.token").read_text().strip()
    results, misses = [], []
    for it in items:
        req = urllib.request.Request(f"{SERVER}/semantic?q={urllib.parse.quote(it['q'])}",
                                     headers={"Authorization": f"Bearer {token}"})
        hits = json.loads(urllib.request.urlopen(req, timeout=30).read())["hits"]
        files = [h["path"] for h in hits]
        top5 = set(list(dict.fromkeys(files))[:5])
        r = ranks(files, it["expected"])
        results.append((r, sum(len(h["text"]) for h in hits if h["path"] in top5)))
        if r != 1:
            misses.append(f"  rank {r}: {it['q'][:70]} -> wanted {it['expected'][0]}, got {files[0] if files else '-'}")
    s = {"at": time.strftime("%Y-%m-%d %H:%M"), **score(results)}
    print(json.dumps(s), *misses, sep="\n")
    if "--save" in sys.argv:
        with Path(args[0]).with_suffix(".history.jsonl").open("a") as f:
            f.write(json.dumps(s) + "\n")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert ranks(["a", "a", "b", "c"], ["c"]) == 3 and ranks(["a"], ["z"]) is None
        assert score([(1, 10), (3, 20), (None, 30)]) == {"n": 3, "hit1": 0.333, "hit5": 0.667, "mrr": 0.444, "top5_chars": 20}
        print("selftest ok")
    else:
        main()
