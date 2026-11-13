#!/usr/bin/env python3
"""More than one Claude account on one pc: each extra account is a Claude Code config folder of its own
(~/.knowledge/claude-accounts/<name>, used as CLAUDE_CONFIG_DIR for a chat that picks it). Only the sign-in is its
own: everything else in ~/.claude (settings, plugins, skills, CLAUDE.md, projects with the sessions) is linked in,
so a chat keeps its plugin and can switch accounts between turns and still resume its session.

Two ways to sign an account in from the web UI (Machines > Claude Code > Accounts):
- browser: `claude auth login` runs here; its sign-in URL goes to the page, the code claude.com shows after signing in
  comes back and goes to its stdin (the code only works with this run's own PKCE verifier).
- long-lived token: `claude setup-token` run anywhere, the token pasted; kept here in the account's folder and given
  to a chat as CLAUDE_CODE_OAUTH_TOKEN (Claude Code prefers it over a stored sign-in).
- API key (Anthropic Console, billed per use): pasted, kept the same way, given as ANTHROPIC_API_KEY.

  accounts.py --self-test   the checks of this file (a fake claude)
Standard library only; Linux, macOS and Windows.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
import token_store  # noqa: E402

ROOT = token_store.KN_HOME / "claude-accounts"
MAIN = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")  # the pc's own (default) account
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
OWN = "knowledge-"  # our files in an account folder: never linked from the main folder
INFO, TOKEN, USAGE = OWN + "account.json", OWN + "token", OWN + "usage.json"
# kind -> (file, variable, form, what to paste); at most one of them per account
SECRETS = {"token": (TOKEN, "CLAUDE_CODE_OAUTH_TOKEN", r"sk-ant-oat\d\d-[\w-]{20,400}", "a token from claude setup-token (sk-ant-oat01-...)"),
           "apikey": (OWN + "api-key", "ANTHROPIC_API_KEY", r"sk-ant-api\d\d-[\w-]{20,400}", "an API key from the Anthropic Console (sk-ant-api03-...)")}
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")  # the pc's own, never passed to another account
# never shared: the sign-in itself, Claude's per-account state (.claude.json and its backups), and the background
# daemon, which would otherwise take a run of this account under the default one
NOT_LINKED = {".credentials.json", ".claude.json", "backups", "daemon", "daemon.lock", "daemon.log", "daemon.status.json"}
URL = re.compile(r"https://\S+/oauth/authorize\?\S+")
STATUS_TTL = 6 * 3600
LOGIN_TTL = 15 * 60  # a sign-in nobody finished: its process ends, the page shows "Sign in" again
LOGINS: dict = {}  # name -> {"p", "url", "out", "at"}: sign-ins waiting for their code
_STATUS: dict = {}  # folder -> (at, {"email", "plan"})
LOCK = threading.Lock()


def folder(name: str) -> Path:
    """The account's folder; ValueError for a bad name or one outside ROOT (never ROOT itself)."""
    if not ID.fullmatch(name or ""):
        raise ValueError("an account name is a-z, 0-9 and -, at most 31 characters")
    d = ROOT / name
    if d.resolve().parent != ROOT.resolve():
        raise ValueError(f"{name} is not under {ROOT}")
    return d


def is_link(p: Path) -> bool:
    """A symlink, or on Windows a directory junction: removing it never touches what it points to."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def link(src: Path, dst: Path):
    if os.name != "nt":
        os.symlink(src, dst)
    elif src.is_dir():  # a junction needs no admin rights or developer mode, a symlink does
        import _winapi
        _winapi.CreateJunction(str(src), str(dst))
    else:  # shortcut: a copy, refreshed before every run; what Claude writes into it there is lost. Use symlinks if that matters.
        shutil.copy2(src, dst)


def sync(d: Path):
    """Link what the main folder has into account folder d (new entries too). A file Claude replaced there (a write
    by rename takes the place of the link) is linked again: the main folder's settings hold for every account."""
    d.mkdir(parents=True, exist_ok=True)
    for src in MAIN.iterdir() if MAIN.is_dir() else []:
        if src.name in NOT_LINKED or src.name.startswith(OWN):
            continue
        dst = d / src.name
        if src.is_file() and dst.is_file() and not is_link(dst):
            if os.name == "nt":  # copy2 keeps the time, but NTFS times move in ~15 ms steps: a small file is compared too
                a, b = src.stat(), dst.stat()
                if (a.st_size, a.st_mtime_ns) != (b.st_size, b.st_mtime_ns) or a.st_size < 1 << 20 and src.read_bytes() != dst.read_bytes():
                    shutil.copy2(src, dst)
            else:
                dst.unlink()
                link(src, dst)
        elif not (dst.exists() or is_link(dst)):
            link(src, dst)


def plugin_secrets(d: Path):
    """Give account folder d the plugin's secret options (the knowledge token on a pc without a keyring), which live
    next to the sign-in; right after the sign-in only, so it never races Claude refreshing its token in a chat.
    shortcut: a knowledge token changed later is not copied; sign the account in again (or use a keyring)."""
    if sys.platform == "darwin":  # macOS keeps both in the Keychain; the knowledge token comes from the keyring there
        return
    try:
        secrets = json.loads((MAIN / ".credentials.json").read_text(encoding="utf-8")).get("pluginSecrets")
        mine = d / ".credentials.json"
        cur = json.loads(mine.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return  # not signed in yet: the next run, after the sign-in, copies them
    if secrets and cur.get("pluginSecrets") != secrets:
        write(mine, {**cur, "pluginSecrets": secrets})


def write(f: Path, data: dict):
    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f.name + ".")  # owner only, and two writers never share one
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out)
    os.replace(tmp, f)


def info(d: Path) -> dict:
    try:
        return json.loads((d / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def names() -> list:
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir() and ID.fullmatch(p.name)) if ROOT.is_dir() else []


def env(name: str) -> dict:
    """What a Claude run under account `name` adds to its environment; {} for the default account."""
    if not name:
        return {}
    d = folder(name)
    if not d.is_dir():
        raise RuntimeError(f"there is no Claude account {name} on this pc (Machines > Claude Code > Accounts)")
    sync(d)
    out = {"CLAUDE_CONFIG_DIR": str(d)}
    for f, var, _, _ in SECRETS.values():
        try:
            out[var] = (d / f).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return out


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in AUTH_ENV}


def command(*args) -> list:
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude is not on PATH")
    cmd = [exe, *args]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        import workers
        return workers.shim_line(cmd)
    return cmd


def status(d: Path, fresh: bool = False) -> dict:
    """{"email", "plan"} of the sign-in in folder d (`claude auth status`), kept for STATUS_TTL."""
    at, got = _STATUS.get(str(d), (0, None))
    if got is not None and not fresh and time.time() - at < STATUS_TTL:
        return got
    e = clean_env() if d != MAIN else dict(os.environ)  # the pc's own account is what its own environment says
    try:
        r = subprocess.run(command("auth", "status", "--json"), capture_output=True, text=True, timeout=30, encoding="utf-8",
                           env={**e, **({"CLAUDE_CONFIG_DIR": str(d)} if d != MAIN else {})})
        s = json.loads(r.stdout or "{}")
        got = {"email": str(s.get("email") or "")[:120] if s.get("loggedIn") else "",
               "plan": str(s.get("planTier") or s.get("subscriptionType") or "")[:40] if s.get("loggedIn") else ""}
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError, AttributeError):
        got = got or {"email": "", "plan": ""}
    _STATUS[str(d)] = (time.time(), got)
    return got


def _drop(name: str):
    rec = LOGINS.pop(name, None)
    if rec and rec["p"].poll() is None:
        if os.name == "nt":  # the .cmd shim's cmd.exe: its node child would go on
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(rec["p"].pid)], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rec["p"].kill()


def login(name: str, wait: float = 30) -> str:
    """Start a browser sign-in for account `name` (made when new); the URL to open. The code goes in with code()."""
    d = folder(name)
    sync(d)
    with LOCK:
        _drop(name)
        e = clean_env()
        # BROWSER: a pc without a desktop session must not start one (xdg-open honours it); the page opens the link
        p = subprocess.Popen(command("auth", "login", "--claudeai"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env={**e, "CLAUDE_CONFIG_DIR": str(d), "BROWSER": "true" if os.name != "nt" else e.get("BROWSER", "")},
                             text=True, encoding="utf-8", errors="replace")
        rec = LOGINS[name] = {"p": p, "url": "", "out": [], "at": time.time()}

    def read():
        for line in p.stdout:
            rec["out"].append(line.rstrip())
            if not rec["url"] and (m := URL.search(line)):
                rec["url"] = m.group(0)
    threading.Thread(target=read, daemon=True).start()
    end = time.time() + wait
    while not rec["url"] and p.poll() is None and time.time() < end:
        time.sleep(0.1)
    if not rec["url"]:
        with LOCK:
            _drop(name)
        raise RuntimeError("claude auth login gave no sign-in link: " + " ".join(rec["out"])[-200:])
    write(d / INFO, {**info(d), "kind": "login", "error": ""})
    return rec["url"]


def code(name: str, text: str, wait: float = 90) -> str:
    """The code from the sign-in page into the waiting `claude auth login`; who is signed in now."""
    d = folder(name)
    with LOCK:
        rec = LOGINS.get(name)
        if not rec or rec["p"].poll() is not None:
            LOGINS.pop(name, None)
            raise RuntimeError("no sign-in waits for a code here (it ends after 15 minutes): press Sign in again")
        try:
            rec["p"].stdin.write(text.strip() + "\n")
            rec["p"].stdin.flush()
        except OSError as e:
            raise RuntimeError(f"the sign-in ended: {e}") from e
    try:
        rc = rec["p"].wait(wait)
    except subprocess.TimeoutExpired:
        rc = None
    with LOCK:
        if LOGINS.get(name) is rec:
            _drop(name)
    if rc != 0:
        said = next((x for x in reversed(rec["out"]) if x.strip()), "no answer")
        write(d / INFO, {**info(d), "error": said[-300:]})
        raise RuntimeError(f"sign-in failed: {said[-200:]}")
    for f, _, _, _ in SECRETS.values():  # a browser sign-in replaces a token or key
        (d / f).unlink(missing_ok=True)
    plugin_secrets(d)
    s = status(d, fresh=True)
    write(d / INFO, {**info(d), "kind": "login", "error": "", **s})
    return f"{name}: signed in" + (f" as {s['email']}" if s.get("email") else "")


def set_secret(name: str, kind: str, value: str) -> str:
    """A long-lived token (`claude setup-token`) or an API key for account `name`, kept in its folder (owner only);
    it replaces the other one, and a browser sign-in there stops counting (the variable wins over it)."""
    f, _, form, what = SECRETS[kind]
    if not re.fullmatch(form, value or ""):
        raise ValueError(f"that is not {what}")
    d = folder(name)
    sync(d)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(value)
    os.replace(tmp, d / f)
    for other, *_ in SECRETS.values():
        if other != f:
            (d / other).unlink(missing_ok=True)
    with LOCK:
        _drop(name)
    write(d / INFO, {**{k: v for k, v in info(d).items() if k not in ("email", "plan")}, "kind": kind, "error": ""})
    return f"{name}: {'long-lived token' if kind == 'token' else 'API key'} saved"


def remove(name: str) -> str:
    """Sign account `name` out and delete its folder: links are unlinked first, what they point to stays."""
    d = folder(name)
    if not d.is_dir():
        raise RuntimeError(f"there is no Claude account {name} on this pc")
    with LOCK:
        _drop(name)
    # only a browser sign-in: a token account has none, and on macOS (the Keychain) its folder is the only trace
    if (d / ".credentials.json").exists() or sys.platform == "darwin" and info(d).get("kind") == "login":
        e = clean_env()
        try:
            subprocess.run(command("auth", "logout"), capture_output=True, timeout=30, env={**e, "CLAUDE_CONFIG_DIR": str(d)})
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
    for p in d.iterdir():
        if is_link(p):
            os.rmdir(p) if os.name == "nt" and p.is_dir() else os.unlink(p)  # a junction goes with rmdir, its target stays
    shutil.rmtree(d)
    _STATUS.pop(str(d), None)
    return f"{name}: removed"


def listing(kept_windows=None, now: float = 0) -> list:
    """The accounts for the Machines list: the default one first, then each extra one with its sign-in state, a
    sign-in link waiting for its code, and its usage from chat turns (kept_windows(file, now))."""
    now = now or time.time()
    with LOCK:
        for n, rec in list(LOGINS.items()):
            if now - rec["at"] > LOGIN_TTL or rec["p"].poll() is not None:
                _drop(n)
    out = [{"id": "", **status(MAIN)}]
    for n in names():
        d, i = ROOT / n, info(ROOT / n)
        kind = next((k for k, (f, *_) in SECRETS.items() if (d / f).exists()), "") \
            or ("login" if (d / ".credentials.json").exists() or sys.platform == "darwin" and i.get("email") else "")
        s = status(d) if kind == "login" else {}
        out.append({"id": n, "kind": kind, "email": s.get("email") or "", "plan": s.get("plan") or "", "error": i.get("error") or "",
                    **({"login": LOGINS[n]["url"]} if n in LOGINS and LOGINS[n]["url"] else {}),
                    "windows": [list(w) for w in kept_windows(d / USAGE, now)] if kept_windows else []})
    return out


def keep_usage(name: str, got: list):
    """A chat turn's usage reading for an extra account (the default one's goes to the companion's own file)."""
    try:
        (folder(name) / USAGE).write_text(json.dumps({"windows": got, "at": time.time()}), encoding="utf-8")
    except (OSError, ValueError):
        pass


def self_test():
    global ROOT, MAIN
    tmp = Path(tempfile.mkdtemp())
    try:
        ROOT, MAIN = tmp / "accounts", tmp / "main"
        (MAIN / "plugins").mkdir(parents=True)
        (MAIN / "projects" / "p").mkdir(parents=True)
        (MAIN / "settings.json").write_text("{}", encoding="utf-8")
        write(MAIN / ".credentials.json", {"claudeAiOauth": {"accessToken": "main"}, "pluginSecrets": {"knowledge@knowledge": {"token": "kt"}}})
        (MAIN / "daemon.lock").write_text("", encoding="utf-8")
        for bad in ("", "Werk", "../x", "a/b", "x" * 32):
            try:
                folder(bad)
                raise AssertionError(f"{bad!r} must be refused")
            except ValueError:
                pass
        assert env("") == {}, "the default account changes nothing"
        try:
            env("werk")
            raise AssertionError("an unknown account must be refused")
        except RuntimeError:
            pass
        d = folder("werk")
        sync(d)
        assert (d / "plugins").is_dir() and (d / "projects" / "p").is_dir() and (d / "settings.json").exists(), "the main folder is linked in"
        assert not (d / ".credentials.json").exists() and not (d / "daemon.lock").exists(), "the sign-in and the daemon are not shared"
        (MAIN / "skills").mkdir()
        assert env("werk") == {"CLAUDE_CONFIG_DIR": str(d)} and (d / "skills").is_dir(), "a later entry is linked on the next run"
        (d / "settings.json").unlink()
        (d / "settings.json").write_text('{"x": 1}', encoding="utf-8")  # Claude wrote it by rename: no longer the link
        (MAIN / "settings.json").write_text('{"y": 2}', encoding="utf-8")
        sync(d)
        assert (d / "settings.json").read_text(encoding="utf-8") == '{"y": 2}' and (MAIN / "settings.json").read_text(encoding="utf-8") == '{"y": 2}', \
            "the main folder's settings come back, the main file untouched"
        write(d / ".credentials.json", {"claudeAiOauth": {"accessToken": "werk"}})
        plugin_secrets(d)
        creds = json.loads((d / ".credentials.json").read_text(encoding="utf-8"))
        if sys.platform != "darwin":
            assert creds == {"claudeAiOauth": {"accessToken": "werk"}, "pluginSecrets": {"knowledge@knowledge": {"token": "kt"}}}, creds
        # the browser sign-in, with a fake claude that prints a link and takes one good code
        fake = tmp / "bin"
        fake.mkdir()
        script = fake / "fake_claude.py"
        script.write_text(
            "import json, os, sys\n"
            "d = os.environ.get('CLAUDE_CONFIG_DIR', '')\n"
            "if sys.argv[1:3] == ['auth', 'login']:\n"
            "    print('If the browser did not open, visit: https://claude.com/cai/oauth/authorize?code=true&state=x', flush=True)\n"
            "    print('Paste code here if prompted > ', end='', flush=True)\n"
            "    c = sys.stdin.readline().strip()\n"
            "    if c != 'good#code':\n"
            "        print('Login failed: That code was not accepted.'); sys.exit(1)\n"
            "    json.dump({'claudeAiOauth': {'accessToken': 'new'}}, open(os.path.join(d, '.credentials.json'), 'w')); print('Login successful.')\n"
            "elif sys.argv[1:3] == ['auth', 'status']:\n"
            "    ok = not d or os.path.exists(os.path.join(d, '.credentials.json'))  # no folder: the default account\n"
            "    print(json.dumps({'loggedIn': ok, 'email': 'w@example.org' if 'accounts' in d else 'me@example.org', 'planTier': 'max_5x'}))\n"
            "elif sys.argv[1:3] == ['auth', 'logout']:\n"
            "    os.remove(os.path.join(d, '.credentials.json'))\n", encoding="utf-8")
        if os.name == "nt":
            (fake / "claude.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            (fake / "claude").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            (fake / "claude").chmod(0o755)
        os.environ["PATH"] = str(fake) + os.pathsep + os.environ["PATH"]
        url = login("thuis")
        assert url == "https://claude.com/cai/oauth/authorize?code=true&state=x", url
        assert {a["id"]: a for a in listing()}["thuis"]["login"] == url, "the waiting link shows on the page"
        try:
            code("thuis", "bad")
            raise AssertionError("a bad code must fail")
        except RuntimeError as e:
            assert "not accepted" in str(e), e
        assert "thuis" not in LOGINS and info(folder("thuis"))["error"], "a failed sign-in ends and says why"
        login("thuis")
        assert code("thuis", "good#code") == "thuis: signed in as w@example.org"
        acc = {a["id"]: a for a in listing()}
        assert acc[""]["email"] == "me@example.org" and acc["thuis"]["kind"] == "login" and "login" not in acc["thuis"], acc
        try:
            code("thuis", "good#code")
            raise AssertionError("a code without a waiting sign-in must fail")
        except RuntimeError:
            pass
        # the long-lived token
        for bad in ("", "sk-ant-api03-" + "x" * 30, "sk-ant-oat01-short", "sk-ant-oat01-" + "x" * 30 + " y"):
            try:
                set_secret("werk", "token", bad)
                raise AssertionError(f"{bad!r} must be refused")
            except ValueError:
                pass
        key = "sk-ant-api03-" + "k" * 40
        try:
            set_secret("werk", "apikey", "sk-ant-oat01-" + "a" * 40)
            raise AssertionError("a token is not an API key")
        except ValueError:
            pass
        set_secret("werk", "apikey", key)
        assert env("werk")["ANTHROPIC_API_KEY"] == key and "CLAUDE_CODE_OAUTH_TOKEN" not in env("werk")
        assert {a["id"]: a for a in listing()}["werk"]["kind"] == "apikey"
        tok = "sk-ant-oat01-" + "a" * 40
        set_secret("werk", "token", tok)
        assert env("werk")["CLAUDE_CODE_OAUTH_TOKEN"] == tok and "ANTHROPIC_API_KEY" not in env("werk"), "a token replaces the key"
        if os.name != "nt":
            assert stat.S_IMODE((folder("werk") / TOKEN).stat().st_mode) == 0o600, "only the owner reads the token"
        keep_usage("werk", [[40.0, "5h", time.time() + 99]])
        acc = {a["id"]: a for a in listing(lambda f, now: json.loads(f.read_text())["windows"] if f.exists() else [])}
        assert acc["werk"]["kind"] == "token" and acc["werk"]["windows"][0][0] == 40.0, acc["werk"]
        # removing keeps everything the links point to
        assert remove("werk") == "werk: removed" and remove("thuis") == "thuis: removed"
        assert (MAIN / "plugins").is_dir() and (MAIN / "projects" / "p").is_dir() and (MAIN / "settings.json").exists() \
            and (MAIN / "skills").is_dir() and json.loads((MAIN / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"] == "main", \
            "the main folder is untouched"
        assert names() == [], names()
    finally:
        for n in list(LOGINS):
            _drop(n)
        shutil.rmtree(tmp, ignore_errors=True)
    print("accounts self-test ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        print(__doc__)
