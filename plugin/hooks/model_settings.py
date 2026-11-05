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

import token_store
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


TOML_TOKEN = re.compile(r'''"""|\'\'\'|"(?:[^"\\\n]|\\.)*"|\'[^\'\n]*\'|#.*|[\[\]]''')
TOML_CLOSE = {'"""': re.compile(r'(?:[^\\]|\\.)*?"{3,5}', re.S), "'''": re.compile(r"(?s).*?'{3,5}")}


def toml_lines(lines: list) -> tuple:
    """(the lines that really open a table, the lines inside a multi-line array or string): those only look like a
    header or a key."""
    heads, inside, opened, depth = set(), set(), "", 0
    for n, line in enumerate(lines):
        if opened or depth > 0:
            inside.add(n)
        elif line.lstrip().startswith("["):
            heads.add(n)
            continue
        i = 0
        while True:
            if opened:
                end = TOML_CLOSE[opened].match(line, i)
                if not end:
                    break
                i, opened = end.end(), ""
            m = TOML_TOKEN.search(line, i)
            if not m:
                break
            t, i = m.group(0), m.end()
            if t in TOML_CLOSE:
                opened = t
            elif t in "[]":
                depth += 1 if t == "[" else -1
    return heads, inside


def toml_set(text: str, table, key: str, raw):
    """Set (raw = a TOML value as text) or remove (raw = None) one key of a table (None = top level), leaving every
    other line as it is; the old value as text (with its comment), or None when it was not there."""
    lines = text.splitlines(keepends=True)
    heads, inside = toml_lines(lines)
    if table is None:
        start, end = 0, min(heads, default=len(lines))
    else:
        at = next((i for i in sorted(heads) if lines[i].split("#")[0].strip() == f"[{table}]"), None)
        if at is None:
            if raw is None:
                return text, None
            sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
            return f"{text}{sep}[{table}]\n{key} = {raw}\n", None
        start, end = at + 1, min((i for i in heads if i > at), default=len(lines))
    pat = re.compile(rf"\s*{re.escape(key)}\s*=\s*(.*?)\s*$")
    for i in range(start, end):
        m = i not in inside and pat.fullmatch(lines[i].rstrip("\r\n"))
        if m:
            if raw is None:
                del lines[i]
            else:
                lines[i] = f"{key} = {raw}\n"
            return "".join(lines), m.group(1)
    if raw is None:
        return text, None
    last = end
    while last > start and not lines[last - 1].strip():
        last -= 1
    if last and not lines[last - 1].endswith("\n"):
        lines[last - 1] += "\n"
    lines.insert(last, f"{key} = {raw}\n")
    return "".join(lines), None


def codex_text(text: str, plan: dict) -> str:
    """Only the keys of this plan change: other lines of the file and of [agents] stay as they are."""
    quoted = lambda v: json.dumps(v)
    if plan.get("reader") and re.search(r"^\s*agents\s*[.=]", text, re.M):
        raise ValueError("codex: config.toml sets agents in a form this cannot edit; left unchanged")
    for key, table, value in (("model", None, plan.get("lead")), ("model_reasoning_effort", None, plan.get("lead") and plan.get("lead_effort")),
                              ("default_subagent_model", "agents", plan.get("reader")),
                              ("default_subagent_reasoning_effort", "agents", plan.get("reader") and plan.get("reader_effort"))):
        if value:
            text = toml_set(text, table, key, quoted(value))[0]
    try:
        import tomllib  # Python 3.11+; older ones write without this last check
    except ImportError:
        return text
    try:
        tomllib.loads(text)
    except ValueError as e:
        raise ValueError(f"codex: the result would not be valid TOML ({e}); left unchanged") from e
    return text


def write(path: Path, text: str, home: Path) -> None:
    if path.exists():
        backup = home / ".knowledge" / "backups" / f"models-{time.strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup / f"{path.parent.name}-{path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())  # here: the mode copied below may be read-only (a config from home-manager)
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    token_store.sync_dir(path.parent)


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
        text = codex_text(path.read_text(encoding="utf-8-sig") if path.exists() else "", plan)
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
            text = (home / ".codex" / "config.toml").read_text(encoding="utf-8-sig")
            top, agents = ("\n" + text).partition("\n[")[0], (re.search(r"^\[agents\][^\n]*\n((?:(?!\[).*\n?)*)", text, re.M) or [None, ""])[1]
            value = lambda block, key: (re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', block, re.M) or [None, None])[1]
            found = {"lead": value(top, "model"), "lead_effort": value(top, "model_reasoning_effort"),
                     "reader": value(agents, "default_subagent_model"), "reader_effort": value(agents, "default_subagent_reasoning_effort")}
        else:
            found = {"lead": read_json(home / ".gemini" / "antigravity-cli" / "settings.json").get("model")}
    except (OSError, ValueError, AttributeError, TypeError):  # a report must not fail on an odd settings file
        return {}
    return {k: str(v)[:80] for k, v in found.items() if v}


def self_test() -> None:
    # A line in a multi-line value is no table header: a key used to land inside the array and break the file.
    for t, table, want in (('notify = [\n  ["a"],\n]\nm = "x"\n[z]\nq = 1\n', None, 'notify = [\n  ["a"],\n]\nm = "x"\nk = 1\n[z]\nq = 1\n'),
                           ('[z]\nn = """\n[y]\n"""\nq = 1\n[y]\nr = 2\n', "y", '[z]\nn = """\n[y]\n"""\nq = 1\n[y]\nr = 2\nk = 1\n'),
                           ('[z]\nm = [\n  ["a"],\n]\n[y]\nr = 2\n', "z", '[z]\nm = [\n  ["a"],\n]\nk = 1\n[y]\nr = 2\n')):
        assert toml_set(t, table, "k", "1") == (want, None), (t, toml_set(t, table, "k", "1"))
    t = 'd = """\nk = "keep"\n"""\nk = "real"\n'
    assert toml_set(t, None, "k", "1") == ('d = """\nk = "keep"\n"""\nk = 1\n', '"real"')
    assert toml_set(t, None, "k", None) == ('d = """\nk = "keep"\n"""\n', '"real"')
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
        for odd in ('model="a"\n[agents] # c\nmax_threads = 4\ndefault_subagent_model   = "z"\n', '\ufeffmodel = "a"\n'):
            (h / ".codex/config.toml").write_text(odd, encoding="utf-8")
            apply("codex", {"lead": "gpt-x", "lead_effort": "medium", "reader": "gpt-y", "reader_effort": "low"}, home=h)
            got = tomllib.loads((h / ".codex/config.toml").read_text(encoding="utf-8"))
            assert got["model"] == "gpt-x" and got["agents"]["default_subagent_model"] == "gpt-y", got
            assert current("codex", h)["reader"] == "gpt-y"
        assert got["agents"]["default_subagent_reasoning_effort"] == "low"
        (h / ".codex/config.toml").write_text('model="a"\n[agents] # c\nmax_threads = 4\n', encoding="utf-8")
        apply("codex", {"reader": "gpt-y"}, home=h)
        kept = (h / ".codex/config.toml").read_text(encoding="utf-8")
        assert tomllib.loads(kept)["agents"]["max_threads"] == 4 and "# c" in kept, kept
        (h / ".codex/config.toml").write_text('agents = {x = 1}\n', encoding="utf-8")
        try:
            apply("codex", {"reader": "gpt-y"}, home=h)
            raise AssertionError("an inline agents table must be refused")
        except ValueError:
            assert (h / ".codex/config.toml").read_text(encoding="utf-8") == 'agents = {x = 1}\n'
        (h / ".claude/settings.json").write_text('{"env": "x"}')
        assert current("claude", h) == {}, "an odd file must not fail the report"
        (h / ".claude/settings.json").write_text('{"model": "opus", "effortLevel": "high", "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}}')
        (h / ".codex/config.toml").write_text("", encoding="utf-8")
        apply("codex", {"lead": "gpt-x", "lead_effort": "medium", "reader": "gpt-y", "reader_effort": "low"}, home=h)
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
