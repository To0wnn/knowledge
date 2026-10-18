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
        "possible_secret_in_memo", "unprocessed_source")}

    for line in INDEX_LINE.finditer(index):
        if line.group("file") not in memos:
            report["index_points_to_missing_file"].append(f"MEMORY.md → {line.group('file')}")
        if len(line.group(0)) > 200:
            report["long_index_line"].append(f"{line.group('file')} ({len(line.group(0))} chars, rule ~150)")

    for name, text in memos.items():
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
        opens = len(OPEN_WORDS.findall(text))
        if last and opens and (today - last).days > 45:
            report["possibly_stale_open_item"].append(f"{name}: {opens} open/TODO mention(s), newest date {last}")
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
    assert r["possible_secret_in_memo"] == ["b.md: 1 line(s) (values not shown)"], r
    assert not lint({"memory/MEMORY.md": "", "memory/a.md": "[[b]]", "trade-memory/b.md": "x"})["dead_wikilink"]  # cross-root
    assert not lint({"memory/MEMORY.md": "", "memory/p.md": "root password: ‹secret:ROOT_PW›\ntoken: /etc/x/tok.txt\n"
                     "secret `HOMELAB_SSH_KEY`\n"})["possible_secret_in_memo"]
    assert r["unprocessed_source"] == ["homelab/sources/2026-10-18-x.md"], r
    t = lint({**files, "trade-memory/MEMORY.md": "- [X](x.md) — y\n"}, today=date(2026, 10, 18), mem="trade-memory")
    assert t["index_points_to_missing_file"] == ["MEMORY.md → x.md"] and not t["unprocessed_source"], t
    print("ok")
