"""Second opinion on "do these two memos cover the same subject?": Qwen3-Reranker-0.6B as int8 ONNX on the CPU.

The similar-memo hint (cosine of embeddings) finds candidates; this model judges each pair and lint reports the pairs it
calls the same (possible_duplicate). Measured 28 Oct 2026 on 94 real memo pairs (42 merged in practice): AUC 0.941
(cosine 0.848); cosine >= 0.68 then score >= 0.5 gave precision 0.94 / recall 0.79. ~1.75 s per pair on 4 threads and
~1.1 GB while loaded, so it runs in a background thread at nice 10, loads only when there is work and unloads after.

Off unless KNOWLEDGE_RERANK_MODEL names a folder with model.onnx and tokenizer.json, made by export_reranker.sh
(Apache-2.0 model; ~600 MB, not in git). "test" = a word-overlap stand-in for the tests.
"""
import gc
import math
import os
import re

MODEL_DIR = os.environ.get("KNOWLEDGE_RERANK_MODEL", "")
THREADS = int(os.environ.get("KNOWLEDGE_RERANK_THREADS", "4"))
THRESHOLD = 0.5  # admin setting rerank_threshold
CANDIDATE = 0.68  # cosine gate: below it the pair is not judged (kept 38 of 42 real pairs)

# The model card's template; the instruction is worth +0.033 AUC (measured without it: 0.908).
PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct '
          'provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
TASK = "Given a new note, decide whether the existing note covers the same subject, so the new note should be merged into it"
FRONT = re.compile(r"\A﻿?---\n.*?\n---\n", re.S)


def side(title: str, text: str, n: int) -> str:
    """What the model sees of a memo: its title line and the body without frontmatter and section names."""
    body = re.sub(r"^## (Stand|Keuzes|Status|Decisions)\s*$", "", FRONT.sub("", text, count=1), flags=re.M).strip()
    return f"{title}\n{body[:n]}"


class Reranker:
    def __init__(self, model_dir: str = MODEL_DIR):
        self.dir, self.sess, self.tok = model_dir, None, None

    def load(self):
        if self.sess or self.dir == "test":
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer
        so = ort.SessionOptions()
        so.intra_op_num_threads = THREADS
        so.add_session_config_entry("session.disable_prepacking", "1")  # peak 1.06 instead of 1.30 GB, ~3% slower
        self.tok = Tokenizer.from_file(os.path.join(self.dir, "tokenizer.json"))
        self.tok.no_truncation()
        self.tok.no_padding()
        self.sess = ort.InferenceSession(os.path.join(self.dir, "model.onnx"), so, providers=["CPUExecutionProvider"])

    def unload(self):
        self.sess = self.tok = None
        gc.collect()

    def cut(self, text: str, n: int) -> str:
        # Cut each side by tokens before templating: truncating the whole input drops SUFFIX, and without it the
        # model scores at random (AUC 0.55 instead of 0.95).
        return self.tok.decode(self.tok.encode(text, add_special_tokens=False).ids[:n], skip_special_tokens=False)

    def score(self, query: str, doc: str) -> float:
        """P(yes): the existing note `doc` covers the subject of the new note `query`."""
        if self.dir == "test":
            a, b = set(re.findall(r"\w+", query.lower())), set(re.findall(r"\w+", doc.lower()))
            return len(a & b) / max(len(a | b), 1)
        import numpy as np
        e = self.tok.encode(f"{PREFIX}<Instruct>: {TASK}\n<Query>: {self.cut(query, 350)}\n",
                            f"<Document>: {self.cut(doc, 560)}{SUFFIX}")
        ids = np.array([e.ids], dtype=np.int64)
        logit = float(self.sess.run(None, {"input_ids": ids, "attention_mask": np.ones_like(ids)})[0][0, 0])
        return 1 / (1 + math.exp(-logit))  # the export's single logit is yes minus no


if __name__ == "__main__":  # self-check without a model
    assert side("t: d", "---\nname: t\n---\n\n## Stand\n\nbody\n\n## Keuzes\n", 100) == "t: d\nbody"
    r = Reranker("test")
    assert r.score("ups shutdown logic", "the ups shutdown logic") > r.score("ups shutdown", "garden hose") == 0
    print("ok")
