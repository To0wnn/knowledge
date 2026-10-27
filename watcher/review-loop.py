#!/usr/bin/env python3
"""Weekly unattended check of the memos that lint reports as review_due (addresses/versions not checked for 45 days).

A loop of fresh `claude -p` passes, two memos each. The loop decides by what is in the files, never by what the agent
says: a memo counts as done only when it carries a "(gecontroleerd <today>)" mark afterwards; the agent may report a
memo as blocked (host off, hardware not bought) with a reason. It keeps going while passes make progress, with no time
limit on the whole run (owner, 25 Oct 2026: never cut off work that is on track), and stops when everything is done
or blocked, or when two passes in a row made none. It ends by itself: the list is finite and every counted pass
handles at least one memo. PASS_TIMEOUT only ends one hung pass; what it finished still counts. One Matrix message at
the end; a crash is reported too. Runs on the VM from review-loop.timer, after which Monday's lint report shows what is left.

  --dry-run   list the targets and the first pass's prompt, run nothing
  --selftest  check the loop logic with a fake agent
Standard library only.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import lint  # noqa: E402  (latest_date, CHECKED: the same rule the lint applies)

HOME = Path.home()
PREFIX = os.environ.get("KNOWLEDGE_VM_CC_PREFIX", "-home-" + HOME.name)
ROOTS = {  # lint's path prefix -> folder on this machine; the homelab memory has no prefix
    "": HOME / f".claude/projects/{PREFIX}-homelab/memory",
    "homelab/": HOME / "homelab",
    "anpitek-memory/": HOME / f".claude/projects/{PREFIX}-anpitek/memory",
    "trade-memory/": HOME / f".claude/projects/{PREFIX}-trade-bot/memory",
}
PER_PASS = 2
PASS_TIMEOUT = 40 * 60
STALL = 2  # passes in a row without progress
LOG = HOME / ".local/state/review-loop"
SCHEMA = {"type": "object", "required": ["blocked"], "properties": {"blocked": {
    "type": "array", "items": {"type": "object", "required": ["memo", "reason"],
                               "properties": {"memo": {"type": "string"}, "reason": {"type": "string"}}}}}}
NL_MONTHS = "jan feb mrt apr mei jun jul aug sep okt nov dec".split()


def today_nl(d: date) -> str:
    return f"{d.day} {NL_MONTHS[d.month - 1]} {d.year}"


def locate(item: str) -> Path | None:
    """'trade-memory/x.md: not checked since …' -> the file here, or None (a root this VM does not hold)."""
    rel = item.split(": ", 1)[0]
    prefix = rel.split("/", 1)[0] + "/" if "/" in rel else ""
    root = ROOTS.get(prefix)
    p = root / rel[len(prefix):] if root else None
    return p if p and p.is_file() else None


def checked_on(text: str, day: date) -> bool:
    return any(lint.latest_date(m if re.search(r"20\d\d", m) else f"{m} {day.year}") == day for m in lint.CHECKED.findall(text))


def prompt(memos: list[Path], day: date) -> str:
    names = "\n".join(f"- {m}" for m in memos)
    return f"""Unattended weekly check of memos whose addresses and versions have not been checked for 45 days:
{names}

For each memo, check every IP address, host, port and version in it against the live system, read-only: ssh/curl/API
GET requests and status commands only. Never change, restart or reconfigure anything on any host; never touch OPNsense
beyond reading. Then edit only that memo file:
- a fact that is right: mark it "(gecontroleerd {today_nl(day)})";
- a fact that is wrong: correct it and mark it "(gecontroleerd {today_nl(day)})";
- a fact you cannot check (host off, not reachable, hardware not bought yet): leave its marks as they are and report the
  memo as blocked with the reason. Never write a "gecontroleerd" mark for something you did not check this session.
Follow the house rules for the memo (Stand/Keuzes, absolute dates, ‹secret:NAME› for secrets). Answer with the memos you
could not check fully, as JSON (empty list when all were checked)."""


def run_agent(memos: list[Path], day: date) -> list[dict]:
    cmd = ["claude", "-p", prompt(memos, day), "--permission-mode", "auto", "--max-turns", "80",
           "--output-format", "json", "--json-schema", json.dumps(SCHEMA)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PASS_TIMEOUT, cwd=HOME / "homelab")
    except subprocess.TimeoutExpired:
        return []  # a hung pass; the memos it did finish still count in loop()
    out = json.loads(r.stdout or "{}")
    LOG.mkdir(parents=True, exist_ok=True)  # each pass kept for checking afterwards: cost, turns, refused actions, answer
    (LOG / f"{day.isoformat()}-{time.strftime('%H%M%S')}.json").write_text(r.stdout or r.stderr, encoding="utf-8")
    if r.returncode or out.get("is_error"):
        raise RuntimeError(f"claude -p failed (exit {r.returncode}): {(out.get('result') or r.stderr)[-300:]}")
    return (out.get("structured_output") or {}).get("blocked") or []


def loop(targets: list[Path], agent, day: date) -> dict:
    done, blocked, idle = [], {}, 0
    while (todo := [t for t in targets if t not in done and t not in blocked]):
        batch = todo[:PER_PASS]
        reported = {str(b["memo"]): b["reason"] for b in agent(batch, day)}
        progress = 0
        for m in batch:
            if checked_on(m.read_text(encoding="utf-8"), day):
                done.append(m); progress += 1
            elif reason := next((r for k, r in reported.items() if k.endswith(m.name)), None):
                blocked[m] = reason; progress += 1
        idle = 0 if progress else idle + 1
        if idle >= STALL:
            return {"done": done, "blocked": blocked, "left": todo, "stop": "no progress"}
    return {"done": done, "blocked": blocked, "left": [], "stop": "all handled"}


def report(res: dict, skipped: list[str]) -> str:
    lines = [f"Memo check (review_due): {len(res['done'])} checked, {len(res['blocked'])} blocked, {len(res['left'])} left"
             f" - stopped: {res['stop']}."]
    if res["stop"] == "no progress":
        lines.append(f"{STALL} passes in a row checked nothing: look at the memos left.")
    lines += [f"- checked: {m.name}" for m in res["done"]]
    lines += [f"- blocked: {m.name}: {r}" for m, r in res["blocked"].items()]
    lines += [f"- left: {m.name}" for m in res["left"]]
    lines += [f"- not on this machine: {s}" for s in skipped]
    return "\n".join(lines)


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    report_mod = __import__("lint-report")  # the same token, Matrix room and sender as the weekly lint report
    items = [f.removeprefix("review_due: ") for f in report_mod.findings() if f.startswith("review_due: ")]
    targets, skipped = [], []
    for item in sorted(items, key=lambda i: (m := re.search(r"since (\S+)", i)) and m.group(1) or ""):  # oldest first
        (targets.append(p) if (p := locate(item)) else skipped.append(item.split(": ", 1)[0]))
    day = date.today()
    if "--dry-run" in sys.argv:
        print("targets:", *targets, "not here:", *skipped, "", prompt(targets[:PER_PASS], day), sep="\n")
        return
    if not targets:
        return
    try:
        res = loop(targets, run_agent, day)
    except Exception as e:  # fail loudly
        report_mod.send(f"Memo check (review_due) failed: {type(e).__name__}: {e}")
        raise
    report_mod.send(report(res, skipped))
    print(report(res, skipped))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import tempfile
        day = date(2026, 10, 25)
        assert today_nl(day) == "25 okt 2026"
        assert checked_on("IP 10.0.0.1 (gecontroleerd 25 okt 2026)", day)
        assert not checked_on("IP 10.0.0.1 (stand 25 okt 2026, niet opnieuw gecontroleerd)", day), "carried over is not checked"
        assert not checked_on("IP 10.0.0.1 (gecontroleerd 1 aug 2026), edited 25 okt 2026", day), "another date in the text does not count"
        with tempfile.TemporaryDirectory() as t:
            ms = [Path(t) / f"m{i}.md" for i in range(5)]
            for m in ms:
                m.write_text("IP 10.0.0.1 (gecontroleerd 1 aug 2026)")

            def honest(batch, d):  # checks m0..m2, m3 is blocked, m4 it claims but does not mark
                for m in batch:
                    if m.name in ("m0.md", "m1.md", "m2.md"):
                        m.write_text(f"IP 10.0.0.1 (gecontroleerd {today_nl(d)})")
                return [{"memo": "m3.md", "reason": "host off"}] if ms[3] in batch else []
            r = loop(ms, honest, day)
            assert r["stop"] == "no progress" and r["done"] == ms[:3] and list(r["blocked"]) == [ms[3]] and r["left"] == [ms[4]], r
            calls = []

            def slow(batch, d):  # one memo per pass, like a pass that timed out after finishing one: still progress
                calls.append(1)
                batch[0].write_text(f"IP 10.0.0.1 (gecontroleerd {today_nl(d)})")
                return []
            fresh = [Path(t) / f"s{i}.md" for i in range(6)]
            for m in fresh:
                m.write_text("x")
            r = loop(fresh, slow, day)
            assert r["stop"] == "all handled" and len(calls) == 6, (r, calls)  # never cut off while progressing
            r = loop(ms[:3], honest, day)
            assert r["stop"] == "all handled", r
        assert locate("nonexistent_memo.md: never checked (no date) (1 addresses/versions)") is None
        print("ok")
    else:
        main()
