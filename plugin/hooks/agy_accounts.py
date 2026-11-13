#!/usr/bin/env python3
"""More than one agy (Antigravity CLI) account on one Linux pc, as accounts.py does for Claude: each extra account is
a gemini folder of its own (~/.knowledge/agy-accounts/<name>), given to `agy acp --gemini-dir`. Its folders (config
with the plugins and skills, conversations, brain) are links to ~/.gemini, so a chat keeps its plugin and resumes its
conversation after a switch; its sign-in, logs and databases are its own.

agy keeps its sign-in in the OS keyring (go-keyring: service gemini, user antigravity) whatever folder it is given,
and only falls back to a file in its folder when the keyring cannot be reached. So every agy run of an extra account
gets a session bus address that leads nowhere (DBUS_OFF): without it, it would run as the pc's own account, and a
sign-in would replace the pc's own in the keyring. Its shell commands inherit that too (no keyring for them, e.g. a
git credential helper on libsecret); the knowledge plugin gets its token as KNOWLEDGE_TOKEN instead.
shortcut: Linux only (macOS Keychain and Windows Credential Manager cannot be hidden from one process); workers and
`agy -p` (no --gemini-dir) stay on the pc's own sign-in.

Sign-in from the web UI (Machines > agy > Accounts): `agy acp --login` runs here; its Google link goes to the page,
the code (or the whole address) the page lands on comes back and goes to its stdin.

  agy_accounts.py --self-test   the checks of this file (a fake agy)
Standard library only.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
import accounts  # noqa: E402
import token_store  # noqa: E402

ROOT = token_store.KN_HOME / "agy-accounts"
MAIN = Path.home() / ".gemini"  # the pc's own: agy's default --gemini-dir
APP = "antigravity-cli"  # agy's --app-data-dir under the gemini folder
ID = accounts.ID
INFO = accounts.INFO
SUPPORTED = sys.platform.startswith("linux")
DBUS_OFF = "unix:path=/nonexistent/knowledge-agy-account-bus"
# under APP: only folders are linked (a file there may be a database, whose -wal beside a link would be another one),
# and of those not what is per process or per account
NOT_LINKED = {"log", "crashes", "updater", "cache", "presence"}
FILES_LINKED = {"settings.json"}  # agy's model and settings: the pc's own hold for every account
URL = re.compile(r"https://accounts\.google\.com/o/oauth2/[^\s\x00-\x1f\x7f]+")
KEY = accounts.OWN + "api-key"  # a Gemini API key (billed per use): given as GEMINI_API_KEY
KEY_FORM = r"AIza[\w-]{35}"
TOKEN = "antigravity-oauth-token"  # under APP: where agy keeps its sign-in without a keyring
LOGIN_TTL = accounts.LOGIN_TTL
LOGINS: dict = {}  # name -> {"p", "url", "out", "at", "by"}
LOCK = threading.Lock()


def folder(name: str) -> Path:
    """The account's gemini folder; ValueError for a bad name or one outside ROOT (never ROOT itself)."""
    if not ID.fullmatch(name or ""):
        raise ValueError("an account name is a-z, 0-9 and -, at most 31 characters")
    d = ROOT / name
    if d.resolve().parent != ROOT.resolve():
        raise ValueError(f"{name} is not under {ROOT}")
    return d


def names() -> list:
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir() and ID.fullmatch(p.name)) if ROOT.is_dir() else []


def _link_in(src_dir: Path, dst_dir: Path, take):
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in src_dir.iterdir() if src_dir.is_dir() else []:
        dst = dst_dir / src.name
        if take(src) and not (dst.exists() or accounts.is_link(dst)):
            os.symlink(src, dst)


def sync(d: Path):
    """Link the pc's own gemini folder into account folder d: every folder at the top (config: plugins, skills) and
    GEMINI.md; under antigravity-cli its folders but those per process, and its settings. A file of the account
    itself (its sign-in) is never replaced."""
    _link_in(MAIN, d, lambda p: p.name != APP and (p.is_dir() or p.name == "GEMINI.md"))
    _link_in(MAIN / APP, d / APP, lambda p: p.is_dir() and p.name not in NOT_LINKED or p.name in FILES_LINKED)


def env(name: str) -> dict:
    """What an agy run under account `name` adds to its environment ({} for the pc's own)."""
    if not name:
        return {}
    if not SUPPORTED:
        raise RuntimeError("agy accounts work on Linux only")
    d = folder(name)
    if not d.is_dir():
        raise RuntimeError(f"there is no agy account {name} on this pc (Machines > agy > Accounts)")
    if accounts.info(d).get("removing"):
        raise RuntimeError(f"the agy account {name} is removed once its running chats end: pick another")
    sync(d)
    tok = os.environ.get("KNOWLEDGE_TOKEN") or token_store.from_keyring()  # read here: the run has no keyring
    try:
        key = (d / KEY).read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    return {"DBUS_SESSION_BUS_ADDRESS": DBUS_OFF, **({"KNOWLEDGE_TOKEN": tok} if tok else {}), **({"GEMINI_API_KEY": key} if key else {})}


def set_key(name: str, key: str, by: str = "") -> str:
    """A Gemini API key for account `name` (made when new): its runs get it as GEMINI_API_KEY, and its settings.json
    becomes a copy of the pc's own with modelProvider "gemini" (agy uses the key only then; tested 1.5.8)."""
    if not SUPPORTED:
        raise RuntimeError("agy accounts work on Linux only")
    if not re.fullmatch(KEY_FORM, key or ""):
        raise ValueError("that is not a Gemini API key (AIza..., from aistudio.google.com/apikey)")
    d = folder(name)
    sync(d)
    with LOCK:
        _drop(name)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=KEY + ".")  # owner only
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(key)
    os.replace(tmp, d / KEY)
    f = d / APP / "settings.json"
    try:
        cur = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    if accounts.is_link(f):
        f.unlink()  # its own copy from now on: the provider is this account's, never the pc's own
    accounts.write(f, {**cur, "modelProvider": "gemini"})
    accounts.write(d / INFO, {**{k: v for k, v in accounts.owned(d, by).items() if k != "email"}, "kind": "apikey", "error": "", "at": int(time.time())})
    return f"{name}: API key saved"


def drop_key(d: Path):
    """Back to a Google sign-in: the key goes and settings.json is linked to the pc's own again (next sync)."""
    (d / KEY).unlink(missing_ok=True)
    f = d / APP / "settings.json"
    if f.exists() and not accounts.is_link(f):
        f.unlink()
    sync(d)


def home(name: str) -> Path:
    """A home of links for `agy -p` (a worker), which takes no --gemini-dir but honours HOME: every entry of the real
    home is a link there, but .gemini, which is the account's folder. Next to the account folder (a dot in its name:
    never an account itself); refreshed at every start.
    shortcut: a file the worker writes new at the top of its home stays in this one, not in the real home."""
    d, h, real = folder(name), ROOT / f"{name}.home", Path.home()
    h.mkdir(parents=True, exist_ok=True)
    for src in real.iterdir():
        dst = h / src.name
        if src.name != ".gemini" and not (dst.exists() or accounts.is_link(dst)):
            os.symlink(src, dst)
    g = h / ".gemini"
    if not accounts.is_link(g):
        os.symlink(d, g)
    return h


def worker_env(name: str) -> dict:
    """What a worker (`agy -p`) under account `name` adds to its environment: env() and its home of links."""
    out = env(name)
    return {**out, "HOME": str(home(name))} if name else out


def flags(name: str) -> list:
    """agy acp's own flags for account `name` (after env() checked it)."""
    return ["--gemini-dir", str(folder(name))] if name else []


def may(name: str, by: str, pc_owner: bool, remove: bool = False):
    """The pc's own owner check, as accounts.may for Claude."""
    d = folder(name) if name else None
    if d is None or not d.is_dir():
        return
    owner = accounts.info(d).get("by", "")
    if not (owner and owner == by or pc_owner and (not owner or remove)):
        raise PermissionError(f"{name} is someone else's agy account on this pc: pick another name")


def command(*args) -> list:
    exe = shutil.which("agy")
    if not exe:
        raise RuntimeError("agy is not on PATH")
    return [exe, *args]


def _drop(name: str):
    rec = LOGINS.pop(name, None)
    if rec and rec["p"].poll() is None:
        rec["p"].kill()


def login(name: str, wait: float = 30, by: str = "") -> str:
    """Start a Google sign-in for extra account `name` (made when new); the link to open. The code goes in with code()."""
    if not SUPPORTED:
        raise RuntimeError("agy accounts work on Linux only")
    if not name:
        raise ValueError("the pc's own agy signs in in agy itself")
    d = folder(name)
    new = not d.is_dir()
    sync(d)
    with LOCK:
        _drop(name)
        e = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": DBUS_OFF, "BROWSER": "true"}
        p = subprocess.Popen(command("acp", "--login", "--gemini-dir", str(d)), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=e, cwd=str(d), text=True, encoding="utf-8", errors="replace")
        rec = LOGINS[name] = {"p": p, "url": "", "out": [], "at": time.time(), "by": by}

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
        if new:
            remove(name)
        raise RuntimeError("agy acp --login gave no sign-in link: " + " ".join(rec["out"])[-200:])
    accounts.write(d / INFO, {**accounts.owned(d, by), "error": ""})
    return rec["url"]


def cancel(name: str) -> str:
    with LOCK:
        _drop(name)
    return f"{name}: sign-in cancelled"


def code(name: str, text: str, wait: float = 90) -> str:
    """The code (or the whole address) the Google page landed on, into the waiting `agy acp --login`."""
    d = folder(name)
    with LOCK:
        rec = LOGINS.get(name)
        if not rec or rec["p"].poll() is not None:
            LOGINS.pop(name, None)
            raise RuntimeError("no sign-in waits for a code here (it ends after 15 minutes): press Sign in again")
        try:
            rec["p"].stdin.write(text.strip() + "\n")
            rec["p"].stdin.close()
        except OSError as e:
            raise RuntimeError(f"the sign-in ended: {e}") from e
    try:
        rec["p"].wait(wait)
    except subprocess.TimeoutExpired:
        pass
    with LOCK:
        if LOGINS.get(name) is rec:
            _drop(name)
    said = next((x for x in reversed(rec["out"]) if x.strip()), "no answer")
    if rec["p"].returncode != 0:
        accounts.write(d / INFO, {**accounts.info(d), "error": said[-300:]})
        raise RuntimeError(f"sign-in failed: {said[-200:]}")
    if not own_token(d):  # it must be this account's own: a token in a linked folder would be every account's
        said = "agy said it signed in, but no token file landed in this account's own folder: do not use it (agy may have changed where it keeps it)"
        accounts.write(d / INFO, {**accounts.info(d), "error": said})
        raise RuntimeError(said)
    email = next((m.group(0) for x in rec["out"] if (m := re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", x))), "")
    drop_key(d)
    accounts.write(d / INFO, {**accounts.info(d), "kind": "login", "email": email[:120], "error": "", "at": int(time.time())})
    return f"{name}: signed in" + (f" as {email}" if email else "")


def own_token(d: Path) -> bool:
    """A token file of agy's own (antigravity-cli/antigravity-oauth-token when there is no keyring) in account folder
    d itself, not behind a link."""
    return any("token" in p.name and p.is_file() and not accounts.is_link(p) for p in (d / APP).iterdir()) if (d / APP).is_dir() else False


def remove_later(name: str) -> str:
    d = folder(name)
    if not d.is_dir():
        raise RuntimeError(f"there is no agy account {name} on this pc")
    accounts.write(d / INFO, {**accounts.info(d), "removing": True})
    return f"{name}: removed once its running chats end"


def removing() -> list:
    return [n for n in names() if accounts.info(ROOT / n).get("removing")]


def remove(name: str) -> str:
    """Delete the account's folder (its sign-in with it): links are unlinked first, what they point to stays."""
    d = folder(name)
    if not d.is_dir():
        raise RuntimeError(f"there is no agy account {name} on this pc")
    with LOCK:
        _drop(name)
    for sub in (d / APP, d):
        for p in sub.iterdir() if sub.is_dir() else []:
            if accounts.is_link(p):
                os.unlink(p)
    shutil.rmtree(d)
    h = ROOT / f"{name}.home"
    if h.is_dir() and not accounts.is_link(h):
        for p in h.iterdir():  # only links: what they point to (the real home) stays
            if accounts.is_link(p):
                os.unlink(p)
        shutil.rmtree(h)  # anything a worker wrote new there
    return f"{name}: removed"


def token_text(name: str) -> str:
    """The sign-in agy saved for extra account `name` (JSON: token.access_token, refresh_token, expiry), or ""."""
    try:
        return (folder(name) / APP / TOKEN).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def listing(now: float = 0, windows_of=None) -> list:
    """The accounts for the Machines list: the pc's own first, then each extra one with who added it, whether it is
    signed in, a sign-in link waiting for its code and its usage windows (windows_of(name))."""
    now = now or time.time()
    with LOCK:
        for n, rec in list(LOGINS.items()):
            if now - rec["at"] > LOGIN_TTL or rec["p"].poll() is not None:
                _drop(n)
    win = lambda n: [list(w) for w in windows_of(n)] if windows_of else []
    out = [{"id": "", "kind": "login", "email": "", "windows": win("")}]
    for n in names():
        d, i = ROOT / n, accounts.info(ROOT / n)
        kind = "apikey" if (d / KEY).exists() else "login" if i.get("kind") == "login" else ""
        try:
            prev = accounts.token_preview((d / KEY).read_text(encoding="utf-8")) if kind == "apikey" else ""
        except OSError:
            prev = ""
        out.append({"id": n, "kind": kind, "email": str(i.get("email") or "") if kind == "login" else "", "preview": prev,
                    "at": int(i.get("at") or 0), "error": str(i.get("error") or ""), "by": str(i.get("by") or ""),
                    "windows": win(n) if kind == "login" else [],
                    **({"removing": True} if i.get("removing") else {}), **({"login": LOGINS[n]["url"]} if n in LOGINS and LOGINS[n]["url"] else {})})
    return out


def self_test():
    global ROOT, MAIN, SUPPORTED
    if os.name == "nt":  # Linux only (symlinks and a sh fake); macOS runs it too
        return print("agy_accounts self-test skipped on Windows")
    tmp, was_path, was_sup, was_tok = Path(tempfile.mkdtemp()), os.environ["PATH"], SUPPORTED, os.environ.get("KNOWLEDGE_TOKEN")
    try:
        ROOT, MAIN, SUPPORTED = tmp / "accounts", tmp / "gemini", True
        os.environ["KNOWLEDGE_TOKEN"] = "kt"
        for sub in ("config/plugins", f"{APP}/conversations", f"{APP}/brain", f"{APP}/log"):
            (MAIN / sub).mkdir(parents=True)
        for f in ("GEMINI.md", "oauth_creds.json", f"{APP}/settings.json", f"{APP}/conversation_summaries.db", f"{APP}/antigravity-oauth-token"):
            (MAIN / f).write_text("x", encoding="utf-8")
        for bad in ("", "Werk", "../x", "a/b", "x" * 32):
            try:
                folder(bad)
                raise AssertionError(f"{bad!r} must be refused")
            except ValueError:
                pass
        assert env("") == {} and flags("") == [], "the pc's own changes nothing"
        try:
            env("werk")
            raise AssertionError("an unknown account must be refused")
        except RuntimeError:
            pass
        d = folder("werk")
        sync(d)
        assert (d / "config" / "plugins").is_dir() and (d / APP / "conversations").is_dir() and (d / APP / "brain").is_dir() \
            and (d / APP / "settings.json").exists() and (d / "GEMINI.md").exists(), "plugins, conversations and settings are shared"
        assert not any((d / f).exists() for f in ("oauth_creds.json", f"{APP}/conversation_summaries.db", f"{APP}/antigravity-oauth-token", f"{APP}/log")), \
            "no sign-in, database or log of the pc's own"
        (d / APP / "jetski-standalone-oauth-token").write_text("mine", encoding="utf-8")
        (MAIN / APP / "jetski-standalone-oauth-token").write_text("pc", encoding="utf-8")
        e = env("werk")
        assert e["DBUS_SESSION_BUS_ADDRESS"] == DBUS_OFF and e["KNOWLEDGE_TOKEN"] == "kt" and flags("werk") == ["--gemini-dir", str(d)], e
        assert (d / APP / "jetski-standalone-oauth-token").read_text(encoding="utf-8") == "mine", "its own sign-in is never replaced"
        fake = tmp / "bin"
        fake.mkdir()
        (fake / "fake_agy.py").write_text(
            "import os, sys\n"
            "assert os.environ.get('DBUS_SESSION_BUS_ADDRESS') == " + repr(DBUS_OFF) + ", 'never the keyring'\n"
            "d = sys.argv[sys.argv.index('--gemini-dir') + 1]\n"
            "if d.endswith('broken'):\n"
            "    print('Error: network'); sys.exit(1)\n"
            "print('Open this link in a browser to log in with Google:\\n\\n  https://accounts.google.com/o/oauth2/auth?client_id=x&state=y\\n', flush=True)\n"
            "print('Then paste the authorization code, or the full URL of the page you land on, and press Enter:', flush=True)\n"
            "c = sys.stdin.readline().strip()\n"
            "if c != '4/good-code':\n"
            "    print('login failed: invalid_grant'); sys.exit(1)\n"
            "if not os.environ.get('FAKE_AGY_NO_TOKEN'):\n"
            "    open(os.path.join(d, 'antigravity-cli', 'token'), 'w').write('t')\n"
            "print('Logged in as w@example.org')\n", encoding="utf-8")
        (fake / "agy").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake / "fake_agy.py"}" "$@"\n', encoding="utf-8")
        (fake / "agy").chmod(0o755)
        os.environ["PATH"] = str(fake) + os.pathsep + was_path
        try:
            login("broken", by="eve")
            raise AssertionError("no link, no sign-in")
        except RuntimeError as e:
            assert "Error: network" in str(e), e
        assert "broken" not in names(), "a failed first sign-in leaves no account"
        url = login("thuis", by="bob")
        assert url == "https://accounts.google.com/o/oauth2/auth?client_id=x&state=y", url
        assert {a["id"]: a for a in listing()}["thuis"]["login"] == url, "the waiting link shows on the page"
        try:
            code("thuis", "bad")
            raise AssertionError("a bad code must fail")
        except RuntimeError as e:
            assert "invalid_grant" in str(e), e
        login("thuis")
        assert code("thuis", " 4/good-code ") == "thuis: signed in as w@example.org"
        (folder("thuis") / APP / "token").unlink()
        os.environ["FAKE_AGY_NO_TOKEN"] = "1"
        login("thuis")
        try:
            code("thuis", "4/good-code")
            raise AssertionError("a sign-in without a token of its own must fail")
        except RuntimeError as e:
            assert "no token file" in str(e), e
        del os.environ["FAKE_AGY_NO_TOKEN"]
        login("thuis")
        code("thuis", "4/good-code")
        a = {a["id"]: a for a in listing()}["thuis"]
        assert (a["kind"], a["email"], a["by"], a["error"]) == ("login", "w@example.org", "bob", "") and "login" not in a, a
        key = "AIza" + "k" * 35
        try:
            set_key("thuis", "sk-nope")
            raise AssertionError("not a Gemini key")
        except ValueError:
            pass
        assert set_key("thuis", key) == "thuis: API key saved" and env("thuis")["GEMINI_API_KEY"] == key
        st = d_t = folder("thuis") / APP / "settings.json"
        assert not accounts.is_link(st) and json.loads(st.read_text())["modelProvider"] == "gemini" and (MAIN / APP / "settings.json").read_text() == "x", \
            "its own settings with the key's provider; the pc's own untouched"
        a = {a["id"]: a for a in listing(windows_of=lambda n: [[10.0, "Gemini 5h", time.time() + 99]])}
        assert a["thuis"]["kind"] == "apikey" and a["thuis"]["preview"].endswith("kkkk") and a["thuis"]["windows"] == [] and a[""]["windows"], a
        login("thuis")
        code("thuis", "4/good-code")
        assert "GEMINI_API_KEY" not in env("thuis") and accounts.is_link(d_t), "a Google sign-in drops the key and its own settings"
        (folder("thuis") / APP / "token").unlink()
        (folder("thuis") / APP / TOKEN).write_text('{"token": {"access_token": "a"}}', encoding="utf-8")
        assert json.loads(token_text("thuis"))["token"]["access_token"] == "a" and token_text("nobody") == ""
        login("t3")
        p3 = LOGINS["t3"]["p"]
        assert cancel("t3") == "t3: sign-in cancelled" and p3.wait(timeout=10) is not None
        may("thuis", "bob", False) or may("thuis", "alice", True, remove=True) or may("new", "eve", False)
        for who, owner_pc, rm in (("eve", False, True), ("alice", True, False)):
            try:
                may("thuis", who, owner_pc, remove=rm)
                raise AssertionError(f"{who} must not touch bob's account")
            except PermissionError:
                pass
        assert remove_later("thuis").startswith("thuis: removed once") and removing() == ["thuis"]
        try:
            env("thuis")
            raise AssertionError("no new run on an account waiting for removal")
        except RuntimeError as e:
            assert "removed once" in str(e), e
        accounts.write(folder("thuis") / INFO, {**accounts.info(folder("thuis")), "removing": False})
        real_home = tmp / "realhome"
        (real_home / ".gitconfig").parent.mkdir()
        (real_home / ".gitconfig").write_text("g", encoding="utf-8")
        (real_home / ".gemini").mkdir()
        was_home = os.environ.get("HOME")
        os.environ["HOME"] = str(real_home)
        try:
            we = worker_env("thuis")
        finally:
            os.environ["HOME"] = was_home
        h = Path(we["HOME"])
        assert we["DBUS_SESSION_BUS_ADDRESS"] == DBUS_OFF and (h / ".gitconfig").read_text() == "g" \
            and (h / ".gemini").resolve() == folder("thuis").resolve() and "werk.home" not in names() and "thuis.home" not in names(), \
            "a worker's home: the real one's entries, its .gemini the account's"
        for n in names():
            remove(n)
        assert not h.exists() and (real_home / ".gitconfig").read_text() == "g", "its home of links goes, the real home stays"
        assert names() == [] and (MAIN / "config" / "plugins").is_dir() and (MAIN / APP / "conversations").is_dir() \
            and (MAIN / APP / "settings.json").exists() and (MAIN / APP / "jetski-standalone-oauth-token").read_text() == "pc", \
            "the pc's own folder is untouched"
    finally:
        os.environ["PATH"] = was_path
        os.environ.pop("KNOWLEDGE_TOKEN", None) if was_tok is None else os.environ.__setitem__("KNOWLEDGE_TOKEN", was_tok)
        SUPPORTED = was_sup
        for n in list(LOGINS):
            _drop(n)
        shutil.rmtree(tmp, ignore_errors=True)
    print("agy_accounts self-test ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        print(__doc__)
