"""Build the "brain" graph for the web UI from the in-memory index: one node per memo, doc, skill and raw source,
edges for wikilinks, markdown links, doc/skill references and cited sources. On top of it: Louvain topic communities,
graph insights (knowledge gaps for lint) and a relevance model for related notes.
Pure functions over {rel: text}, tens of ms."""
import math
import re

import networkx as nx

from lint import FRONT, frontmatter, links as wikilinks

MD_LINK = re.compile(r"\]\(([^)#\s]+\.md)\)")
SKILL_REF = re.compile(r"\bskill[s]? `([a-z0-9][a-z0-9-]*)`", re.I)
NOT_DOCS = {"CLAUDE.md", "README.md"}
RULES = "claude/house_rules.md"  # the shared house rules: a node on its frontmatter topic, like a doc
BARE_MD = re.compile(r"(?<![\w/.-])([A-Za-z0-9][\w.-]*\.md)\b")  # "see ENTRY_TIMING_FIXES.md" in the same project
# A path to a shared note: "~/anpitek/klant-gema/VERKOOP.md", "/home/x/homelab/netwerk.md" or "anpitek/website/X.md".
PATH_MD = re.compile(r"(?<![\w.-])((?:~/|/home/[\w.-]+/)?[\w.-]+/[\w./-]*\.md)\b")
SOURCE_REF = re.compile(r"\b(20\d\d-\d\d-\d\d-[a-z0-9-]+\.md)\b")
FM_SOURCES = re.compile(r"^sources:[ \t]*(\[[^\]\n]*\]|(?:\n[ \t]+-[^\n]*)+)", re.M)
# project -> (docs root, memory root). Docs roots other than homelab are code repos: only their Markdown is shared.
PROJECTS = {"homelab": ("homelab", "memory"), "anpitek": ("anpitek", "anpitek-memory"), "trade": ("trade", "trade-memory")}
MEMORY_ROOTS = {mem for _, mem in PROJECTS.values()}
MD_ONLY = {docs for docs, _ in PROJECTS.values()} - {"homelab"}


def project_of(rel: str) -> str:
    root = rel.split("/", 1)[0]
    return next((p for p, roots in list(PROJECTS.items()) if root in roots), "shared")  # copy: the UI can add projects


def mem_root(rel: str) -> str | None:
    root = rel.split("/", 1)[0]
    return root if root in MEMORY_ROOTS and rel.count("/") == 1 else None


def is_doc(rel: str) -> bool:
    """A homelab doc: a Markdown file at the top of homelab/ (sources/ and archief/ are below it)."""
    parts = rel.split("/")
    return parts[0] == "homelab" and len(parts) == 2 and rel.endswith(".md") and parts[1] not in NOT_DOCS


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
    if not mem_root(rel) and fm.get("title"):
        return fm["title"].strip('"')[:60]
    if not mem_root(rel) and (h1 := re.search(r"^# +(.+?)\s*#*$", FRONT.sub("", text, count=1), re.M)):
        return re.sub(r"[*_`]", "", h1.group(1))[:60]  # a doc's own heading reads better than "BRONBEWAKING"
    return name[:-3].replace("_", " ")


def desc_of(rel: str, text: str) -> str:
    """One line about a note for the overview: its frontmatter description, else (docs) the first paragraph."""
    if d := frontmatter(text).get("description", "").strip('"'):
        return d[:140]
    if mem_root(rel) or rel.startswith("claude/skills/"):
        return ""
    for para in re.split(r"\n\s*\n", FRONT.sub("", text, count=1)):
        line = " ".join(para.split())
        if line and not line.startswith(("#", "|", "```", "<", "---", "- ", "* ", ">")):
            return re.sub(r"[*_`]|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", line)[:140]
    return ""


def sources_of(text: str) -> list[str]:
    """Raw sources a note is based on: the frontmatter `sources:` list (inline [a, b] or YAML lines) plus source file
    names cited in the text."""
    found = []
    fm = FRONT.match(text)
    m = FM_SOURCES.search(fm.group(1)) if fm else None
    if m:
        found = [x.strip().strip("'\"").split("/")[-1] for x in re.split(r"[,\n]", m.group(1).strip("[]")) if x.strip(" -\t")]
        found = [x.lstrip("- ").strip().strip("'\"") for x in found]
    return sorted({x for x in found if x} | set(SOURCE_REF.findall(text)))


def build(files: dict[str, str]) -> dict:
    keep = {}
    for rel, text in files.items():
        parts = rel.split("/")
        if mem_root(rel) \
                or (parts[0] in MD_ONLY and rel.endswith(".md") and len(parts) <= 3 and not any(x.startswith(".") for x in parts)) \
                or (rel.startswith("homelab/") and len(parts) == 2 and rel.endswith(".md") and parts[1] not in NOT_DOCS) \
                or (rel.startswith("homelab/sources/") and parts[-1] != "LOG.md") \
                or (rel.startswith("claude/skills/") and parts[-1] == "SKILL.md" and len(parts) == 4) or rel == RULES:
            keep[rel] = text
    titles, sections = {}, {}
    for mem in sorted(MEMORY_ROOTS):
        section = ""
        for line in files.get(f"{mem}/MEMORY.md", "").splitlines():
            if line.startswith("## "):
                section = line[3:].strip()
            elif m := re.match(r"- \[([^\]]+)\]\(([^)]+\.md)\)", line):
                titles[f"{mem}/{m.group(2)}"] = m.group(1)
                if section:
                    sections[f"{mem}/{m.group(2)}"] = section  # the user's own grouping of memos
    # A doc names its topic (a MEMORY.md heading) in its frontmatter, so it sits on that island: homelab's top-level
    # docs and any Markdown in another project's docs root (a guest's mirrored folder).
    for rel, text in keep.items():
        if (is_doc(rel) or rel == RULES or project_of(rel) not in ("homelab", "shared") and not mem_root(rel)) and (topic := frontmatter(text).get("topic", "").strip('"')):
            sections[rel] = topic
    # The same heading in two projects (e.g. "## General" of new UI projects) must stay two topics; a shared note
    # (the house rules) joins the heading it names.
    owners: dict[str, set] = {}
    for r, sec in sections.items():
        owners.setdefault(sec, set()).update({project_of(r)} - {"shared"})
    sections = {r: f"{sec} ({project_of(r)})" if len(owners[sec]) > 1 else sec for r, sec in sections.items()}
    # Projects without MEMORY.md headings: a doc's subfolder ("gema-truckwaardering"), or a file-name family of 3+
    # files ("FABLE_*", "OPUS_REVIEW_*") is its section.
    headed = {project_of(r) for r in sections}
    flat = [r for r in keep if project_of(r) not in headed | {"shared"} and not mem_root(r)]
    family = {}
    for r in flat:
        family.setdefault(r.split("/")[-1].split("_")[0], []).append(r)
    for r in flat:
        parts, head = r.split("/"), r.split("/")[-1].split("_")[0]
        if len(parts) >= 3:
            sections[r] = parts[1]
        elif "_" in parts[-1] and len(family[head]) >= 3:
            sections[r] = head.capitalize()
    by_slug = {}  # slug -> memo, and (memory root, slug) -> memo so a link prefers its own project's memo
    for rel, text in keep.items():
        if mem_root(rel):
            stem = rel.split("/")[-1][:-3]
            for key in (stem, frontmatter(text).get("name", "")):
                if key:
                    key = key.replace("-", "_")  # hyphen/underscore variants point to the same memo
                    by_slug[(mem_root(rel), key)] = rel
                    by_slug.setdefault(key, rel)
    docs = {rel.split("/")[-1]: rel for rel in keep if is_doc(rel) or rel == RULES}  # "netwerk.md" -> homelab/netwerk.md
    skills = {rel.split("/")[2]: rel for rel in keep if rel.startswith("claude/skills/")}
    sources = {rel.split("/")[-1]: rel for rel in keep if rel.startswith("homelab/sources/")}
    # Any mention of a skill's name counts ("`forge-stack`", "skill forge-stack"); names with a hyphen only,
    # so a one-word skill name does not match ordinary prose. Paths count too.
    names = sorted((n for n in skills if "-" in n), key=len, reverse=True)
    skill_names = re.compile(r"(?<![\w-])(" + "|".join(map(re.escape, names)) + r")(?![\w-])") if names else re.compile(r"(?!)")
    by_name: dict[tuple[str, str], str] = {}
    for rel in keep:
        by_name.setdefault((project_of(rel), rel.split("/")[-1]), rel)

    edges = set()
    for rel, text in keep.items():
        targets = set()
        home = mem_root(rel) or PROJECTS.get(project_of(rel), ("", ""))[1]
        for slug in wikilinks(text):  # outside code, already normalized
            targets.add(by_slug.get((home, slug)) or by_slug.get(slug))
        if mem_root(rel):
            targets |= {f"{mem_root(rel)}/{f.split('/')[-1]}" for f in MD_LINK.findall(text) if not f.startswith("http")}
        if project_of(rel) in ("homelab", "shared"):  # skills mention docs by file name too
            targets |= {docs.get(n) for n in BARE_MD.findall(text)}
        targets |= {skills.get(s) for s in SKILL_REF.findall(text)}
        targets |= {skills.get(s) for s in skill_names.findall(text)}
        targets |= {sources.get(s) for s in SOURCE_REF.findall(text)}
        targets |= {by_name.get((project_of(rel), n)) for n in BARE_MD.findall(text)}
        targets |= {re.sub(r"^(?:~/|/home/[\w.-]+/)", "", p) for p in PATH_MD.findall(text)}
        for t in targets:
            if t and t != rel and t in keep:
                edges.add((rel, t))
    # MEMORY.md links to every memo: a star that only adds clutter. The index stays readable and searchable.
    edges = {(a, b) for a, b in edges if not a.endswith("/MEMORY.md") and not b.endswith("/MEMORY.md")}
    keep = {rel: text for rel, text in keep.items() if not rel.endswith("/MEMORY.md")}
    degree: dict[str, int] = {}
    for a, b in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    labels = {rel: label_of(rel, text, titles) for rel, text in keep.items()}
    groups = {rel: group_of(rel, text) for rel, text in keep.items()}
    comm, communities = louvain(sorted(keep), edges, degree, labels, sections)
    # Shared notes (skills) show only in the projects that link to them; unlinked ones in homelab, where lint flags them.
    linked = {}
    for a, b in edges:
        linked.setdefault(a, set()).add(project_of(b)); linked.setdefault(b, set()).add(project_of(a))
    def shown_in(rel):
        if project_of(rel) != "shared":
            return project_of(rel)
        ps = linked.get(rel, set()) - {"shared"}
        return ps.pop() if len(ps) == 1 else "shared" if ps else "homelab"
    nodes = [{"id": rel, "label": labels[rel], "group": groups[rel], "project": shown_in(rel),
              **({"projects": sorted(linked[rel] - {"shared"})} if shown_in(rel) == "shared" else {}),
              "cluster": comm[rel], "size": len(text), "degree": degree.get(rel, 0), "sources": sources_of(text),
              **({"desc": d} if (d := desc_of(rel, text)) else {}),
              # [[slug]] links may use the frontmatter name; the UI needs it when it differs from the file name
              **({"name": nm} if mem_root(rel) and (nm := frontmatter(text).get("name", "")) and nm != rel.split("/")[-1][:-3] else {})}
             for rel, text in sorted(keep.items())]
    graph = {"nodes": nodes, "links": [{"source": a, "target": b} for a, b in sorted(edges)], "communities": communities}
    graph["insights"] = insights(graph)
    return graph


def topic_graph(ids, edges) -> nx.Graph:
    """Undirected graph without the MEMORY.md index files: they link to every memo and would merge all topics."""
    g = nx.Graph()
    g.add_nodes_from(i for i in ids if not i.endswith("/MEMORY.md"))
    g.add_edges_from((a, b) for a, b in edges if a in g and b in g)
    return g


def louvain(ids, edges, degree, labels, sections=None) -> tuple[dict[str, str], list[dict]]:
    """Topics, scored by cohesion = internal edges / possible internal edges.
    1. A note with a section takes it as its topic: its MEMORY.md heading (the user's own grouping), or for docs in
       projects without headings their subfolder or file-name family (see build).
    2. Other notes (and shared skills) join the topic most of their linked notes are in, repeated so chains settle.
    3. What is still open forms Louvain communities (fixed seed) named after their best-connected member;
       singletons there get no topic."""
    sections = sections or {}
    g = topic_graph(ids, edges)
    comm = {i: sections.get(i, "") for i in ids}
    same = lambda i, j: project_of(i) == "shared" or project_of(i) == project_of(j)
    for _ in range(3):
        for i in sorted((i for i in g if i not in sections), key=lambda i: (-degree.get(i, 0), i)):
            near = [comm[j] for j in g[i] if comm[j] and same(i, j)]
            if near:
                comm[i] = max(set(near), key=lambda c: (near.count(c), c))
    rest = [i for i in g if not comm[i]]
    for members in nx.community.louvain_communities(g.subgraph(rest), seed=42):
        if len(members) >= 2:
            top = labels[max(members, key=lambda i: (degree.get(i, 0), i))]
            for i in members:
                comm[i] = top
    out = []
    for name in sorted({c for c in comm.values() if c}):
        members = [i for i in g if comm[i] == name]
        n, inner = len(members), g.subgraph(members).number_of_edges()
        hub = max(members, key=lambda i: (degree.get(i, 0), i))
        out.append({"name": name, "size": n, "cohesion": round(inner / (n * (n - 1) / 2), 3) if n > 1 else 0.0,
                    "project": project_of(hub), "kind": "section" if name in set(sections.values()) else "links"})
    return comm, sorted(out, key=lambda c: (-c["size"], c["name"]))


def insights(graph: dict) -> dict[str, list[dict]]:
    """Knowledge gaps from the graph structure, reported by lint: isolated notes (no links; the MEMORY.md index is not
    counted) and sparse topics (found from links, not a user's section: cohesion < 0.15, 3+ notes)."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    g = topic_graph(nodes, [(l["source"], l["target"]) for l in graph["links"]])
    isolated = [{"id": i, "project": nodes[i]["project"], "degree": g.degree(i)} for i in sorted(g) if g.degree(i) == 0
                and nodes[i]["group"] not in ("skill",)]
    sparse = [c for c in graph["communities"] if c["size"] >= 3 and c["cohesion"] < 0.15 and c.get("kind") != "section"]
    return {"isolated": isolated, "sparse_topics": sparse}


def related(graph: dict, rel: str, limit: int = 10, semantic: dict[str, float] | None = None) -> list[dict]:
    """Notes most related to `rel`, by a relevance model with 4 graph signals, plus meaning when vectors are ready:
    direct link x3, shared raw source x4 (per source), Adamic-Adar (shared neighbours, weighted by their rarity) x1.5,
    same type x1, and semantic similarity x3 (cosine of the note vectors, above 0.3)."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    if rel not in nodes:
        return []
    g = topic_graph(nodes, [(l["source"], l["target"]) for l in graph["links"]])
    me = nodes[rel]
    cands = set()
    if rel in g:
        for j in g[rel]:
            cands.add(j)
            cands |= set(g[j])
    mine = set(me["sources"])
    cands |= {i for i, n in nodes.items() if mine & set(n["sources"])}
    cands |= set(semantic or {})
    cands.discard(rel)
    out = []
    for c in cands:
        if c not in nodes:
            continue
        why, score = [], 0.0
        if rel in g and c in g and g.has_edge(rel, c):
            score += 3.0
            why.append("linked")
        shared = mine & set(nodes[c]["sources"])
        if shared:
            score += 4.0 * len(shared)
            why.append(f"same source ({len(shared)})")
        if rel in g and c in g:
            aa = sum(1 / math.log(g.degree(z)) for z in nx.common_neighbors(g, rel, c) if g.degree(z) > 1)
            if aa:
                score += 1.5 * aa
                why.append("shared neighbours")
        if nodes[c]["group"] == me["group"]:
            score += 1.0
        sim = (semantic or {}).get(c, 0.0)
        if sim > 0.3:
            score += 3.0 * sim
            why.append(f"similar meaning ({sim:.2f})")
        if why:
            out.append({"id": c, "label": nodes[c]["label"], "project": nodes[c]["project"], "score": round(score, 2),
                        "why": ", ".join(why)})
    return sorted(out, key=lambda r: (-r["score"], r["id"]))[:limit]


if __name__ == "__main__":
    g = build({
        "memory/MEMORY.md": "## Werk\n- [Alpha memo](a.md) — x\n",
        "memory/a.md": "---\nname: a\nmetadata:\n  type: feedback\n---\nsee [[b_c]], `framework.md`, skill `forge-stack`, 2026-10-18-x.md",
        "memory/b_c.md": "---\nname: b-c\n---\nback to [[a]]",
        "homelab/framework.md": "---\ntitle: \"Framework-laptop\"\ntopic: \"Werk\"\n---\ndoc",
        "homelab/CLAUDE.md": "instructions, not a doc",
        "homelab/sources/2026-10-18-x.md": "---\ntitle: \"X\"\n---\nraw",
        "claude/skills/forge-stack/SKILL.md": "skill",
        "claude/house_rules.md": "---\ntitle: House rules\ntopic: Werk\n---\n1. Search first. Background: [[b_c]]",
        "homelab/tools/ignored.md": "not a node",
        "trade-memory/MEMORY.md": "- [Bot plan](a.md) — x\n",
        "trade-memory/a.md": "---\nname: a\n---\nsee [[b_c]] and STATUS.md",
        "trade/FIXES_PLAN.md": "read STATUS.md first; not docs/STATUS.md",
        "trade/STATUS.md": "# Trading **status**\n\nWhere the bot stands.\n\n## More\n",
        "trade/NOTES.md": "---\ntopic: Bot\n---\nnotes",
        "trade/bot.py": "code",
        "anpitek/boekhouding/.agents/skills/x.md": "hidden",
        "anpitek/website/OVERZICHT.md": "site",
        "anpitek-memory/k.md": "---\nname: k\n---\nprice in `~/anpitek/website/OVERZICHT.md`",
    })
    ids = {n["id"]: n for n in g["nodes"]}
    assert "homelab/tools/ignored.md" not in ids and ids["memory/a.md"]["label"] == "Alpha memo"
    assert ids["memory/a.md"]["group"] == "feedback" and ids["homelab/sources/2026-10-18-x.md"]["label"] == "X"
    doc = ids["homelab/framework.md"]  # a doc: its frontmatter title and topic, on the island of that heading
    assert doc["label"] == "Framework-laptop" and doc["cluster"] == "Werk" == ids["memory/a.md"]["cluster"], doc
    assert "homelab/CLAUDE.md" not in ids
    hr = ids["claude/house_rules.md"]  # the house rules: their own node on their topic, linked to what they name
    assert hr["label"] == "House rules" and hr["cluster"] == "Werk" and hr["project"] == "homelab", hr
    links = {(l["source"], l["target"]) for l in g["links"]}
    for t in ("memory/b_c.md", "homelab/framework.md", "claude/skills/forge-stack/SKILL.md", "homelab/sources/2026-10-18-x.md"):
        assert ("memory/a.md", t) in links, (t, links)
    assert ("memory/b_c.md", "memory/a.md") in links and "memory/MEMORY.md" not in ids  # the index is not drawn
    assert ids["memory/b_c.md"]["name"] == "b-c" and "name" not in ids["memory/a.md"]  # only when it differs
    assert ids["trade-memory/a.md"]["label"] == "Bot plan" and ids["trade-memory/a.md"]["project"] == "trade"
    assert ids["anpitek/website/OVERZICHT.md"]["project"] == "anpitek"
    assert ("anpitek-memory/k.md", "anpitek/website/OVERZICHT.md") in links  # a path mention is a link
    assert ids["trade/NOTES.md"]["cluster"] == "Bot"  # any project's doc sits on its frontmatter topic
    assert ids["trade/STATUS.md"]["label"] == "Trading status" and ids["trade/STATUS.md"]["desc"] == "Where the bot stands.", ids["trade/STATUS.md"]
    assert "desc" not in ids["memory/a.md"]  # a memo without description gets none
    assert "trade/bot.py" not in ids and "anpitek/boekhouding/.agents/skills/x.md" not in ids
    assert ids["claude/skills/forge-stack/SKILL.md"]["project"] == "homelab"  # linked from homelab only
    s3 = build({"memory/a.md": "skills in ~/.claude/skills/: `forge-stack`, wolf-spel", "trade/X.md": "use forge-stack",
                "claude/skills/forge-stack/SKILL.md": "s", "claude/skills/wolf-spel/SKILL.md": "s",
                "claude/skills/lonely-one/SKILL.md": "s", "memory/b.md": "not-wolf-spelx, forge-stacks"})
    p3 = {n["id"]: n["project"] for n in s3["nodes"]}
    l3 = {(l["source"], l["target"]) for l in s3["links"]}
    assert ("memory/a.md", "claude/skills/wolf-spel/SKILL.md") in l3 and ("trade/X.md", "claude/skills/forge-stack/SKILL.md") in l3, l3
    assert not any(a == "memory/b.md" for a, _ in l3), l3  # no partial-name matches
    assert [n.get("projects") for n in s3["nodes"] if n["id"] == "claude/skills/forge-stack/SKILL.md"] == [["homelab", "trade"]]
    assert p3["claude/skills/forge-stack/SKILL.md"] == "shared" and p3["claude/skills/wolf-spel/SKILL.md"] == "homelab"
    assert p3["claude/skills/lonely-one/SKILL.md"] == "homelab", p3
    assert ("trade-memory/a.md", "memory/b_c.md") in links
    assert ("trade-memory/a.md", "trade/STATUS.md") in links and ("trade/FIXES_PLAN.md", "trade/STATUS.md") in links
    assert ("trade-memory/a.md", "memory/a.md") not in links
    s4 = build({"memory/MEMORY.md": "## Netwerk\n- [A](a.md) — x\n- [B](b.md) — x\n## Huis\n- [C](c.md) — x\n",
                "memory/a.md": "[[b]] [[c]]", "memory/b.md": "[[a]]", "memory/c.md": "[[a]]", "memory/d.md": "[[a]]"})
    c4 = {n["id"]: n["cluster"] for n in s4["nodes"]}
    assert c4["memory/a.md"] == "Netwerk" and c4["memory/d.md"] == c4["memory/a.md"], c4  # section name; singleton adopted
    s5 = build({"trade/FABLE_A.md": "x", "trade/FABLE_B.md": "x", "trade/FABLE_C.md": "x", "trade/ONE_X.md": "y",
                "anpitek/website/X.md": "z", "trade-memory/m.md": "see FABLE_A.md"})
    c5 = {n["id"]: n["cluster"] for n in s5["nodes"]}
    assert c5["trade/FABLE_A.md"] == "Fable" and c5["trade/ONE_X.md"] == "" and c5["anpitek/website/X.md"] == "website", c5
    assert c5["trade-memory/m.md"] == "Fable", c5  # joins its linked notes
    comm, cs = louvain(["a", "b", "c", "x", "y", "z", "lone"], {("a", "b"), ("b", "c"), ("a", "c"), ("x", "y"), ("y", "z"), ("x", "z"), ("c", "x")},
                       {"a": 2, "b": 2, "c": 3, "x": 3, "y": 2, "z": 2}, {k: k.upper() for k in "abcxyz"} | {"lone": "L"})
    assert comm["a"] == comm["b"] == comm["c"] == "C" and comm["x"] == comm["y"] == comm["z"] == "X" and comm["lone"] == "", comm
    assert {c["name"]: c["cohesion"] for c in cs} == {"C": 1.0, "X": 1.0}, cs
    assert sources_of("---\nname: x\nsources: [2026-10-01-a.md, \"b.md\"]\n---\nsee 2026-10-02-c.md") == ["2026-10-01-a.md", "2026-10-02-c.md", "b.md"]
    assert sources_of("---\nsources:\n  - one.md\n  - two.md\n---\n") == ["one.md", "two.md"]
    s2 = build({"memory/m1.md": "---\nsources: [s.md]\n---\nx", "memory/m2.md": "---\nsources: [s.md]\n---\ny",
                "memory/m3.md": "z [[m1]]"})
    rel = {r["id"]: r for r in related(s2, "memory/m1.md")}
    assert rel["memory/m2.md"]["why"].startswith("same source") and rel["memory/m3.md"]["why"] == "linked", rel
    assert related(s2, "memory/m1.md", semantic={"memory/m2.md": 0.9})[0]["why"].endswith("similar meaning (0.90)")
    assert any(i["id"] == "memory/m2.md" for i in s2["insights"]["isolated"])  # same slug resolves inside its own project
    print("ok", len(g["nodes"]), "nodes", len(g["links"]), "links")
