"""Share of knowledge tool calls that failed, per agent and model, from the web UI chat logs. Measures whether weaker
models handle the tools better after a change (13 Nov 2026 before the argument-name mapping: opus 0.0%, agy
gemini-3.8-flash-high 4.7%, 34 of 717). Runs inside the server container, standard library only:

    ssh <forge> 'docker exec -i knowledge python3 - [since-epoch]' < tests/tool_errors_by_model.py
"""
import collections
import glob
import json
import re
import sys

since = float(sys.argv[1]) if len(sys.argv) > 1 else 0
stats = collections.defaultdict(collections.Counter)
errors = collections.defaultdict(collections.Counter)
for f in glob.glob("/admin/chats/*/*.json"):
    try:
        c = json.load(open(f, encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if not isinstance(c, dict) or "events" not in c:
        continue
    model = f'{c.get("agent")}:{c.get("model") or "default"}'
    for e in c["events"]:
        t, k = e.get("text") or "", e.get("kind")
        if e.get("sub") or (e.get("at") or 0) < since:
            continue
        if k == "tool" and re.search(r"knowledge_(?!knowledge)[a-z_]+", t):
            stats[model]["calls"] += 1
        if k in ("failed", "refused", "error") and "Error executing tool knowledge" in t:
            stats[model]["failed"] += 1
            errors[model][re.sub(r"[0-9a-f]{8,}|\n.*", "", t)[37:140]] += 1
for model, s in sorted(stats.items(), key=lambda x: -x[1]["calls"]):
    print(f'{model:45} calls={s["calls"]:5} failed={s["failed"]:4} rate={s["failed"] / max(1, s["calls"]):.1%}')
for model, e in errors.items():
    print("==", model)
    for t, n in e.most_common(6):
        print(f"  {n:3} {t}")
