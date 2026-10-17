"""Build the "brain" graph for the web UI from the in-memory index: one node per memo, doc, skill and raw source,
edges for wikilinks, markdown links, doc/skill references and cited sources. Pure function, a few ms."""
import re

from lint import WIKILINK, frontmatter

MD_LINK = re.compile(r"\]\(([^)#\s]+\.md)\)")
DOC_FILE = re.compile(r"\b(\d\d)_[A-Z0-9_]+\.md\b")
DOC_WORD = re.compile(r"\bdocs? (\d\d)(?:\s*(?:,|en|and|/|–|-)\s*(\d\d))?\b", re.I)
SKILL_REF = re.compile(r"\bskill[s]? `([a-z0-9][a-z0-9-]*)`", re.I)
SOURCE_REF = re.compile(r"\b(20\d\d-\d\d-\d\d-[a-z0-9-]+\.md)\b")


def group_of(rel: str, text: str) -> str:
    if rel == "memory/MEMORY.md":
        return "index"
    if rel.startswith("memory/"):
        t = frontmatter(text).get("type", "")
        name = rel.split("/")[-1]
        if name.startswith("incident"):
            return "incident"
        if name.startswith("infra"):
            return "infra"
        return t if t in ("user", "feedback", "project", "reference") else "project"
    if rel.startswith("homelab/sources/"):
        return "source"
    if rel.startswith("claude/skills/"):
        return "skill"
    return "doc"


def label_of(rel: str, text: str, titles: dict[str, str]) -> str:
    name = rel.split("/")[-1]
    if rel.startswith("claude/skills/"):
        return rel.split("/")[2]
    if rel in titles:
        return titles[rel]
    fm = frontmatter(text)
    if rel.startswith("homelab/sources/") and fm.get("title"):
        return fm["title"].strip('"')[:60]
    return name[:-3].replace("_", " ")


def build(files: dict[str, str]) -> dict:
    keep = {}
    for rel, text in files.items():
        parts = rel.split("/")
        if (rel.startswith("memory/") and len(parts) == 2) \
                or (rel.startswith("homelab/") and len(parts) == 2 and re.match(r"\d\d_.*\.md$", parts[1])) \
                or (rel.startswith("homelab/sources/") and parts[-1] != "LOG.md") \
                or (rel.startswith("claude/skills/") and parts[-1] == "SKILL.md" and len(parts) == 4):
            keep[rel] = text
    titles = {}
    index = files.get("memory/MEMORY.md", "")
    for m in re.finditer(r"^- \[([^\]]+)\]\(([^)]+\.md)\)", index, re.M):
        titles[f"memory/{m.group(2)}"] = m.group(1)
    by_slug = {}
    for rel, text in keep.items():
        if rel.startswith("memory/"):
            stem = rel.split("/")[-1][:-3]
            for key in (stem, frontmatter(text).get("name", "")):
                if key:
                    by_slug[key.replace("-", "_")] = rel  # hyphen/underscore variants point to the same memo
    docs = {rel.split("/")[1][:2]: rel for rel in keep if rel.startswith("homelab/") and not rel.startswith("homelab/sources/")}
    skills = {rel.split("/")[2]: rel for rel in keep if rel.startswith("claude/skills/")}
    sources = {rel.split("/")[-1]: rel for rel in keep if rel.startswith("homelab/sources/")}

    edges = set()
    for rel, text in keep.items():
        targets = set()
        targets |= {by_slug.get(s.replace("-", "_")) for s in WIKILINK.findall(text)}
        if rel.startswith("memory/"):
            targets |= {f"memory/{f.split('/')[-1]}" for f in MD_LINK.findall(text) if not f.startswith("http")}
        targets |= {docs.get(n) for n in DOC_FILE.findall(text)}
        for a, b in DOC_WORD.findall(text):
            targets |= {docs.get(a), docs.get(b)}
        targets |= {skills.get(s) for s in SKILL_REF.findall(text)}
        targets |= {sources.get(s) for s in SOURCE_REF.findall(text)}
        for t in targets:
            if t and t != rel and t in keep:
                edges.add((rel, t))
    degree: dict[str, int] = {}
    for a, b in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    nodes = [{"id": rel, "label": label_of(rel, text, titles), "group": group_of(rel, text),
              "size": len(text), "degree": degree.get(rel, 0)} for rel, text in sorted(keep.items())]
    return {"nodes": nodes, "links": [{"source": a, "target": b} for a, b in sorted(edges)]}


if __name__ == "__main__":
    g = build({
        "memory/MEMORY.md": "- [Alpha memo](a.md) — x\n",
        "memory/a.md": "---\nname: a\nmetadata:\n  type: feedback\n---\nsee [[b_c]], docs 14, skill `forge-stack`, 2026-10-18-x.md",
        "memory/b_c.md": "---\nname: b-c\n---\nback to [[a]]",
        "homelab/14_FRAMEWORK.md": "doc",
        "homelab/sources/2026-10-18-x.md": "---\ntitle: \"X\"\n---\nraw",
        "claude/skills/forge-stack/SKILL.md": "skill",
        "homelab/tools/ignored.md": "not a node",
    })
    ids = {n["id"]: n for n in g["nodes"]}
    assert "homelab/tools/ignored.md" not in ids and ids["memory/a.md"]["label"] == "Alpha memo"
    assert ids["memory/a.md"]["group"] == "feedback" and ids["homelab/sources/2026-10-18-x.md"]["label"] == "X"
    links = {(l["source"], l["target"]) for l in g["links"]}
    for t in ("memory/b_c.md", "homelab/14_FRAMEWORK.md", "claude/skills/forge-stack/SKILL.md", "homelab/sources/2026-10-18-x.md"):
        assert ("memory/a.md", t) in links, (t, links)
    assert ("memory/MEMORY.md", "memory/a.md") in links and ("memory/b_c.md", "memory/a.md") in links
    print("ok", len(g["nodes"]), "nodes", len(g["links"]), "links")
