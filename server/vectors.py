"""Semantic search: finds notes by meaning (synonyms, paraphrases, Dutch or English) where a regex finds nothing.

Markdown files are split into chunks of ~1200 characters on paragraph boundaries; each chunk is embedded with a local
model (embeddinggemma-300m through fastembed/ONNX, no GPU, no external service; best of four models in a test on the
real memos, 18 Oct). Vectors live in RAM as one normalized matrix, so a query is one matrix product (~1 ms for a few
thousand chunks) plus embedding the query (~50 ms). They are cached on disk by chunk hash (saved every few hundred
chunks), so a restart or an edit only embeds new text. One background thread at nice 10 with 6 CPU threads: the
kernel serves every other forge service first. Measured on forge (18 Oct): 6 threads 5.5 chunks/s (full run ~13 min),
2 threads 3.2/s; the Intel UHD 770 iGPU via OpenVINO only 1.2/s (and out of memory at batch 16), so no GPU.
"""
import hashlib
import os
import re
import threading
import time
from pathlib import Path

import numpy as np

MODEL = os.environ.get("KNOWLEDGE_EMBED_MODEL", "google/embeddinggemma-300m")  # "hash" = tiny test embedder
MODELS_DIR = os.environ.get("KNOWLEDGE_MODELS", "")
CACHE = Path(os.environ.get("KNOWLEDGE_VECTORS", Path.home() / ".local/state/knowledge-server/vectors.npz"))
QUERY_PREFIX = "task: search result | query: "  # embeddinggemma prompt format; documents get "title: ... | text: ..."
CHUNK_CHARS = 1200
# Raw sources (ingested pages, "<root>/sources/") rank this much lower than notes: the processed memo is the better
# answer when both match. Measured 25 Oct 2026 on 45 real questions: hit@5 0.80 -> 0.84 at 0.1 (0.82 at 0.02-0.05);
# the same penalty for Markdown in code folders and skills changed nothing, so they are left alone.
SOURCE_PENALTY = 0.1
MAX_FILE = 300_000  # bigger files (generated logs such as trade/claude_insights.md) stay regex-only
THREADS = int(os.environ.get("KNOWLEDGE_EMBED_THREADS", "6"))


def title_of(rel: str, text: str) -> str:
    """Context for every chunk of a file: frontmatter name + description, else the first heading, else the file name.
    Without it a chunk from the middle of a memo does not know what the memo is about (live test 18 Oct: 8/14 in the
    top 3 without titles)."""
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    fm = dict(re.findall(r"^(name|description|title):[ \t]*(.+)$", m.group(1), re.M)) if m else {}
    if fm.get("description") or fm.get("title"):
        return f"{fm.get('name') or fm.get('title')}: {fm.get('description', '')}".strip(" :")[:300]
    h = re.search(r"^#{1,2} (.+)$", text, re.M)
    return (h.group(1) if h else rel.split("/")[-1][:-3].replace("_", " "))[:300]


def chunks(text: str) -> list[tuple[int, str]]:
    """(start line, chunk text): paragraphs packed up to CHUNK_CHARS; a longer paragraph becomes its own chunk(s)."""
    out, buf, start, line = [], "", 1, 1
    for para in re.split(r"(\n\s*\n)", text):
        if para.strip() and buf and len(buf) + len(para) > CHUNK_CHARS:
            out.append((start, buf.strip()))
            buf, start = "", line
        if not buf.strip():
            start = line
        buf += para
        line += para.count("\n")
    if buf.strip():
        out.append((start, buf.strip()))
    return [(ln, c[i:i + 2 * CHUNK_CHARS]) for ln, c in out for i in range(0, len(c), 2 * CHUNK_CHARS)]


class HashEmbedder:
    """Deterministic bag-of-words vectors, only for the self-tests (the real model is 1.2 GB)."""

    def embed(self, texts, batch_size=4):
        for t in texts:
            v = np.zeros(256, dtype=np.float32)
            for w in re.findall(r"\w+", t.lower().split("| text: ")[-1].split("| query: ")[-1]):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1
            yield v


class Vectors:
    def __init__(self, index):
        self.index = index
        self.model = None
        self.cache: dict[str, np.ndarray] = {}  # sha1 of chunk text -> unit vector
        self.rows: list[tuple[str, int, str]] = []  # (rel, line, chunk) per matrix row
        self.matrix = None
        self.bias = None  # per row: what search subtracts from the score (SOURCE_PENALTY for raw sources)
        self.doc_ids: list[str] = []
        self.doc_matrix = None  # mean vector per file, for "similar notes"
        self.pending = 0
        self.seen_gen = -1
        self.lock = threading.Lock()
        self.error = ""

    # ------------------------------------------------------------------ background work
    def load(self):
        if CACHE.exists():
            try:
                with np.load(CACHE) as z:
                    self.cache = dict(zip(z["keys"].tolist(), z["vecs"]))
            except Exception as e:  # a broken cache is only a slower start
                print(f"vector cache unreadable, rebuilding: {e}", flush=True)
        if MODEL == "hash":
            self.model = HashEmbedder()
        else:
            from fastembed import TextEmbedding
            self.model = TextEmbedding(MODEL, threads=THREADS, cache_dir=MODELS_DIR or None)

    def embed(self, texts: list[str]) -> np.ndarray:
        v = np.array(list(self.model.embed(texts, batch_size=4)), dtype=np.float32)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)

    def loop(self):
        try:
            os.nice(10)  # embedding yields to everything else on forge
            self.load()
        except Exception as e:
            self.error = f"embedding model unavailable: {e}"
            print(self.error, flush=True)
            return
        while True:
            try:
                if self.index.gen != self.seen_gen:
                    self.sync()
            except Exception as e:  # never let it die; retried on the next change
                self.error = str(e)
                print(f"vector sync failed: {e}", flush=True)
            time.sleep(10)

    def sync(self):
        gen = self.index.gen
        wanted: list[tuple[str, int, str, str]] = []
        texts: dict[str, str] = {}  # hash -> exact text embedded (title + chunk), so a new title re-embeds the chunk
        for rel, text, _ in self.index.items():
            if rel.endswith(".md") and len(text) <= MAX_FILE:
                title = title_of(rel, text)
                for line, chunk in chunks(text):
                    doc = f"title: {title} | text: {chunk}"
                    h = hashlib.sha1(doc.encode()).hexdigest()
                    texts[h] = doc
                    wanted.append((rel, line, chunk, h))
        todo = list(dict.fromkeys(h for *_, h in wanted if h not in self.cache))
        self.pending = len(todo)
        for i in range(0, len(todo), 32):  # publish progress between batches
            batch = todo[i:i + 32]
            for h, v in zip(batch, self.embed([texts[h] for h in batch])):
                self.cache[h] = v
            self.pending = len(todo) - i - len(batch)
            if i % 640 == 0:  # make finished chunks searchable and keep them across a restart
                self.publish(wanted)
                self.save()
        live = {h for *_, h in wanted}
        changed = bool(todo) or len(self.cache) != len(live)
        self.cache = {h: v for h, v in self.cache.items() if h in live}
        self.publish(wanted)
        self.seen_gen = gen
        if changed:
            self.save()

    def publish(self, wanted):
        rows = [(rel, line, chunk) for rel, line, chunk, h in wanted if h in self.cache]
        if not rows:
            return
        matrix = np.stack([self.cache[h] for *_, h in wanted if h in self.cache])
        docs: dict[str, list[int]] = {}
        for i, (rel, _, _) in enumerate(rows):
            docs.setdefault(rel, []).append(i)
        doc_ids = list(docs)
        dm = np.stack([matrix[idx].mean(axis=0) for idx in docs.values()])
        dm /= np.maximum(np.linalg.norm(dm, axis=1, keepdims=True), 1e-9)
        bias = np.array([SOURCE_PENALTY if "/sources/" in f"/{rel}" else 0.0 for rel, _, _ in rows], dtype=matrix.dtype)
        with self.lock:
            self.rows, self.matrix, self.doc_ids, self.doc_matrix, self.bias = rows, matrix, doc_ids, dm, bias

    def save(self):
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp.npz")
        keys = list(self.cache)
        np.savez(tmp, keys=np.array(keys), vecs=np.stack([self.cache[k] for k in keys]) if keys else np.zeros((0, 1)))
        os.replace(tmp, CACHE)

    # ------------------------------------------------------------------ queries
    def status(self) -> str:
        if self.error and self.matrix is None:
            return self.error
        if self.matrix is None:
            return "semantic index is being built for the first time; use knowledge_search meanwhile"
        return f"{len(self.rows)} chunks" + (f", {self.pending} still being embedded" if self.pending else "")

    def search(self, query: str, roots: tuple[str, ...] = (), limit: int = 10, skip=None) -> list[dict]:
        with self.lock:
            rows, matrix, bias = self.rows, self.matrix, self.bias
        if matrix is None:
            return []
        scores = matrix @ self.embed([QUERY_PREFIX + query])[0] - bias
        out, per_file = [], {}
        for i in np.argsort(-scores):
            rel, line, chunk = rows[i]
            if roots and not rel.startswith(roots) or skip and skip(rel, line):
                continue
            if per_file.get(rel, 0) >= 2:  # at most two passages per file, so one long file cannot fill the list
                continue
            per_file[rel] = per_file.get(rel, 0) + 1
            out.append({"path": rel, "line": line, "score": round(float(scores[i]), 3), "text": chunk[:300]})
            if len(out) >= limit:
                break
        return out

    def similar(self, rel: str, limit: int = 30) -> dict[str, float]:
        """Files whose overall meaning is closest to `rel` (cosine of the mean chunk vectors)."""
        with self.lock:
            ids, dm = self.doc_ids, self.doc_matrix
        if dm is None or rel not in ids:
            return {}
        sims = dm @ dm[ids.index(rel)]
        return {ids[i]: round(float(sims[i]), 3) for i in np.argsort(-sims)[1:limit + 1]}


if __name__ == "__main__":  # self-check of the chunker and the search ranking with the test embedder
    text = "---\nname: x\n---\n\nIntro line.\n\n" + "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(8))
    cs = chunks(text)
    assert cs[0][0] == 1 and all(len(c) <= 2 * CHUNK_CHARS for _, c in cs) and len(cs) >= 3, cs
    assert text.splitlines()[cs[1][0] - 1].startswith("Paragraph"), cs[1]
    assert title_of("memory/x.md", "---\nname: wolf\ndescription: games per kind\n---\nbody") == "wolf: games per kind"
    assert title_of("trade/A_B.md", "intro\n# Kelly sizing\ntext") == "Kelly sizing" and title_of("trade/A_B.md", "x") == "A B"

    class FakeIndex:
        gen = 1
        def items(self):
            return [("memory/a.md", "the boiler heats water for the house", None),
                    ("homelab/sources/s.md", "boiler water boiler water", None),
                    ("memory/b.md", "the printer prints paper", None), ("trade/c.md", "boiler water again", None)]
    import tempfile
    CACHE = Path(tempfile.mkdtemp()) / "vectors.npz"  # module global: keep the self-check away from the real cache
    v = Vectors(FakeIndex())
    v.model = HashEmbedder()
    v.sync()
    hits = v.search("boiler water", roots=("memory/",))
    assert hits[0]["path"] == "memory/a.md" and all(h["path"].startswith("memory/") for h in hits), hits
    assert list(v.similar("memory/a.md"))[0] in ("trade/c.md", "homelab/sources/s.md"), v.similar("memory/a.md")
    src = next(h for h in v.search("boiler water") if h["path"] == "homelab/sources/s.md")
    raw = float(v.matrix[[r[0] for r in v.rows].index("homelab/sources/s.md")] @ v.embed([QUERY_PREFIX + "boiler water"])[0])
    assert abs(src["score"] - (raw - SOURCE_PENALTY)) < 1e-3, (src, raw)  # a raw source ranks lower by the penalty
    print("ok", len(cs), "chunks")
