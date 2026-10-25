#!/usr/bin/env python3
"""Count how often the user corrects the agent: the measurement behind the lesson loop.

Reads the Claude Code transcripts on this machine (interactive sessions only; `claude -p` runs are skipped), takes every
follow-up prompt with the end of the agent's answer before it, and lets Haiku label each one once as a correction or
not. Labels are cached by prompt hash, so a weekly run only pays for new prompts. Prints corrections per 100 follow-up
prompts per ISO week. The prompts stay on this machine; only the label call sends them to the model.

  --calibrate FILE  precision and recall against a hand-labelled set (JSON list of {prompt, prev, correction})
  --selftest        check the parsing and counting with a fake classifier
Standard library only.
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

TRANSCRIPTS = os.path.expanduser(os.environ.get("KNOWLEDGE_TRANSCRIPTS", "~/.claude/projects/*/*.jsonl"))
STATE = Path(os.environ.get("KNOWLEDGE_CORRECTIONS_STATE", "~/.local/state/corrections")).expanduser()
BATCH = 100
MODEL = "haiku"  # measured 25 Oct 2026 on a held-out set: haiku P 0.72 R 0.85, sonnet P 0.78 R 0.52 at 10x the cost
INSTR = """Below are follow-up messages a user (Dutch, many typos) sent to an AI coding agent, each with the end of the \
agent's previous answer. Label a message a CORRECTION when it signals the agent's previous work or answer was wrong, \
incomplete, unwanted, misunderstood, or did not work (e.g. "werkt nog steeds niet", "nee ik wil...", "waarom zit je op \
X?", a challenge of the agent's claim). Not a correction: a new request or idea, approval, a plain question, a status \
update, or a new problem the agent did not cause or was not already fixing.
Answer with only a JSON list of the numbers that are corrections, nothing else."""


def prompts(lines) -> list[dict]:
    """Follow-up prompts of one interactive transcript: [{ts, prompt, prev}]. The first prompt cannot correct anything."""
    out, prev, interactive = [], "", None
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain") or d.get("isMeta"):
            continue
        m = d.get("message") or {}
        if d.get("type") == "assistant":
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and c["text"].strip():
                    prev = c["text"].replace("\0", "")  # a NUL cannot go into a command-line argument
        elif d.get("type") == "user":
            interactive = d.get("entrypoint", "cli") == "cli" if interactive is None else interactive
            if not interactive:
                return []
            c = m.get("content")
            t = c if isinstance(c, str) else " ".join(
                x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text")
            t = " ".join(t.replace("\0", "").split())
            if not t or t.startswith(("<", "[Request interrupted", "Caveat:", "This session is being continued")):
                continue
            if prev:
                out.append({"ts": d.get("timestamp", "")[:10], "prompt": t[:300], "prev": prev[-200:]})
            prev = ""
    return out


def key(p: dict) -> str:
    return hashlib.sha1(f"{p['ts']}\0{p['prompt']}\0{p['prev']}".encode()).hexdigest()[:16]


def classify(items: list[dict]) -> tuple[set[int], float]:
    """Indices of the corrections in items, and the cost. A bare call: no tools, MCP, settings or CLAUDE.md (~600
    tokens of overhead instead of ~100k)."""
    body = "\n".join(f'{i}. [agent: ...{x["prev"][-150:]}] USER: {x["prompt"][:250]}' for i, x in enumerate(items))
    r = subprocess.run(["claude", "-p", body, "--system-prompt", INSTR, "--model", MODEL, "--output-format", "json",
                        "--max-turns", "1", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                        "--no-session-persistence"], capture_output=True, text=True, timeout=300, cwd=STATE)
    out = json.loads(r.stdout or "{}")
    if r.returncode or out.get("is_error"):
        raise RuntimeError(f"claude -p failed (exit {r.returncode}): {(out.get('result') or r.stderr)[-300:]}")
    txt = out["result"]
    ids = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
    return {i for i in ids if isinstance(i, int) and 0 <= i < len(items)}, out.get("total_cost_usd") or 0.0


def label_all(items: list[dict], labels: dict, clf=classify) -> float:
    """Label the items not in the cache yet; returns the cost of this run."""
    todo = [p for p in items if key(p) not in labels]
    cost = 0.0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        hits, c = clf(batch)
        cost += c
        labels.update({key(p): j in hits for j, p in enumerate(batch)})
    return cost


def weekly(items: list[dict], labels: dict) -> list[tuple[str, int, int]]:
    """[(ISO week, follow-up prompts, corrections)], oldest first."""
    n, k = Counter(), Counter()
    for p in items:
        y, w, _ = date.fromisoformat(p["ts"]).isocalendar()
        wk = f"{y}-W{w:02d}"
        n[wk] += 1
        k[wk] += bool(labels.get(key(p)))
    return [(w, n[w], k[w]) for w in sorted(n)]


def calibrate(path: str) -> None:
    s = json.load(open(path, encoding="utf-8"))
    got, cost = set(), 0.0
    for i in range(0, len(s), BATCH):
        hits, c = classify(s[i:i + BATCH])
        got |= {i + h for h in hits}
        cost += c
    pos = {i for i, x in enumerate(s) if x["correction"]}
    tp, fp, fn = len(got & pos), len(got - pos), len(pos - got)
    print(f"{len(pos)} corrections, {len(s) - len(pos)} others: precision {tp / max(tp + fp, 1):.2f}, "
          f"recall {tp / max(tp + fn, 1):.2f}, cost ${cost:.3f}")


def selftest() -> None:
    lines = [json.dumps(d) for d in [
        {"type": "user", "entrypoint": "cli", "timestamp": "2026-10-19T10:00", "message": {"content": "fix the dns"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed it."}]}},
        {"type": "user", "timestamp": "2026-10-19T10:05", "message": {"content": "werkt nog\u0000 steeds niet"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Now it is."}]}},
        {"type": "user", "isMeta": True, "message": {"content": "meta"}},
        {"type": "user", "timestamp": "2026-10-26T09:00", "message": {"content": [{"type": "text", "text": "ok top"}]}}]]
    ps = prompts(lines)
    assert [p["prompt"] for p in ps] == ["werkt nog steeds niet", "ok top"], ps
    assert prompts([lines[0].replace('"cli"', '"sdk-cli"'), *lines[1:]]) == []
    calls = []
    fake = lambda b: (calls.append(len(b)) or {j for j, p in enumerate(b) if "niet" in p["prompt"]}, 0.01)
    labels = {}
    label_all(ps, labels, fake)
    assert label_all(ps, labels, fake) == 0.0 and calls == [2]  # cached: the second run pays nothing
    assert weekly(ps, labels) == [("2026-W43", 1, 1), ("2026-W44", 1, 0)], weekly(ps, labels)
    print("selftest ok")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    STATE.mkdir(parents=True, exist_ok=True)
    if a.calibrate:
        return calibrate(a.calibrate)
    items = [p for f in sorted(glob.glob(TRANSCRIPTS)) for p in prompts(open(f, errors="replace"))]
    lf = STATE / "labels.json"
    labels = json.loads(lf.read_text()) if lf.exists() else {}
    try:
        cost = label_all(items, labels)
    finally:
        lf.write_text(json.dumps(labels))  # keep what was paid for, also when a batch fails
    rows = weekly(items, labels)
    (STATE / "weekly.json").write_text(json.dumps(rows))
    for w, n, k in rows:
        print(f"{w}  {k:4d}/{n:<5d} {100 * k / n:5.1f} per 100")
    print(f"cost this run ${cost:.3f}", file=sys.stderr)


if __name__ == "__main__":
    main()
