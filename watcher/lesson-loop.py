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
public repo). At most MAX_PER_RUN lessons per run.
5. probation: PROBATION days after a lesson was applied, a pass checks the corrections since then. No correction of the
   same kind: the lesson stays ("proef" is dropped from its mark). It came back: a research pass looks up how others
   solve it (zendriver first, WebSearch as fallback, at most RESEARCH_PAGES pages) and the line is replaced by a better
   one for a second probation. Failed twice: the line is removed and the owner gets one Matrix message with a proposal
   for a hook or check, the only thing that needs him.
Projects whose memory is not on this machine (guests, e.g. a350) take part too: their lessons may only go into their
own memos, which are read and written through the server (its history, secret check and house rules apply), and the
evidence is what their session logs on the server show (the "Asked:" lines, labelled by the same Haiku counter).
6. aliases from real misses: the server keeps (search that found nothing, note the same device opened next) pairs; a
   pass checks each pair ("does this note answer that search?") and a good one adds the search's words to the note's
   frontmatter aliases:, which the server weighs in its search title. Memos of any project (a guest's through the
   server), top-level homelab docs; never a guest's mirrored docs (their next sync would undo it), never code-like
   searches. Measured on the 45-question set first: generated aliases gave nothing, so only real misses become one.
Silent when it works (Uptime Kuma heartbeat); a failure goes to Matrix and turns the heartbeat red.

  --dry-run        show what pass 1 would get, call nothing, change nothing
  --research ID    run only the research pass for one applied lesson and print it (changes nothing)
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
LOCAL = {"homelab": "memory", "anpitek": "anpitek-memory", "trade": "trade-memory"}  # projects with memory here
SKILLS = HOME / ".claude/skills"
REPO = Path(__file__).resolve().parent.parent  # plugin skills live here: changing them is a public commit, never
SERVER = os.environ["KNOWLEDGE_URL"]  # from local.env via the unit; missing = fail loudly
TOKEN = "KNOWLEDGE_TOKEN_VM_SESSIONS"  # vault name: a write token for all projects (the queue routes need one)
PUSH = "UPTIME_KUMA_PUSH_LESSON_LOOP"  # vault name of the heartbeat token (Uptime Kuma "Lesson loop")
MODEL = "sonnet"  # judging rules is the hard part and the volume is tiny (<= MAX_PER_RUN a week); counting uses haiku
MAX_PER_RUN = 10
EVIDENCE = 80  # most recent corrections shown to pass 1
PROBATION = 28  # days
RESEARCH_PAGES = 5
ZENDRIVER = os.environ.get("KNOWLEDGE_ZENDRIVER_MCP", "")
# The one comparison the loop was built to answer: corrections per 100 follow-up prompts in the 4 weeks before it
# started (baseline 25 Oct 2026: ~21) against the first 4 full weeks after. Sent once, when those weeks are complete.
BEFORE, AFTER = ("2026-W40", "2026-W41", "2026-W42", "2026-W43"), ("2026-W44", "2026-W45", "2026-W46", "2026-W47")
COMPARE_FROM = date(2026, 11, 23)  # Monday after 2026-W47
MEANINGFUL = 5.0  # points per 100: the week-to-week noise in the baseline was 15-47
ALIAS_MAX = 8  # per note: a few real names, not a keyword dump
ALIAS_QUERY = re.compile(r"^[^\W_][\w' .-]{2,59}$")  # plain words; no regex, paths or code names
ALIAS_PASS = """Each item is a search an AI agent made in a knowledge base that found nothing, and the note the agent opened
right after. Decide per item whether that note really answers the search. If it does, give an alias: the search's own
words, shortened to the 1-4 words that name the subject (lowercase, Dutch or English as written). If the note does not
answer it, or the search is too vague to name anything, say no."""
SCHEMA_A = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "answers"], "properties": {
        "id": {"type": "integer"}, "answers": {"type": "boolean"}, "alias": {"type": "string"}}}}}}
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
PASS3 = """Each lesson below was added to a memo or skill some weeks ago, to stop a kind of mistake. Below them are the \
messages in which the user corrected an agent since then (Dutch, many typos). For each lesson decide whether a \
correction of the SAME kind came back after the lesson was added (quote it), or not. Only the same mistake counts, not \
any correction on the same subject."""
RESEARCH = """A lesson for AI coding agents did not stop a mistake from coming back. Find out how others prevent this \
kind of mistake: official docs first, then issue trackers and forums. Search with read_page on \
https://html.duckduckgo.com/html/?q=<words> and read the best pages with read_page (max_chars 8000); at most {pages} \
pages in total. If read_page fails, use WebSearch instead. Then give one better line for the memo or skill (max ~200 \
characters, concrete, in the language of the old line, no leading "- ", no date), the source URLs, and a proposal for a \
hook or automatic check that would enforce it (one or two sentences)."""
SCHEMA3 = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "recurred", "reason"], "properties": {
        "id": {"type": "string"}, "recurred": {"type": "boolean"}, "reason": {"type": "string"}}}}}}
SCHEMA_R = {"type": "object", "required": ["line", "sources", "hook"], "properties": {
    "line": {"type": "string"}, "sources": {"type": "array", "items": {"type": "string"}}, "hook": {"type": "string"}}}
GENERAL = """
These lessons come from one project. A lesson about that project goes into its own memos. When a lesson is about how
any agent in any project should work (not about this project's code, tools or domain), answer "general" instead: it
is then proposed to the server's owner as a shared rule, never applied by you."""
SCHEMA1 = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "verdict", "reason"], "properties": {
        "id": {"type": "string"}, "verdict": {"enum": ["apply", "reject", "general"]}, "reason": {"type": "string"},
        "target": {"type": "string"}}}}}}
SCHEMA2 = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "verdict", "reason"], "properties": {
        "id": {"type": "string"}, "verdict": {"enum": ["add", "skip"]}, "reason": {"type": "string"},
        "line": {"type": "string"}}}}}}


def today_nl(d: date) -> str:
    return f"{d.day} {NL_MONTHS[d.month - 1]} {d.year}"


def mcp(tool: str, args: dict) -> str:
    """One knowledge tool call as the VM token (a session per call: a handful of calls a week)."""
    token = subprocess.run(["secret", TOKEN], capture_output=True, text=True, check=True).stdout.strip()
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def post(body, sid=None):
        req = urllib.request.Request(f"{SERVER}/mcp", json.dumps(body).encode(), dict(h, **({"mcp-session-id": sid} if sid else {})))
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.headers.get("mcp-session-id"), r.read().decode()
    sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "lesson-loop", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _, out = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}, sid)
    d = json.loads(([x[5:] for x in out.splitlines() if x.startswith("data:")] or [out])[-1])
    res = d.get("result") or {}
    if "error" in d or res.get("isError"):
        raise RuntimeError(f"{tool} failed: {(res.get('content') or [{}])[0].get('text') or d.get('error')}")
    return res["content"][0]["text"]


MCP = mcp  # tests swap in a fake


class Remote:
    """A file of a project that lives on the server only (a guest's memo), read and written through the knowledge
    tools, so the server's history, secret check and house-rule hints apply. Offers the Path methods the loop uses."""
    def __init__(self, rel: str):
        self.rel, self.name, self.version = rel, rel.rsplit("/", 1)[-1], None

    def read_text(self, encoding: str = "utf-8", errors: str | None = None) -> str:
        head, _, body = MCP("knowledge_read", {"path": self.rel, "limit": 5000}).partition("\n")
        self.version = re.search(r"version (\w+)", head).group(1)
        return "\n".join(x.split("\t", 1)[1] for x in body.split("\n") if re.match(r"^\d+\t", x)) + "\n"

    def write_text(self, text: str, encoding: str = "utf-8"):
        if self.version is None:
            self.read_text()
        MCP("knowledge_write", {"path": self.rel, "content": text, "base_version": self.version})
        self.version = None


def project_roots() -> dict[str, str]:
    """{project: memory root} from the server (knowledge_projects: "a350: docs a350/, memory a350-memory/, write")."""
    return dict(re.findall(r"^([\w-]+): docs [^,]*, memory ([\w-]+)/", MCP("knowledge_projects", {}), re.M))


def target_path(target: str, allowed: set[str] | None = None):
    """The file a target names (a Path here, a Remote for a guest's memo), or None when the loop may not change it.
    allowed: the roots this lesson may write to."""
    root, _, rest = target.strip().partition("/")
    if allowed is not None and root not in allowed:
        return None
    if root not in MEMORY and root != "skills":
        if not root.endswith("-memory") or not re.fullmatch(r"[A-Za-z0-9_.-]+\.md", rest) or rest == "MEMORY.md":
            return None
        r = Remote(f"{root}/{rest}")
        try:
            r.read_text()
            return r
        except Exception:
            return None
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


def guest_targets(root: str) -> str:
    text = Remote(f"{root}/MEMORY.md").read_text()
    return "\n".join(f"{root}/{m.group(1)}: {m.group(2)[:150]}"
                     for m in re.finditer(r"^- \[[^\]]*\]\(([^)]+\.md)\)\s*[—-]?\s*(.*)$", text, re.M))


def guest_evidence(root: str, since: str) -> tuple[str, float]:
    """Corrections in a guest project's session logs on the server since a date: the "Asked:" lines (the first 8
    prompts of each session, 200 characters each: thinner than the transcripts here), labelled by the Haiku counter."""
    asked = []
    for month in sorted({since[:7], date.today().isoformat()[:7]}):
        try:
            text = Remote(f"{root}/sessions/{month}.md").read_text()
        except Exception:
            continue
        day = ""
        for line in text.splitlines():
            if (m := re.match(r"^## (\d{4}-\d\d-\d\d)", line)):
                day = m.group(1)
            elif line.startswith("- Asked: ") and day >= since:
                asked.append({"ts": day, "prompt": line[9:], "prev": ""})
    if not asked:
        return "(none)", 0.0
    asked = asked[-corrections.BATCH:]
    hits, cost = corrections.classify(asked)
    return "\n".join(f"- {asked[i]['ts']} USER: {asked[i]['prompt'][:200]}" for i in sorted(hits)) or "(none)", cost


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


def bare(prompt: str, system: str, schema: dict, web: bool = False):
    """One claude -p call without settings or CLAUDE.md (~1k tokens overhead instead of ~100k). No tools, or with
    web=True only zendriver's read_page and WebSearch. Returns (structured output, cost)."""
    tools = ["--tools", "WebSearch", "--allowedTools", "WebSearch,mcp__zendriver__read_page", "--max-turns",
             str(2 * RESEARCH_PAGES + 4)] if web else ["--tools", "", "--max-turns", "3"]
    mcp = ["--mcp-config", json.dumps({"mcpServers": {"zendriver": {"type": "http", "url": ZENDRIVER}}})] if web and ZENDRIVER else []
    r = subprocess.run(["claude", "-p", prompt, "--system-prompt", system, "--model", MODEL, "--output-format", "json",
                        *tools, *mcp, "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                        "--json-schema", json.dumps(schema)],
                       capture_output=True, text=True, timeout=1800, cwd=corrections.STATE)
    out = json.loads(r.stdout or "{}")
    if r.returncode or out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude -p failed (exit {r.returncode}): {(out.get('result') or r.stderr)[-300:]}")
    so = out["structured_output"]
    return (so if web else so["verdicts"]), out.get("total_cost_usd") or 0.0


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


def run(lessons: list[dict], ev: str, pass1, pass2, decide, day: date, targets: str | None = None,
        allowed: set[str] | None = None, notify=None) -> dict:
    """The loop with its passes and the queue update injected (tests use fakes). Returns counts per outcome.
    targets/allowed: what these lessons may go into (default: this machine's memories and skills)."""
    done = {"applied": [], "rejected": []}
    targets = targets_text() if targets is None else targets
    allowed = set(MEMORY) | {"skills"} if allowed is None else allowed
    v1, cost = pass1(f"{GENERAL if notify else ''}\nLESSONS\n{lessons_text(lessons)}\n\nCORRECTIONS\n{ev}\n\nTARGETS\n{targets}")
    by_id = {x["id"]: x for x in lessons}
    chosen = []
    for v in v1:
        if v["id"] not in by_id:
            continue
        if v["verdict"] == "general" and notify:  # one project's lesson that would hold for all: the owner decides
            x = by_id[v["id"]]
            decide(x["id"], "escalated", note=f"general, proposed as a shared rule: {v['reason']}"[:300])
            notify(f"Lesson loop: a lesson from project {x['project']} seems to hold for every project. Make it a shared "
                   f"rule or skill? What: {x['what']} | prevent: {x['prevent']} | why general: {v['reason']}")
            done.setdefault("general", []).append(x["id"])
            continue
        if v["verdict"] == "general":
            v = {**v, "verdict": "reject", "reason": f"general (not for one project): {v['reason']}"}
        path = target_path(v.get("target", ""), allowed) if v["verdict"] == "apply" else None
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


def mark(line: str, day: date, lid: str, attempt: int) -> str:
    return f"{line} (les {today_nl(day)}, proef {lid}{f', poging {attempt}' if attempt > 1 else ''})"


def replace_line(path: Path | None, old: str, new: str | None) -> bool:
    """Swap (or with new=None remove) the exact line the loop added; False when it is no longer there."""
    if not path:
        return False
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if old not in lines:
        return False
    if new is None:
        lines.remove(old)
    else:
        lines[lines.index(old)] = new
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def probation(due: list[dict], ev_since, pass3, research, decide, notify, day: date) -> dict:
    """Judge the lessons whose probation ended. ev_since(date) gives the corrections since a date."""
    out = {"kept": [], "retried": [], "escalated": [], "gone": [], "cost": 0.0}
    if not due:
        return out
    since = min(x["changed"] for x in due)
    body = "\n".join(f"[{x['id']}] added {x['changed']} to {x['target']}: {x['line']}" for x in due)
    v3, out["cost"] = pass3(f"LESSONS\n{body}\n\nCORRECTIONS SINCE {since}\n{ev_since(since)}")
    verdicts = {v["id"]: v for v in v3}
    for x in due:
        v, path = verdicts.get(x["id"]), target_path(x["target"])
        if not v:
            continue  # no verdict: try again next week
        if not v["recurred"]:
            kept = re.sub(r", proef \w+(, poging \d)?\)$", ")", x["line"])
            if replace_line(path, x["line"], kept):
                decide(x["id"], "kept", line=kept, note=v["reason"][:300]); out["kept"].append(x["id"])
            else:
                decide(x["id"], "removed", note="line no longer in the target (edited by hand?)"); out["gone"].append(x["id"])
            continue
        if x.get("attempt", 1) == 1:
            r, c = research(x, v["reason"])
            out["cost"] += c
            new = "- " + mark(" ".join(r["line"].split()).removeprefix("- ")[:300], day, x["id"], 2)
            if replace_line(path, x["line"], new):
                decide(x["id"], "applied", line=new, attempt=2, changed=day.isoformat(),
                       note=f"came back: {v['reason']}"[:200] + f" | sources: {' '.join(r['sources'])}"[:250])
                out["retried"].append(x["id"])
            else:
                decide(x["id"], "removed", note="line no longer in the target (edited by hand?)"); out["gone"].append(x["id"])
        else:
            replace_line(path, x["line"], None)
            decide(x["id"], "escalated", note=f"failed twice: {v['reason']}"[:300])
            notify(f"Lesson loop: a lesson failed twice, a hook or check may be needed.\nLesson: {x['what']}\n"
                   f"Last line ({x['target']}, removed): {x['line']}\nCame back: {v['reason']}\n{x.get('note', '')}")
            out["escalated"].append(x["id"])
    return out


def comparison(rows: list, day: date, done: bool) -> str | None:
    """The before/after message, once, when the AFTER weeks are complete; None otherwise."""
    if done or day < COMPARE_FROM:
        return None
    per = {w: (n, k) for w, n, k in rows}
    rate = lambda ws: 100 * sum(per.get(w, (0, 0))[1] for w in ws) / max(sum(per.get(w, (0, 0))[0] for w in ws), 1)
    b, a = rate(BEFORE), rate(AFTER)
    verdict = ("better" if b - a >= MEANINGFUL else "worse" if a - b >= MEANINGFUL else "no measurable difference")
    return (f"Lesson loop, 4 weeks in: corrections per 100 follow-up prompts {b:.1f} before ({BEFORE[0]}-{BEFORE[-1]}) "
            f"vs {a:.1f} after ({AFTER[0]}-{AFTER[-1]}): {verdict} (threshold {MEANINGFUL:g} points). Other changes in "
            f"the same weeks count too; keep the loop when better, look at the lessons when not.")


def alias_file(rel: str):
    """Where an alias may be written for this note: a memo (here or a guest's on the server) or a top-level homelab
    doc; None for anything else (a guest's mirrored docs would lose it at their next sync)."""
    root, _, rest = rel.partition("/")
    if "/" in rest or not rest.endswith(".md") or rest == "MEMORY.md":
        return None
    if root == "homelab":
        p = HOME / "homelab" / rest
        return p if p.is_file() else None
    if root in MEMORY:
        p = MEMORY[root] / rest
        return p if p.is_file() else None
    return Remote(rel) if root.endswith("-memory") else None


def add_alias(text: str, alias: str) -> str | None:
    """The text with the alias in its frontmatter aliases: line (made after description when missing); None when
    there is no frontmatter, the alias is there already or the line is full."""
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    fm = m.group(1)
    if (line := re.search(r"^aliases:[ \t]*(.*)$", fm, re.M)):
        have = [a.strip() for a in line.group(1).strip("[]").split(",") if a.strip()]
        if alias in have or len(have) >= ALIAS_MAX:
            return None
        fm = fm[:line.start()] + "aliases: " + ", ".join(have + [alias]) + fm[line.end():]
    else:
        d = re.search(r"^description:.*$", fm, re.M)
        fm = fm[:d.end()] + f"\naliases: {alias}" + fm[d.end():] if d else fm + f"\naliases: {alias}"
    return text[:m.start(1)] + fm + text[m.end(1):]


def alias_loop(pairs: list[dict], handled: set, check, today: date) -> tuple[list[str], float]:
    """Check the new (search, note) pairs and add the good aliases. Returns (added "note: alias", cost)."""
    todo, seen = [], set()
    for x in pairs:
        key = f"{x['query'].lower()}|{x['path']}"
        if key in handled or key in seen:
            continue
        seen.add(key)
        if ALIAS_QUERY.match(x["query"]) and re.search(r"[^\W\d_]", x["query"]) and alias_file(x["path"]) is not None:
            todo.append(x)
        else:
            handled.add(key)
    todo = todo[:40]
    if not todo:
        return [], 0.0
    files = {x["path"]: alias_file(x["path"]) for x in todo}
    body = "\n\n".join(f"[{i}] SEARCH: {x['query']}\nNOTE {x['path']}:\n{files[x['path']].read_text()[:700]}" for i, x in enumerate(todo))
    verdicts, cost = check(body)
    added = []
    for v in verdicts:
        if not isinstance(v.get("id"), int) or not 0 <= v["id"] < len(todo):
            continue
        x = todo[v["id"]]
        alias = " ".join(str(v.get("alias", "")).lower().replace(",", " ").split())
        words = set(x["query"].lower().split())
        if v.get("answers") and alias and len(alias) <= 40 and set(alias.split()) <= words:  # only the searcher's own words
            f = files[x["path"]]
            if (new := add_alias(f.read_text(), alias)) is not None:
                f.write_text(new)
                added.append(f"{x['path']}: {alias}")
    handled.update(f"{x['query'].lower()}|{x['path']}" for x in todo)
    return added, cost


def research_pass(x: dict, why: str):
    return bare(f"LESSON: {x['what']} | prevent: {x['prevent']}\nCURRENT LINE: {x['line']}\nCAME BACK: {why}",
                RESEARCH.format(pages=RESEARCH_PAGES), SCHEMA_R, web=True)


def main() -> None:
    queue = api("GET", "/lessons")
    lessons = [x for x in queue if x["status"] == "new"][:MAX_PER_RUN]
    if "--research" in sys.argv:
        x = next(x for x in queue if x["id"] == sys.argv[sys.argv.index("--research") + 1])
        print(json.dumps(research_pass(x, "(test run: assume it came back)"), indent=1, ensure_ascii=False))
        return
    if "--dry-run" in sys.argv:
        print(lessons_text(lessons), "", targets_text()[:3000], sep="\n")
        return
    report_mod = __import__("lint-report")  # the same Matrix room and sender as the weekly lint report
    try:
        items, labels, rows, cost = corrections.update()
        roots = project_roots()
        decide = lambda lid, status, **kw: api("POST", "/lessons", {"id": lid, "status": status, **kw})
        res = {"applied": [], "rejected": [], "cost": 0.0}
        pro = {"kept": [], "retried": [], "escalated": [], "gone": [], "cost": 0.0}
        cutoff = (date.today() - timedelta(days=PROBATION)).isoformat()
        due_all = [x for x in queue if x["status"] == "applied" and x.get("changed", "9") <= cutoff]
        for proj in sorted({x["project"] for x in lessons} | {x["project"] for x in due_all}):
            mine, due = [x for x in lessons if x["project"] == proj], [x for x in due_all if x["project"] == proj]
            root = roots.get(proj)
            if not root:
                for x in mine:
                    decide(x["id"], "rejected", note=f"project {proj} is not on the server")
                    res["rejected"].append(x["id"])
                continue
            if proj in LOCAL:
                ev_since = lambda since: (evidence(items, labels, since), 0.0)
                targets, allowed = None, None
            else:  # a guest: only its own memos, its own session logs as evidence
                ev_since = lambda since, root=root: guest_evidence(root, since)
                targets, allowed = guest_targets(root), {root}
            if mine:
                since = (date.fromisoformat(min(x["at"][:10] for x in mine)) - timedelta(days=1)).isoformat()
                ev, c = ev_since(since)
                r = run(mine, ev, lambda p: bare(p, PASS1, SCHEMA1), lambda p: bare(p, PASS2, SCHEMA2), decide,
                        date.today(), targets, allowed, None if proj in LOCAL else report_mod.send)
                res["applied"] += r["applied"]; res["rejected"] += r["rejected"]; res["cost"] += r["cost"] + c
                res.setdefault("general", []).extend(r.get("general", []))
            if due:
                spent = []
                p = probation(due, lambda since: (lambda e: spent.append(e[1]) or e[0])(ev_since(since)),
                              lambda q: bare(q, PASS3, SCHEMA3), research_pass, decide, report_mod.send, date.today())
                for k in ("kept", "retried", "escalated", "gone"):
                    pro[k] += p[k]
                pro["cost"] += p["cost"] + sum(spent)
        hf = corrections.STATE / "gap_aliases.json"
        handled = set(json.loads(hf.read_text())) if hf.exists() else set()
        aliases, acost = alias_loop(api("GET", "/gap-answers"), handled,
                                    lambda b: bare(b, ALIAS_PASS, SCHEMA_A), date.today())
        hf.write_text(json.dumps(sorted(handled)))
        res["cost"] += acost
        flag = corrections.STATE / "compared"
        if (msg := comparison(rows, date.today(), flag.exists())):
            report_mod.send(msg)
            flag.write_text(msg)
        last = rows[-1] if rows else ("-", 0, 0)
        summary = (f"{len(res['applied'])} applied, {len(res['rejected'])} rejected, {len(res.get('general', []))} proposed "
                   f"as shared, {len(pro['kept'])} kept, "
                   f"{len(pro['retried'])} retried, {len(pro['escalated'])} escalated, {len(aliases)} aliases; corrections {last[0]}: "
                   f"{last[2]}/{last[1]}; cost ${cost + res['cost'] + pro['cost']:.3f}")
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
        # Probation: kept drops "proef", a comeback gets a researched second line, a second failure escalates.
        later = date(2026, 11, 22)
        a1 = {"id": "a1", "what": "w", "prevent": "p", "target": "memory/feedback_werk.md", "changed": "2026-10-25",
              "line": "- check eerst de exitcode (les 25 okt 2026, proef a1)", "attempt": 1}
        p3 = lambda keep: (lambda p: ([{"id": "a1", "recurred": not keep, "reason": "same again"}], 0.01))
        res_fn = lambda x, why: ({"line": "lees altijd het foutbestand", "sources": ["https://ex.org"], "hook": "h"}, 0.1)
        calls, sent = [], []
        dec = lambda lid, st, **kw: calls.append((lid, st, kw))
        r = probation([a1], lambda s: "", p3(False), res_fn, dec, sent.append, later)
        assert r["retried"] == ["a1"] and "- lees altijd het foutbestand (les 22 nov 2026, proef a1, poging 2)" in memo.read_text()
        a1b = {**a1, "line": calls[-1][2]["line"], "attempt": 2, "changed": "2026-11-22"}
        r = probation([a1b], lambda s: "", p3(False), res_fn, dec, sent.append, date(2026, 12, 20))
        assert r["escalated"] == ["a1"] and sent and "lees altijd" not in memo.read_text() and "regel een" in memo.read_text()
        insert(memo, "x2 (les 25 okt 2026, proef a1)")
        r = probation([{**a1, "line": "- x2 (les 25 okt 2026, proef a1)"}], lambda s: "", p3(True), res_fn, dec, sent.append, later)
        assert r["kept"] == ["a1"] and "- x2 (les 25 okt 2026)\n" in memo.read_text(), memo.read_text()
        r = probation([{**a1, "line": "- gone"}], lambda s: "", p3(True), res_fn, dec, sent.append, later)
        assert r["gone"] == ["a1"] and calls[-1][1] == "removed"
        assert probation([], None, None, None, None, None, later)["cost"] == 0.0  # nothing due: no call
        # A guest project: its memo lives on the server (fake MCP), it may only target its own root, and a lesson
        # that holds for every project is proposed, not applied.
        global MCP
        store = {"g-memory/MEMORY.md": "- [Flow](project_flow.md) — how flows run\n",
                 "g-memory/project_flow.md": "---\nname: f\n---\n## Stand\n- one\n\n## Keuzes\n- **k** (1 okt 2026): r\n"}

        def fake_mcp(tool, args):
            if tool == "knowledge_read":
                if args["path"] not in store:
                    raise RuntimeError("missing")
                lines = store[args["path"]].rstrip("\n").split("\n")
                return f"{args['path']} (version v1, {len(lines)} lines)\n" + "\n".join(f"{i}\t{x}" for i, x in enumerate(lines, 1))
            assert tool == "knowledge_write" and args["base_version"] == "v1", args
            store[args["path"]] = args["content"]
            return "wrote"
        MCP = fake_mcp
        assert guest_targets("g-memory") == "g-memory/project_flow.md: how flows run"
        assert target_path("memory/feedback_werk.md", {"g-memory"}) is None  # a guest never writes outside its root
        assert target_path("g-memory/nope.md", {"g-memory"}) is None
        gl = [{"id": i, "at": "2026-10-24T10:00", "project": "g", "what": f"w{i}", "why": "y", "prevent": "p"} for i in ("g1", "g2", "g3")]
        gp1 = lambda p: ([{"id": "g1", "verdict": "apply", "reason": "ok", "target": "g-memory/project_flow.md"},
                          {"id": "g2", "verdict": "general", "reason": "any agent"},
                          {"id": "g3", "verdict": "apply", "reason": "ok", "target": "memory/feedback_werk.md"}], 0.0)
        gp2 = lambda p: ([{"id": "g1", "verdict": "add", "reason": "new", "line": "wacht 15 s na boarding"}], 0.0)
        calls, sent = [], []
        r = run(gl, "(none)", gp1, gp2, lambda lid, st, **kw: calls.append((lid, st)), day, guest_targets("g-memory"),
                {"g-memory"}, sent.append)
        assert r["applied"] == ["g1"] and r["general"] == ["g2"] and r["rejected"] == ["g3"], r
        assert "- one\n- wacht 15 s na boarding (les 25 okt 2026, proef g1)\n\n## Keuzes" in store["g-memory/project_flow.md"]
        assert ("g2", "escalated") in calls and sent and "every project" in sent[0]
        # Aliases from real misses: plain searches only, own words only, guest memos via the server, not their docs.
        assert add_alias("---\nname: x\ndescription: d\n---\nb", "dev vm") == "---\nname: x\ndescription: d\naliases: dev vm\n---\nb"
        assert add_alias("---\nname: x\naliases: a, b\n---\n", "c") == "---\nname: x\naliases: a, b, c\n---\n"
        assert add_alias("---\naliases: a\n---\n", "a") is None and add_alias("no front", "a") is None
        assert add_alias("---\naliases: " + ", ".join(map(str, range(ALIAS_MAX))) + "\n---\n", "z") is None
        assert isinstance(alias_file("g-memory/project_flow.md"), Remote) and alias_file("g/docs/x.md") is None
        assert alias_file("memory/feedback_werk.md") == memo and alias_file("memory/MEMORY.md") is None
        pairs = [{"query": "Flow Timing", "path": "g-memory/project_flow.md"}, {"query": "PASSENGER_MISMATCH", "path": "g-memory/project_flow.md"},
                 {"query": "werk afspraken", "path": "memory/feedback_werk.md"}, {"query": "x|y.*", "path": "memory/feedback_werk.md"}]
        handled = set()
        got, _ = alias_loop(pairs, handled, lambda b: ([{"id": 0, "answers": True, "alias": "flow timing"},
                                                         {"id": 1, "answers": True, "alias": "invented words"}], 0.0), day)
        assert got == ["g-memory/project_flow.md: flow timing"], got
        assert "aliases: flow timing" in store["g-memory/project_flow.md"] and "aliases" not in memo.read_text()
        assert len(handled) == 4 and alias_loop(pairs, handled, None, day) == ([], 0.0)  # every pair once
        rows = [("2026-W40", 100, 25), ("2026-W43", 100, 17), ("2026-W44", 100, 10), ("2026-W47", 100, 14)]
        assert comparison(rows, date(2026, 11, 22), False) is None and comparison(rows, date(2026, 11, 29), True) is None
        msg = comparison(rows, date(2026, 11, 29), False)
        assert "21.0 before" in msg and "12.0 after" in msg and ": better" in msg, msg
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
