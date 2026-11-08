"""Tool add-ons: install, check and remove an outside tool (an MCP server, an agent plugin, a skill repo) and the engines
it uses, for every agent on this pc, from a manifest in plugin/tools/<id>.json.

The manifests ship with the plugin, so a pc only ever runs steps its own plugin version brought: the server sends no
more than a tool id (the companion's rule that no free text reaches a pc). A manifest is plain data:

    {"name", "description", "homepage", "needs": [command on PATH, ...],
     "path": [folder put in front of PATH for every step], "env": {"NAME": "value"} (every step),
     "bin": {"name": [argv]} (a launcher per program in ~/.local/bin, so an agent finds it on its own PATH),
     "install": [step], "check": [step], "remove": [step], "version": step}
    step = one of {"run": [argv]}, {"fetch": fetch}, {"delete": "<name in the tools folder>"}, plus
           "os": ["linux"|"darwin"|"windows"], "agent": "claude"|"codex"|"agy",
           "ok_if": [text in the output that makes a non-zero exit fine], "expect": "a regex the output must match",
           "info": true (check only: reported, never makes the tool unhealthy), "timeout": seconds,
           "env": {"NAME": "value"} (this step only, e.g. DO_NOT_TRACK)
    fetch = {"github": "owner/repo" (its latest release, or with "tag": "regex" the newest release whose tag matches,
             for a line the tool supports; GitHub's sha256 of the asset is checked)
             or "shasums": "https://.../SHASUMS256.txt" (a list of file names with their sha256, e.g. Node.js),
             "asset": {"<os>-<x64|arm64>": "regex of the file name" or {"match": regex, "strip": folders to drop}},
             "into": "<name in the tools folder>"}

"{tools}" in an argument, a path or an env value is the tools folder (~/.knowledge/tools), "{exe}" and "{cmd}" are
".exe" and ".cmd" on Windows and nothing elsewhere; an env value that starts with "?" is set only when that path exists (an engine this
platform has no build of). A step with "agent" runs
only when that agent is installed here, one with "os" only on those systems; a fetch with no asset for this platform
is skipped. Install runs again as the update: "@latest" in a step, a fetch takes the newest release (and skips it
when that one is there already). Standard library only; Python 3.9.
"""
import hashlib
import json
import os
import platform
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

CATALOG = Path(os.environ.get("KNOWLEDGE_TOOLS_DIR") or Path(__file__).resolve().parent.parent / "tools")  # a fixture in the tests
TOOLS = Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge") / "tools"
API = os.environ.get("KNOWLEDGE_GITHUB_API", "https://api.github.com")  # a local fake in the tests: then http is allowed
OS = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
ARCH = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
AGENTS = ("claude", "codex", "agy")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
NAME = re.compile(r"^[\w][\w.-]{0,79}$")  # a file or folder right in the tools folder
STEP_KEYS = {"run", "fetch", "delete", "os", "agent", "ok_if", "expect", "info", "timeout", "env"}
TOP_KEYS = {"name", "description", "homepage", "needs", "path", "env", "bin", "install", "check", "remove", "version"}
TIMEOUT = 900  # an npm install of a large package on a slow line
SHIMS = Path(os.environ.get("KNOWLEDGE_SHIMS") or Path.home() / ".local" / "bin")  # Claude Code, uv and pipx put theirs here too
MARK = "knowledge-tool-launcher"
MAX_DOWNLOAD = 3 << 30  # Ghidra is 0.6 GB
RETRY_WAIT = 5.0


def strings(d) -> bool:
    return isinstance(d, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in d.items())


def validate(m: dict) -> dict:
    """The manifest, or ValueError with what is wrong: a typo must not turn into a step that runs on every OS."""
    if not isinstance(m, dict) or set(m) - TOP_KEYS or not isinstance(m.get("name"), str) or not m.get("install"):
        raise ValueError(f"needs name and install, and only the keys {sorted(TOP_KEYS)}")
    if not (all(isinstance(c, str) for c in m.get("needs", [])) and all(isinstance(c, str) for c in m.get("path", []))
            and strings(m.get("env", {})) and isinstance(m.get("bin", {}), dict)
            and all(re.fullmatch(r"[\w][\w.-]{0,40}", k) and isinstance(v, list) and v and all(isinstance(a, str) for a in v) for k, v in m.get("bin", {}).items())):
        raise ValueError("needs and path are lists of strings, env maps names to strings, bin maps program names to argv")
    all_steps = [*m.get("install", []), *m.get("check", []), *m.get("remove", []), *([m["version"]] if "version" in m else [])]
    for s in all_steps:
        if not isinstance(s, dict) or set(s) - STEP_KEYS or len({"run", "fetch", "delete"} & set(s)) != 1:
            raise ValueError(f"a step has one of run, fetch or delete, and only the keys {sorted(STEP_KEYS)}: {s}")
        if "run" in s and not (isinstance(s["run"], list) and s["run"] and all(isinstance(a, str) for a in s["run"])):
            raise ValueError(f"run must be a list of strings: {s}")
        if "delete" in s and not (isinstance(s["delete"], str) and NAME.match(s["delete"])):
            raise ValueError(f"delete names a file or folder in the tools folder: {s}")
        if "fetch" in s:
            f = s["fetch"]
            src = f.get("github") if isinstance(f, dict) else None
            if not (isinstance(f, dict) and set(f) <= {"github", "shasums", "asset", "into", "tag"} and isinstance(f.get("tag", ""), str) and len({"github", "shasums"} & set(f)) == 1
                    and (re.fullmatch(r"[\w.-]+/[\w.-]+", src) if src else str(f.get("shasums", "")).startswith("https://"))
                    and isinstance(f.get("into"), str) and NAME.match(f["into"]) and isinstance(f.get("asset"), dict)
                    and all(re.fullmatch(r"(linux|darwin|windows)-(x64|arm64)", k) and (isinstance(v, str) or (
                        isinstance(v, dict) and set(v) <= {"match", "strip"} and isinstance(v.get("match"), str) and type(v.get("strip", 0)) is int))
                        for k, v in f["asset"].items())):
                raise ValueError(f"fetch needs github owner/repo or an https shasums list, asset per <os>-<arch>, and into: {s}")
        if not set(s.get("os", [])) <= {"linux", "darwin", "windows"} or s.get("agent", "claude") not in AGENTS:
            raise ValueError(f"unknown os or agent: {s}")
        if not (isinstance(s.get("ok_if", []), list) and all(isinstance(x, str) and x == x.lower() for x in s.get("ok_if", []))
                and isinstance(s.get("expect", ""), str) and isinstance(s.get("info", False), bool)
                and type(s.get("timeout", TIMEOUT)) in (int, float) and s.get("timeout", TIMEOUT) > 0 and strings(s.get("env", {}))):
            raise ValueError(f"ok_if is a list of lowercase strings, expect a string, info true or false, timeout seconds, env names to strings: {s}")
        try:
            re.compile(s.get("expect", ""))
            re.compile((s.get("fetch") or {}).get("tag", ""))
            for v in (s.get("fetch") or {}).get("asset", {}).values():
                re.compile(v if isinstance(v, str) else v["match"])
        except re.error as e:
            raise ValueError(f"not a regex: {e}") from None
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


def fill(text: str) -> str:
    return text.replace("{tools}", str(TOOLS)).replace("{exe}", ".exe" if OS == "windows" else "").replace("{cmd}", ".cmd" if OS == "windows" else "")


def environment(m: dict, s: dict) -> dict:
    """This pc's environment with the tool's path folders in front of PATH and its env, then the step's env."""
    raw = {**m.get("env", {}), **s.get("env", {})}
    mine = {k: fill(v[1:] if v.startswith("?") else v) for k, v in raw.items()}
    env = {**os.environ, **{k: v for k, v in mine.items() if not raw[k].startswith("?") or Path(v).exists()}}
    front = [str(Path(fill(p))) for p in m.get("path", []) if Path(fill(p)).is_dir()]
    env["PATH"] = os.pathsep.join(front + [env.get("PATH", "")])
    return env


def summary(out: str, text: str) -> str:
    """One line for the page: a JSON report's failed checks or remediation (REA's doctor and setup), else the last line."""
    try:
        d = json.loads(out)
    except ValueError:
        d = None
    if isinstance(d, dict):
        scoped = d.get("scope_checks") if isinstance(d.get("scope_checks"), list) and d["scope_checks"] else d.get("checks", [])
        bad = [f"{c.get('name', '?')} ({c['detail'][:80]})" if c.get("detail") and scoped is not d.get("checks") else c.get("name", "?")
               for c in scoped if isinstance(c, dict) and c.get("ok") is False]  # what decides, with why when it is few
        if bad:
            return "not ok: " + ", ".join(bad)
        for k in ("remediation", "error", "status"):
            if isinstance(d.get(k), str) and d[k]:
                return d[k]
    lines = [x for x in text.splitlines() if x.strip()]
    return lines[-1].strip() if lines else ""


def run(s: dict, env: dict = None) -> tuple:
    """(ok, stdout, all output) of one command step, looked up on the step's PATH (npm.cmd on Windows)."""
    env = env or dict(os.environ)
    argv = [fill(a) for a in s["run"]]
    exe = shutil.which(argv[0], path=env.get("PATH"))
    if not exe:
        return False, "", f"{argv[0]} is not installed on this pc"
    try:  # its own process group: a timeout must also stop the node an npm.cmd started, or its pipes keep us waiting
        p = subprocess.Popen([exe, *argv[1:]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                             encoding="utf-8", errors="replace", env=env, **({} if os.name == "nt" else {"start_new_session": True}))
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


class _SameHostAuth(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


OPENER = urllib.request.build_opener(_SameHostAuth)


def _get(url: str, timeout: float = 30, tries: int = 3):
    """GET with three tries on a server error or a dropped line (GitHub times out anonymous callers at busy moments);
    a GH_TOKEN or GITHUB_TOKEN in the environment goes to the GitHub API and github.com only, never along a redirect
    to another host (the file storage)."""
    if not url.startswith("https://") and "KNOWLEDGE_GITHUB_API" not in os.environ:
        raise RuntimeError(f"only https downloads: {url}")
    headers = {"User-Agent": "knowledge-tools", "Accept": "application/vnd.github+json"}
    if url.startswith((API + "/", "https://github.com/")) and (tok := os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        headers["Authorization"] = f"Bearer {tok}"
    for n in range(tries):
        try:
            return OPENER.open(urllib.request.Request(url, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code < 500 or n == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if n == tries - 1:
                raise
        time.sleep(RETRY_WAIT * (n + 1))


def latest(f: dict) -> tuple:
    """(file name, url, sha256, version) of the asset for this platform, or None when there is none for it."""
    spec = f["asset"].get(f"{OS}-{ARCH}")
    if spec is None:
        return None
    pattern = re.compile(spec if isinstance(spec, str) else spec["match"])
    if "github" in f:
        if f.get("tag"):  # newest first; drafts and pre-releases never
            with _get(f"{API}/repos/{f['github']}/releases?per_page=100") as r:
                rel = next((x for x in json.load(r) if not x.get("draft") and not x.get("prerelease") and re.search(f["tag"], x.get("tag_name", ""))), None)
            if not rel:
                raise RuntimeError(f"{f['github']}: no release tagged like {f['tag']}")
        else:
            with _get(f"{API}/repos/{f['github']}/releases/latest") as r:
                rel = json.load(r)
        a = next((a for a in rel.get("assets", []) if pattern.search(a.get("name", ""))), None)
        if not a:
            raise RuntimeError(f"{f['github']} {rel.get('tag_name')}: no file for {OS}-{ARCH}")
        digest = str(a.get("digest") or "")
        if not digest.startswith("sha256:"):  # older uploads have none: never install what cannot be checked
            raise RuntimeError(f"{f['github']}: GitHub gives no sha256 for {a['name']}")
        return a["name"], a["browser_download_url"], digest[7:], str(rel.get("tag_name", ""))
    with _get(f["shasums"]) as r:
        lines = r.read(1 << 20).decode("utf-8", "replace").split("\n")
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and pattern.search(parts[1]):
            return parts[1], f["shasums"].rsplit("/", 1)[0] + "/" + parts[1], parts[0], parts[1]
    raise RuntimeError(f"{f['shasums']}: no file for {OS}-{ARCH}")


def _inside(root: Path, name: str) -> bool:
    return not name.startswith(("/", "\\")) and ".." not in re.split(r"[\\/]", name) and ":" not in name


def unpack(archive: Path, name: str, dest: Path, strip: int):
    """A .zip, .tar.gz/.tgz/.tar.xz into dest, dropping `strip` leading folders; a path outside dest is refused."""
    cut = lambda n: "/".join(n.replace("\\", "/").split("/")[strip:])
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for i in z.infolist():
                rel = cut(i.filename)
                if not rel or i.is_dir():
                    continue
                if not _inside(dest, rel):
                    raise RuntimeError(f"{name}: refused a path outside its folder: {i.filename}")
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                mode = i.external_attr >> 16
                if os.name != "nt" and mode & 0o111:  # zip keeps unix modes here: Ghidra's launchers must stay executable
                    out.chmod(mode & 0o777)
        return
    with tarfile.open(archive) as t:
        members = []
        for mem in t.getmembers():
            mem.name = cut(mem.name)
            if mem.islnk():
                mem.linkname = cut(mem.linkname)
            if not mem.name:
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(mem.name), mem.linkname)) if mem.issym() else ""
            if not _inside(dest, mem.name) or (mem.issym() and (mem.linkname.startswith("/") or target == ".." or target.startswith("../"))):
                raise RuntimeError(f"{name}: refused a path outside its folder: {mem.name}")
            members.append(mem)
        if hasattr(tarfile, "data_filter"):  # 3.12+ (and later 3.9-3.11 patches) check again in the library
            t.extractall(dest, members, filter="data")
        else:
            t.extractall(dest, members)


def fetch(f: dict) -> tuple:
    """Download the newest release file for this platform, check its sha256, unpack or place it as {tools}/<into>.
    The old copy is replaced only once the new one is complete. (ok, version, what happened)."""
    try:
        found = latest(f)
        if not found:
            return True, "", f"{f['into']}: nothing for {OS}-{ARCH}, skipped"
        name, url, sha, version = found
        TOOLS.mkdir(parents=True, exist_ok=True)
        stamps = TOOLS / ".versions.json"
        have = json.loads(stamps.read_text(encoding="utf-8")) if stamps.exists() else {}
        dest = TOOLS / f["into"]
        if have.get(f["into"]) == name and dest.exists():
            return True, version, f"{f['into']} {version} is current"
        spec = f["asset"][f"{OS}-{ARCH}"]
        with tempfile.TemporaryDirectory(dir=TOOLS) as tmp:
            part, h, size = Path(tmp) / name, hashlib.sha256(), 0
            with _get(url, 120) as r, open(part, "wb") as out:
                while chunk := r.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise RuntimeError(f"{name} is larger than {MAX_DOWNLOAD >> 30} GB")
                    h.update(chunk)
                    out.write(chunk)
            if h.hexdigest() != sha.lower():
                raise RuntimeError(f"{name}: sha256 does not match, not installed")
            new = Path(tmp) / "new"
            if re.search(r"\.(zip|tar\.gz|tgz|tar\.xz)$", name):
                new.mkdir()
                unpack(part, name, new, spec.get("strip", 1) if isinstance(spec, dict) else 1)
            else:
                part.rename(new)
                if os.name != "nt":  # a single program (micromamba) must run; a jar does not mind
                    new.chmod(0o755)
            if dest.exists():  # a file in use (Windows) fails here, before anything changed
                dest.rename(Path(tmp) / "old")
            new.rename(dest)
        stamps.write_text(json.dumps({**have, f["into"]: name}, indent=1), encoding="utf-8")
        return True, version, f"{f['into']} {version} installed"
    except (OSError, ValueError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as e:
        return False, "", f"{f['into']}: {type(e).__name__}: {e}"


def delete(name: str) -> tuple:
    target = TOOLS / name
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        stamps = TOOLS / ".versions.json"
        if stamps.exists():
            stamps.write_text(json.dumps({k: v for k, v in json.loads(stamps.read_text(encoding="utf-8")).items() if k != name}, indent=1), encoding="utf-8")
        return True, "", f"{name} deleted"
    except OSError as e:
        return False, "", f"{name}: {e}"


def ours(f: Path) -> bool:
    try:
        with open(f, "rb") as h:  # ~/.local/bin also holds big programs: read the head only
            return MARK in h.read(300).decode("utf-8", "replace")
    except OSError:
        return False


def launcher(argv: list) -> str:
    if OS == "windows":  # cmd reads a .cmd in the OEM code page: a home folder's non-ASCII name breaks, so go from %~dp0
        text = f"@echo off\r\nrem {MARK}: made by the knowledge companion\r\n" + " ".join(f'"{a}"' for a in argv) + " %*\r\n"
        return text.replace(str(Path.home()) + "\\", "%~dp0..\\..\\") if SHIMS == Path.home() / ".local" / "bin" else text
    return f"#!/bin/sh\n# {MARK}: made by the knowledge companion\nexec " + " ".join(shlex.quote(a) for a in argv) + ' "$@"\n'


def link(m: dict, on: bool = True) -> list:
    """Write (or with on=False remove) the tool's launchers; the names left out because another program has them."""
    skipped = []
    for name, argv in m.get("bin", {}).items():
        f, argv = SHIMS / (name + (".cmd" if OS == "windows" else "")), [fill(a) for a in argv]
        if f.exists() and not ours(f):
            skipped.append(name)
        elif not on or not Path(argv[0]).exists():  # removed, or no build of it for this platform
            f.unlink(missing_ok=True) if f.exists() else None
        else:
            SHIMS.mkdir(parents=True, exist_ok=True)
            with open(f, "w", encoding="utf-8", newline="") as out:  # write_text has no newline= on Python 3.9
                out.write(launcher(argv))
            f.chmod(0o755)
    if on and OS == "windows" and m.get("bin"):
        user_path(SHIMS)
    return skipped


def user_path(folder: Path):
    """Put the launcher folder on the user's own PATH (no admin), as scoop and uv do, and tell running programs."""
    if os.environ.get("KNOWLEDGE_COMPANION_DRY_RUN") == "1":  # the tests never touch this user's PATH
        return
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            have, kind = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            have, kind = "", winreg.REG_EXPAND_SZ
        if str(folder).lower() in [os.path.expandvars(p).strip().lower() for p in have.split(";")]:
            return
        winreg.SetValueEx(k, "Path", 0, kind, (have.rstrip(";") + ";" if have else "") + str(folder))
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)


def do(m: dict, s: dict) -> tuple:
    if "fetch" in s:
        return fetch(s["fetch"])
    if "delete" in s:
        return delete(s["delete"])
    return run(s, environment(m, s))


def said(s: dict, text: str, out: str = "") -> str:
    label = " ".join(s["run"][:3]) if "run" in s else "fetch" if "fetch" in s else "delete"
    return f"{label}: {summary(out, text)[:200]}"


def check(m: dict, agents) -> dict:
    """The tool's own health checks: ok when every non-info check passes; the version from its version step."""
    ok, notes = True, []
    for s in steps(m, "check", agents):
        good, out, text = do(m, s)
        if not good:
            ok = ok and bool(s.get("info"))
            notes.append(said(s, text, out))
    version = ""
    if "version" in m:
        good, out, text = do(m, m["version"])
        try:
            d = json.loads(out)
            version = str(d.get("version", "")) if isinstance(d, dict) else ""
        except ValueError:
            version = (out.strip().splitlines() or [""])[0]
    return {"installed": True, "ok": ok, "version": version[:40], "detail": "; ".join(notes)[:300]}


def install(m: dict, agents) -> dict:
    """Run the install steps in order (stop at the first failure), then the checks. Also the update: run it again."""
    path = environment(m, {})["PATH"]
    if missing := [c for c in m.get("needs", []) if not shutil.which(c, path=path)]:
        raise RuntimeError(f"needs {', '.join(missing)} on this pc first")
    todo = steps(m, "install", agents)
    if not todo:
        raise RuntimeError(f"nothing to install on {OS} for the agents here")
    for s in todo:
        good, out, text = do(m, s)
        if not good:
            raise RuntimeError(said(s, text, out))
    skipped = link(m)
    got = check(m, agents)
    if skipped:  # never overwrite another program's file, but say so
        got["detail"] = (f"not linked, another program has the name: {', '.join(skipped)}; " + got["detail"])[:300]
    return got


def remove(m: dict, agents) -> dict:
    """Every remove step, also after one failed: a registration left behind is worse than a package left behind."""
    link(m, on=False)
    failed = [said(s, text, out) for s in steps(m, "remove", agents) for good, out, text in [do(m, s)] if not good]
    if failed:
        raise RuntimeError("; ".join(failed)[:500])
    return {"installed": False, "ok": None, "version": "", "detail": ""}


def self_test():
    global TOOLS, API, RETRY_WAIT, SHIMS
    import io
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        doc = json.dumps({"checks": [{"name": "hopper", "ok": False}], "scope_checks": [{"name": "ghidra-version", "ok": False, "detail": "12.2; accepted 12.1.x"}]})
        assert summary(doc, "") == "not ok: ghidra-version (12.2; accepted 12.1.x)", summary(doc, "")
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
                     {"name": "x", "install": [{"run": ["a"], "ok_if": ["Already"]}]}, {"name": "x", "install": [{"run": ["a"], "expect": "("}]},
                     {"name": "x", "install": [{"run": ["a"], "env": {"A": 1}}]}, {"name": "x", "install": [{"run": ["a"], "delete": "x"}]},
                     {"name": "x", "install": [{"delete": "../home"}]}, {"name": "x", "install": [{"fetch": {"shasums": "http://x/S", "asset": {}, "into": "n"}}]},
                     {"name": "x", "install": [{"fetch": {"github": "a/b", "asset": {"windows": "x"}, "into": "n"}}]},
                     {"name": "x", "install": [{"fetch": {"github": "a/b", "asset": {"linux-x64": "("}, "into": "n"}}]}):
            try:
                validate(typo)
                raise AssertionError(f"refused: {typo}")
            except ValueError:
                pass
        shims_was, SHIMS = SHIMS, Path(d) / "shims"
        os.environ["KNOWLEDGE_COMPANION_DRY_RUN"] = "1"  # never this user's real PATH
        try:  # launchers: ours written and removed, a foreign file kept, a program this platform lacks skipped
            SHIMS.mkdir()
            ext = ".cmd" if OS == "windows" else ""
            (SHIMS / ("foreign" + ext)).write_text("someone else's")
            lm = {**m, "bin": {"hello": [py, "-c", "print('hi from the launcher')"], "foreign": [py], "absent": [str(Path(d) / "none")]}}
            got = install(lm, [])
            out = subprocess.run([str(SHIMS / ("hello" + ext))], capture_output=True, text=True).stdout
            assert "hi from the launcher" in out and "another program has the name: foreign" in got["detail"], (out, got)
            assert (SHIMS / ("foreign" + ext)).read_text() == "someone else's" and not (SHIMS / ("absent" + ext)).exists()
            try:
                remove(lm, [])
            except RuntimeError:
                pass  # its remove steps fail on purpose (above)
            assert not (SHIMS / ("hello" + ext)).exists() and (SHIMS / ("foreign" + ext)).exists()
        finally:
            SHIMS = shims_was
        (Path(d) / "good.json").write_text(json.dumps(m), encoding="utf-8")
        (Path(d) / "broken.json").write_text("{", encoding="utf-8")
        (Path(d) / "Bad Name.json").write_text(json.dumps(m), encoding="utf-8")
        assert list(catalog(Path(d))) == ["good"] and catalog(Path(d) / "none") == {}

        # fetch: a fake GitHub and a SHASUMS list on this machine; an archive with an executable, a broken checksum, a bad path
        def archive(kind: str, files: dict) -> bytes:
            b = io.BytesIO()
            if kind == "zip":
                with zipfile.ZipFile(b, "w") as z:
                    for n, (data, mode) in files.items():
                        i = zipfile.ZipInfo(n)
                        i.external_attr = mode << 16
                        z.writestr(i, data)
            else:
                with tarfile.open(fileobj=b, mode="w:gz") as t:
                    for n, (data, mode) in files.items():
                        i = tarfile.TarInfo(n)
                        i.size, i.mode = len(data), mode
                        t.addfile(i, io.BytesIO(data))
            return b.getvalue()
        good = archive("zip", {"engine_1.0/bin/run": (b"#!/bin/sh\n", 0o755), "engine_1.0/README": (b"hi", 0o644)})
        evil = archive("tgz", {"top/../../escape": (b"x", 0o644)})
        with tempfile.TemporaryDirectory() as t2:  # a link inside the folder (Node's bin/npm) is fine, one that leaves it is not
            for link, fine in (("../lib/npm-cli.js", True), ("../../outside", False)):
                b = io.BytesIO()
                with tarfile.open(fileobj=b, mode="w:gz") as t:
                    i = tarfile.TarInfo("node/bin/npm")
                    i.type, i.linkname = tarfile.SYMTYPE, link
                    t.addfile(i)
                (Path(t2) / "a.tar.gz").write_bytes(b.getvalue())
                out = Path(t2) / ("ok" if fine else "no")
                out.mkdir()
                try:
                    unpack(Path(t2) / "a.tar.gz", "a.tar.gz", out, 1)
                    assert fine, link
                except RuntimeError:
                    assert not fine, link
        files = {"/dl/engine_1.0.zip": good, "/dl/evil.tar.gz": evil, "/dl/tool.jar": b"JAR"}
        sha = lambda b: hashlib.sha256(b).hexdigest()
        state = {"digest": "sha256:" + sha(good)}

        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                base = f"http://127.0.0.1:{srv.server_port}"
                state.setdefault("auth", []).append((self.path, self.headers.get("Authorization")))
                if self.path == "/hop":  # a redirect to another host name of this machine
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{srv.server_port}/dl/tool.jar")
                    self.end_headers()
                    return
                rel = {"/repos/o/engine/releases/latest": {"tag_name": "v1.0", "assets": [{"name": "engine_1.0.zip", "browser_download_url": base + "/dl/engine_1.0.zip", "digest": state["digest"]}]},
                       "/repos/o/evil/releases/latest": {"tag_name": "v6", "assets": [{"name": "evil.tar.gz", "browser_download_url": base + "/dl/evil.tar.gz", "digest": "sha256:" + sha(evil)}]},
                       "/repos/o/engine/releases?per_page=100": [{"tag_name": "v2.0", "assets": []}, {"tag_name": "v1.1-rc", "prerelease": True, "assets": []},
                                                                 {"tag_name": "v1.0", "assets": [{"name": "engine_1.0.zip", "browser_download_url": base + "/dl/engine_1.0.zip", "digest": state["digest"]}]}],
                       "/repos/o/old/releases/latest": {"tag_name": "v0", "assets": [{"name": "engine_1.0.zip", "browser_download_url": base + "/dl/engine_1.0.zip"}]}}
                if self.path.startswith("/repos/o/flaky/") and not state.setdefault("flaked", False):
                    state["flaked"] = True
                    self.send_response(503)
                    self.end_headers()
                    return
                rel["/repos/o/flaky/releases/latest"] = rel["/repos/o/engine/releases/latest"]
                body = json.dumps(rel[self.path]).encode() if self.path in rel else files.get(self.path) if self.path in files else \
                    f"{sha(b'JAR')}  tool.jar\n{sha(b'x')}  tool-other.jar\n".encode() if self.path == "/dl/SHASUMS256.txt" else None
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(body or b"")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = TOOLS, API, os.environ.get("KNOWLEDGE_GITHUB_API")
        TOOLS, API = Path(d) / "tools", f"http://127.0.0.1:{srv.server_port}"
        os.environ["KNOWLEDGE_GITHUB_API"] = API
        try:
            plat = f"{OS}-{ARCH}"
            eng = {"github": "o/engine", "asset": {plat: r"engine_.*\.zip$"}, "into": "engine"}
            ok, version, text = fetch(eng)
            assert ok and version == "v1.0" and (TOOLS / "engine" / "README").read_text() == "hi", (ok, text)  # top folder dropped
            assert os.name == "nt" or os.access(TOOLS / "engine" / "bin" / "run", os.X_OK), "the launcher stays executable"
            assert fetch(eng)[2] == "engine v1.0 is current"
            (TOOLS / ".versions.json").write_text("{}")
            assert fetch({**eng, "tag": r"^v1\."})[1] == "v1.0", "the newest release of a line, never a pre-release"
            assert "no release tagged" in fetch({**eng, "tag": r"^v9\."})[2]
            RETRY_WAIT = 0.05
            assert fetch({**eng, "github": "o/flaky"})[0] and state["flaked"], "a 503 is tried again"
            os.environ["GH_TOKEN"], state["auth"] = "tok", []
            try:
                with _get(API + "/hop") as r:
                    assert r.read() == b"JAR"
            finally:
                os.environ.pop("GH_TOKEN")
            assert state["auth"] == [("/hop", "Bearer tok"), ("/dl/tool.jar", None)], state["auth"]
            assert fetch({**eng, "asset": {"linux-arm64" if plat != "linux-arm64" else "darwin-x64": "x"}})[2].endswith("skipped")
            state["digest"] = "sha256:" + "0" * 64
            (TOOLS / ".versions.json").write_text("{}")
            ok, _, text = fetch(eng)
            assert not ok and "does not match" in text and (TOOLS / "engine" / "README").exists(), text  # the old copy stays
            assert "no sha256" in fetch({**eng, "github": "o/old"})[2]
            ok, _, text = fetch({"github": "o/evil", "asset": {plat: "evil"}, "into": "evil"})
            assert not ok and "outside" in text and not (Path(d) / "escape").exists() and not (TOOLS / "evil").exists(), text
            ok, version, text = fetch({"shasums": API + "/dl/SHASUMS256.txt", "asset": {plat: r"^tool\.jar$"}, "into": "tool.jar"})
            assert ok and (TOOLS / "tool.jar").read_bytes() == b"JAR", text
            assert delete("engine")[0] and not (TOOLS / "engine").exists() and "engine" not in json.loads((TOOLS / ".versions.json").read_text())
            (TOOLS / "pbin").mkdir()
            env = environment({"path": ["{tools}/pbin", "{tools}/none"], "env": {"A": "{tools}/a", "C": "?{tools}/pbin", "D": "?{tools}/none{exe}"}}, {"env": {"B": "b"}})
            assert env["PATH"].split(os.pathsep)[0] == str(TOOLS / "pbin") and env["A"] == str(TOOLS / "a") and env["B"] == "b", env["PATH"][:200]
            assert env["C"] == str(TOOLS / "pbin") and "D" not in env, "a ? value only when its path is there"
            assert os.name == "nt" or os.access(TOOLS / "tool.jar", os.X_OK), "a single fetched file can run"
        finally:
            srv.shutdown()
            TOOLS, API = old[0], old[1]
            if old[2] is None:
                os.environ.pop("KNOWLEDGE_GITHUB_API", None)
    print("ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
