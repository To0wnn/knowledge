#!/usr/bin/env python3
"""Weekly lesson loop: turn the lessons agents queued (knowledge_lesson) into small, checked additions on probation.

Research on agent memory: lessons an agent writes about itself make it worse unless something outside the agent checks
them, and memories that only grow get copied blindly. So nothing goes straight into the memos:
1. refresh the correction count (corrections.py), which is also the evidence: what the user actually corrected;
2. pass 1 (one bare `claude -p`, no tools): per new lesson, is it backed by a correction or a concrete mistake, is it
   about the task and concrete, and which memo or skill does it belong in;
3. pass 2: with the text of each chosen target, is it already there or does it clash with a decision ("## Keuzes"),
   and the one line to add;
4. this script, not the agent, inserts that line, marked "(les <date>, proef <id>)", and records target and line in the
   queue, so a later run can find and remove it again.
Never touched: the shared rules, CLAUDE.md, hooks, the index files, archives, and skills that ship with the plugin (the
public repo). At most MAX_PER_RUN lessons per run. Probation (does the same correction come back within 4 weeks?) is
a later step. Silent when it works (Uptime Kuma heartbeat); a failure goes to Matrix and turns the heartbeat red.

  --dry-run   show what pass 1 would get, call nothing, change nothing
  --selftest  check target validation and insertion with fake passes
Standard library only.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import corrections  # noqa: E402  (next to this file)
import kuma  # noqa: E402

HOME = Path.home()
PREFIX = os.environ.get("KNOWLEDGE_VM_CC_PREFIX", "-home-" + HOME.name)
MEMORY = {  # root name on the server -> folder on this machine
    "memory": HOME / f".claude/projects/{PREFIX}-homelab/memory",
    "anpitek-memory": HOME / f".claude/projects/{PREFIX}-anpitek/memory",
    "trade-memory": HOME / f".claude/projects/{PREFIX}-trade-bot/memory",
}
SKILLS = HOME / ".claude/skills"
REPO = Path(__file__).resolve().parent.parent  # plugin skills live here: changing them is a public commit, never
SERVER = os.environ.get("KNOWLEDGE_URL", "https://knowledge.to0wnn.com")
TOKEN = "KNOWLEDGE_TOKEN_VM_SESSIONS"  # vault name: a write token for all projects (the queue routes need one)
PUSH = "UPTIME_KUMA_PUSH_LESSON_LOOP"  # vault name of the heartbeat token (Uptime Kuma "Lesson loop")
MODEL = "sonnet"  # judging rules is the hard part and the volume is tiny (<= MAX_PER_RUN a week); counting uses haiku
MAX_PER_RUN = 10
EVIDENCE = 80  # most recent corrections shown to pass 1
NL_MONTHS = "jan feb mrt apr mei jun jul aug sep okt nov dec".split()
PASS1 = """You review lessons that AI coding agents recorded after a mistake, for a household's homelab, trading bot and \
small company. Each lesson says what happened, why, and what would have prevented it. Below the lessons are recent \
messages in which the user corrected an agent (Dutch, many typos), and the list of memos and skills a lesson can go into.
For each lesson decide:
- apply: it is backed by one of the corrections or describes a concrete, checkable mistake; it is about the task (a \
command, a check, a pitfall, a fact), not about the agent's attitude; and it is concrete enough that a future agent can \
act on it. Pick the ONE target where a future agent working on that subject will read it: a memo by its path as listed \
(e.g. "memory/feedback_werkwijze.md") or a skill as "skills/<name>".
- reject: vague ("be more careful"), not backed, already a general rule, or a one-off that will not come back. Say why.
Never pick a target that is not in the list."""
PASS2 = """For each lesson you get the full text of the memo or skill it should go into. Decide:
- add: give the ONE line to add, short (max ~200 characters), concrete, in the language of the target (memos Dutch, \
skills English), without a leading "- " and without a date.
- skip: the target already says this, or it contradicts a decision under "## Keuzes" (a lesson must not undo a \
decision). Say why."""
SCHEMA1 = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "verdict", "reason"], "properties": {
        "id": {"type": "string"}, "verdict": {"enum": ["apply", "reject"]}, "reason": {"type": "string"},
        "target": {"type": "string"}}}}}}
SCHEMA2 = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "verdict", "reason"], "properties": {
        "id": {"type": "string"}, "verdict": {"enum": ["add", "skip"]}, "reason": {"type": "string"},
        "line": {"type": "string"}}}}}}


def today_nl(d: date) -> str:
    return f"{d.day} {NL_MONTHS[d.month - 1]} {d.year}"


def target_path(target: str) -> Path | None:
    """The file a target names, or None when it is not one the loop may change."""
    root, _, rest = target.strip().partition("/")
    if root == "skills":
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", rest):
            return None
        p = SKILLS / rest / "SKILL.md"
        return p if p.is_file() and REPO not in p.resolve().parents else None
    base = MEMORY.get(root)
    if not base or not re.fullmatch(r"[A-Za-z0-9_.-]+\.md", rest) or rest in ("MEMORY.md",):
        return None
    p = base / rest
    return p if p.is_file() else None


def targets_text() -> str:
    """What pass 1 may choose from: the memory indexes (path and hook per memo) and the skills with their description."""
    out = []
    for root, base in MEMORY.items():
        idx = base / "MEMORY.md"
        if idx.is_file():
            for m in re.finditer(r"^- \[[^\]]*\]\(([^)]+\.md)\)\s*[—-]?\s*(.*)$", idx.read_text(encoding="utf-8"), re.M):
                out.append(f"{root}/{m.group(1)}: {m.group(2)[:150]}")
    for s in sorted(SKILLS.glob("*/SKILL.md")):
        if REPO in s.resolve().parents:
            continue
        d = re.search(r"^description:\s*(.+)$", s.read_text(encoding="utf-8"), re.M)
        out.append(f"skills/{s.parent.name}: {(d.group(1) if d else '')[:150]}")
    return "\n".join(out)


def insert(path: Path, line: str) -> str:
    """Add one bullet: in a memo at the end of "## Stand" (before "## Keuzes"), in a skill at the end. Returns it."""
    text = path.read_text(encoding="utf-8")
    bullet = f"- {line}"
    m = re.search(r"\n## Keuzes\b", text)
    if m and path.name != "SKILL.md":
        head = text[:m.start()].rstrip("\n")
        text = f"{head}\n{bullet}\n{text[m.start():]}"
    else:
        text = text.rstrip("\n") + f"\n{bullet}\n"
    path.write_text(text, encoding="utf-8")
    return bullet


def bare(prompt: str, system: str, schema: dict) -> tuple[list[dict], float]:
    """One claude -p call without tools, MCP, settings or CLAUDE.md (~1k tokens overhead instead of ~100k)."""
    r = subprocess.run(["claude", "-p", prompt, "--system-prompt", system, "--model", MODEL, "--output-format", "json",
                        "--max-turns", "3", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                        "--no-session-persistence", "--json-schema", json.dumps(schema)],
                       capture_output=True, text=True, timeout=900, cwd=corrections.STATE)
    out = json.loads(r.stdout or "{}")
    if r.returncode or out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude -p failed (exit {r.returncode}): {(out.get('result') or r.stderr)[-300:]}")
    return out["structured_output"]["verdicts"], out.get("total_cost_usd") or 0.0


def api(method: str, path: str, body: dict | None = None):
    token = subprocess.run(["secret", TOKEN], capture_output=True, text=True, check=True).stdout.strip()
    req = urllib.request.Request(f"{SERVER}{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def lessons_text(lessons: list[dict]) -> str:
    return "\n".join(f"[{x['id']}] ({x['at'][:10]}, {x['project']}) what: {x['what']} | why: {x['why']} | "
                     f"prevent: {x['prevent']}" for x in lessons)


def evidence(items: list[dict], labels: dict, since: str) -> str:
    hits = [p for p in items if p["ts"] >= since and labels.get(corrections.key(p))][-EVIDENCE:]
    return "\n".join(f"- {p['ts']} [agent: ...{p['prev'][-120:]}] USER: {p['prompt'][:200]}" for p in hits) or "(none)"


def run(lessons: list[dict], ev: str, pass1, pass2, decide, day: date) -> dict:
    """The loop with its passes and the queue update injected (tests use fakes). Returns counts per outcome."""
    done = {"applied": [], "rejected": []}
    v1, cost = pass1(f"LESSONS\n{lessons_text(lessons)}\n\nCORRECTIONS\n{ev}\n\nTARGETS\n{targets_text()}")
    by_id = {x["id"]: x for x in lessons}
    chosen = []
    for v in v1:
        if v["id"] not in by_id:
            continue
        path = target_path(v.get("target", "")) if v["verdict"] == "apply" else None
        if v["verdict"] == "apply" and not path:
            v = {**v, "verdict": "reject", "reason": f"target not allowed or missing: {v.get('target')}"}
        if v["verdict"] == "reject":
            decide(v["id"], "rejected", note=v["reason"][:300]); done["rejected"].append(v["id"])
        else:
            chosen.append((by_id[v["id"]], v["target"], path))
    if chosen:
        body = "\n\n".join(f"LESSON [{x['id']}] what: {x['what']} | prevent: {x['prevent']}\nTARGET {t}\n"
                           f"{p.read_text(encoding='utf-8')[:12000]}" for x, t, p in chosen)
        v2, c2 = pass2(body)
        cost += c2
        verdicts = {v["id"]: v for v in v2}
        for x, t, p in chosen:
            v = verdicts.get(x["id"]) or {"verdict": "skip", "reason": "no verdict from pass 2"}
            line = " ".join(str(v.get("line", "")).split()).removeprefix("- ")[:300]
            if v["verdict"] == "add" and line:
                bullet = insert(p, f"{line} (les {today_nl(day)}, proef {x['id']})")
                decide(x["id"], "applied", target=t, line=bullet, attempt=1, changed=day.isoformat())
                done["applied"].append(x["id"])
            else:
                decide(x["id"], "rejected", note=f"{t}: {v['reason']}"[:300]); done["rejected"].append(x["id"])
    done["cost"] = cost
    return done


def main() -> None:
    lessons = [x for x in api("GET", "/lessons") if x["status"] == "new"][:MAX_PER_RUN]
    if "--dry-run" in sys.argv:
        print(lessons_text(lessons), "", targets_text()[:3000], sep="\n")
        return
    report_mod = __import__("lint-report")  # the same Matrix room and sender as the weekly lint report
    try:
        items, labels, rows, cost = corrections.update()
        res = {"applied": [], "rejected": [], "cost": 0.0}
        if lessons:
            since = (date.fromisoformat(min(x["at"][:10] for x in lessons)) - timedelta(days=1)).isoformat()
            res = run(lessons, evidence(items, labels, since),
                      lambda p: bare(p, PASS1, SCHEMA1), lambda p: bare(p, PASS2, SCHEMA2),
                      lambda lid, status, **kw: api("POST", "/lessons", {"id": lid, "status": status, **kw}), date.today())
        last = rows[-1] if rows else ("-", 0, 0)
        summary = (f"{len(res['applied'])} applied, {len(res['rejected'])} rejected; corrections {last[0]}: {last[2]}/{last[1]}"
                   f"; cost ${cost + res['cost']:.3f}")
        print(summary)
        kuma.push(PUSH, msg=summary)
    except Exception as e:  # fail loudly
        report_mod.send(f"Lesson loop failed: {type(e).__name__}: {e}")
        kuma.push(PUSH, up=False, msg=f"{type(e).__name__}: {e}")
        raise


def selftest() -> None:
    import tempfile
    global SKILLS, MEMORY
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        MEMORY = {"memory": t / "mem"}
        SKILLS = t / "skills"
        (t / "mem").mkdir(); (SKILLS / "own").mkdir(parents=True)
        (t / "mem/MEMORY.md").write_text("# Index\n- [Werkwijze](feedback_werk.md) — hoe we werken\n")
        memo = t / "mem/feedback_werk.md"
        memo.write_text("---\nname: x\n---\n\n## Stand\n\n- regel een\n\n## Keuzes\n- **besluit** (1 okt 2026): reden\n")
        (SKILLS / "own/SKILL.md").write_text("---\nname: own\ndescription: my skill\n---\n# Own\n")
        (SKILLS / "plug").symlink_to(REPO / "plugin/skills/verify-done")  # ships with the plugin: never a target
        assert target_path("memory/feedback_werk.md") == memo
        for bad in ("memory/MEMORY.md", "memory/../x.md", "memory/nope.md", "skills/plug", "skills/../own",
                    "other/x.md", "", "claude/dev_rules.md"):
            assert target_path(bad) is None, bad
        assert target_path("skills/own") == SKILLS / "own/SKILL.md"
        assert "memory/feedback_werk.md: hoe we werken" in targets_text() and "skills/plug" not in targets_text()
        day = date(2026, 10, 25)
        lessons = [{"id": i, "at": "2026-10-24T10:00", "project": "homelab", "what": f"w{i}", "why": "y", "prevent": "p"}
                   for i in ("a1", "b2", "c3", "d4")]
        p1 = lambda p: ([{"id": "a1", "verdict": "apply", "reason": "ok", "target": "memory/feedback_werk.md"},
                         {"id": "b2", "verdict": "reject", "reason": "vague"},
                         {"id": "c3", "verdict": "apply", "reason": "ok", "target": "skills/plug"},
                         {"id": "d4", "verdict": "apply", "reason": "ok", "target": "skills/own"},
                         {"id": "zz", "verdict": "apply", "reason": "invented", "target": "memory/feedback_werk.md"}], 0.01)
        p2 = lambda p: ([{"id": "a1", "verdict": "add", "reason": "new", "line": "- check eerst de exitcode"},
                         {"id": "d4", "verdict": "skip", "reason": "already there"}], 0.02)
        calls = []
        res = run(lessons, "(none)", p1, p2, lambda lid, st, **kw: calls.append((lid, st, kw)), day)
        assert res["applied"] == ["a1"] and sorted(res["rejected"]) == ["b2", "c3", "d4"], res
        text = memo.read_text()
        assert "- regel een\n- check eerst de exitcode (les 25 okt 2026, proef a1)\n\n## Keuzes" in text, text
        assert calls[-1][0] == "d4" and "already there" in calls[-1][2]["note"]
        assert ("a1", "applied") in [(c[0], c[1]) for c in calls] and "zz" not in [c[0] for c in calls]
        assert (SKILLS / "own/SKILL.md").read_text().endswith("# Own\n")  # skipped: unchanged
        insert(SKILLS / "own/SKILL.md", "x")
        assert (SKILLS / "own/SKILL.md").read_text().endswith("# Own\n- x\n")
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
