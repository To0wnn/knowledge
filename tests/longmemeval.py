"""LongMemEval-S retrieval (R@5/R@10/NDCG@10, 500 questions) for the knowledge server's search.

One document per session = the user turns joined; recall_any at session level over all 500 questions.
Modes, same documents:
  minilm  all-MiniLM-L6-v2 on whole sessions (cut at 256 tokens): a well-known baseline to check the harness
  gemma   our search: vectors.chunks + "title: | text:" + embeddinggemma, a session ranks by its best chunk (server.py)
  qwen    gemma, then Qwen3-Reranker re-orders the top 10 (second stage; reordering inside the top 5 cannot change R@5)
Embeddings are cached per mode in $LME_DATA (default ~/.cache/longmemeval/), so a crash only loses the current batch.
Data: longmemeval_s_cleaned.json from https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
EMBED_URL=<llama-server /v1/embeddings> embeds on a GPU instead (use the ggml-org GGUF: some community GGUFs of
embeddinggemma lack its projection layers and give unrelated vectors); EMBED_NAME names that cache.
  uv run --no-project --with fastembed==0.9.0 --with onnxruntime==1.31.0 --with tokenizers==0.23.3 --with numpy \
    python lme_eval.py <mode> [rerank model dir]
"""
import hashlib, json, math, os, sys, time, urllib.request
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import vectors

DATA = Path(os.environ.get("LME_DATA", Path.home() / ".cache/longmemeval"))  # holds longmemeval_s_cleaned.json
mode = sys.argv[1]
items = json.loads((DATA / "longmemeval_s_cleaned.json").read_text())


def doc(session):
    return "\n".join(t["content"] for t in session if t["role"] == "user")


def ndcg(ranked_ids, gold, k):
    dcg = sum(1 / math.log2(i + 2) for i, c in enumerate(ranked_ids[:k]) if c in gold)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(gold), k)))
    return dcg / ideal if ideal else 0.0


GPU = os.environ.get("EMBED_URL", "")  # llama-server /v1/embeddings (ggml-org GGUF, cosine 0.9985 vs the ONNX on CPU)


def embedder():
    if GPU and mode != "minilm":
        from tokenizers import Tokenizer
        tok = Tokenizer.from_pretrained("onnx-community/embeddinggemma-300m-ONNX")

        def cut(s):  # fastembed truncates at 2048 tokens; llama-server refuses longer input instead
            ids = tok.encode(s, add_special_tokens=False).ids
            return s if len(ids) <= 2040 else tok.decode(ids[:2040])

        def embed(texts):
            texts = [cut(s) for s in texts]
            r = urllib.request.Request(GPU, json.dumps({"input": texts}).encode(), {"Content-Type": "application/json"})
            v = np.array([x["embedding"] for x in json.loads(urllib.request.urlopen(r, timeout=600).read())["data"]], dtype=np.float32)
            return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
        return embed
    from fastembed import TextEmbedding
    name = "sentence-transformers/all-MiniLM-L6-v2" if mode == "minilm" else vectors.MODEL
    m = TextEmbedding(name, threads=8)
    def embed(texts):
        # batch 4 as vectors.py: 16 long chunks went past 5 GB (OOM 28 Oct 2026)
        v = np.array(list(m.embed(texts, batch_size=4)), dtype=np.float32)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    return embed


def pieces(text, date):
    """The texts embedded for one session: whole (MiniLM, as ChromaDB) or our chunks with a title."""
    if mode == "minilm":
        return [text]
    return [f"title: chat session {date} | text: {c}" for _, c in vectors.chunks(text)] or [f"title: chat session {date} | text: "]


# 1. Embed every unique piece once (cached).
cache_file = DATA / f"emb_{'minilm' if mode == 'minilm' else os.environ.get('EMBED_NAME', 'gemmagpu') if GPU else 'gemma'}.npz"  # GPU vectors never mix with CPU ones
cache = {}
if cache_file.exists():
    with np.load(cache_file) as z:
        cache = dict(zip(z["keys"].tolist(), z["vecs"]))
todo = {}
for e in items:
    for s, date in zip(e["haystack_sessions"], e["haystack_dates"]):
        for p in pieces(doc(s), date):
            h = hashlib.sha1(p.encode()).hexdigest()
            if h not in cache:
                todo[h] = p
if todo:
    embed, keys, t0 = embedder(), list(todo), time.time()
    for i in range(0, len(keys), 256):
        batch = keys[i:i + 256]
        cache.update(zip(batch, embed([todo[h] for h in batch])))
        np.savez(cache_file, keys=np.array(list(cache)), vecs=np.stack(list(cache.values())))
        print(f"embedded {i + len(batch)}/{len(keys)} ({time.time() - t0:.0f} s)", flush=True)
embed_q = embedder()
prefix = "" if mode == "minilm" else vectors.QUERY_PREFIX

# 2. Optional second stage.
rr = None
if mode == "qwen":
    import rerank
    rr = rerank.Reranker(sys.argv[2])
    rr.load()
    rr.task = "Given a question about the user's past conversations, decide whether this chat session holds the answer"


def rr_score(question, text):
    e = rr.tok.encode(f"{rerank.PREFIX}<Instruct>: {rr.task}\n<Query>: {question}\n",
                      f"<Document>: {rr.cut(text, 560)}{rerank.SUFFIX}")
    ids = np.array([e.ids], dtype=np.int64)
    return float(rr.sess.run(None, {"input_ids": ids, "attention_mask": np.ones_like(ids)})[0][0, 0])


# 3. Rank the sessions of each question.
out, by_type, t0 = [], {}, time.time()
for n, e in enumerate(items):
    q = embed_q([prefix + e["question"]])[0]
    sids, texts, best = [], [], []
    for s, sid, date in zip(e["haystack_sessions"], e["haystack_session_ids"], e["haystack_dates"]):
        t = doc(s)
        if not t:
            continue
        vecs = np.stack([cache[hashlib.sha1(p.encode()).hexdigest()] for p in pieces(t, date)])
        sids.append(sid); texts.append(t); best.append(float((vecs @ q).max()))
    order = list(np.argsort(-np.array(best)))
    if rr:
        top = sorted(order[:10], key=lambda i: -rr_score(e["question"], texts[i]))
        order = top + order[10:]
    ranked = [sids[i] for i in order]
    gold = set(e["answer_session_ids"])
    row = {"id": e["question_id"], "type": e["question_type"], "r5": any(c in gold for c in ranked[:5]),
           "r10": any(c in gold for c in ranked[:10]), "ndcg10": ndcg(ranked, gold, 10)}
    out.append(row)
    by_type.setdefault(row["type"], []).append(row)
    if rr and n % 25 == 24:
        print(f"{n + 1}/500 R@5 so far {np.mean([r['r5'] for r in out]):.3f} ({time.time() - t0:.0f} s)", flush=True)

(DATA / f"results_{mode}{os.environ.get('EMBED_NAME', '')}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in out))
m = lambda rows, k: np.mean([r[k] for r in rows])
print(f"{mode}: R@5 {m(out, 'r5'):.3f}  R@10 {m(out, 'r10'):.3f}  NDCG@10 {m(out, 'ndcg10'):.3f}  (n={len(out)})")
for t, rows in sorted(by_type.items()):
    print(f"  {t:26} n={len(rows):3}  R@5 {m(rows, 'r5'):.3f}")

assert ndcg(["a", "b"], {"b"}, 10) == 1 / math.log2(3) and ndcg(["a"], {"x"}, 10) == 0.0
