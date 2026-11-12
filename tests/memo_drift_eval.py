#!/usr/bin/env python3
"""Measure whether a weekly "memo vs reality" pass would pay off, before building one.

For the newest real chats: find the memos each chat relied on or touched, ask one bare `claude -p` per chat (no tools)
for claims in those memos' "## Status" that the transcript shows to be wrong, outdated or incomplete, and keep only
findings whose transcript quote and memo line both occur literally. The labelled set lives outside the repo (private
data): OUT/<date>.json. Labels (real-stale, already-fixed, false, trivial) are set by a person or the lead agent in
that file; a re-run keeps them for findings it finds again and prints the counts.

  uv run --project server python tests/memo_drift_eval.py [--chats 25] [--out FILE]   fetch, extract, write the set
  uv run --project server python tests/memo_drift_eval.py --report FILE               counts of a labelled set
  uv run --project server python tests/memo_drift_eval.py --selftest

Needs KNOWLEDGE_URL and KNOWLEDGE_TOKEN (a read token is enough): it only reads chats and memos, never writes.
Transcripts go to Claude only (private data).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

OUT = Path.home() / ".knowledge/eval/memo_drift"
MODEL = "sonnet"
CHAT_CHARS = 60000
MEMOS_PER_CHAT = 4
TRIVIAL = re.compile(r"^(reply only|what was the code word|which two code words)", re.I)
LABELS = ("real-stale", "already-fixed", "false", "trivial")
SCHEMA = {"type": "object", "required": ["findings"], "properties": {"findings": {"type": "array", "items": {
    "type": "object", "required": ["memo", "memo_line", "quote", "problem"], "properties": {
        "memo": {"type": "string"}, "memo_line": {"type": "string"}, "quote": {"type": "string"},
        "problem": {"type": "string"}}}}}}
PROMPT = """Below is the transcript of one AI agent chat, then the CURRENT text of memos that chat used or edited.
Find claims in a memo's "## Status" section that the transcript shows to be wrong, outdated or incomplete NOW: a value,
path, version, state or behaviour the chat found to be different. Not wanted: things the memo already says, decisions,
style, wishes, open items that are still open, anything the transcript does not show. For each finding give:
- memo: its path as given below;
- memo_line: a verbatim fragment (20-200 characters) copied from that memo line, exactly as written;
- quote: a verbatim fragment (20-300 characters) copied from the transcript that shows the problem, exactly as written;
- problem: one sentence, what is wrong and what is true instead.
No finding is a fine answer; never invent or paraphrase a quote."""


def mcp(tool: str, args: dict) -> str:
    """One knowledge tool call (a session per call: a few dozen calls per run)."""
    url, token = os.environ["KNOWLEDGE_URL"].rstrip("/"), os.environ["KNOWLEDGE_TOKEN"]
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def post(body, sid=None):
        req = urllib.request.Request(f"{url}/mcp", json.dumps(body).encode(), dict(h, **({"mcp-session-id": sid} if sid else {})))
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.headers.get("mcp-session-id"), r.read().decode()
    sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "memo-drift-eval", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _, out = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}, sid)
    d = json.loads(([x[5:] for x in out.splitlines() if x.startswith("data:")] or [out])[-1])
    res = d.get("result") or {}
    if "error" in d or res.get("isError"):
        raise RuntimeError(f"{tool} failed: {(res.get('content') or [{}])[0].get('text') or d.get('error')}")
    text = res["content"][0]["text"]
    try:  # some tools wrap their text as {"result": "..."}
        return json.loads(text)["result"]
    except (ValueError, KeyError, TypeError):
        return text


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\\n", " ").replace('\\"', '"')).strip().lower()


def occurs(fragment: str, text: str) -> bool:
    """The quote check: a fragment counts only when it is long enough to mean something and occurs literally."""
    f = norm(fragment)
    return len(f) >= 15 and f in norm(text)


def memo_refs(text: str, project_root: str, roots: set[str]) -> list[str]:
    """Memo paths a transcript names, most mentioned first: 'root/name.md', or a bare memo file name (taken to be in
    the chat's own project memory). Index files are left out."""
    hits = Counter()
    for root, name in re.findall(r"(?<![\w./-])(?:([a-z0-9-]+)/)?((?:project|reference|feedback|user)_[\w-]+\.md|[\w-]+\.md)", text):
        if root in roots:
            hits[f"{root}/{name}"] += 1
        elif not root and re.match(r"(project|reference|feedback|user)_", name) and project_root:
            hits[f"{project_root}/{name}"] += 1
    return [p for p, _ in hits.most_common() if not p.endswith("/MEMORY.md")]


def read_memo(path: str) -> str | None:
    try:
        body = mcp("knowledge_read", {"path": path, "limit": 5000}).partition("\n")[2]
    except Exception:
        return None
    return "\n".join(x.split("\t", 1)[1] for x in body.split("\n") if re.match(r"^\d+\t", x))


def extract(transcript: str, memos: dict[str, str]) -> tuple[list[dict], dict]:
    body = PROMPT + "\n\n=== TRANSCRIPT ===\n" + transcript + "".join(f"\n\n=== MEMO {p} ===\n{t}" for p, t in memos.items())
    r = subprocess.run(["claude", "-p", "--model", MODEL, "--output-format", "json", "--max-turns", "3",
                        "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                        "--json-schema", json.dumps(SCHEMA)], input=body,  # stdin: one argv entry is capped at 128 KB
                       capture_output=True, text=True, timeout=900, cwd=OUT)
    out = json.loads(r.stdout or "{}")
    if r.returncode or out.get("is_error"):
        raise RuntimeError(f"claude -p failed (exit {r.returncode}): {(out.get('result') or r.stderr)[-300:]}")
    return (out.get("structured_output") or {}).get("findings") or [], {"usd": out.get("total_cost_usd") or 0, **(out.get("usage") or {})}


def fid(f: dict) -> str:
    return hashlib.sha1("|".join(norm(f[k]) for k in ("memo", "memo_line", "quote")).encode()).hexdigest()[:12]


def run(n: int, out_file: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    roots = dict(re.findall(r"^([\w-]+): docs [^,]*, memory ([\w-]+)/", mcp("knowledge_projects", {}), re.M))
    chats = [m.groups() for m in re.finditer(r"^(\w+): .+? in (\S+) on .+?, (\w+)(?:, closed)?: (.*)$", mcp("knowledge_chats", {"limit": 100}), re.M)]
    chats = [(cid, proj, title) for cid, proj, state, title in chats if state != "running" and not TRIVIAL.match(title)][:n]
    old = {f["id"]: f for f in (json.loads(out_file.read_text())["findings"] if out_file.exists() else [])}

    def one(chat):
        cid, proj, title = chat
        text = mcp("knowledge_chat_read", {"chat": cid, "chars": CHAT_CHARS})
        memos = {p: t for p in memo_refs(text, roots.get(proj, ""), set(roots.values()))[:MEMOS_PER_CHAT] if (t := read_memo(p))}
        if not memos:
            return {"chat": cid, "project": proj, "title": title, "chars": len(text), "memos": [], "candidates": [], "cost": {}}
        found, cost = extract(text, memos)
        for f in found:
            f |= {"chat": cid, "id": fid(f), "quote_ok": occurs(f["quote"], text) and f["memo"] in memos and occurs(f["memo_line"], memos[f["memo"]])}
            f["label"], f["note"] = old.get(f["id"], {}).get("label"), old.get(f["id"], {}).get("note", "")
        return {"chat": cid, "project": proj, "title": title, "chars": len(text), "memos": list(memos), "candidates": found, "cost": cost}

    with ThreadPoolExecutor(4) as pool:
        per_chat = list(pool.map(one, chats))
    findings = [f for c in per_chat for f in c.pop("candidates")]
    out_file.write_text(json.dumps({"date": date.today().isoformat(), "model": MODEL, "chats": per_chat, "findings": findings}, indent=1, ensure_ascii=False))
    print(f"wrote {out_file}")
    report(out_file)


def report(path: Path) -> None:
    d = json.loads(path.read_text())
    ok = [f for f in d["findings"] if f["quote_ok"]]
    labels = Counter(f.get("label") or "unlabelled" for f in ok)
    judged = sum(labels[x] for x in LABELS)
    usd = sum(c["cost"].get("usd", 0) for c in d["chats"])
    tok = sum(c["cost"].get(k, 0) for c in d["chats"] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
    print(f"chats {len(d['chats'])}, with memos {sum(1 for c in d['chats'] if c['memos'])}, pairs {sum(len(c['memos']) for c in d['chats'])}, "
          f"candidates {len(d['findings'])}, quote check passed {len(ok)}")
    print("labels:", dict(labels), f"precision {labels['real-stale'] / judged:.2f}" if judged else "precision n/a")
    print(f"extraction cost: {tok} tokens, ${usd:.2f}")


def selftest() -> None:
    t = 'see memory/project_ha.md and a350-memory/project_x.md; also project_ha.md twice, "MEMORY.md", homelab/netwerk.md'
    assert memo_refs(t, "memory", {"memory", "a350-memory"}) == ["memory/project_ha.md", "a350-memory/project_x.md"]
    assert memo_refs("feedback_y.md", "", {"memory"}) == []
    assert occurs("the dryer is  loaded now", "### Agent\nthe Dryer is loaded now (checked)")
    assert not occurs("short", "short") and not occurs("the dryer is unloaded", "the dryer is loaded now")
    assert occurs('old_string\\": \\"x y z w v u t', 'old_string": "x y z w v u t')
    a = {"memo": "m", "memo_line": "a", "quote": "b"}
    assert fid(a) == fid({"memo": "m", "memo_line": "a ", "quote": "B"})
    print("selftest ok")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
    elif "--report" in args:
        report(Path(args[args.index("--report") + 1]))
    else:
        n = int(args[args.index("--chats") + 1]) if "--chats" in args else 25
        run(n, Path(args[args.index("--out") + 1]) if "--out" in args else OUT / f"{date.today().isoformat()}.json")
