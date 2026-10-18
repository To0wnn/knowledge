"""Build the "brain" graph for the web UI from the in-memory index: one node per memo, doc, skill and raw source,
edges for wikilinks, markdown links, doc/skill references and cited sources. Pure function, a few ms."""
import re

from lint import WIKILINK, frontmatter

MD_LINK = re.compile(r"\]\(([^)#\s]+\.md)\)")
DOC_FILE = re.compile(r"\b(\d\d)_[A-Z0-9_]+\.md\b")
DOC_WORD = re.compile(r"\bdocs? (\d\d)(?:\s*(?:,|en|and|/|–|-)\s*(\d\d))?\b", re.I)
SKILL_REF = re.compile(r"\bskill[s]? `([a-z0-9][a-z0-9-]*)`", re.I)
BARE_MD = re.compile(r"(?<![\w/.-])([A-Za-z0-9][\w.-]*\.md)\b")  # "see ENTRY_TIMING_FIXES.md" in the same project
SOURCE_REF = re.compile(r"\b(20\d\d-\d\d-\d\d-[a-z0-9-]+\.md)\b")
# project -> (docs root, memory root). Docs roots other than homelab are code repos: only their Markdown is shared.
PROJECTS = {"homelab": ("homelab", "memory"), "anpitek": ("anpitek", "anpitek-memory"), "trade": ("trade", "trade-memory")}
MEMORY_ROOTS = {mem for _, mem in PROJECTS.values()}
MD_ONLY = {docs for docs, _ in PROJECTS.values()} - {"homelab"}


def project_of(rel: str) -> str:
    root = rel.split("/", 1)[0]
    return next((p for p, roots in PROJECTS.items() if root in roots), "shared")


def mem_root(rel: str) -> str | None:
    root = rel.split("/", 1)[0]
    return root if root in MEMORY_ROOTS and rel.count("/") == 1 else None


def group_of(rel: str, text: str) -> str:
    if mem_root(rel) and rel.endswith("/MEMORY.md"):
        return "index"
    if mem_root(rel):
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
        if mem_root(rel) \
                or (parts[0] in MD_ONLY and rel.endswith(".md") and len(parts) <= 3 and not any(x.startswith(".") for x in parts)) \
                or (rel.startswith("homelab/") and len(parts) == 2 and re.match(r"\d\d_.*\.md$", parts[1])) \
                or (rel.startswith("homelab/sources/") and parts[-1] != "LOG.md") \
                or (rel.startswith("claude/skills/") and parts[-1] == "SKILL.md" and len(parts) == 4):
            keep[rel] = text
    titles = {}
    for mem in MEMORY_ROOTS:
        for m in re.finditer(r"^- \[([^\]]+)\]\(([^)]+\.md)\)", files.get(f"{mem}/MEMORY.md", ""), re.M):
            titles[f"{mem}/{m.group(2)}"] = m.group(1)
    by_slug = {}  # slug -> memo, and (memory root, slug) -> memo so a link prefers its own project's memo
    for rel, text in keep.items():
        if mem_root(rel):
            stem = rel.split("/")[-1][:-3]
            for key in (stem, frontmatter(text).get("name", "")):
                if key:
                    key = key.replace("-", "_")  # hyphen/underscore variants point to the same memo
                    by_slug[(mem_root(rel), key)] = rel
                    by_slug.setdefault(key, rel)
    docs = {rel.split("/")[1][:2]: rel for rel in keep if rel.startswith("homelab/") and not rel.startswith("homelab/sources/")}
    skills = {rel.split("/")[2]: rel for rel in keep if rel.startswith("claude/skills/")}
    sources = {rel.split("/")[-1]: rel for rel in keep if rel.startswith("homelab/sources/")}
    by_name: dict[tuple[str, str], str] = {}
    for rel in keep:
        by_name.setdefault((project_of(rel), rel.split("/")[-1]), rel)

    edges = set()
    for rel, text in keep.items():
        targets = set()
        home = mem_root(rel) or PROJECTS.get(project_of(rel), ("", ""))[1]
        for slug in WIKILINK.findall(text):
            slug = slug.replace("-", "_")
            targets.add(by_slug.get((home, slug)) or by_slug.get(slug))
        if mem_root(rel):
            targets |= {f"{mem_root(rel)}/{f.split('/')[-1]}" for f in MD_LINK.findall(text) if not f.startswith("http")}
        targets |= {docs.get(n) for n in DOC_FILE.findall(text)}
        for a, b in DOC_WORD.findall(text):
            targets |= {docs.get(a), docs.get(b)}
        targets |= {skills.get(s) for s in SKILL_REF.findall(text)}
        targets |= {sources.get(s) for s in SOURCE_REF.findall(text)}
        targets |= {by_name.get((project_of(rel), n)) for n in BARE_MD.findall(text)}
        for t in targets:
            if t and t != rel and t in keep:
                edges.add((rel, t))
    degree: dict[str, int] = {}
    for a, b in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    labels = {rel: label_of(rel, text, titles) for rel, text in keep.items()}
    cluster = clusters(sorted(keep), edges, degree, labels)
    nodes = [{"id": rel, "label": labels[rel], "group": group_of(rel, text), "project": project_of(rel),
              "cluster": cluster[rel], "size": len(text), "degree": degree.get(rel, 0)} for rel, text in sorted(keep.items())]
    return {"nodes": nodes, "links": [{"source": a, "target": b} for a, b in sorted(edges)]}


def clusters(ids: list[str], edges: set[tuple[str, str]], degree: dict[str, int], labels: dict[str, str]) -> dict[str, str]:
    """Topic clusters by label propagation: each node repeatedly takes the label most common among its neighbours
    (ties: the label of the best-connected node), in a fixed order, so the result is deterministic. Index files are
    left out (they link to everything). A cluster is named after its best-connected member."""
    hubs = {i for i in ids if i.endswith("/MEMORY.md")}
    nb: dict[str, list[str]] = {i: [] for i in ids}
    for a, b in edges:
        if a not in hubs and b not in hubs:
            nb[a].append(b)
            nb[b].append(a)
    label = {i: i for i in ids}
    order = sorted(ids, key=lambda i: (-degree.get(i, 0), i))
    for _ in range(20):
        changed = False
        for i in order:
            if not nb[i]:
                continue
            count: dict[str, int] = {}
            for j in nb[i]:
                count[label[j]] = count.get(label[j], 0) + 1
            best = max(count, key=lambda lab: (count[lab], degree.get(lab, 0), lab))
            if count[best] > count.get(label[i], 0) and best != label[i]:
                label[i], changed = best, True
        if not changed:
            break
    members: dict[str, list[str]] = {}
    for i, lab in label.items():
        members.setdefault(lab, []).append(i)
    name = {lab: labels[max(m, key=lambda i: (degree.get(i, 0), i))] for lab, m in members.items()}
    return {i: name[label[i]] if len(members[label[i]]) > 1 else "" for i in ids}


if __name__ == "__main__":
    g = build({
        "memory/MEMORY.md": "- [Alpha memo](a.md) — x\n",
        "memory/a.md": "---\nname: a\nmetadata:\n  type: feedback\n---\nsee [[b_c]], docs 14, skill `forge-stack`, 2026-10-18-x.md",
        "memory/b_c.md": "---\nname: b-c\n---\nback to [[a]]",
        "homelab/14_FRAMEWORK.md": "doc",
        "homelab/sources/2026-10-18-x.md": "---\ntitle: \"X\"\n---\nraw",
        "claude/skills/forge-stack/SKILL.md": "skill",
        "homelab/tools/ignored.md": "not a node",
        "trade-memory/MEMORY.md": "- [Bot plan](a.md) — x\n",
        "trade-memory/a.md": "---\nname: a\n---\nsee [[b_c]] and STATUS.md",
        "trade/FIXES_PLAN.md": "read STATUS.md first; not docs/STATUS.md",
        "trade/STATUS.md": "status",
        "trade/bot.py": "code",
        "anpitek/boekhouding/.agents/skills/x.md": "hidden",
        "anpitek/website/OVERZICHT.md": "site",
    })
    ids = {n["id"]: n for n in g["nodes"]}
    assert "homelab/tools/ignored.md" not in ids and ids["memory/a.md"]["label"] == "Alpha memo"
    assert ids["memory/a.md"]["group"] == "feedback" and ids["homelab/sources/2026-10-18-x.md"]["label"] == "X"
    links = {(l["source"], l["target"]) for l in g["links"]}
    for t in ("memory/b_c.md", "homelab/14_FRAMEWORK.md", "claude/skills/forge-stack/SKILL.md", "homelab/sources/2026-10-18-x.md"):
        assert ("memory/a.md", t) in links, (t, links)
    assert ("memory/MEMORY.md", "memory/a.md") in links and ("memory/b_c.md", "memory/a.md") in links
    assert ids["trade-memory/a.md"]["label"] == "Bot plan" and ids["trade-memory/a.md"]["project"] == "trade"
    assert ids["trade-memory/MEMORY.md"]["group"] == "index" and ids["anpitek/website/OVERZICHT.md"]["project"] == "anpitek"
    assert "trade/bot.py" not in ids and "anpitek/boekhouding/.agents/skills/x.md" not in ids
    assert ids["claude/skills/forge-stack/SKILL.md"]["project"] == "shared"
    assert ("trade-memory/MEMORY.md", "trade-memory/a.md") in links and ("trade-memory/a.md", "memory/b_c.md") in links
    assert ("trade-memory/a.md", "trade/STATUS.md") in links and ("trade/FIXES_PLAN.md", "trade/STATUS.md") in links
    assert ("trade-memory/a.md", "memory/a.md") not in links
    two = clusters(["a", "b", "c", "x", "y", "z", "lone"], {("a", "b"), ("b", "c"), ("a", "c"), ("x", "y"), ("y", "z"), ("x", "z")},
                   {"a": 3, "b": 2, "c": 2, "x": 2, "y": 3, "z": 2}, {k: k.upper() for k in "abcxyz"} | {"lone": "L"})
    assert two["a"] == two["b"] == two["c"] == "A" and two["x"] == two["y"] == two["z"] == "Y" and two["lone"] == "", two  # same slug resolves inside its own project
    print("ok", len(g["nodes"]), "nodes", len(g["links"]), "links")
