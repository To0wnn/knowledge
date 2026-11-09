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
             or "shasums": "https://.../SHASUMS256.txt" (a list of file names with their sha256, e.g. Node.js)
             or "android": "platform-tools" (the stable package from Google's SDK repository; Google gives a sha1),
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
ANDROID = os.environ.get("KNOWLEDGE_ANDROID_REPO", "https://dl.google.com/android/repository/")
OS = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
ARCH = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
AGENTS = ("claude", "codex", "agy")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
NAME = re.compile(r"^[\w][\w.-]{0,79}$")  # a file or folder right in the tools folder
STEP_KEYS = {"run", "fetch", "delete", "os", "agent", "ok_if", "expect", "info", "timeout", "env"}
TOP_KEYS = {"name", "kind", "description", "homepage", "needs", "path", "env", "bin", "install", "check", "remove", "version", "skills"}
# Claude Code's skills folder. A skill a tool put there must stay on this pc: the shared-skills sync (memory_mirror.py,
# and the server reading the admin's folder) would otherwise hand it to every device, next to the tool's own install.
SKILLS = Path(os.environ.get("KNOWLEDGE_SKILLS") or Path.home() / ".claude" / "skills")
# what Codex and agy read; a test that moves SKILLS gets these beside it, never the real home's
AGENT_SKILLS = Path(os.environ.get("KNOWLEDGE_AGENT_SKILLS") or (SKILLS.parent / "agent-skills" if os.environ.get("KNOWLEDGE_SKILLS")
                    else Path.home() / ".agents" / "skills"))
AGY_SKILLS = Path(os.environ.get("KNOWLEDGE_AGY_SKILLS") or (SKILLS.parent / "agy-skills" if os.environ.get("KNOWLEDGE_SKILLS")
                  else Path.home() / ".gemini" / "config" / "skills"))  # = memory_mirror
NOT_SHARED = ".not-shared"  # = memory_mirror.py and server.py
KINDS = {"skill", "program"}  # skill: instructions an agent loads; program: software it runs (an MCP server, engines)
TIMEOUT = 900  # an npm install of a large package on a slow line
SHIMS = Path(os.environ.get("KNOWLEDGE_SHIMS") or Path.home() / ".local" / "bin")  # Claude Code, uv and pipx put theirs here too
MARK = "knowledge-tool-launcher"
MAX_DOWNLOAD = 3 << 30  # Ghidra is 0.6 GB
RETRY_WAIT = 5.0
TOKEN = ""  # set by the companion from the server's answer: a read-only token for public repositories


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
            if not (isinstance(f, dict) and set(f) <= {"github", "shasums", "android", "asset", "into", "tag"} and isinstance(f.get("tag", ""), str)
                    and len({"github", "shasums", "android"} & set(f)) == 1
                    and (re.fullmatch(r"[\w.-]+/[\w.-]+", src) if src else re.fullmatch(r"[\w-]+", str(f["android"])) if "android" in f
                         else str(f.get("shasums", "")).startswith("https://"))
                    and isinstance(f.get("into"), str) and NAME.match(f["into"]) and isinstance(f.get("asset"), dict)
                    and all(re.fullmatch(r"(linux|darwin|windows)-(x64|arm64)", k) and (isinstance(v, str) or (
                        isinstance(v, dict) and set(v) <= {"match", "strip"} and isinstance(v.get("match"), str) and type(v.get("strip", 0)) is int))
                        for k, v in f["asset"].items())):
                raise ValueError(f"fetch needs github owner/repo, an https shasums list or an android package, asset per <os>-<arch>, and into: {s}")
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
    if not (isinstance(m.get("skills", []), list) and all(isinstance(n, str) and ID.match(n) for n in m.get("skills", []))):
        raise ValueError("skills lists the folder names the tool installs in ~/.claude/skills")
    if m.get("kind") not in KINDS:  # the page labels each tool with it
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
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
    # a {tools} path gets this OS's separators, as the PATH folders do (C:\...\tools/a would reach Java as is)
    bare = {k: v[1:] if v.startswith("?") else v for k, v in raw.items()}
    mine = {k: str(Path(fill(v))) if v.startswith("{tools}") else fill(v) for k, v in bare.items()}
    env = {**os.environ, **{k: v for k, v in mine.items() if not raw[k].startswith("?") or Path(v).exists()}}
    front = [str(Path(fill(p))) for p in m.get("path", []) if Path(fill(p)).is_dir()]
    env["PATH"] = os.pathsep.join(front + [env.get("PATH", "")])
    return env


def summary(out: str, text: str, info: bool = False) -> str:
    """One line for the page: a JSON report's failed checks or remediation (REA's doctor and setup), else the last line.
    info: the checks are optional extras (REA's paid engines), named only, so the note does not read as an error."""
    try:
        d = json.loads(out)
    except ValueError:
        d = None
    if isinstance(d, dict):
        scoped = d.get("scope_checks") if isinstance(d.get("scope_checks"), list) and d["scope_checks"] else d.get("checks", [])
        bad = [f"{c.get('name', '?')} ({c['detail'][:80]})" if c.get("detail") and scoped is not d.get("checks") and not info else c.get("name", "?")
               for c in scoped if isinstance(c, dict) and c.get("ok") is False]  # what decides, with why when it is few
        if bad:
            return ("optional, not set up: " if info else "not ok: ") + ", ".join(bad)
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
    a GitHub token (the server's read-only one for the admins' own devices, else GH_TOKEN or GITHUB_TOKEN) goes to the
    GitHub API and github.com only, never along a redirect to another host (the file storage)."""
    if not url.startswith("https://") and "KNOWLEDGE_GITHUB_API" not in os.environ:
        raise RuntimeError(f"only https downloads: {url}")
    headers = {"User-Agent": "knowledge-tools", "Accept": "application/vnd.github+json"}
    if url.startswith((API + "/", "https://github.com/")) and (tok := TOKEN or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        headers["Authorization"] = f"Bearer {tok}"
    for n in range(tries):
        try:
            return OPENER.open(urllib.request.Request(url, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401 and headers.pop("Authorization", None):  # an expired token: anonymous still works most of the time
                continue
            if e.code < 500 or n == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if n == tries - 1:
                raise
        time.sleep(RETRY_WAIT * (n + 1))


def latest(f: dict) -> tuple:
    """(file name, url, "sha256:<hex>" or "sha1:<hex>", version) of the asset for this platform, or None when there is none."""
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
        return a["name"], a["browser_download_url"], digest, str(rel.get("tag_name", ""))
    if "android" in f:  # channel-0 is stable
        import xml.etree.ElementTree as ET
        with _get(ANDROID + "repository2-1.xml") as r:
            root = ET.fromstring(r.read(8 << 20))
        for p in root.iter("remotePackage"):
            ch = p.find("channelRef")
            if p.get("path") == f["android"] and (ch is None or ch.get("ref") == "channel-0"):
                for a in p.iter("archive"):
                    name = a.findtext("complete/url") or ""
                    if pattern.search(name):
                        return name, ANDROID + name, "sha1:" + (a.findtext("complete/checksum") or ""), ".".join(x.text or "" for x in p.find("revision"))
        raise RuntimeError(f"{f['android']}: no stable build for {OS}-{ARCH}")
    with _get(f["shasums"]) as r:
        lines = r.read(1 << 20).decode("utf-8", "replace").split("\n")
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and pattern.search(parts[1]):
            return parts[1], f["shasums"].rsplit("/", 1)[0] + "/" + parts[1], "sha256:" + parts[0], parts[1]
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
                    out.chmod(mode & 0o755)
        return
    root = os.path.realpath(dest)
    within = lambda p: p == root or p.startswith(root + os.sep)
    with tarfile.open(archive) as t:
        for mem in t.getmembers():  # one by one, each checked against what is on disk by now: links in a chain too
            mem.name = cut(mem.name)
            if mem.islnk():
                mem.linkname = cut(mem.linkname)
            if not mem.name:
                continue
            parent = os.path.realpath(os.path.join(root, os.path.dirname(mem.name)))
            bad = not _inside(dest, mem.name) or not within(parent) or (
                mem.issym() and (os.path.isabs(mem.linkname) or not within(os.path.realpath(os.path.join(parent, mem.linkname))))) or (
                mem.islnk() and (not _inside(dest, mem.linkname) or not within(os.path.realpath(os.path.join(root, mem.linkname)))))
            if bad or not (mem.isfile() or mem.isdir() or mem.issym() or mem.islnk()):  # no devices or fifos
                raise RuntimeError(f"{name}: refused a path outside its folder: {mem.name}")
            if hasattr(tarfile, "data_filter"):  # 3.12+ (and later 3.9-3.11 patches) check again in the library
                t.extract(mem, dest, filter="data")
            else:
                t.extract(mem, dest)


def stamps() -> dict:
    try:
        return json.loads((TOOLS / ".versions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):  # missing, or cut off by a crash: everything is fetched again, nothing breaks
        return {}


def save_stamps(d: dict):
    tmp = TOOLS / ".versions.json.tmp"
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, TOOLS / ".versions.json")


def fetch(f: dict) -> tuple:
    """Download the newest release file for this platform, check its sha256, unpack or place it as {tools}/<into>.
    The old copy is replaced only once the new one is complete. (ok, version, what happened)."""
    try:
        found = latest(f)
        if not found:
            return True, "", f"{f['into']}: nothing for {OS}-{ARCH}, skipped"
        name, url, sha, version = found
        TOOLS.mkdir(parents=True, exist_ok=True)
        have, stamp = stamps(), f"{name} {sha}"  # the checksum too: micromamba's file name never changes
        dest = TOOLS / f["into"]
        if have.get(f["into"]) == stamp and dest.exists():
            return True, version, f"{f['into']} {version} is current"
        spec = f["asset"][f"{OS}-{ARCH}"]
        with tempfile.TemporaryDirectory(dir=TOOLS) as tmp:
            algo, sha = sha.split(":", 1)
            part, h, size = Path(tmp) / name, hashlib.new(algo), 0
            with _get(url, 120) as r, open(part, "wb") as out:
                while chunk := r.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise RuntimeError(f"{name} is larger than {MAX_DOWNLOAD >> 30} GB")
                    h.update(chunk)
                    out.write(chunk)
            if h.hexdigest() != sha.lower():
                raise RuntimeError(f"{name}: {algo} does not match, not installed")
            new = Path(tmp) / "new"
            if re.search(r"\.(zip|tar\.gz|tgz|tar\.xz)$", name):
                new.mkdir()
                unpack(part, name, new, spec.get("strip", 1) if isinstance(spec, dict) else 1)
            else:
                part.rename(new)
                if os.name != "nt":  # a single program (micromamba) must run; a jar does not mind
                    new.chmod(0o755)
            try:
                if dest.exists():  # a file in use (Windows) fails here, before anything changed
                    dest.rename(Path(tmp) / "old")
            except PermissionError:
                raise RuntimeError(f"{f['into']} is in use (an agent runs it): close it, the next update tries again") from None
            try:
                new.rename(dest)
            except OSError:
                if (Path(tmp) / "old").exists():  # put the old copy back, or the temp folder takes it along
                    (Path(tmp) / "old").rename(dest)
                raise
        save_stamps({**stamps(), f["into"]: stamp})
        return True, version, f"{f['into']} {version} installed"
    except (OSError, ValueError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as e:
        return False, "", f"{f['into']}: {type(e).__name__}: {e}"


def delete(name: str) -> tuple:
    target = TOOLS / name
    try:
        if (TOOLS / ".versions.json").exists():  # first: a delete that fails halfway must make the next install fetch it again
            save_stamps({k: v for k, v in stamps().items() if k != name})
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
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
    line = summary(out, text, bool(s.get("info")))
    return line if line.startswith("optional, not set up: ") else f"{label}: {line[:200]}"


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


def detect(m: dict, agents):
    """A tool installed by hand, not through the companion: there when one of its own checks passes (e.g. a plugin added
    to Claude Code only), then its full check; None when none passes."""
    # a skill tool's check may download and run its installer (npx): only when one of its skill folders is there
    if m.get("skills") and not any((d / n).is_dir() for d in (SKILLS, AGENT_SKILLS, AGY_SKILLS) for n in m["skills"]):
        return None
    if not any(do(m, s)[0] for s in steps(m, "check", agents) if not s.get("info")):
        return None
    return check(m, agents)


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
    for name in m.get("skills", []):  # after every install and update: an update may have replaced the folder
        if (SKILLS / name).is_dir():
            (SKILLS / name / NOT_SHARED).write_text("installed by a knowledge tool add-on: stays on this pc\n", encoding="utf-8")
    got = check(m, agents)
    if skipped:  # never overwrite another program's file, but say so
        got["detail"] = (f"not linked, another program has the name: {', '.join(skipped)}; " + got["detail"])[:300]
    return got


def uses(m: dict) -> set:
    """The folders in {tools} this tool downloads (node is shared by several tools)."""
    return {s["fetch"]["into"] for s in m.get("install", []) if "fetch" in s}


def remove(m: dict, agents, keep=()) -> dict:
    """Every remove step, also after one failed: a registration left behind is worse than a package left behind.
    keep: folders another installed tool still uses; their delete steps are skipped."""
    link(m, on=False)
    failed = [said(s, text, out) for s in steps(m, "remove", agents) if s.get("delete") not in keep
              for good, out, text in [do(m, s)] if not good]
    if failed:
        raise RuntimeError("; ".join(failed)[:500])
    return {"installed": False, "ok": None, "version": "", "detail": ""}


# Drafting a manifest (maintainer side, never run by the companion): the result is a proposal a person reviews and
# commits to plugin/tools/; nothing here installs anything.
REGISTRY = os.environ.get("KNOWLEDGE_MCP_REGISTRY", "https://registry.modelcontextprotocol.io")
LAUNCH = {"npm": ("npx", ["-y"]), "pypi": ("uvx", [])}  # registry type -> runner that fetches "<package>@latest" each start


def _fixed(items: list, what: str) -> list:
    """name/value pairs of the registry's arguments or variables with a fixed value; a required one without a value
    (a token, a path of the user's) cannot go into a public one-click manifest."""
    out = []
    for a in items or []:
        value = a.get("value", a.get("default"))
        if value is None or "{" in str(value):  # "{token}": filled in by the user
            if a.get("isRequired"):
                raise ValueError(f"needs a value of the user's ({what} {a.get('name') or a.get('valueHint') or '?'}): write the manifest by hand")
            continue
        out.append((a.get("name"), str(value)))
    return out


def from_registry(server: dict) -> dict:
    """A draft manifest for an MCP server from the official MCP registry (its server.json): an npm or PyPI package run
    with npx/uvx at its newest version, or else a streamable-http remote, registered with each agent's own
    `mcp add`. ValueError when the entry has neither in a form that needs no user value."""
    tid = re.sub(r"[^a-z0-9-]+", "-", server["name"].rsplit("/", 1)[-1].lower()).strip("-")[:40]
    if not ID.match(tid):
        raise ValueError(f"no usable id in {server['name']!r}")
    pkg = next((p for p in server.get("packages", []) if p.get("registryType") in LAUNCH
                and p.get("transport", {}).get("type", "stdio") == "stdio"), None)
    remote = next((r for r in server.get("remotes", []) if r.get("type") == "streamable-http"), None)
    if pkg:
        runner, pre = LAUNCH[pkg["registryType"]]
        env = [f"{k}={v}" for k, v in _fixed(pkg.get("environmentVariables"), "environment variable")]
        args = [x for k, v in _fixed(pkg.get("packageArguments"), "argument") for x in ((k, v) if k else (v,))]
        _fixed(pkg.get("runtimeArguments"), "runtime argument")  # only the check: npx/uvx get their own flags
        cmd = [runner, *pre, f"{pkg['identifier']}@latest", *args]
        e = lambda flag: [x for kv in env for x in (flag, kv)]
        claude, codex, agy = [tid, *e("-e"), "--", *cmd], [tid, *e("--env"), "--", *cmd], [*e("-e"), tid, "--", *cmd]
        needs = [runner]
    elif remote:
        _fixed(remote.get("headers"), "header")
        claude, codex, agy = ["--transport", "http", tid, remote["url"]], [tid, "--url", remote["url"]], [tid, remote["url"]]
        needs = []
    else:
        raise ValueError(f"{server['name']}: no npm or PyPI package with stdio and no streamable-http remote: write the manifest by hand")
    m = {"name": server.get("title") or tid, "kind": "program", "description": server.get("description", ""),
         "homepage": (server.get("repository") or {}).get("url") or server.get("websiteUrl") or f"{REGISTRY}/v0/servers?search={tid}",
         **({"needs": needs} if needs else {}),
         "install": [{"run": ["claude", "mcp", "remove", "--scope", "user", tid], "agent": "claude", "ok_if": ["no mcp server"]},  # Claude refuses to overwrite
                     {"run": ["claude", "mcp", "add", "--scope", "user", *claude], "agent": "claude"},
                     {"run": ["codex", "mcp", "add", *codex], "agent": "codex"},
                     {"run": ["agy", "mcp", "add", *agy], "agent": "agy"}],
         "check": [{"run": ["claude", "mcp", "get", tid], "agent": "claude"},
                   {"run": ["codex", "mcp", "get", tid], "agent": "codex"},
                   {"run": ["agy", "mcp", "list"], "agent": "agy", "expect": rf"(?m)^{re.escape(tid)}\s"}],
         "remove": [{"run": ["claude", "mcp", "remove", "--scope", "user", tid], "agent": "claude", "ok_if": ["no mcp server"]},
                    {"run": ["codex", "mcp", "remove", tid], "agent": "codex"},
                    {"run": ["agy", "mcp", "remove", tid], "agent": "agy", "ok_if": ["not found"]}]}
    return {"id": tid, "manifest": validate(m)}


def describe(m: dict) -> list:
    """The steps as lines a reviewer reads before approving a draft; "!" marks a step that runs a shell or inline code."""
    out = []
    for key in ("install", "check", "version", "remove"):
        for s in m.get(key, []) if key != "version" else [m["version"]] if "version" in m else []:
            where = " ".join(x for x in (s.get("agent", ""), ",".join(s.get("os", []))) if x) or "all"
            what = (" ".join(s["run"]) if "run" in s else f"fetch {json.dumps(s['fetch'])}" if "fetch" in s else f"delete {s['delete']}")
            risky = "run" in s and (Path(s["run"][0]).stem.lower() in ("sh", "bash", "zsh", "cmd", "powershell", "pwsh")
                                    or any(a in ("-c", "-e", "/c", "-command", "--eval") for a in (x.lower() for x in s["run"][1:])))
            out.append(f"{'!' if risky else ' '} {key:7} [{where}] {what}")
    return out


def self_test():
    global TOOLS, API, RETRY_WAIT, SHIMS, TOKEN, ANDROID, SKILLS
    import io
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    for tid, f in ((f.stem, f) for f in sorted(CATALOG.glob("*.json"))):  # the shipped manifests are valid
        assert ID.match(tid), tid
        validate(json.loads(f.read_text(encoding="utf-8")))
    npm = {"registryType": "npm", "identifier": "@o/srv-mcp", "transport": {"type": "stdio"},
           "environmentVariables": [{"name": "KEY", "isSecret": True}, {"name": "MODE", "isRequired": True, "default": "ro"}],
           "packageArguments": [{"type": "named", "name": "--dir", "value": "."}, {"type": "positional", "valueHint": "x"}]}
    draft = from_registry({"name": "io.github.o/Srv_MCP", "packages": [{"registryType": "oci"}, npm], "remotes": [{"type": "streamable-http", "url": "https://u"}]})
    run_of = lambda m, agent: [s["run"] for s in m["install"] if s.get("agent") == agent][-1]
    assert draft["id"] == "srv-mcp" and draft["manifest"]["needs"] == ["npx"], draft  # the package wins over the remote
    assert run_of(draft["manifest"], "claude")[4:] == ["user", "srv-mcp", "-e", "MODE=ro", "--", "npx", "-y", "@o/srv-mcp@latest", "--dir", "."], draft
    assert run_of(draft["manifest"], "agy")[3:6] == ["-e", "MODE=ro", "srv-mcp"]  # agy wants flags before the name
    http = from_registry({"name": "com.x/y", "remotes": [{"type": "streamable-http", "url": "https://u", "headers": [{"name": "Authorization", "isSecret": True}]}]})["manifest"]
    assert run_of(http, "codex") == ["codex", "mcp", "add", "y", "--url", "https://u"] and "needs" not in http, http
    for bad in ({"name": "a/b", "packages": [{**npm, "environmentVariables": [{"name": "TOKEN", "isRequired": True, "value": "{t}"}]}]},
                {"name": "a/b", "packages": [{"registryType": "oci", "identifier": "img"}]}):
        try:
            from_registry(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    lines = describe({"name": "x", "install": [{"run": ["bash", "-c", "curl x | sh"]}, {"run": ["npm", "i"], "agent": "codex", "os": ["linux"]}]})
    assert lines[0].startswith("! install [all] bash") and lines[1] == "  install [codex linux] npm i", lines
    with tempfile.TemporaryDirectory() as d:
        mark, py = Path(d) / "mark", sys.executable
        put = lambda text: [py, "-c", f"open({str(mark)!r}, 'a').write({text!r})"]
        m = validate({"name": "Fixture", "kind": "program", "needs": [Path(py).name],
             "install": [{"run": [py, "-c", "import os, sys; sys.exit(os.environ.get('KN_T') != 'x')"], "env": {"KN_T": "x"}}, {"run": put("i")}, {"run": put("C"), "agent": "claude"}, {"run": put("X"), "agent": "codex"},
                         {"run": put("W"), "os": ["windows"]}, {"run": put("P"), "os": ["linux", "darwin"]},
                         {"run": [py, "-c", "import sys; print('already there'); sys.exit(1)"], "ok_if": ["already"]}],
             "check": [{"run": [py, "-c", "print('{\"checks\": [{\"name\": \"node\", \"ok\": true}]}')"], "expect": "node"},
                       {"run": [py, "-c", "import sys; print('{\"checks\": [{\"name\": \"ghidra\", \"ok\": false}]}'); sys.exit(1)"], "info": True}],
             "version": {"run": [py, "-c", "print('{\"version\": \"1.2.3\"}')"]},
             "remove": [{"run": [py, "-c", "import sys; sys.exit(3)"]}, {"run": put("r")}]})
        skills_was, SKILLS = SKILLS, Path(d) / "skills"
        try:  # a skill the tool installed is marked to stay on this pc; a folder it does not name is left alone
            (SKILLS / "fx-skill").mkdir(parents=True)
            (SKILLS / "other").mkdir()
            got = install({**m, "skills": ["fx-skill", "fx-gone"]}, here(["claude", "agy"]))
            assert (SKILLS / "fx-skill" / NOT_SHARED).exists() and not (SKILLS / "other" / NOT_SHARED).exists() and not (SKILLS / "fx-gone").exists()
            mark.write_text("")
            assert detect({**m, "skills": ["fx-gone"], "check": [{"run": put("c")}]}, []) is None and mark.read_text() == "", \
                "a skill tool whose folder is missing is not looked for: its check may download and run its installer"
            assert detect({**m, "skills": ["fx-skill"]}, [])["ok"] is True
        finally:
            SKILLS = skills_was
        mark.write_text("")
        got = install(m, here(["claude", "agy"]))
        assert mark.read_text() == "iC" + ("W" if OS == "windows" else "P"), mark.read_text()  # codex is not here
        assert got["ok"] is True and got["version"] == "1.2.3" and got["detail"] == "optional, not set up: ghidra", got  # info only
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
        assert summary(doc, "", True) == "optional, not set up: ghidra-version", summary(doc, "", True)  # no cut-off detail
        assert check(bad, [])["ok"] is False and "Install Node.js 22" in check(bad, [])["detail"]
        assert detect(bad, []) is None and detect(m, [])["ok"] is True, "a hand-installed tool is found, a missing one not"
        half = {**m, "check": [*m["check"], {"run": [py, "-c", "import sys; sys.exit(1)"], "agent": "codex"}]}
        assert detect(half, ["codex"])["ok"] is False, "in one agent only: found, but not ok"
        for broken, why in (({**m, "needs": ["no-such-command-xyz"]}, "needs no-such-command-xyz"),
                            ({**m, "install": [{"run": ["no-such-command-xyz"]}]}, "not installed on this pc"),
                            ({**m, "install": [{"run": put("x"), "agent": "agy"}]}, "nothing to install")):
            try:
                install(broken, [])
                raise AssertionError(why)
            except RuntimeError as e:
                assert why in str(e), e
        for typo in ({**m, "instal": []}, {k: v for k, v in m.items() if k != "kind"}, {**m, "kind": "plugin"}, {"name": "x", "install": [{"run": "npm i"}]}, {"name": "x", "install": [{"run": ["a"], "agent": "cursor"}]}, {**m, "skills": ["../x"]}, {**m, "skills": "x"},
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
        with tempfile.TemporaryDirectory() as t2:  # links in a chain that together leave the folder, and a hard link out
            for entries in ([("top/a/b", tarfile.SYMTYPE, ".."), ("top/a/b/c", tarfile.SYMTYPE, ".."), ("top/a/b/c/ESCAPED", tarfile.REGTYPE, "")],
                            [("top/h", tarfile.LNKTYPE, "../../etc/passwd")]):
                b = io.BytesIO()
                with tarfile.open(fileobj=b, mode="w:gz") as t:
                    for n, kind, link in entries:
                        i = tarfile.TarInfo(n)
                        i.type, i.linkname = kind, link
                        t.addfile(i, io.BytesIO(b"") if kind == tarfile.REGTYPE else None)
                (Path(t2) / "c.tar.gz").write_bytes(b.getvalue())
                out = Path(t2) / "deep" / "dest"
                out.mkdir(parents=True, exist_ok=True)
                try:
                    unpack(Path(t2) / "c.tar.gz", "c.tar.gz", out, 1)
                    raise AssertionError(f"refused: {entries}")
                except RuntimeError:
                    pass
                assert not list(Path(t2).rglob("ESCAPED")), "nothing written outside"
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
                if self.headers.get("Authorization") == "Bearer expired":
                    self.send_response(401)
                    self.end_headers()
                    return
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
                    f"{sha(b'JAR')}  tool.jar\n{sha(b'x')}  tool-other.jar\n".encode() if self.path == "/dl/SHASUMS256.txt" else \
                    (f'<r><remotePackage path="platform-tools"><revision><major>9</major><minor>1</minor></revision><channelRef ref="channel-0"/>'
                     f'<archives><archive><complete><checksum>{hashlib.sha1(good).hexdigest()}</checksum><url>engine_1.0.zip</url></complete></archive></archives>'
                     f'</remotePackage></r>').encode() if self.path == "/android/repository2-1.xml" else \
                    good if self.path == "/android/engine_1.0.zip" else None
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
            (TOOLS / ".versions.json").write_text('{"engine": "cut off')  # a crash mid-write: fetched again, not broken
            assert fetch(eng)[2] == "engine v1.0 installed" and fetch(eng)[2] == "engine v1.0 is current"
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
            TOKEN = "expired"
            try:
                assert fetch({**eng, "into": "engine2"})[0], "an expired token falls back to no token"
            finally:
                TOKEN = ""
            assert fetch({**eng, "asset": {"linux-arm64" if plat != "linux-arm64" else "darwin-x64": "x"}})[2].endswith("skipped")
            state["digest"] = "sha256:" + "0" * 64
            (TOOLS / ".versions.json").write_text("{}")
            ok, _, text = fetch(eng)
            assert not ok and "does not match" in text and (TOOLS / "engine" / "README").exists(), text  # the old copy stays
            assert "no sha256" in fetch({**eng, "github": "o/old"})[2]
            ok, _, text = fetch({"github": "o/evil", "asset": {plat: "evil"}, "into": "evil"})
            assert not ok and "outside" in text and not (Path(d) / "escape").exists() and not (TOOLS / "evil").exists(), text
            ANDROID = API + "/android/"
            ok, version, text = fetch({"android": "platform-tools", "asset": {plat: r"engine_.*\.zip$"}, "into": "platform-tools"})
            assert ok and version == "9.1" and (TOOLS / "platform-tools" / "README").exists(), text  # Google's sha1 checked
            ok, version, text = fetch({"shasums": API + "/dl/SHASUMS256.txt", "asset": {plat: r"^tool\.jar$"}, "into": "tool.jar"})
            assert ok and (TOOLS / "tool.jar").read_bytes() == b"JAR", text
            assert delete("engine")[0] and not (TOOLS / "engine").exists() and "engine" not in stamps()
            (TOOLS / "shared").mkdir()
            (TOOLS / "own").mkdir()
            save_stamps({**stamps(), "shared": "x", "half": "y"})
            remove({"name": "x", "remove": [{"delete": "shared"}, {"delete": "own"}]}, [], keep={"shared"})
            assert (TOOLS / "shared").is_dir() and not (TOOLS / "own").exists(), "a folder another installed tool uses stays"
            assert uses({"install": [{"fetch": {"into": "node"}}, {"run": ["a"]}]}) == {"node"}
            (TOOLS / "half").mkdir()
            real_rmtree, shutil.rmtree = shutil.rmtree, lambda p: (_ for _ in ()).throw(OSError("in use"))
            try:
                assert not delete("half")[0] and "half" not in stamps(), "a delete that fails halfway still forgets the version"
            finally:
                shutil.rmtree = real_rmtree
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
    elif len(sys.argv) == 3 and sys.argv[1] == "--from-registry":  # print a draft manifest; a person reviews it
        try:
            with _get(f"{REGISTRY}/v0/servers/{urllib.parse.quote(sys.argv[2], safe='')}/versions/latest") as r:
                draft = from_registry(json.load(r)["server"])
        except (OSError, ValueError, KeyError) as e:
            sys.exit(f"{sys.argv[2]}: {e}")
        print(f"plugin/tools/{draft['id']}.json:", file=sys.stderr)
        m = draft["manifest"]  # one step per line, like the shipped manifests
        print("{\n" + ",\n".join(f"  {json.dumps(k)}: " + ("[\n" + ",\n".join(f"    {json.dumps(s)}" for s in v) + "\n  ]"
                                    if k in ("install", "check", "remove") else json.dumps(v)) for k, v in m.items()) + "\n}")
    elif len(sys.argv) == 3 and sys.argv[1] == "--validate":  # a draft before it is committed: valid, and what it runs
        try:
            if not ID.match(Path(sys.argv[2]).stem):
                raise ValueError(f"the file name is the id: {ID.pattern}")
            print("\n".join(describe(validate(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))))))
        except (OSError, ValueError) as e:
            sys.exit(f"{sys.argv[2]}: {e}")
    else:
        sys.exit("usage: tools.py --self-test | --from-registry <name in the MCP registry> | --validate <manifest.json>")
