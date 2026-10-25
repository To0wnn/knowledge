#!/usr/bin/env python3
"""Weekly report on how the agent CLIs on the VM (Claude Code, Codex, agy) are used, and which models they offer.

Writes one note into the homelab memory, rapporten/agent-gebruik.md, overwritten every week: its History in the
knowledge UI is the trend. Per CLI over the last 7 days: sessions, model requests, context per request (median and
p90 of the main sessions), output tokens, share of subagent requests, parallel subagent launches, tool errors and
interruptions, requests per model. Plus the models each CLI offers on this machine, read from the CLI itself (what
this account may use), compared with last week.

Matrix only when something asks for action: a model appeared or disappeared, or the configured model is no longer
offered. Quality signals stay in the note until their thresholds are calibrated. A failing run reports itself.
Runs from agent-report.timer. Config from local.env (EnvironmentFile): KNOWLEDGE_MATRIX_URL, KNOWLEDGE_MATRIX_ROOM,
KNOWLEDGE_VM_CC_PREFIX. State: ~/.local/state/agent-report.json.
  --dry-run   print the note and the Matrix message instead of writing and sending
  --selftest  the checks of this file
Standard library only (the Claude model list falls back to the Agent SDK through uv).
"""
import glob
import json
import os
import re
import statistics
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

import kuma  # noqa: E402  (next to this file)

HOME = Path.home()
PREFIX = os.environ.get("KNOWLEDGE_VM_CC_PREFIX", "-home-" + HOME.name)
NOTE = HOME / ".claude/projects" / f"{PREFIX}-homelab" / "memory/rapporten/agent-gebruik.md"
STATE = HOME / ".local/state/agent-report.json"
WEEK = 7 * 86400
PUSH = "UPTIME_KUMA_PUSH_AGENT_REPORT"  # vault name of the heartbeat token (Uptime Kuma "Agent report")


def ts(value) -> float:
    """ISO timestamp ('2026-10-25T11:09:45.285Z') to epoch seconds; 0 when missing or unreadable."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def lines(path: str):
    with open(path, errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def recent(pattern: str, since: float) -> list[str]:
    return [p for p in glob.glob(pattern, recursive=True) if os.path.getmtime(p) >= since]


def summary(sessions: int, ctx: list[int], out: int, requests: int, sub: int, launches: int, parallel: int,
            calls: int, errors: int, interrupts: int, models: Counter) -> dict:
    ctx = sorted(ctx)
    return {"sessions": sessions, "requests": requests, "ctx_median": int(statistics.median(ctx)) if ctx else 0,
            "ctx_p90": ctx[int(len(ctx) * 0.9)] if ctx else 0, "output_tokens": out,
            "subagent_share": round(sub / requests, 3) if requests else 0.0, "subagent_launches": launches,
            "parallel_launches": parallel, "tool_calls": calls, "tool_errors": errors,
            "tool_error_rate": round(errors / calls, 3) if calls else 0.0, "interrupts": interrupts,
            "models": dict(models.most_common())}


def claude(root: Path, since: float) -> dict:
    """Claude Code transcripts. Every content block of one reply is its own line sharing message.id, so a reply is
    counted (and its usage taken) once per id; subagent transcripts live under <session>/subagents/."""
    ctx, models, seen = [], Counter(), set()
    out = requests = sub = launches = parallel = calls = errors = interrupts = 0
    files = recent(str(root / "*/*.jsonl"), since) + recent(str(root / "*/*/subagents/*.jsonl"), since)
    per_reply: Counter = Counter()
    for path in files:
        is_sub = "/subagents/" in path
        for d in lines(path):
            if ts(d.get("timestamp")) < since:
                continue
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            content = m.get("content") if isinstance(m.get("content"), list) else []
            if d.get("type") == "assistant":
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        calls += 1
                        if c.get("name") in ("Agent", "Task"):
                            launches += 1
                            per_reply[(path, m.get("id"))] += 1
                key = (path, m.get("id"))
                u = m.get("usage")
                if u and m.get("model") and m["model"] != "<synthetic>" and key not in seen:
                    seen.add(key)
                    requests += 1
                    sub += is_sub
                    models[m["model"]] += 1
                    out += u.get("output_tokens") or 0
                    if not is_sub:
                        ctx.append(sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                               "cache_creation_input_tokens")))
            elif d.get("type") == "user":
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                        errors += 1
                    if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").startswith("[Request interrupted by user"):
                        interrupts += 1
    parallel = sum(1 for n in per_reply.values() if n >= 2)
    sessions = len({p for p in files if "/subagents/" not in p})
    return summary(sessions, ctx, out, requests, sub, launches, parallel, calls, errors, interrupts, models)


EXIT = re.compile(r"Process exited with code (\d+)")


def codex(root: Path, since: float) -> dict:
    """Codex rollouts: a token_count event per model request (repeated events with the same running total are one
    request), the model and effort from turn_context, tool results with a non-zero exit code, aborted turns."""
    ctx, models = [], Counter()
    out = requests = launches = calls = errors = interrupts = 0
    files = recent(str(root / "**/rollout-*.jsonl"), since)
    for path in files:
        last_total, model = None, "?"
        for d in lines(path):
            if ts(d.get("timestamp")) < since:
                continue
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            kind = (d.get("type"), p.get("type"))
            if d.get("type") == "turn_context":
                model = f"{p.get('model')} ({p.get('effort') or p.get('reasoning_effort') or 'default'})"
            elif kind == ("event_msg", "token_count") and p.get("info"):
                total = (p["info"].get("total_token_usage") or {}).get("total_tokens")
                if total == last_total:
                    continue
                last_total, last = total, p["info"].get("last_token_usage") or {}
                requests += 1
                models[model] += 1
                ctx.append(last.get("input_tokens") or 0)
                out += last.get("output_tokens") or 0
            elif kind in (("response_item", "function_call"), ("response_item", "custom_tool_call")):
                calls += 1
                launches += "agent" in str(p.get("name"))
            elif kind == ("response_item", "function_call_output"):
                hit = EXIT.search(str(p.get("output")))
                errors += bool(hit and hit.group(1) != "0")
            elif kind == ("event_msg", "turn_aborted") and p.get("reason") == "interrupted":
                interrupts += 1
    return summary(len(files), ctx, out, requests, 0, launches, 0, calls, errors, interrupts, models)


def agy(root: Path, since: float, model: str = "?") -> dict:
    """agy transcripts: one PLANNER_RESPONSE per model request (tokens, tool calls), GENERIC steps ending in ERROR.
    The transcript names no model: the configured one is reported."""
    ctx, models = [], Counter()
    out = requests = launches = calls = errors = 0
    files = recent(str(root / "*/.system_generated/logs/transcript.jsonl"), since)
    for path in files:
        for d in lines(path):
            if ts(d.get("created_at")) < since:
                continue
            if d.get("type") == "PLANNER_RESPONSE" and d.get("input_tokens") is not None:
                requests += 1
                models[model] += 1
                ctx.append((d.get("input_tokens") or 0) + (d.get("cache_read_tokens") or 0))
                out += d.get("output_tokens") or 0
                tools = d.get("tool_calls") or []
                calls += len(tools)
                launches += sum("subagent" in json.dumps(t).lower() for t in tools)
            elif d.get("type") == "GENERIC" and d.get("status") == "ERROR":
                errors += 1
    return summary(len(files), ctx, out, requests, 0, launches, 0, calls, errors, 0, models)


# --- models each CLI offers on this machine ---

def claude_models() -> list[str]:
    """Claude Code's own model catalogue cache (refreshed when Claude Code runs), else the Agent SDK."""
    caches = sorted(glob.glob(str(HOME / ".claude/cache/model-catalog/*.json")), key=os.path.getmtime)
    if caches and time.time() - os.path.getmtime(caches[-1]) < WEEK:
        cfg = json.load(open(caches[-1])).get("catalog", {}).get("config", {})
        ids = [m["id"] for m in cfg.get("models", []) if m.get("id")]
        if ids:
            return sorted(set(ids))
    code = ("import asyncio,json\nfrom claude_agent_sdk import ClaudeSDKClient,ClaudeAgentOptions\n"
            "async def m():\n async with ClaudeSDKClient(options=ClaudeAgentOptions(max_turns=1)) as c:\n"
            "  print(json.dumps([x['resolvedModel'] for x in (await c.get_server_info())['models']]))\nasyncio.run(m())")
    r = subprocess.run([str(HOME / ".local/bin/uv"), "run", "-q", "--with", "claude-agent-sdk", "python", "-c", code],
                       capture_output=True, text=True, timeout=300)
    return sorted(set(json.loads(r.stdout.strip().splitlines()[-1])))


def codex_models(cache: Path = HOME / ".codex/models_cache.json") -> list[str]:
    data = json.load(open(cache))
    return sorted(m["slug"] for m in data.get("models", []) if m.get("visibility") == "list")


def read_models(readers) -> tuple[dict, list[str]]:
    """Each CLI's model list; one that cannot be read (or is empty) is an action, never silence, and does not hide
    the others."""
    models, unreadable = {}, []
    for cli, read in readers:
        try:
            models[cli] = read()
            if not models[cli]:
                raise ValueError("empty list")
        except Exception as e:
            models[cli] = []
            unreadable.append(f"{cli}: model list could not be read ({type(e).__name__}: {e}); new or retired models go unseen")
    return models, unreadable


def configured() -> dict:
    """The model each CLI is set to (None = its default)."""
    out = {"claude": json.load(open(HOME / ".claude/settings.json")).get("model")}
    try:
        out["codex"] = tomllib.load(open(HOME / ".codex/config.toml", "rb")).get("model")
    except OSError:
        out["codex"] = None
    try:
        out["agy"] = json.load(open(HOME / ".gemini/antigravity-cli/settings.json")).get("model")
    except OSError:
        out["agy"] = None
    return out


def changes(previous: dict, current: dict) -> list[str]:
    """Per CLI: models that appeared or disappeared since last week (nothing on the first run)."""
    out = []
    for cli, now in current.items():
        before = previous.get(cli)
        if before is None:
            continue
        out += [f"{cli}: new model {m}" for m in sorted(set(now) - set(before))]
        out += [f"{cli}: model gone {m}" for m in sorted(set(before) - set(now))]
    return out


def missing_config(models: dict, config: dict, agy_labels: dict) -> list[str]:
    """A configured model the CLI no longer offers (agy stores its label, e.g. 'Gemini 3.8 Flash (Medium)')."""
    out = []
    if config.get("codex") and config["codex"] not in models.get("codex", []):
        out.append(f"codex: configured model {config['codex']} is not offered any more")
    if config.get("agy") and agy_labels and config["agy"] not in agy_labels.values():
        out.append(f"agy: configured model {config['agy']} is not offered any more")
    return out


def agy_labels() -> dict:
    r = subprocess.run([str(HOME / ".local/bin/agy"), "models"], capture_output=True, text=True, timeout=120,
                       stdin=subprocess.DEVNULL)
    return dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)


# --- output ---

ROWS = [("sessions", "sessions"), ("requests", "model requests"), ("ctx_median", "context per request, median"),
        ("ctx_p90", "context per request, p90"), ("output_tokens", "output tokens"),
        ("subagent_share", "share of subagent requests"), ("subagent_launches", "subagent launches"),
        ("parallel_launches", "replies launching 2+ agents"), ("tool_error_rate", "tool errors per call"),
        ("interrupts", "interruptions")]


def fmt(v) -> str:
    return f"{v:.1%}" if isinstance(v, float) else f"{v / 1000:.0f}k" if isinstance(v, int) and v >= 10000 else str(v)


def note(stats: dict, before: dict, models: dict, config: dict, actions: list[str], day: str) -> str:
    out = [f"# Agent-gebruik (week tot {day})", "",
           "Generated every Monday by `watcher/agent-report.py` on the VM from the local logs of Claude Code, Codex and "
           "agy (last 7 days); the History of this note is the trend. Background and decisions: "
           "[[reference_model_effort_en_parallel_werk_claude_codex_agy]].", ""]
    for cli, s in stats.items():
        b = before.get(cli, {})
        out += [f"## {cli}", "", "| | this week | last week |", "|---|---|---|"]
        out += [f"| {label} | {fmt(s.get(k, 0))} | {fmt(b[k]) if k in b else '-'} |" for k, label in ROWS]
        out += ["", "Requests per model: " + (", ".join(f"{m} {n}" for m, n in s["models"].items()) or "none"), ""]
    out += ["## Models offered on this machine", ""]
    out += [f"- {cli} (configured: {config.get(cli) or 'default'}): {', '.join(ms) or 'could not be read'}"
            for cli, ms in models.items()]
    out += ["", "## Needs action", ""] + ([f"- {a}" for a in actions] or ["- nothing"])
    return "\n".join(out) + "\n"


def send(text: str) -> None:
    token = subprocess.run([str(HOME / ".local/bin/secret"), "MATRIX_BOT_TOKEN"], capture_output=True, text=True,
                           check=True).stdout.strip()
    url = (f"{os.environ['KNOWLEDGE_MATRIX_URL']}/_matrix/client/v3/rooms/"
           f"{urllib.parse.quote(os.environ['KNOWLEDGE_MATRIX_ROOM'])}/send/m.room.message/agents-{time.time_ns()}")
    req = urllib.request.Request(url, method="PUT", data=json.dumps({"msgtype": "m.text", "body": text}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status != 200:
            raise RuntimeError(f"Matrix answered {r.status}")


def main() -> None:
    since = time.time() - WEEK
    config = configured()
    labels = agy_labels()
    agy_model = next((i for i, label in labels.items() if label == config.get("agy")), config.get("agy") or "?")
    stats = {"claude": claude(HOME / ".claude/projects", since), "codex": codex(HOME / ".codex/sessions", since),
             "agy": agy(HOME / ".gemini/antigravity-cli/brain", since, agy_model)}
    models, unreadable = read_models((("claude", claude_models), ("codex", codex_models), ("agy", lambda: sorted(labels))))
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    actions = (unreadable + changes(state.get("models", {}), {c: m for c, m in models.items() if m})
               + missing_config(models, config, labels))
    text = note(stats, state.get("stats", {}), models, config, actions, time.strftime("%Y-%m-%d"))
    message = ("Agent report needs action (a changed model: test it before using it):\n" + "\n".join(f"- {a}" for a in actions)
               + "\nDetails: rapporten/agent-gebruik.md in the homelab memory") if actions else ""
    if "--dry-run" in sys.argv:
        print(text, message, sep="\n")
        return
    NOTE.parent.mkdir(parents=True, exist_ok=True)
    NOTE.write_text(text)
    if message:
        send(message)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"stats": stats, "models": {c: m for c, m in models.items() if m}
                                 or state.get("models", {}), "at": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
    print(f"note written, {len(actions)} action(s)")
    kuma.push(PUSH, msg=f"{len(actions)} action(s)")


def selftest() -> None:
    import tempfile
    now = time.time()
    iso = lambda t: datetime.fromtimestamp(t).astimezone().isoformat()
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        # Claude: one reply split over two lines (same id), two Agent calls in it = one parallel launch, a subagent
        # request, a tool error, an interruption, and an old line outside the week.
        (t / "c/p/s1/subagents").mkdir(parents=True)
        u = {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0, "output_tokens": 5}
        main = [{"type": "assistant", "timestamp": iso(now), "message": {"id": "m1", "model": "claude-opus-5-5", "usage": u,
                 "content": [{"type": "tool_use", "name": "Agent"}]}},
                {"type": "assistant", "timestamp": iso(now), "message": {"id": "m1", "model": "claude-opus-5-5", "usage": u,
                 "content": [{"type": "tool_use", "name": "Agent"}]}},
                {"type": "user", "timestamp": iso(now), "message": {"content": [{"type": "tool_result", "is_error": True}]}},
                {"type": "user", "timestamp": iso(now), "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
                {"type": "assistant", "timestamp": iso(now - 9 * 86400), "message": {"id": "old", "model": "x", "usage": u, "content": []}}]
        (t / "c/p/s1.jsonl").write_text("\n".join(map(json.dumps, main)))
        (t / "c/p/s1/subagents/a.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": iso(now), "message": {
            "id": "s", "model": "claude-haiku-5-5", "usage": u, "content": [{"type": "tool_use", "name": "Read"}]}}))
        c = claude(t / "c", now - WEEK)
        assert (c["sessions"], c["requests"], c["subagent_launches"], c["parallel_launches"]) == (1, 2, 2, 1), c
        assert c["ctx_median"] == 1010 and c["subagent_share"] == 0.5 and c["tool_errors"] == 1 and c["interrupts"] == 1, c
        assert c["models"] == {"claude-opus-5-5": 1, "claude-haiku-5-5": 1}, c
        # Codex: a repeated token_count is one request; exit code 1 is an error, 0 is not.
        (t / "x/2026").mkdir(parents=True)
        tc = lambda total, inp: {"timestamp": iso(now), "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": total}, "last_token_usage": {"input_tokens": inp, "output_tokens": 3}}}}
        rows = [{"timestamp": iso(now), "type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "medium"}},
                tc(100, 90), tc(100, 90), tc(300, 190),
                {"timestamp": iso(now), "type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}},
                {"timestamp": iso(now), "type": "response_item", "payload": {"type": "function_call_output", "output": "Process exited with code 1"}},
                {"timestamp": iso(now), "type": "response_item", "payload": {"type": "function_call_output", "output": "Process exited with code 0"}},
                {"timestamp": iso(now), "type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}}]
        (t / "x/2026/rollout-a.jsonl").write_text("\n".join(map(json.dumps, rows)))
        x = codex(t / "x", now - WEEK)
        assert (x["requests"], x["ctx_median"], x["ctx_p90"], x["tool_errors"], x["interrupts"]) == (2, 140, 190, 1, 1), x
        assert x["models"] == {"gpt-6-luna (medium)": 2}, x
        # agy
        (t / "g/b1/.system_generated/logs").mkdir(parents=True)
        rows = [{"type": "PLANNER_RESPONSE", "created_at": iso(now), "input_tokens": 100, "cache_read_tokens": 50,
                 "output_tokens": 7, "tool_calls": [{"name": "invoke_subagent"}, {"name": "view_file"}]},
                {"type": "GENERIC", "status": "ERROR", "created_at": iso(now)}]
        (t / "g/b1/.system_generated/logs/transcript.jsonl").write_text("\n".join(map(json.dumps, rows)))
        g = agy(t / "g", now - WEEK, "gemini-3.8-flash-medium")
        assert (g["requests"], g["ctx_median"], g["tool_calls"], g["subagent_launches"], g["tool_errors"]) == (1, 150, 2, 1, 1), g
        # Codex model list: only visible models.
        (t / "mc.json").write_text(json.dumps({"models": [{"slug": "a", "visibility": "list"}, {"slug": "h", "visibility": "hide"}]}))
        assert codex_models(t / "mc.json") == ["a"]
    def broken():
        raise OSError("no cache")
    m, bad = read_models((("codex", lambda: ["a"]), ("agy", broken), ("claude", lambda: [])))
    assert m == {"codex": ["a"], "agy": [], "claude": []} and len(bad) == 2 and bad[0].startswith("agy: model list could not be read"), bad
    assert changes({}, {"codex": ["a"]}) == []  # first run: a baseline, no alarm
    assert changes({"codex": ["a", "b"]}, {"codex": ["a", "c"]}) == ["codex: new model c", "codex: model gone b"]
    assert missing_config({"codex": ["a"]}, {"codex": "b", "agy": "L"}, {"id": "L"}) == ["codex: configured model b is not offered any more"]
    assert missing_config({"codex": ["a"]}, {"codex": "a", "agy": "M"}, {"id": "L"}) == ["agy: configured model M is not offered any more"]
    n = note({"codex": x}, {}, {"codex": ["a"]}, {"codex": "a"}, [], "2026-10-26")
    assert "| context per request, median | 140 | - |" in n and "- nothing" in n, n
    print("ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        try:
            main()
        except Exception as e:  # fail loudly: a silent weekly job is worse than a noisy one
            if "--dry-run" not in sys.argv:
                send(f"Agent report failed: {type(e).__name__}: {e}")
                kuma.push(PUSH, up=False, msg=f"{type(e).__name__}: {e}")
            raise
