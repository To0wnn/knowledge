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
INFO, TOKEN, USAGE, SPEND = OWN + "account.json", OWN + "token", OWN + "usage.json", OWN + "spend.json"
# kind -> (file, variable, form, what to paste); at most one of them per account
SECRETS = {"token": (TOKEN, "CLAUDE_CODE_OAUTH_TOKEN", r"sk-ant-oat\d\d-[\w-]{20,400}", "a token from claude setup-token (sk-ant-oat01-...)"),
           "apikey": (OWN + "api-key", "ANTHROPIC_API_KEY", r"sk-ant-api\d\d-[\w-]{20,400}", "an API key from the Anthropic Console (sk-ant-api03-...)")}
AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")  # the pc's own, never passed to another account
# never shared: the sign-in itself, Claude's per-account state (.claude.json and its backups), and the background
# daemon, which would otherwise take a run of this account under the default one
NOT_LINKED = {".credentials.json", ".claude.json", "backups", "daemon", "daemon.lock", "daemon.log", "daemon.status.json"}
# stops at control characters: in a pty claude draws the link as an OSC 8 hyperlink (ESC]8;;<url>BEL<text>)
URL = re.compile(r"https://[^\s\x00-\x1f\x7f]+/oauth/authorize\?[^\s\x00-\x1f\x7f]+")
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


def kept(name: str) -> Path:
    """Where account `name` keeps its token or key, spending and budget: its folder, and for the pc's own sign-in a
    folder of ours beside them (never in ~/.claude, whose sign-in stays the terminal's)."""
    return ROOT / ".own" if name == "" else folder(name)


def own_kind() -> str:
    """"token" or "apikey" when the pc's own account runs chats and workers on one; "" when on its browser sign-in."""
    return next((k for k, (f, *_) in SECRETS.items() if (kept("") / f).exists()), "")


def env(name: str) -> dict:
    """What a Claude run under account `name` adds to its environment; for the pc's own sign-in only its token or key
    when it has one ({} otherwise: the run uses ~/.claude as before)."""
    if not name:
        return {var: (kept("") / f).read_text(encoding="utf-8").strip() for kind, (f, var, _, _) in SECRETS.items() if kind == own_kind()}
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


def parse_iso(ts) -> int:
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, OSError):
        return 0


def token_preview(val: str) -> str:
    """A safe preview of a secret (the prefix and the last 4 characters, e.g. 'sk-ant-oat01-…7890'): never the secret itself."""
    val = (val or "").strip()
    if len(val) <= 12:
        return ""
    m = re.match(r"^(sk-ant-[a-z0-9]+-)", val)
    pref = m.group(1) if m else val[:8]
    return f"{pref}…{val[-4:]}"


def file_time(p: Path) -> int:
    try:
        return int(p.stat().st_mtime)
    except OSError:
        return 0


def secret_preview(d: Path, kind: str) -> str:
    if kind not in SECRETS:
        return ""
    try:
        return token_preview((d / SECRETS[kind][0]).read_text(encoding="utf-8").strip())
    except OSError:
        return ""


def status(d: Path, fresh: bool = False) -> dict:
    """{"email", "plan", "expires"} of the sign-in in folder d (`claude auth status`), kept for STATUS_TTL."""
    at, got = _STATUS.get(str(d), (0, None))
    if got is not None and not fresh and time.time() - at < STATUS_TTL:
        return got
    e = clean_env() if d != MAIN else dict(os.environ)  # the pc's own account is what its own environment says
    try:
        r = subprocess.run(command("auth", "status", "--json"), capture_output=True, text=True, timeout=30, encoding="utf-8",
                           env={**e, **({"CLAUDE_CONFIG_DIR": str(d)} if d != MAIN else {})})
        s = json.loads(r.stdout or "{}")
        exp = parse_iso(s.get("loginExpiresAt")) if s.get("loginExpiresAt") else 0
        got = {"email": str(s.get("email") or "")[:120] if s.get("loggedIn") else "",
               "plan": str(s.get("planTier") or s.get("subscriptionType") or "")[:40] if s.get("loggedIn") else "",
               "expires": exp}
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError, AttributeError):
        got = got or {"email": "", "plan": "", "expires": 0}
    if not got.get("expires"):
        try:
            creds = json.loads((d / ".credentials.json").read_text(encoding="utf-8"))
            oauth = creds.get("claudeAiOauth") or {}
            re_exp = oauth.get("refreshTokenExpiresAt") or oauth.get("expiresAt")
            if re_exp and isinstance(re_exp, (int, float)):
                got["expires"] = int(re_exp / 1000.0 if re_exp > 1e11 else re_exp)
        except (OSError, ValueError):
            pass
    _STATUS[str(d)] = (time.time(), got)
    return got


def _drop(name: str):
    rec = LOGINS.pop(name, None)
    if not rec:
        return
    if "master" in rec:
        try:
            os.close(rec["master"])
        except OSError:
            pass
    if rec["p"].poll() is None:
        if os.name == "nt":  # the .cmd shim's cmd.exe: its node child would go on
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(rec["p"].pid)], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rec["p"].kill()


def owned(d: Path, by: str) -> dict:
    """The account's info with who added it (the server checks that only they use or change it); kept once set."""
    i = info(d)
    return {**i, "by": i.get("by") or by} if (i.get("by") or by) else i


OWN_ERROR = {"": ""}  # the pc's own sign-in has no info file of ours (that would land in ~/.claude): its last error here


def where(name: str) -> Path:
    """The config folder of account `name`; "" is the pc's own (~/.claude or its CLAUDE_CONFIG_DIR)."""
    return MAIN if name == "" else folder(name)


def run_env(name: str, d: Path) -> dict:
    """The environment of a claude auth command for account `name`: never the pc's own token or key."""
    e = clean_env()
    return e if name == "" else {**e, "CLAUDE_CONFIG_DIR": str(d)}


def note(name: str, d: Path, **change):
    if name == "":
        OWN_ERROR[""] = change.get("error", OWN_ERROR[""])
    else:
        write(d / INFO, {**info(d), **change})


def may(name: str, by: str, pc_owner: bool, remove: bool = False):
    """The pc's own owner check of an action on account `name`, as the server's: the server only knows the accounts
    the pc reported (a capped list, a minute late), so it lets an unknown name through as a new one."""
    d = folder(name) if name else None
    if d is None or not d.is_dir():
        return
    owner = info(d).get("by", "")
    if not (owner and owner == by or pc_owner and (not owner or remove)):  # no owner: from before owners, the pc owner's
        raise PermissionError(f"{name} is someone else's Claude account on this pc: pick another name")


def login(name: str, wait: float = 30, by: str = "", setup: bool = False) -> str:
    """Start a browser sign-in for account `name` (made when new; "" signs the pc's own in again); the URL to open.
    The code goes in with code(). When `setup` is True, it runs `claude setup-token` in a pty to create a 1-year
    long-lived token directly; when False (default), it runs standard `claude auth login`."""
    d = where(name)
    new = bool(name) and not d.is_dir()
    if name:
        sync(d)
    with LOCK:
        _drop(name)
        e = run_env(name, d)
        cmd = command("setup-token") if setup else command("auth", "login", "--claudeai")
        if setup and os.name != "nt":
            import pty
            import fcntl
            import struct
            import termios
            master, slave = pty.openpty()
            # wide, so claude never wraps the 1-year token over two lines
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 500, 0, 0))
            p = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave,
                                 env={**e, "BROWSER": "true"}, close_fds=True)
            os.close(slave)
            rec = LOGINS[name] = {"p": p, "master": master, "url": "", "out": [], "raw": "", "at": time.time(), "by": by,
                                  "setup": True}

            def read_pty():
                while True:
                    try:
                        chunk = os.read(master, 4096)
                        if not chunk:
                            break
                        text = chunk.decode("utf-8", errors="replace")
                        rec["out"].extend(line.rstrip() for line in text.splitlines())
                        rec["raw"] += text  # the link and the token can be split over two reads: searched whole here
                        if not rec["url"] and (m := URL.search(rec["raw"])) and m.end() < len(rec["raw"]):
                            rec["url"] = m.group(0)
                    except OSError:
                        break
            threading.Thread(target=read_pty, daemon=True).start()
        else:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env={**e, "BROWSER": "true" if os.name != "nt" else e.get("BROWSER", "")},
                                 text=True, encoding="utf-8", errors="replace")
            rec = LOGINS[name] = {"p": p, "url": "", "out": [], "at": time.time(), "by": by, "setup": setup}

            def read_pipe():
                for line in p.stdout:
                    rec["out"].append(line.rstrip())
                    if not rec["url"] and (m := URL.search(line)):
                        rec["url"] = m.group(0)
            threading.Thread(target=read_pipe, daemon=True).start()

    end = time.time() + wait
    while not rec["url"] and rec["p"].poll() is None and time.time() < end:
        time.sleep(0.1)
    if not rec["url"]:
        with LOCK:
            _drop(name)
        if new:  # else it stays as an account without an owner: the pc owner's, and its name taken for whoever tried
            remove(name)
        raise RuntimeError(f"claude {'setup-token' if setup else 'auth login'} gave no sign-in link: " + " ".join(rec["out"])[-200:])
    if name:
        write(d / INFO, {**owned(d, by), "kind": "token" if setup else "login", "error": ""})
    return rec["url"]


def plain(text: str) -> str:
    """Terminal output as text: cursor moves within a line become spaces, other escapes (colours, OSC 8 links) go."""
    text = re.sub(r"\x1b\[\d*[CG]", " ", text)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r", "", text)


def printed_token(rec: dict) -> str:
    """The whole 1-year token claude setup-token printed, once a character after it shows it is complete."""
    m = re.search(r"sk-ant-oat\d\d-[\w-]{20,400}(?=[^\w-])", rec.get("raw") or "\n".join(rec["out"]) + "\n")
    return m.group(0) if m else ""


def cancel(name: str) -> str:
    """Stop a waiting sign-in of account `name`: its link leaves the page, the account stays as it was."""
    with LOCK:
        _drop(name)
    return f"{name or 'own'}: sign-in cancelled"


def code(name: str, text: str, wait: float = 90, by: str = "") -> str:
    """The code from the sign-in page into the waiting `claude setup-token` or `auth login`; who is signed in now."""
    clean = text.strip()
    if re.fullmatch(r"sk-ant-oat\d\d-[\w-]{20,400}", clean):
        with LOCK:
            _drop(name)
        return set_secret(name, "token", clean, by=by)
    if re.fullmatch(r"sk-ant-api\d\d-[\w-]{20,400}", clean):
        with LOCK:
            _drop(name)
        return set_secret(name, "apikey", clean, by=by)

    d = where(name)
    with LOCK:
        rec = LOGINS.get(name)
        if not rec or rec["p"].poll() is not None:
            LOGINS.pop(name, None)
            raise RuntimeError("no sign-in waits for a code here (it ends after 15 minutes): press Sign in again")
        by = rec.get("by", "")
        setup = rec.get("setup", False)
        if "master" in rec:
            try:
                # claude's TUI reads raw keys: Enter is \r (\n does nothing), and a long code with \r in one write is
                # taken as a paste that swallows the Enter, so the Enter goes on its own
                os.write(rec["master"], clean.encode("utf-8"))
                time.sleep(0.5)
                os.write(rec["master"], b"\r")
            except OSError as e:
                raise RuntimeError(f"the sign-in ended: {e}") from e
        else:
            try:
                rec["p"].stdin.write(clean + "\n\n")
                rec["p"].stdin.flush()
                try:
                    rec["p"].stdin.close()
                except OSError:
                    pass
            except OSError as e:
                raise RuntimeError(f"the sign-in ended: {e}") from e

    end = time.time() + wait
    if setup:
        while rec["p"].poll() is None and time.time() < end and not printed_token(rec) \
                and "Press Enter to retry" not in rec.get("raw", ""):  # a refused code: claude waits for a retry forever
            time.sleep(0.1)
    else:
        try:
            rec["p"].wait(wait)
        except subprocess.TimeoutExpired:
            pass

    with LOCK:
        if LOGINS.get(name) is rec:
            _drop(name)

    # A 1-year token printed by claude setup-token:
    token = printed_token(rec)
    if token:
        set_secret(name, "token", token, by=by)
        s = status(MAIN) if not name else {}
        return f"{name or 'own'}: long-lived token saved" + (f" as {s['email']}" if s.get("email") else "")

    if setup:
        lines = [x.strip() for x in plain(rec.get("raw") or "\n".join(rec["out"])).splitlines() if x.strip()]
        said = next((x for x in reversed(lines) if "error" in x.lower()), lines[-1] if lines else "no answer")
        note(name, d, error=said[-300:])
        raise RuntimeError(f"setup-token failed: {said[-200:]}")

    cred = (MAIN if not name else d) / ".credentials.json"
    if rec["p"].poll() != 0 and not (cred.exists() and time.time() - cred.stat().st_mtime < wait + 15):
        said = next((x for x in reversed(rec["out"]) if x.strip()), "no answer")
        note(name, d, error=said[-300:])
        raise RuntimeError(f"sign-in failed: {said[-200:]}")

    for f, _, _, _ in SECRETS.values():  # a browser sign-in replaces a token or key (the pc's own: kept beside ~/.claude)
        (kept(name) / f).unlink(missing_ok=True)
    if name:
        plugin_secrets(d)
    s = status(d, fresh=True)
    note(name, d, kind="login", error="", at=int(time.time()), **s)
    return f"{name or 'own'}: signed in" + (f" as {s['email']}" if s.get("email") else "")


def logout_own() -> str:
    """Sign the pc's own Claude out (its terminal sessions, workers and chats on it stop until it signs in again);
    the companion stays online, so Sign in on the page works right after."""
    with LOCK:
        _drop("")
    r = subprocess.run(command("auth", "logout"), capture_output=True, text=True, timeout=60, env=clean_env(), encoding="utf-8", errors="replace")
    _STATUS.pop(str(MAIN), None)
    if r.returncode != 0:
        raise RuntimeError(f"claude auth logout failed: {(r.stderr or r.stdout).strip()[-200:]}")
    return "own: signed out"


def set_secret(name: str, kind: str, value: str, by: str = "") -> str:
    """A long-lived token (`claude setup-token`) or an API key for account `name`, kept in its folder (owner only);
    it replaces the other one, and a browser sign-in there stops counting (the variable wins over it)."""
    f, _, form, what = SECRETS[kind]
    if not re.fullmatch(form, value or ""):
        raise ValueError(f"that is not {what}")
    d = kept(name)
    if name:
        sync(d)
    else:
        d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(value)
    os.replace(tmp, d / f)
    for other, *_ in SECRETS.values():
        if other != f:
            (d / other).unlink(missing_ok=True)
    with LOCK:
        _drop(name)
    write(d / INFO, {**{k: v for k, v in (owned(d, by) if name else info(d)).items() if k not in ("email", "plan")}, "kind": kind, "error": "", "at": int(time.time())})
    return f"{name or 'own'}: {'long-lived token' if kind == 'token' else 'API key'} saved" + (" (chats and workers; the terminal keeps its own sign-in)" if not name else "")


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


def listing(windows_of=None, now: float = 0) -> list:
    """The accounts for the Machines list: the pc's own sign-in first, then each extra one with who added it, its
    sign-in state, a sign-in link waiting for its code, its usage windows (windows_of(name)) and, for an API key, what
    it spent this month and its budget."""
    now = now or time.time()
    with LOCK:
        for n, rec in list(LOGINS.items()):
            if now - rec["at"] > LOGIN_TTL or rec["p"].poll() is not None:
                _drop(n)
    win = lambda n: [list(w) for w in windows_of(n)] if windows_of else []
    ok, oi = own_kind(), info(kept(""))
    s = status(MAIN)
    if ok:
        prev = secret_preview(kept(""), ok)
        at = int(oi.get("at") or file_time(kept("") / SECRETS[ok][0]))
        exp = (at + 365 * 86400) if ok == "token" and at else 0
    else:
        prev = ""
        at = int(oi.get("at") or file_time(MAIN / ".credentials.json"))
        exp = int(s.get("expires") or 0)
    out = [{"id": "", **s, "kind": ok, "preview": prev, "at": at, "expires": exp,
            "windows": win(""), "error": OWN_ERROR[""], **({"login": LOGINS[""]["url"], "setup": LOGINS[""].get("setup", False)} if "" in LOGINS and LOGINS[""]["url"] else {}),
            **({"spent": spent("", now), **({"budget": oi["budget"]} if oi.get("budget") else {})} if ok == "apikey" else {})}]
    for n in names():
        d, i = ROOT / n, info(ROOT / n)
        kind = next((k for k, (f, *_) in SECRETS.items() if (d / f).exists()), "") \
            or ("login" if (d / ".credentials.json").exists() or sys.platform == "darwin" and i.get("email") else "")
        if kind in SECRETS:
            prev = secret_preview(d, kind)
            at = int(i.get("at") or file_time(d / SECRETS[kind][0]))
            exp = (at + 365 * 86400) if kind == "token" and at else 0
            st = {}
        else:
            prev = ""
            st = status(d) if kind == "login" else {}
            at = int(i.get("at") or file_time(d / ".credentials.json"))
            exp = int(st.get("expires") or 0)
        out.append({"id": n, "kind": kind, "email": st.get("email") or "", "plan": st.get("plan") or "",
                    "preview": prev, "at": at, "expires": exp, "error": i.get("error") or "",
                    "by": str(i.get("by") or ""), **({"login": LOGINS[n]["url"], "setup": LOGINS[n].get("setup", False)} if n in LOGINS and LOGINS[n]["url"] else {}),
                    "windows": win(n), **({"spent": spent(n, now), **({"budget": i["budget"]} if i.get("budget") else {})} if kind == "apikey" else {})})
    return out


def month(now: float) -> str:
    return time.strftime("%Y-%m", time.localtime(now))


def spent(name: str, now: float = 0) -> float:
    """What an API key account's chat turns and workers cost this month (Claude Code's own total_cost_usd per turn):
    the API itself has no balance a key can read."""
    try:
        got = json.loads((kept(name) / SPEND).read_text(encoding="utf-8"))
        return round(float(got["usd"]), 4) if got.get("month") == month(now or time.time()) else 0.0
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def add_spend(name: str, usd: float):
    """Count one turn's cost for an API key account (a subscription's cost is no money spent: not counted)."""
    try:
        d = kept(name)
        if not (d / SECRETS["apikey"][0]).exists() or not 0 < usd < 10_000:
            return
        with LOCK:
            write(d / SPEND, {"month": month(time.time()), "usd": spent(name) + usd})
    except (OSError, ValueError):
        pass


def set_budget(name: str, usd: float) -> str:
    """A monthly budget for an API key account (0: none): the page shows what is left of it."""
    d = kept(name)
    if not d.is_dir():
        raise RuntimeError(f"there is no Claude account {name} on this pc")
    if not 0 <= usd < 1_000_000:
        raise ValueError("a budget is 0 (none) up to 1000000 dollars a month")
    write(d / INFO, {**{k: v for k, v in info(d).items() if k != "budget"}, **({"budget": round(usd, 2)} if usd else {})})
    return f"{name or 'own'}: " + (f"budget ${usd:.2f} a month" if usd else "no budget")


def keep_usage(name: str, got: list):
    """A chat turn's usage reading for an extra account (the default one's goes to the companion's own file)."""
    try:
        (folder(name) / USAGE).write_text(json.dumps({"windows": got, "at": time.time()}), encoding="utf-8")
    except (OSError, ValueError):
        pass


def self_test():
    global ROOT, MAIN
    tmp, was = Path(tempfile.mkdtemp()), os.environ.get("CLAUDE_CONFIG_DIR")
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
            "if sys.argv[1:3] == ['auth', 'login'] and d.endswith('broken'):\n"
            "    print('Error: network'); sys.exit(1)\n"
            "elif sys.argv[1:3] == ['auth', 'login']:\n"
            "    print('If the browser did not open, visit: https://claude.com/cai/oauth/authorize?code=true&state=x', flush=True)\n"
            "    print('Paste code here if prompted > ', end='', flush=True)\n"
            "    c = sys.stdin.readline().strip()\n"
            "    if c != 'good#code':\n"
            "        print('Login failed: That code was not accepted.'); sys.exit(1)\n"
            "    json.dump({'claudeAiOauth': {'accessToken': 'new'}}, open(os.path.join(d, '.credentials.json'), 'w')); print('Login successful.')\n"
            "elif sys.argv[1:2] == ['setup-token']:\n"
            "    u = 'https://claude.com/cai/oauth/authorize?code=true&state=x'\n"
            "    # the real one draws the link as an OSC 8 hyperlink in a terminal, the URL split over two writes\n"
            "    sys.stdout.write('Use the url below\\r\\n\\x1b]8;id=1;' + u[:20]); sys.stdout.flush()\n"
            "    sys.stdout.write(u[20:] + '\\x07' + u[:30] + '\\x1b]8;;\\x07\\r\\n'); sys.stdout.flush()\n"
            "    print('Paste code here if prompted > ', end='', flush=True)\n"
            "    c = sys.stdin.readline().strip()\n"
            "    if c != 'good#code':\n"
            "        print('\\x1b[3AOAuth error:\\x1b[1CThat code was not accepted. Press Enter to retry.', flush=True); sys.stdin.readline(); sys.exit(1)\n"
            "    sys.stdout.write('Long-lived authentication token created successfully!\\nYour OAuth token (valid for 1 year):\\nsk-ant-oat01-0123456789'); sys.stdout.flush()\n"
            "    import time; time.sleep(0.3); print('abcdef0123456789abcdef0123456789\\n', flush=True)\n"
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
        os.environ["CLAUDE_CONFIG_DIR"] = str(MAIN)  # the pc's own sign-in of this test: never the real ~/.claude
        try:
            login("broken", by="eve")
            raise AssertionError("no link, no sign-in")
        except RuntimeError as e:
            assert "Error: network" in str(e), e
        assert "broken" not in names(), "a failed first sign-in leaves no account without an owner"
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
        assert login("") == url and {a["id"]: a for a in listing()}[""]["login"] == url, "the pc's own signs in again the same way"
        assert code("", "good#code").startswith("own: signed in") and not (MAIN / INFO).exists(), "no file of ours in ~/.claude"
        assert json.loads((MAIN / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"] == "new"
        assert logout_own() == "own: signed out" and not (MAIN / ".credentials.json").exists()
        # setup-token creates a 1-year token directly via the browser flow
        login("tok1", setup=True)
        t0 = time.time()
        try:
            code("tok1", "bad")
            raise AssertionError("a refused code must fail")
        except RuntimeError as e:
            assert "OAuth error: That code was not accepted" in str(e) and time.time() - t0 < 10, (str(e), time.time() - t0)
        u2 = login("tok1", setup=True)
        assert u2 == url and {a["id"]: a for a in listing()}["tok1"]["login"] == url
        assert code("tok1", "good#code").startswith("tok1: long-lived token saved")
        assert {a["id"]: a for a in listing()}["tok1"]["kind"] == "token"
        assert (kept("tok1") / TOKEN).read_text(encoding="utf-8").endswith("abcdef0123456789"), "the whole token, though printed in two parts"
        remove("tok1")
        login("tok3", setup=True)
        p3 = LOGINS["tok3"]["p"]
        assert cancel("tok3") == "tok3: sign-in cancelled" and "login" not in {a["id"]: a for a in listing()}["tok3"], \
            "a cancelled sign-in leaves the page"
        assert p3.wait(timeout=10) is not None and cancel("tok3"), "its claude stops; cancelling again is harmless"
        remove("tok3")
        # pasting a token directly into code() saves it without waiting for claude
        assert code("tok2", "sk-ant-oat01-" + "t" * 40).startswith("tok2: long-lived token saved")
        assert {a["id"]: a for a in listing()}["tok2"]["kind"] == "token"
        remove("tok2")
        # the pc's own on a long-lived token or API key: for the companion's runs only, beside ~/.claude, never in it
        assert env("") == {} and own_kind() == ""
        own_key = "sk-ant-api03-" + "o" * 40
        assert set_secret("", "apikey", own_key).startswith("own: API key saved") and env("") == {"ANTHROPIC_API_KEY": own_key}
        add_spend("", 0.5)
        set_budget("", 5)
        o = {a["id"]: a for a in listing()}[""]
        assert (o["kind"], o["spent"], o["budget"]) == ("apikey", 0.5, 5) and not list(MAIN.glob(OWN + "*")), o
        set_secret("", "token", "sk-ant-oat01-" + "o" * 40)
        assert list(env("")) == ["CLAUDE_CODE_OAUTH_TOKEN"] and ".own" not in names()
        login("", setup=False)
        code("", "good#code")
        assert env("") == {} and own_kind() == "", "a browser sign-in of the pc's own drops its token"
        write(MAIN / ".credentials.json", {"claudeAiOauth": {"accessToken": "main"}})
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
        acc = {a["id"]: a for a in listing(lambda n: json.loads((folder(n) / USAGE).read_text())["windows"] if n and (folder(n) / USAGE).exists() else [])}
        assert acc["werk"]["kind"] == "token" and acc["werk"]["windows"][0][0] == 40.0 and "spent" not in acc["werk"], acc["werk"]
        assert acc["werk"]["preview"] == "sk-ant-oat01-…aaaa" and acc["werk"]["at"] > 0 and acc["werk"]["expires"] == acc["werk"]["at"] + 365 * 86400
        add_spend("werk", 1.5)
        assert spent("werk") == 0.0, "a subscription's turns cost no money"
        set_secret("werk", "apikey", key, by="bob")
        add_spend("werk", 1.25)
        add_spend("werk", 0.5)
        add_spend("werk", -3)
        assert set_budget("werk", 20) == "werk: budget $20.00 a month"
        acc = {a["id"]: a for a in listing()}["werk"]
        assert (acc["spent"], acc["budget"], acc["by"]) == (1.75, 20, "bob"), acc  # made before by was given: the next one who signs it in owns it
        assert acc["preview"] == "sk-ant-api03-…kkkk" and acc["at"] > 0 and acc["expires"] == 0
        assert spent("werk", time.time() + 40 * 86400) == 0.0, "a new month starts at zero"
        set_secret("nieuw", "apikey", key, by="bob")
        assert {a["id"]: a for a in listing()}["nieuw"]["by"] == "bob" and info(folder("nieuw"))["by"] == "bob"
        set_secret("nieuw", "token", tok, by="eve")
        assert info(folder("nieuw"))["by"] == "bob", "who added an account stays its owner"
        may("nieuw", "bob", False) or may("nieuw", "alice", True, remove=True) or may("brandnew", "eve", False)
        for who, owner_pc, rm in (("eve", False, True), ("alice", True, False)):
            try:
                may("nieuw", who, owner_pc, remove=rm)
                raise AssertionError(f"{who} must not touch bob's account")
            except PermissionError:
                pass
        set_secret("nieuw", "token", tok, by="")
        (folder("nieuw") / INFO).write_text(json.dumps({k: v for k, v in info(folder("nieuw")).items() if k != "by"}))
        may("nieuw", "alice", True)  # from before owners were kept: the pc owner's
        try:
            may("nieuw", "eve", False)
            raise AssertionError("a guest takes no ownerless account over")
        except PermissionError:
            pass
        remove("nieuw")
        # removing keeps everything the links point to
        assert remove("werk") == "werk: removed" and remove("thuis") == "thuis: removed"
        assert (MAIN / "plugins").is_dir() and (MAIN / "projects" / "p").is_dir() and (MAIN / "settings.json").exists() \
            and (MAIN / "skills").is_dir() and json.loads((MAIN / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"] == "main", \
            "the main folder is untouched"
        assert names() == [], names()
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None) if was is None else os.environ.__setitem__("CLAUDE_CONFIG_DIR", was)
        for n in list(LOGINS):
            _drop(n)
        shutil.rmtree(tmp, ignore_errors=True)
    print("accounts self-test ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        print(__doc__)
