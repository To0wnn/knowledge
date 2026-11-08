"""Tool add-ons: install, check and remove an outside tool (an MCP server, an agent plugin, a skill repo) for every agent
on this pc, from a manifest in plugin/tools/<id>.json.

The manifests ship with the plugin, so a pc only ever runs steps its own plugin version brought: the server sends no
more than a tool id (the companion's rule that no free text reaches a pc). A manifest is plain data:

    {"name", "description", "homepage", "needs": [command on PATH, ...],
     "install": [step], "check": [step], "remove": [step], "version": step}
    step = {"run": [argv], "os": ["linux"|"darwin"|"windows"], "agent": "claude"|"codex"|"agy",
            "ok_if": [text in the output that makes a non-zero exit fine], "expect": "a regex the output must match",
            "info": true (check only: reported, never makes the tool unhealthy), "timeout": seconds,
            "env": {"NAME": "value"} (added to this step's environment, e.g. DO_NOT_TRACK)}

A step with "agent" runs only when that agent is installed here, one with "os" only on those systems. Install runs
again as an update ("@latest" in a step, or the agent's own marketplace update). Standard library only; Python 3.9.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CATALOG = Path(os.environ.get("KNOWLEDGE_TOOLS_DIR") or Path(__file__).resolve().parent.parent / "tools")  # a fixture in the tests
OS = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
AGENTS = ("claude", "codex", "agy")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
STEP_KEYS = {"run", "os", "agent", "ok_if", "expect", "info", "timeout", "env"}
TOP_KEYS = {"name", "description", "homepage", "needs", "install", "check", "remove", "version"}
TIMEOUT = 900  # an npm install of a large package on a slow line


def validate(m: dict) -> dict:
    """The manifest, or ValueError with what is wrong: a typo must not turn into a step that runs on every OS."""
    if not isinstance(m, dict) or set(m) - TOP_KEYS or not isinstance(m.get("name"), str) or not m.get("install"):
        raise ValueError(f"needs name and install, and only the keys {sorted(TOP_KEYS)}")
    steps = [*m.get("install", []), *m.get("check", []), *m.get("remove", []), *([m["version"]] if "version" in m else [])]
    for s in steps:
        if not isinstance(s, dict) or set(s) - STEP_KEYS:
            raise ValueError(f"a step has only the keys {sorted(STEP_KEYS)}: {s}")
        if not (isinstance(s.get("run"), list) and s["run"] and all(isinstance(a, str) for a in s["run"])):
            raise ValueError(f"run must be a list of strings: {s}")
        if not set(s.get("os", [])) <= {"linux", "darwin", "windows"} or s.get("agent", "claude") not in AGENTS:
            raise ValueError(f"unknown os or agent: {s}")
        if not (isinstance(s.get("ok_if", []), list) and all(isinstance(x, str) and x == x.lower() for x in s.get("ok_if", []))
                and isinstance(s.get("expect", ""), str) and isinstance(s.get("info", False), bool)
                and type(s.get("timeout", TIMEOUT)) in (int, float) and s.get("timeout", TIMEOUT) > 0
                and isinstance(s.get("env", {}), dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in s.get("env", {}).items())):
            raise ValueError(f"ok_if is a list of lowercase strings, expect a string, info true or false, timeout seconds, env names to strings: {s}")
    for s in steps:
        try:
            re.compile(s.get("expect", ""))
        except re.error as e:
            raise ValueError(f"expect is not a regex: {e}") from None
    if not all(isinstance(c, str) for c in m.get("needs", [])):
        raise ValueError("needs is a list of command names")
    return m


def catalog(folder: Path = CATALOG) -> dict:
    """id -> manifest; a broken one is left out (its self-test catches it), it never stops the others."""
    out = {}
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            if ID.match(f.stem):
                out[f.stem] = validate(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def here(agents) -> list:
    """The agents installed on this pc."""
    return [a for a in AGENTS if a in agents]


def steps(m: dict, key: str, agents) -> list:
    return [s for s in m.get(key, []) if OS in s.get("os", [OS]) and s.get("agent") in (None, *agents)]


def summary(out: str, text: str) -> str:
    """One line for the page: a JSON report's failed checks or remediation (REA's doctor and setup), else the last line."""
    try:
        d = json.loads(out)
    except ValueError:
        d = None
    if isinstance(d, dict):
        bad = [c.get("name", "?") for c in d.get("checks", []) if isinstance(c, dict) and c.get("ok") is False]
        if bad:
            return "not ok: " + ", ".join(bad)
        for k in ("remediation", "error", "status"):
            if isinstance(d.get(k), str) and d[k]:
                return d[k]
    lines = [x for x in text.splitlines() if x.strip()]
    return lines[-1].strip() if lines else ""


def run(s: dict) -> tuple:
    """(ok, stdout, all output) of one step. The command is looked up on PATH (npm.cmd on Windows)."""
    exe = shutil.which(s["run"][0])
    if not exe:
        return False, "", f"{s['run'][0]} is not installed on this pc"
    try:  # its own process group: a timeout must also stop the node an npm.cmd started, or its pipes keep us waiting
        p = subprocess.Popen([exe, *s["run"][1:]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", env={**os.environ, **s.get("env", {})}, **({} if os.name == "nt" else {"start_new_session": True}))
        try:
            out, err = p.communicate(timeout=s.get("timeout", TIMEOUT))
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                os.killpg(p.pid, 9)
            p.communicate()
            return False, "", f"timed out after {s.get('timeout', TIMEOUT)} s"
    except (OSError, subprocess.SubprocessError) as e:
        return False, "", f"{type(e).__name__}: {e}"
    text = (out + err).strip()
    ok = (p.returncode == 0 or any(x in text.lower() for x in s.get("ok_if", []))) and re.search(s.get("expect", ""), text) is not None
    return ok, out, text


def said(s: dict, text: str, out: str = "") -> str:
    return f"{' '.join(s['run'][:3])}: {summary(out, text)[:200]}"


def check(m: dict, agents) -> dict:
    """The tool's own health checks: ok when every non-info check passes; the version from its version step."""
    ok, notes = True, []
    for s in steps(m, "check", agents):
        good, out, text = run(s)
        if not good:
            ok = ok and bool(s.get("info"))
            notes.append(said(s, text, out))
    version = ""
    if "version" in m:
        good, out, text = run(m["version"])
        try:
            d = json.loads(out)
            version = str(d.get("version", "")) if isinstance(d, dict) else ""
        except ValueError:
            version = (out.strip().splitlines() or [""])[0]
    return {"installed": True, "ok": ok, "version": version[:40], "detail": "; ".join(notes)[:300]}


def install(m: dict, agents) -> dict:
    """Run the install steps in order (stop at the first failure), then the checks. Also the update: run it again."""
    if missing := [c for c in m.get("needs", []) if not shutil.which(c)]:
        raise RuntimeError(f"needs {', '.join(missing)} on this pc first")
    todo = steps(m, "install", agents)
    if not todo:
        raise RuntimeError(f"nothing to install on {OS} for the agents here")
    for s in todo:
        good, out, text = run(s)
        if not good:
            raise RuntimeError(said(s, text, out))
    return check(m, agents)


def remove(m: dict, agents) -> dict:
    """Every remove step, also after one failed: a registration left behind is worse than a package left behind."""
    failed = [said(s, text, out) for s in steps(m, "remove", agents) for good, out, text in [run(s)] if not good]
    if failed:
        raise RuntimeError("; ".join(failed)[:500])
    return {"installed": False, "ok": None, "version": "", "detail": ""}


def self_test():
    import tempfile
    for tid, f in ((f.stem, f) for f in sorted(CATALOG.glob("*.json"))):  # the shipped manifests are valid
        assert ID.match(tid), tid
        validate(json.loads(f.read_text(encoding="utf-8")))
    with tempfile.TemporaryDirectory() as d:
        mark, py = Path(d) / "mark", sys.executable
        put = lambda text: [py, "-c", f"open({str(mark)!r}, 'a').write({text!r})"]
        m = validate({"name": "Fixture", "needs": [Path(py).name],
             "install": [{"run": [py, "-c", "import os, sys; sys.exit(os.environ.get('KN_T') != 'x')"], "env": {"KN_T": "x"}}, {"run": put("i")}, {"run": put("C"), "agent": "claude"}, {"run": put("X"), "agent": "codex"},
                         {"run": put("W"), "os": ["windows"]}, {"run": put("P"), "os": ["linux", "darwin"]},
                         {"run": [py, "-c", "import sys; print('already there'); sys.exit(1)"], "ok_if": ["already"]}],
             "check": [{"run": [py, "-c", "print('{\"checks\": [{\"name\": \"node\", \"ok\": true}]}')"], "expect": "node"},
                       {"run": [py, "-c", "import sys; print('{\"checks\": [{\"name\": \"ghidra\", \"ok\": false}]}'); sys.exit(1)"], "info": True}],
             "version": {"run": [py, "-c", "print('{\"version\": \"1.2.3\"}')"]},
             "remove": [{"run": [py, "-c", "import sys; sys.exit(3)"]}, {"run": put("r")}]})
        got = install(m, here(["claude", "agy"]))
        assert mark.read_text() == "iC" + ("W" if OS == "windows" else "P"), mark.read_text()  # codex is not here
        assert got["ok"] is True and got["version"] == "1.2.3" and got["detail"].endswith("not ok: ghidra"), got  # info only
        try:
            remove(m, [])
            raise AssertionError("a failed remove step is said")
        except RuntimeError as e:
            assert mark.read_text().endswith("r"), (e, mark.read_text())  # the next step still ran
        bad = {**m, "check": [{"run": [py, "-c", "print('{\"status\": \"needs_human\", \"remediation\": \"Install Node.js 22\"}')"], "expect": "ready"}]}
        slow = run({"run": [py, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)"], "timeout": 1})
        assert slow[0] is False and "timed out" in slow[2], slow  # back in a second, the grandchild too
        assert check(bad, [])["ok"] is False and "Install Node.js 22" in check(bad, [])["detail"]
        for broken, why in (({**m, "needs": ["no-such-command-xyz"]}, "needs no-such-command-xyz"),
                            ({**m, "install": [{"run": ["no-such-command-xyz"]}]}, "not installed on this pc"),
                            ({**m, "install": [{"run": put("x"), "agent": "agy"}]}, "nothing to install")):
            try:
                install(broken, [])
                raise AssertionError(why)
            except RuntimeError as e:
                assert why in str(e), e
        for typo in ({**m, "instal": []}, {"name": "x", "install": [{"run": "npm i"}]}, {"name": "x", "install": [{"run": ["a"], "agent": "cursor"}]},
                     {"name": "x", "install": [{"run": ["a"], "os": ["win"]}]}, {"name": "x", "install": [{"run": ["a"], "shell": True}]},
                     {"name": "x", "install": [{"run": ["a"], "ok_if": "already"}]}, {"name": "x", "install": [{"run": ["a"], "timeout": "60"}]},
                     {"name": "x", "install": [{"run": ["a"], "ok_if": ["Already"]}]}, {"name": "x", "install": [{"run": ["a"], "expect": "("}]}, {"name": "x", "install": [{"run": ["a"], "env": {"A": 1}}]}):
            try:
                validate(typo)
                raise AssertionError(f"refused: {typo}")
            except ValueError:
                pass
        (Path(d) / "good.json").write_text(json.dumps(m), encoding="utf-8")
        (Path(d) / "broken.json").write_text("{", encoding="utf-8")
        (Path(d) / "Bad Name.json").write_text(json.dumps(m), encoding="utf-8")
        assert list(catalog(Path(d))) == ["good"] and catalog(Path(d) / "none") == {}
    print("ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
