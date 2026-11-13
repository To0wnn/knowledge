"""Share of knowledge tool calls that failed, per agent and model, from the web UI chat logs. Measures whether weaker
models handle the tools better after a change (13 Nov 2026 before the argument-name mapping: opus 0.0%, agy
gemini-3.8-flash-high 4.7%, 34 of 717). Also how often a knowledge_search was followed within two tool calls by a
local search (Grep, Glob, grep, Select-String; before the semantic fallback of 13 Nov 2026 claude-haiku-4-5 5 of 10,
opus 27%, which also greps code after a good hit: compare a model with itself), and ToolSearch calls per chat (3.2 for
opus, 4.0 for claude-haiku-4-5 before always-load). Runs inside the server container, standard library only:

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
    tools = [e.get("text") or "" for e in c["events"] if e.get("kind") == "tool" and not e.get("sub") and (e.get("at") or 0) >= since]
    stats[model]["chats"] += bool(tools)
    stats[model]["toolsearch"] += sum(t.startswith("ToolSearch") for t in tools)
    for i, t in enumerate(tools):
        if re.search(r"knowledge_search\b", t):
            stats[model]["searches"] += 1
            stats[model]["gave_up"] += any(re.match(r"(Grep|Glob|grep_search|find_by_name)\b|(Bash|run_command).*(grep|Select-String|find )", n)
                                           for n in tools[i + 1:i + 3])
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
    print(f'{model:45} calls={s["calls"]:5} failed={s["failed"]:4} rate={s["failed"] / max(1, s["calls"]):.1%}'
          f' searches={s["searches"]:4} gave_up={s["gave_up"] / max(1, s["searches"]):.0%}'
          f' toolsearch/chat={s["toolsearch"] / max(1, s["chats"]):.1f}')
for model, e in errors.items():
    print("==", model)
    for t, n in e.most_common(6):
        print(f"  {n:3} {t}")
