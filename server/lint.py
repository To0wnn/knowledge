"""Deterministic health checks for the knowledge base (the "lint" step of the LLM-wiki pattern).

Pure functions over {rel_path: text}, so they run on the in-memory index in milliseconds and are easy to test.
The server only reports facts; judging contradictions or rewriting memos is left to the Claude session that
reads the report.
"""
import os
import posixpath
import re
from datetime import date

# The words of the memo layout. Both languages are always read; KNOWLEDGE_LANGUAGE picks what the server writes
# (new memos, refusals, instructions). Sections "## Status"/"## Stand", "## Decisions"/"## Keuzes".
WORDS = {"en": {"now": "Status", "decisions": "Decisions", "open": "open until", "claimed": "claimed", "archive": "archive"},
         "nl": {"now": "Stand", "decisions": "Keuzes", "open": "open tot", "claimed": "bezig", "archive": "archief"}}
LANG = os.environ.get("KNOWLEDGE_LANGUAGE", "en") if os.environ.get("KNOWLEDGE_LANGUAGE") in WORDS else "en"
W = WORDS[LANG]
NOW_H, DEC_H = r"## (?:Status|Stand)", r"## (?:Decisions|Keuzes)"
MONTHS = {"jan": 1, "feb": 2, "mrt": 3, "maa": 3, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6, "jul": 7,
          "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dec": 12}
WIKILINK = re.compile(r"\[\[([a-z0-9][a-z0-9_-]*)\]\]")  # [[slug]]; skips shell tests like [[ -t 1 ]]
INDEX_LINE = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<file>[^)]+\.md)\)(?P<hook>.*)$", re.M)
DATE_NL = re.compile(r"\b(\d{1,2}) (jan|feb|mrt|maa|mar|apr|mei|may|jun|jul|aug|sep|okt|oct|nov|dec)[a-z]* (20\d\d)\b", re.I)
DATE_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
# An open item with a deadline: "open until 29 Oct 2026: …" / "open tot 29 okt 2026: …"; overdue ones are listed
# every week and at session start.
OPEN_DUE = re.compile(r"\bopen (?:tot|until) (\d{1,2} [a-z]+ 20\d\d|20\d\d-\d\d-\d\d)\b:?\s*(.*)", re.I)
OPEN_WORDS = re.compile(r"\b(TODO|open|nog niet|wacht op|later|volgende stap|not yet|waiting for|next step)\b", re.I)
SECRET_HINT = re.compile(r"(wachtwoord|password|passwd|token|api[-_ ]?key|secret)\s*[:=]?\s*`?[A-Za-z0-9_\-+/=@!.]{8,}", re.I)
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)  # [[x]] inside code is syntax (TOML tables, shell tests), not a link


def links(text: str) -> list[str]:
    """[[wikilink]] slugs outside code, normalized so [[a-b]] and [[a_b]] point to the same memo."""
    return [s.replace("-", "_") for s in WIKILINK.findall(CODE.sub("", text))]


FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)  # \r: files written on Windows (CRLF)
BIG_MEMO = 15_000
# Claude Code loads only the first 200 lines / 25 KB of MEMORY.md and silently drops the rest; warn well before.
INDEX_WARN_LINES, INDEX_WARN_BYTES = 150, 20_000
# Relative dates go stale the day after they are written; memos need absolute dates.
RELATIVE_DATE = re.compile(r"\b(eergisteren|gisteren|overmorgen|morgen|vandaag|vanavond|vannacht|vanochtend|vanmiddag|"
                           r"(?:volgende|vorige|deze|afgelopen) (?:week|maand)|yesterday|tomorrow|today|tonight|"
                           r"(?:next|last) week)\b", re.I)


def frontmatter(text: str) -> dict:
    m = FRONT.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
        elif line.strip().startswith("type:"):
            out["type"] = line.split(":", 1)[1].strip()
    return out


def looks_secret(hit: str) -> bool:
    """Keep only values that look like a password or token: with a digit, not a path, file name or CONSTANT_NAME.
    ‹secret:NAME› placeholders are removed before matching: they are the fix, not a leak."""
    value = re.sub(r"^[A-Za-z_ -]+\s*[:=]?\s*`?", "", hit)
    return bool(re.search(r"\d", value)) and not value.startswith(("/", ".", "~", "$", "_", "-")) \
        and not re.fullmatch(r"[A-Z0-9_/]+", value) and not re.match(r"(seconds|minutes|hours|days|weeks)=", value)


def latest_date(text: str, until: date | None = None) -> date | None:
    found = []
    for d, mon, y in DATE_NL.findall(text):
        try:
            found.append(date(int(y), MONTHS[mon[:3].lower()], int(d)))
        except ValueError:
            pass
    for y, m, d in DATE_ISO.findall(text):
        try:
            found.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    found = [d for d in found if until is None or d <= until]  # a contract end in 2029 is not a check
    return max(found) if found else None


# Every memo (except feedback/user rules) and every homelab doc is built the same way: the current state, then the
# deliberate choices with their reasons; history goes to archief/<name>_archief.md.
STRUCTURE = (f"## {W['now']}", f"## {W['decisions']}")  # what a report asks for; both languages count as present
STRUCTURE_RE = (NOW_H, DEC_H)
NOT_DOCS = {"CLAUDE.md", "README.md"}


def memo_type(text: str) -> str:
    m = FRONT.match(text)
    t = re.search(r"^\s*type:\s*(\w+)", m.group(1), re.M) if m else None
    return t.group(1) if t else ""


def open_text(text: str) -> str:
    """The part of a memo where an open item can live: not the frontmatter, not "## Keuzes" (a decision is no open
    item), not headings ("### Open"), not "niets open" / "geen open punten" / "nothing open"."""
    body = FRONT.sub("", text, count=1)
    body = re.sub(rf"^{DEC_H}\b.*?(?=^## |\Z)", "", body, flags=re.M | re.S)
    return "\n".join(l for l in body.splitlines()
                     if not l.startswith("#") and not re.search(r"\b(niets|geen|nothing|no)\s+open", l, re.I))


_MON = "jan|feb|mrt|maa|mar|apr|mei|may|jun|jul|aug|sep|okt|oct|nov|dec"
# A decision carries when it was taken: "20 okt 2026", "20 okt", "okt 2026" or 2026-10-20.
DECISION_DATE = re.compile(rf"\b\d{{1,2}} ({_MON})[a-z]*\b|\b({_MON})[a-z]* 20\d\d\b|\b20\d\d-\d\d", re.I)
UNCHECKED = re.compile(r"\((?:stand|as of) ([^)]*?),? (?:niet (?:opnieuw )?gecontroleerd|not (?:re-?)?checked)\)", re.I)
STALE_UNCHECKED_DAYS = 60
# Facts that change on the live system (IP addresses, x.y.z versions), and the "(gecontroleerd <date>)" mark of rule 4.
CHANGEABLE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\bv?\d+\.\d+\.\d+\b")
CHECKED = re.compile(r"\b(?:gecontroleerd|checked) (\d[^)]*?)\)", re.I)
REVIEW_AFTER_DAYS = 45  # Guru-style review cycle; measured 21 Oct 2026: 8 of 68 such memos were older


# Question words and fillers (Dutch and English) that say nothing about the subject of a search.
QUERY_STOP = frozenset("welk welke wat hoe hoeveel waar waarom wanneer wie is de het een en van in op met voor door ik je "
                       "mijn heeft hebben draait staat staan maak maakt werkt gebruikt kost per moet nieuw nieuwe update "
                       "herstart verbind koppel the a an of to how what where which why is are do does".split())


def query_coverage(query: str, text: str) -> float:
    """Share of the query's subject words found in `text` (lowercase); a 5-letter prefix covers Dutch inflections."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3 and w not in QUERY_STOP]
    return sum((w if len(w) < 6 else w[:5]) in text for w in words) / len(words) if words else 1.0


def last_checked(text: str, today: date) -> date | None:
    """Newest "(gecontroleerd <date>)" mark; without one the newest date in the memo.
    shortcut: the newest date is when something was written, not checked; good enough until most memos carry marks."""
    marks = [latest_date(w + ("" if re.search(r"20\d\d", w) else f" {today.year}"), today) for w in CHECKED.findall(text)]
    return max((m for m in marks if m), default=None) or latest_date(text, today)


def keuzes(text: str) -> list[str]:
    """The decision bullets under "## Keuzes": a decision starts with its bold title ("- **..."); plain bullets there are
    explanation or pointers."""
    m = re.search(rf"^{DEC_H}\b(.*?)(?=^## |\Z)", text, re.M | re.S)
    return [l for l in (m.group(1).splitlines() if m else []) if l.startswith("- **")]


# A decision that replaces an earlier one; the old wording often lives on in the description, index line, other memos.
REPLACED = re.compile(r"\b(vervangt|vervangen door|replaces|replaced by|supersedes)\b", re.I)
REPLACED_HINT = ("\nThis change adds a decision that replaces an earlier one (house rule 3). In the same change: update this "
                 f"memo's description and its MEMORY.md index line, move the old decision to {W['archive']}/<name>_{W['archive']}.md "
                 "('replaced on <date> by ...'), and knowledge_search the old wording in other memos, docs and the "
                 "project's AGENTS.md and skills.")


def replaced_decision(old: str, new: str) -> str:
    """REPLACED_HINT when new has more replacing decisions under "## Keuzes" than old (a memo that already had one stays quiet)."""
    count = lambda text: sum(len(REPLACED.findall(k)) for k in keuzes(text))
    return REPLACED_HINT if count(new) > count(old) else ""


def stale_unchecked(text: str, today: date) -> int:
    """Facts carried over with "(stand <date>, niet opnieuw gecontroleerd)" whose date is older than 60 days."""
    n = 0
    for when in UNCHECKED.findall(text):
        d = latest_date(when + (" " + str(today.year) if not re.search(r"20\d\d", when) else ""))
        n += bool(d and (today - d).days > STALE_UNCHECKED_DAYS)
    return n


def missing_structure(text: str) -> list[str]:
    return [h for h, r in zip(STRUCTURE, STRUCTURE_RE) if not re.search(rf"^{r}\b", text, re.M)]


SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_LINES = 500  # Anthropic's skill guidance: keep SKILL.md under ~500 lines, details in referenced files
MEMO_FILE = re.compile(r"\b((?:project|reference|feedback|incident|infra|user)_[\w-]+\.md)\b")
MD_LINK = re.compile(r"\]\(([^)\s#]+)\)")


def skill_broken(text: str) -> str:
    """Why Claude Code could not use this SKILL.md ("" = fine): without frontmatter or description it loads with
    empty metadata and is never picked up by itself. A write that would cause this is refused."""
    if not FRONT.match(text):
        return "no frontmatter (--- name / description --- at the top)"
    desc = frontmatter(text).get("description", "").strip("'\"")
    if not desc or desc in (">", "|", ">-", "|-"):  # a folded YAML block is not read by this parser: write it on one line
        return "no description on one line in the frontmatter (it is how Claude decides to use the skill)"
    return ""


def skill_lint(skills: dict[str, str], memo_slugs: set[str], memo_files: set[str]) -> dict[str, list[str]]:
    """Checks for the shared skills: {"<folder>/<file>": text} of claude/skills. Calibrated 23 Oct 2026 on 44 skills
    (22 own, 22 of Anthropic): every own skill passes; 9 of Anthropic's use a display name or run long."""
    report: dict[str, list[str]] = {k: [] for k in ("skill_broken", "skill_name_mismatch", "skill_too_long",
                                                   "skill_dead_link", "possible_secret_in_skill")}
    for rel, text in sorted(skills.items()):
        folder, _, file = rel.partition("/")
        if file == "SKILL.md":
            if why := skill_broken(text):
                report["skill_broken"].append(f"{folder}: {why}")
            name = frontmatter(text).get("name", "").strip("'\"")
            if name and (name != folder or not SKILL_NAME.match(name) or len(name) > 64):
                report["skill_name_mismatch"].append(f"{folder}: name '{name}' (use the folder name: lowercase, digits, hyphens)")
            if (n := text.count("\n")) > SKILL_LINES:
                report["skill_too_long"].append(f"{folder}: {n} lines (move details to a referenced file)")
        if not file.endswith(".md"):
            continue
        plain = CODE.sub("", text)
        dead = [f"[[{s}]]" for s in links(text) if s not in memo_slugs]
        dead += [m for m in MEMO_FILE.findall(plain) if m not in memo_files]
        here = rel.rsplit("/", 1)[0]  # a link is relative to the file's own folder (references/a.md -> b.md)
        dead += [t for t in MD_LINK.findall(plain) if "://" not in t and not t.startswith(("/", "~", "mailto:"))
                 and posixpath.normpath(f"{here}/{t}") not in skills]
        if dead:
            report["skill_dead_link"].append(f"{rel}: {', '.join(sorted(set(dead))[:5])}")
        if hits := sum(1 for m in SECRET_HINT.finditer(re.sub(r"‹[^›\n]*›", "", text)) if looks_secret(m.group(0))):
            report["possible_secret_in_skill"].append(f"{rel}: {hits} line(s) (values not shown)")
    return report


def new_memo_lacks(text: str) -> list[str]:
    """What a new memo misses by house rule 2: frontmatter name/description/type and, except feedback and user memos,
    "## Stand" and "## Keuzes". The server refuses such a write, and so does the VM hook (memory-check.py --pre)."""
    fm = frontmatter(text)
    lacks = [f"frontmatter {k}" for k in ("name", "description", "type") if not fm.get(k)]
    return lacks + (missing_structure(text) if memo_type(text) not in ("feedback", "user") else [])


def check_decisions(report: dict, name: str, text: str, today: date):
    if undated := [k for k in keuzes(text) if not DECISION_DATE.search(k)]:
        report["decision_without_date"].append(f"{name}: {len(undated)} decision(s), e.g. {undated[0][2:70]}")
    if stale := stale_unchecked(text, today):
        report["stale_unchecked_fact"].append(f"{name}: {stale} fact(s) unchecked for over {STALE_UNCHECKED_DAYS} days")


def lint(files: dict[str, str], today: date | None = None, mem: str = "memory") -> dict[str, list[str]]:
    """Check one memory root (index, links, frontmatter, size, staleness, secrets); with the homelab root "memory"
    also homelab/sources/ (unprocessed raw sources)."""
    today = today or date.today()
    memos = {rel[len(mem) + 1:]: text for rel, text in files.items()
             if rel.startswith(mem + "/") and rel.count("/") == 1 and rel.endswith(".md")}
    index = memos.get("MEMORY.md", "")
    indexed = {m.group("file") for m in INDEX_LINE.finditer(index)}
    # A [[link]] may point to a memo in another project's memory root (the graph resolves those too).
    all_memos = {rel: t for rel, t in files.items() if rel.count("/") == 1 and rel.endswith(".md") and rel.split("/")[0].endswith("memory")}
    slugs = {s.replace("-", "_") for s in [rel.split("/")[1][:-3] for rel in all_memos] + [frontmatter(t).get("name", "") for t in all_memos.values()]}
    inbound: dict[str, int] = {}
    report: dict[str, list[str]] = {k: [] for k in (
        "index_points_to_missing_file", "memo_not_in_index", "dead_wikilink", "missing_frontmatter",
        "big_memo_without_archive", "long_index_line", "orphan_memo", "possibly_stale_open_item",
        "possible_secret_in_memo", "unprocessed_source", "index_line_without_heading", "index_near_load_limit",
        "relative_date", "missing_structure", "doc_without_topic", "decision_without_date", "stale_unchecked_fact",
        "review_due", "open_overdue")}

    lines, size = index.count("\n") + 1 if index else 0, len(index.encode())
    if lines >= INDEX_WARN_LINES or size >= INDEX_WARN_BYTES:
        report["index_near_load_limit"].append(f"MEMORY.md: {lines} lines / {size // 1024} KB (Claude Code loads 200 lines / 25 KB)")
    # Where the index uses "## " headings (the topics in the graph), every index line belongs under one.
    if re.search(r"^## ", index, re.M):
        first = re.search(r"^## ", index, re.M).start()
        report["index_line_without_heading"] += [f"MEMORY.md → {m.group('file')}" for m in INDEX_LINE.finditer(index[:first])]
    for line in INDEX_LINE.finditer(index):
        if line.group("file") not in memos:
            report["index_points_to_missing_file"].append(f"MEMORY.md → {line.group('file')}")
        if len(line.group(0)) > 200:
            report["long_index_line"].append(f"{line.group('file')} ({len(line.group(0))} chars, rule ~150)")

    for name, text in memos.items():
        if name != "MEMORY.md":
            no_code = CODE.sub(lambda m: "\n" * m.group(0).count("\n"), text)  # keep line numbers right
            for no, line in enumerate(no_code.splitlines(), 1):
                if (d := OPEN_DUE.search(line)) and (due := latest_date(d.group(1))) and due < today:
                    report["open_overdue"].append(f"{name}: due {due.isoformat()}: {d.group(2).strip()[:100]}")
            for no, line in enumerate(no_code.splitlines(), 1):
                if m := RELATIVE_DATE.search(line):
                    report["relative_date"].append(f"{name}: line {no} '{m.group(0)}'")
                    break  # one per memo is enough to act on
        if name == "MEMORY.md":
            continue
        for slug in links(text):
            inbound[slug] = inbound.get(slug, 0) + 1
            if slug not in slugs:
                report["dead_wikilink"].append(f"{name}: [[{slug}]]")
        fm = frontmatter(text)
        missing = [k for k in ("name", "description", "type") if not fm.get(k)]
        if missing:
            report["missing_frontmatter"].append(f"{name}: {', '.join(missing)}")
        if name not in indexed:
            report["memo_not_in_index"].append(name)
        if len(text.encode()) > BIG_MEMO and not re.search(r"\b(?:archief|archive)/", text):
            report["big_memo_without_archive"].append(f"{name} ({len(text.encode()) // 1024} KB)")
        last = latest_date(text)
        opens = len(OPEN_WORDS.findall(open_text(no_code)))
        if last and opens and (today - last).days > 45:
            report["possibly_stale_open_item"].append(f"{name}: {opens} open/TODO mention(s), newest date {last}")
        if memo_type(text) not in ("feedback", "user") and (gone := missing_structure(text)):
            report["missing_structure"].append(f"{name}: no {' / '.join(gone)}")
        check_decisions(report, name, text, today)
        # The whole text: IPs and versions usually sit in `code`.
        facts = len(CHANGEABLE.findall(text)) if memo_type(text) not in ("feedback", "user") else 0
        checked = last_checked(text, today) if facts else None
        if facts and (not checked or (today - checked).days > REVIEW_AFTER_DAYS):
            since = f"not checked since {checked.isoformat()}" if checked else "never checked (no date)"
            report["review_due"].append(f"{name}: {since} ({facts} addresses/versions)")
        hits = sum(1 for m in SECRET_HINT.finditer(re.sub(r"‹[^›\n]*›", "", text)) if looks_secret(m.group(0)))
        if hits:
            report["possible_secret_in_memo"].append(f"{name}: {hits} line(s) (values not shown)")

    for name in memos:
        stem = name[:-3].replace("-", "_")
        fm_name = frontmatter(memos[name]).get("name", "").replace("-", "_")
        if name != "MEMORY.md" and name not in indexed and not inbound.get(stem) and not inbound.get(fm_name):
            report["orphan_memo"].append(name)

    # A raw source counts as processed once anything outside sources/ (a memo, a doc) mentions its file name.
    if mem != "memory":
        return report
    # Homelab docs: the same structure, and a topic that is a heading of the homelab index (their island in the graph).
    headings = {h.strip() for h in re.findall(r"^## (.+)$", index, re.M)}
    for rel, text in sorted(files.items()):
        parts = rel.split("/")
        if parts[0] != "homelab" or len(parts) != 2 or not rel.endswith(".md") or parts[1] in NOT_DOCS:
            continue
        if (gone := missing_structure(text)):
            report["missing_structure"].append(f"{rel}: no {' / '.join(gone)}")
        check_decisions(report, rel, text, today)
        topic = frontmatter(text).get("topic", "").strip('"')
        if topic not in headings:
            report["doc_without_topic"].append(f"{rel}: topic '{topic}' is no heading of MEMORY.md" if topic else f"{rel}: no topic")
    outside = "\n".join(t for r, t in files.items() if not r.startswith("homelab/sources/"))
    for rel in files:
        name = rel.split("/")[-1]
        if rel.startswith("homelab/sources/") and rel.endswith(".md") and name != "LOG.md" and name not in outside:
            report["unprocessed_source"].append(rel)
    return report


def usage_outliers(rows: list[dict], now: float, history_days: int = 30, recent_days: int = 7) -> list[str]:
    """Sessions of the last week that used far more tokens than that agent usually does: modified z-score
    0.6745 * (x - median) / MAD above 3.5 (Iglewicz-Hoaglin), over the sessions of the last month, only with at least 10
    of them. Rows are the session logs' usage parts; one session can log several parts (each compaction), so they are
    summed. The text names no score, so the same session is reported once by the weekly diff."""
    sessions: dict[tuple, dict] = {}
    for r in rows:
        if now - r.get("at", 0) < history_days * 86400:
            s = sessions.setdefault((r.get("agent", "?"), r.get("session", "")), {"tokens": 0, **r})
            s["at"] = max(s["at"], r.get("at", 0))  # recent = its latest part: a long session counts this week too
            s["tokens"] += sum(int(r.get(k) or 0) for k in ("in", "cache_write", "cache_read", "out"))
    out = []
    for agent in sorted({a for a, _ in sessions}):
        mine = [s for (a, _), s in sessions.items() if a == agent]
        xs = sorted(s["tokens"] for s in mine)
        if len(xs) < 10:
            continue
        median = xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
        dev = sorted(abs(x - median) for x in xs)
        mad = dev[len(dev) // 2] or 1
        out += [f"{agent} session {s['session'][:8]} ({s.get('device', '?')}, {s.get('project', '?')}, "
                f"{date.fromtimestamp(s['at']).isoformat()}): {s['tokens'] / 1e6:.1f}M tokens"
                for s in sorted(mine, key=lambda s: -s["tokens"])
                if now - s["at"] < recent_days * 86400 and 0.6745 * (s["tokens"] - median) / mad > 3.5]
    return out


def render(report: dict[str, list[str]]) -> str:
    lines = []
    for check, items in report.items():
        if items:
            lines.append(f"## {check} ({len(items)})")
            lines += [f"- {i}" for i in items[:200]]
    return "\n".join(lines) or "No findings."


if __name__ == "__main__":  # self-check
    m = lambda k: f"---\nname: a\n---\n## Stand\nVervangt in Stand telt niet\n## Keuzes\n{k}"
    assert replaced_decision(m(""), m("- **Worktrees** (25 okt 2026): x. Vervangt main-only van 23 okt.")) == REPLACED_HINT
    assert replaced_decision(m("- **A** (1 okt): replaces B"), m("- **A** (1 okt): replaces B\n- **C** (2 okt): y")) == ""
    assert replaced_decision("", m("")) == "" and replaced_decision("", m("- uitleg: vervangt niets, geen besluit")) == ""
    assert new_memo_lacks("plain") == ["frontmatter name", "frontmatter description", "frontmatter type", *STRUCTURE]
    assert new_memo_lacks("---\nname: a\ndescription: d\nmetadata:\n  type: project\n---\n## Status\nx\n## Decisions\n") == []
    assert stale_unchecked("x (as of 1 jul 2026, not re-checked)", date(2026, 10, 20)) == 1
    assert OPEN_DUE.search("- open until 2026-10-30: later")
    assert skill_broken("# just text") and skill_broken("---\nname: a\n---\nx") and skill_broken("---\nname: a\ndescription: >\n  x\n---\n")
    assert skill_broken("---\nname: a\ndescription: Does a.\n---\nbody") == ""
    assert skill_broken("---\r\nname: a\r\ndescription: Does a.\r\n---\r\nbody\r\n") == ""  # written on Windows
    assert new_memo_lacks("---\r\nname: a\r\ndescription: d\r\nmetadata:\r\n  type: project\r\n---\r\n## Stand\r\nx\r\n## Keuzes\r\n") == []
    sk = skill_lint({"a/SKILL.md": "---\nname: b\ndescription: d\n---\nsee [[project_x]], project_gone.md, [ref](ref.md), [ok](ok.md)\n",
                     "a/ok.md": "fine", "a/refs/one.md": "[two](two.md) [up](../ok.md)", "a/refs/two.md": "x", "c/SKILL.md": "---\ndescription: d\n---\n" + "x\n" * 501}, {"project_x"}, {"project_x.md"})
    assert sk["skill_name_mismatch"] == ["a: name 'b' (use the folder name: lowercase, digits, hyphens)"], sk
    assert sk["skill_dead_link"] == ["a/SKILL.md: project_gone.md, ref.md"], sk
    assert sk["skill_too_long"] == ["c: 504 lines (move details to a referenced file)"] and not sk["skill_broken"], sk
    assert new_memo_lacks("---\nname: a\ndescription: d\nmetadata:\n  type: feedback\n---\nRule.") == []
    files = {
        "memory/MEMORY.md": "- [A](a.md) — hook\n- [Gone](gone.md) — x\n",
        "memory/a.md": "---\nname: a\ndescription: d\nmetadata:\n  type: project\n---\nSee [[b]] and [[nope]]. TODO 1 jan 2026\n",
        "memory/b.md": "---\nname: b\n---\npassword: Sup3rSecret99\n",
        "memory/c.md": "---\nname: c\ndescription: d\nmetadata:\n  type: user\n---\nlonely\n",
        "homelab/sources/2026-10-18-x.md": "raw",
    }
    r = lint(files, today=date(2026, 10, 18))
    assert r["index_points_to_missing_file"] == ["MEMORY.md → gone.md"], r
    assert r["dead_wikilink"] == ["a.md: [[nope]]"], r
    r2 = lint({"memory/MEMORY.md": "", "memory/x_y.md": "---\nname: x-y\n---\n", "memory/z.md": "see [[x-y]] and `[[apps]]`\n```\n[[tool]]\n```"})
    assert not r2["dead_wikilink"] and "x_y.md" not in r2["orphan_memo"], r2
    assert sorted(r["memo_not_in_index"]) == ["b.md", "c.md"], r
    assert r["orphan_memo"] == ["c.md"], r  # b.md is linked from a.md
    assert r["missing_frontmatter"] == ["b.md: description, type"], r
    assert r["possibly_stale_open_item"][0].startswith("a.md"), r
    old = "---\ndescription: hardware nog niet gekocht\n---\n1 jun 2026\n### Open\n- Niets open.\n## Keuzes\n- **Keuze geparkeerd, nog niet besloten**\n"
    assert not lint({"memory/MEMORY.md": "", "memory/p.md": old}, today=date(2026, 10, 18))["possibly_stale_open_item"]
    assert lint({"memory/MEMORY.md": "", "memory/q.md": "1 jun 2026\n### Open\n- wacht op onderdelen\n"},
                today=date(2026, 10, 18))["possibly_stale_open_item"]
    assert not lint({"memory/MEMORY.md": "", "memory/o.md": "1 jun 2026\n```\nopen('x').read()\n```\nuse `open()`\n"},
                    today=date(2026, 10, 18))["possibly_stale_open_item"]
    assert r["possible_secret_in_memo"] == ["b.md: 1 line(s) (values not shown)"], r
    assert not lint({"memory/MEMORY.md": "", "memory/a.md": "[[b]]", "trade-memory/b.md": "x"})["dead_wikilink"]  # cross-root
    r4 = lint({"memory/MEMORY.md": "x\n" * 160, "memory/a.md": "```\ntoday\n```\nfixed gisteren", "memory/b.md": "goedemorgen, 's morgens"})
    assert r4["index_near_load_limit"] and r4["relative_date"] == ["a.md: line 4 'gisteren'"], r4
    r3 = lint({"memory/MEMORY.md": "- [A](a.md) — x\n## Net\n- [B](b.md) — y\n", "memory/a.md": "", "memory/b.md": ""})
    assert r3["index_line_without_heading"] == ["MEMORY.md → a.md"], r3
    assert not lint({"memory/MEMORY.md": "- [A](a.md) — x\n", "memory/a.md": ""})["index_line_without_heading"]  # no headings: fine
    assert not lint({"memory/MEMORY.md": "", "memory/p.md": "root password: ‹secret:ROOT_PW›\ntoken: /etc/x/tok.txt\n"
                     "secret `HOMELAB_SSH_KEY`\n"})["possible_secret_in_memo"]
    assert r["unprocessed_source"] == ["homelab/sources/2026-10-18-x.md"], r
    assert r["missing_structure"] == [f"{n}.md: no {' / '.join(STRUCTURE)}" for n in "ab"], r  # c.md is a user memo
    r5 = lint({"memory/MEMORY.md": "## Net\n- [N](n.md) — x\n", "memory/n.md": "---\nmetadata:\n  type: project\n---\n## Stand\nx\n## Keuzes\ny\n",
               "homelab/netwerk.md": "---\ntopic: \"Net\"\n---\n## Stand\n## Keuzes\n", "homelab/x.md": "---\ntopic: \"Nope\"\n---\n",
               "homelab/CLAUDE.md": "rules", "homelab/archief/x_archief.md": "old"})
    assert r5["missing_structure"] == [f"homelab/x.md: no {' / '.join(STRUCTURE)}"], r5
    assert r5["doc_without_topic"] == ["homelab/x.md: topic 'Nope' is no heading of MEMORY.md"], r5
    k = "---\nmetadata:\n  type: project\n---\n## Stand\nIP 10.0.0.9 (stand 1 jul 2026, niet opnieuw gecontroleerd), port 80 (stand 15 okt, niet gecontroleerd)\n## Keuzes\n- **A** (20 okt 2026): x\n- **B**: no date\n- **C** (2026-10-01, the owner): y\n"
    r6 = lint({"memory/MEMORY.md": "", "memory/k.md": k, "memory/n.md": "## Stand\n## Keuzes\n- Nog geen bewuste keuzes vastgelegd.\n"}, today=date(2026, 10, 20))
    assert r6["decision_without_date"] == ["k.md: 1 decision(s), e.g. **B**: no date"], r6
    assert r6["stale_unchecked_fact"] == ["k.md: 1 fact(s) unchecked for over 60 days"], r6
    old_ip = "---\nmetadata:\n  type: project\n---\nIP 10.0.0.9 (gecontroleerd 1 aug 2026), contract t/m 1 nov 2029\n"
    r7 = lint({"memory/MEMORY.md": "", "memory/o.md": old_ip, "memory/f.md": old_ip.replace("1 aug", "15 okt"),
               "memory/u.md": old_ip.replace("project", "feedback"), "memory/v.md": "---\nmetadata:\n  type: project\n---\n`v1.2.3` on 1 jun 2026\n", "memory/w.md": "host `10.0.0.1`\n"},
              today=date(2026, 10, 20))
    assert r7["review_due"] == ["o.md: not checked since 2026-08-01 (1 addresses/versions)",
                                "v.md: not checked since 2026-06-01 (1 addresses/versions)",
                                "w.md: never checked (no date) (1 addresses/versions)"], r7  # 2029 is no check
    assert not r6["review_due"], r6  # k.md: IP, but a decision of 20 Oct
    od = lint({"memory/MEMORY.md": "", "memory/d.md": "- open tot 21 okt 2026: HA-herhaling weghalen\n- Open tot 2026-10-30: later\n"
               "- open tot 20 okt 2026 zonder dubbele punt\n```\nopen tot 1 jan 2026: in code\n```\n"}, today=date(2026, 10, 22))
    assert od["open_overdue"] == ["d.md: due 2026-10-21: HA-herhaling weghalen", "d.md: due 2026-10-20: zonder dubbele punt"], od
    assert query_coverage("welk IP heeft de NAS BMC", "bmc van de nas: 10.0.0.12 ip") == 1.0
    assert query_coverage("waarom bevriest de NAS", "nas bevriezen dinsdag") == 1.0  # prefix: bevri
    assert query_coverage("hoe werkt de Nest thermostaat", "honeywell en hue") == 0.0 and query_coverage("hoe is het", "") == 1.0
    t = lint({**files, "trade-memory/MEMORY.md": "- [X](x.md) — y\n"}, today=date(2026, 10, 18), mem="trade-memory")
    assert t["index_points_to_missing_file"] == ["MEMORY.md → x.md"] and not t["unprocessed_source"], t
    day, now = 86400, 1_800_000_000
    rows = [{"agent": "codex", "session": f"s{i}", "at": now - i * day, "in": 1_000_000 + i * 10_000} for i in range(12)]
    rows += [{"agent": "codex", "session": "big", "at": now - day, "in": 5_000_000},
             {"agent": "codex", "session": "big", "at": now - day, "out": 4_000_000},  # second part of the same session
             {"agent": "codex", "session": "old", "at": now - 20 * day, "in": 50_000_000},  # outside the last week
             {"agent": "agy", "session": "few", "at": now, "in": 90_000_000}]  # fewer than 10 sessions: no verdict
    hits = usage_outliers(rows, now)
    assert len(hits) == 1 and hits[0].startswith("codex session big (?, ?, ") and hits[0].endswith(": 9.0M tokens"), hits
    print("ok")
