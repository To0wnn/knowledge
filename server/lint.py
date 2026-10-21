"""Deterministic health checks for the knowledge base (the "lint" step of the LLM-wiki pattern).

Pure functions over {rel_path: text}, so they run on the in-memory index in milliseconds and are easy to test.
The server only reports facts; judging contradictions or rewriting memos is left to the Claude session that
reads the report.
"""
import re
from datetime import date

MONTHS = {"jan": 1, "feb": 2, "mrt": 3, "maa": 3, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6, "jul": 7,
          "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dec": 12}
WIKILINK = re.compile(r"\[\[([a-z0-9][a-z0-9_-]*)\]\]")  # [[slug]]; skips shell tests like [[ -t 1 ]]
INDEX_LINE = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<file>[^)]+\.md)\)(?P<hook>.*)$", re.M)
DATE_NL = re.compile(r"\b(\d{1,2}) (jan|feb|mrt|maa|mar|apr|mei|may|jun|jul|aug|sep|okt|oct|nov|dec)[a-z]* (20\d\d)\b", re.I)
DATE_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
OPEN_WORDS = re.compile(r"\b(TODO|open|nog niet|wacht op|later|volgende stap)\b", re.I)
SECRET_HINT = re.compile(r"(wachtwoord|password|passwd|token|api[-_ ]?key|secret)\s*[:=]?\s*`?[A-Za-z0-9_\-+/=@!.]{8,}", re.I)
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)  # [[x]] inside code is syntax (TOML tables, shell tests), not a link


def links(text: str) -> list[str]:
    """[[wikilink]] slugs outside code, normalized so [[a-b]] and [[a_b]] point to the same memo."""
    return [s.replace("-", "_") for s in WIKILINK.findall(CODE.sub("", text))]


FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
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


def latest_date(text: str) -> date | None:
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
    return max(found) if found else None


# Every memo (except feedback/user rules) and every homelab doc is built the same way: the current state, then the
# deliberate choices with their reasons; history goes to archief/<name>_archief.md.
STRUCTURE = ("## Stand", "## Keuzes")
NOT_DOCS = {"CLAUDE.md", "README.md"}


def memo_type(text: str) -> str:
    m = FRONT.match(text)
    t = re.search(r"^\s*type:\s*(\w+)", m.group(1), re.M) if m else None
    return t.group(1) if t else ""


def open_text(text: str) -> str:
    """The part of a memo where an open item can live: not the frontmatter, not "## Keuzes" (a decision is no open
    item), not headings ("### Open"), not "niets open" / "geen open punten"."""
    body = FRONT.sub("", text, count=1)
    body = re.sub(r"^## Keuzes\b.*?(?=^## |\Z)", "", body, flags=re.M | re.S)
    return "\n".join(l for l in body.splitlines() if not l.startswith("#") and not re.search(r"\b(niets|geen)\s+open", l, re.I))


_MON = "jan|feb|mrt|maa|mar|apr|mei|may|jun|jul|aug|sep|okt|oct|nov|dec"
# A decision carries when it was taken: "20 okt 2026", "20 okt", "okt 2026" or 2026-10-20.
DECISION_DATE = re.compile(rf"\b\d{{1,2}} ({_MON})[a-z]*\b|\b({_MON})[a-z]* 20\d\d\b|\b20\d\d-\d\d", re.I)
UNCHECKED = re.compile(r"\(stand ([^)]*?),? niet (?:opnieuw )?gecontroleerd\)", re.I)
STALE_UNCHECKED_DAYS = 60


def keuzes(text: str) -> list[str]:
    """The decision bullets under "## Keuzes": a decision starts with its bold title ("- **..."); plain bullets there are
    explanation or pointers."""
    m = re.search(r"^## Keuzes\b(.*?)(?=^## |\Z)", text, re.M | re.S)
    return [l for l in (m.group(1).splitlines() if m else []) if l.startswith("- **")]


def stale_unchecked(text: str, today: date) -> int:
    """Facts carried over with "(stand <date>, niet opnieuw gecontroleerd)" whose date is older than 60 days."""
    n = 0
    for when in UNCHECKED.findall(text):
        d = latest_date(when + (" " + str(today.year) if not re.search(r"20\d\d", when) else ""))
        n += bool(d and (today - d).days > STALE_UNCHECKED_DAYS)
    return n


def missing_structure(text: str) -> list[str]:
    return [h for h in STRUCTURE if not re.search(rf"^{h}\b", text, re.M)]


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
        "relative_date", "missing_structure", "doc_without_topic", "decision_without_date", "stale_unchecked_fact")}

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
        if len(text.encode()) > BIG_MEMO and "archief/" not in text:
            report["big_memo_without_archive"].append(f"{name} ({len(text.encode()) // 1024} KB)")
        last = latest_date(text)
        opens = len(OPEN_WORDS.findall(open_text(no_code)))
        if last and opens and (today - last).days > 45:
            report["possibly_stale_open_item"].append(f"{name}: {opens} open/TODO mention(s), newest date {last}")
        if memo_type(text) not in ("feedback", "user") and (gone := missing_structure(text)):
            report["missing_structure"].append(f"{name}: no {' / '.join(gone)}")
        check_decisions(report, name, text, today)
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


def render(report: dict[str, list[str]]) -> str:
    lines = []
    for check, items in report.items():
        if items:
            lines.append(f"## {check} ({len(items)})")
            lines += [f"- {i}" for i in items[:200]]
    return "\n".join(lines) or "No findings."


if __name__ == "__main__":  # self-check
    assert new_memo_lacks("plain") == ["frontmatter name", "frontmatter description", "frontmatter type", "## Stand", "## Keuzes"]
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
    assert r["missing_structure"] == ["a.md: no ## Stand / ## Keuzes", "b.md: no ## Stand / ## Keuzes"], r  # c.md is a user memo
    r5 = lint({"memory/MEMORY.md": "## Net\n- [N](n.md) — x\n", "memory/n.md": "---\nmetadata:\n  type: project\n---\n## Stand\nx\n## Keuzes\ny\n",
               "homelab/netwerk.md": "---\ntopic: \"Net\"\n---\n## Stand\n## Keuzes\n", "homelab/x.md": "---\ntopic: \"Nope\"\n---\n",
               "homelab/CLAUDE.md": "rules", "homelab/archief/x_archief.md": "old"})
    assert r5["missing_structure"] == ["homelab/x.md: no ## Stand / ## Keuzes"], r5
    assert r5["doc_without_topic"] == ["homelab/x.md: topic 'Nope' is no heading of MEMORY.md"], r5
    k = "---\nmetadata:\n  type: project\n---\n## Stand\nIP 10.0.0.9 (stand 1 jul 2026, niet opnieuw gecontroleerd), port 80 (stand 15 okt, niet gecontroleerd)\n## Keuzes\n- **A** (20 okt 2026): x\n- **B**: no date\n- **C** (2026-10-01, the owner): y\n"
    r6 = lint({"memory/MEMORY.md": "", "memory/k.md": k, "memory/n.md": "## Stand\n## Keuzes\n- Nog geen bewuste keuzes vastgelegd.\n"}, today=date(2026, 10, 20))
    assert r6["decision_without_date"] == ["k.md: 1 decision(s), e.g. **B**: no date"], r6
    assert r6["stale_unchecked_fact"] == ["k.md: 1 fact(s) unchecked for over 60 days"], r6
    t = lint({**files, "trade-memory/MEMORY.md": "- [X](x.md) — y\n"}, today=date(2026, 10, 18), mem="trade-memory")
    assert t["index_points_to_missing_file"] == ["MEMORY.md → x.md"] and not t["unprocessed_source"], t
    print("ok")
