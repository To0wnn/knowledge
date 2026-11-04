#!/usr/bin/env python3
"""Write the server's model choice into one agent's own settings on this pc. Only when its owner clicks Apply: nothing
calls this by itself. Keeps a copy of each file it changes in ~/.knowledge/backups/.
  model_settings.py --self-test
"""
from __future__ import annotations  # X | None on Python 3.9 (macOS's /usr/bin/python3)

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

MODEL = re.compile(r"[\w.:\[\]-]{1,80}")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")


def checked(plan: dict) -> dict:
    """A plan comes over the network: only well-formed names and known efforts reach a settings file."""
    out = {}
    for k in ("lead", "reader"):
        if plan.get(k) and MODEL.fullmatch(str(plan[k])):
            out[k] = str(plan[k])
        if plan.get(f"{k}_effort") in EFFORTS:
            out[f"{k}_effort"] = plan[f"{k}_effort"]
    return out


def toml_top(text: str, key: str, value: str) -> str:
    """Set a top-level key: it has to stand before the first [table] to stay top-level."""
    head, sep, rest = ("\n" + text).partition("\n[")
    line = f'{key} = "{value}"'
    head, n = re.subn(rf"^{key} = .*$", lambda _: line, head, count=1, flags=re.M)
    return (head if n else head.rstrip("\n") + f"\n{line}").lstrip("\n") + sep + rest


def toml_agents(text: str, model: str, effort: str | None) -> str:
    block = f'[agents]\ndefault_subagent_model = "{model}"\n' + (f'default_subagent_reasoning_effort = "{effort}"\n' if effort else "")
    if re.search(r"^\[agents\]$", text, re.M):
        return re.sub(r"^\[agents\]\n(?:(?!\[).*\n?)*", lambda m: block + ("" if m.end() == len(text) else "\n"), text, count=1, flags=re.M)
    return text.rstrip("\n") + "\n\n" + block


def write(path: Path, text: str, home: Path) -> None:
    if path.exists():
        backup = home / ".knowledge" / "backups" / f"models-{time.strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup / f"{path.parent.name}-{path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    """{} for no file; a file that cannot be read stops here, so it is never overwritten."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def apply(agent: str, plan: dict, label: str = "", home: Path | None = None) -> str:
    """label: agy stores a model's display name. Returns what changed, for the pc's action log."""
    home, plan = home or Path.home(), checked(plan)
    lead, reader = plan.get("lead"), plan.get("reader")
    if agent == "claude":
        path = home / ".claude" / "settings.json"
        s = read_json(path)
        if lead:
            s["model"] = lead
            if plan.get("lead_effort"):
                s["effortLevel"] = plan["lead_effort"]
        if reader:
            s.setdefault("env", {})["CLAUDE_CODE_SUBAGENT_MODEL"] = reader
        text = json.dumps(s, indent=2, ensure_ascii=False) + "\n"
    elif agent == "codex":
        path = home / ".codex" / "config.toml"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if lead:
            text = toml_top(text, "model", lead)
            if plan.get("lead_effort"):
                text = toml_top(text, "model_reasoning_effort", plan["lead_effort"])
        if reader:
            text = toml_agents(text, reader, plan.get("reader_effort"))
    elif agent == "agy":  # its subagents follow the main model or a fixed flash/pro: only the lead has a setting
        path = home / ".gemini" / "antigravity-cli" / "settings.json"
        s = read_json(path)
        if lead:
            if not label:
                raise ValueError(f"agy does not offer {lead} on this pc")
            s["model"] = label
        text = json.dumps(s, indent=2, ensure_ascii=False) + "\n"
    else:
        raise ValueError(f"unknown agent {agent}")
    if not lead and not reader:
        return f"{agent}: nothing to change"
    write(path, text, home)
    return f"{agent}: lead {lead or 'unchanged'}, reader {reader or 'unchanged'}"


def current(agent: str, home: Path | None = None) -> dict:
    """What an agent is set to now, for the server's "matches or not"; {} when it cannot be read."""
    home = home or Path.home()
    try:
        if agent == "claude":
            s = read_json(home / ".claude" / "settings.json")
            found = {"lead": s.get("model"), "lead_effort": s.get("effortLevel"), "reader": (s.get("env") or {}).get("CLAUDE_CODE_SUBAGENT_MODEL")}
        elif agent == "codex":
            text = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            top, agents = ("\n" + text).partition("\n[")[0], (re.search(r"^\[agents\]\n((?:(?!\[).*\n?)*)", text, re.M) or [None, ""])[1]
            value = lambda block, key: (re.search(rf'^{key} = "([^"]*)"', block, re.M) or [None, None])[1]
            found = {"lead": value(top, "model"), "lead_effort": value(top, "model_reasoning_effort"),
                     "reader": value(agents, "default_subagent_model"), "reader_effort": value(agents, "default_subagent_reasoning_effort")}
        else:
            found = {"lead": read_json(home / ".gemini" / "antigravity-cli" / "settings.json").get("model")}
    except (OSError, ValueError):
        return {}
    return {k: str(v)[:80] for k, v in found.items() if v}


def self_test() -> None:
    import tomllib
    with tempfile.TemporaryDirectory() as t:
        h = Path(t)
        (h / ".claude").mkdir()
        (h / ".claude/settings.json").write_text('{"model": "old", "env": {"KEEP": "1"}, "other": true}')
        apply("claude", {"lead": "opus", "lead_effort": "high", "reader": "sonnet"}, home=h)
        s = json.loads((h / ".claude/settings.json").read_text())
        assert (s["model"], s["effortLevel"], s["env"], s["other"]) == ("opus", "high", {"KEEP": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}, True), s
        assert list((h / ".knowledge/backups").glob("*/.claude-settings.json")), "a copy of the old file"
        (h / ".codex").mkdir()
        (h / ".codex/config.toml").write_text('personality = "p"\nmodel = "a"\n\n[projects."x"]\ntrust_level = "trusted"\n')
        apply("codex", {"lead": "gpt-x", "lead_effort": "medium", "reader": "gpt-y", "reader_effort": "low"}, home=h)
        first = (h / ".codex/config.toml").read_text()
        c = tomllib.loads(first)
        assert (c["model"], c["model_reasoning_effort"], c["agents"]["default_subagent_model"], c["projects"]["x"]["trust_level"]) == ("gpt-x", "medium", "gpt-y", "trusted"), c
        apply("codex", {"lead": "gpt-x", "lead_effort": "medium", "reader": "gpt-y", "reader_effort": "low"}, home=h)
        assert (h / ".codex/config.toml").read_text() == first, "applying twice changes nothing"
        assert current("codex", h) == {"lead": "gpt-x", "lead_effort": "medium", "reader": "gpt-y", "reader_effort": "low"}, current("codex", h)
        assert current("claude", h) == {"lead": "opus", "lead_effort": "high", "reader": "sonnet"} and current("agy", h) == {}
        apply("agy", {"lead": "flash-low"}, label="Flash (Low)", home=h)
        assert current("agy", h) == {"lead": "Flash (Low)"}
        assert json.loads((h / ".gemini/antigravity-cli/settings.json").read_text())["model"] == "Flash (Low)"
        try:
            apply("agy", {"lead": "gone"}, home=h)
            raise AssertionError("agy needs a label")
        except ValueError:
            pass
        (h / ".claude/settings.json").write_text("{broken")
        try:
            apply("claude", {"lead": "opus"}, home=h)
            raise AssertionError("a broken file must stop it")
        except ValueError:
            assert (h / ".claude/settings.json").read_text() == "{broken"
        assert checked({"lead": "a b; rm -rf", "reader": "ok-1", "reader_effort": "huge"}) == {"reader": "ok-1"}
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] != ["--self-test"]:
        sys.exit(__doc__)
    self_test()
