#!/usr/bin/env python3
"""More than one Codex account on one pc, as accounts.py does for Claude: each extra account is a CODEX_HOME of its
own (~/.knowledge/codex-accounts/<name>). Everything of ~/.codex is linked in (config.toml, plugins, skills, AGENTS.md,
sessions with the rollouts), so a chat keeps its plugin and resumes its thread after a switch (tested on codex 0.169:
thread/resume from such a home); its sign-in (auth.json), databases (a linked *.sqlite would get a second -wal beside
the link), logs and app-server daemon (it would take the run under the pc's own sign-in) are its own.

Sign-in from the web UI (Machines > Codex > Accounts):
- ChatGPT: `codex login --device-auth` runs here; the page shows its link and one-time code, the person enters the code
  there, and codex ends by itself once it is signed in (nothing goes back to the pc).
- API key: `codex login --with-api-key` gets the key on its stdin.

  codex_accounts.py --self-test   the checks of this file (a fake codex)
Standard library only; Linux, macOS and Windows.
"""
import base64
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

ROOT = token_store.KN_HOME / "codex-accounts"
MAIN = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")  # the pc's own
ID = accounts.ID
INFO = accounts.INFO
# .credentials.json: codex's MCP sign-ins (OAuth) when there is no keyring; with one they are per server, shared anyway
NOT_LINKED = {"auth.json", ".credentials.json", "log", "tmp", "app-server-control", "app-server-daemon", "shell_snapshots", "thread-writer-locks"}
AUTH_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY")  # the pc's own, never passed to another account
KEY_FORM = r"sk-[\w-]{20,300}"
URL = re.compile(r"https://auth\.openai\.com/[^\s\x00-\x1f\x7f]+")
CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")
THREADS = ROOT / ".threads.json"  # thread id -> account: rollouts the pc's own usage reading must skip
LOGIN_TTL = accounts.LOGIN_TTL
LOGINS: dict = {}  # name -> {"p", "url", "code", "out", "at", "by"}
LOCK = threading.Lock()


def folder(name: str) -> Path:
    """The account's CODEX_HOME; ValueError for a bad name or one outside ROOT (never ROOT itself)."""
    if not ID.fullmatch(name or ""):
        raise ValueError("an account name is a-z, 0-9 and -, at most 31 characters")
    d = ROOT / name
    if d.resolve().parent != ROOT.resolve():
        raise ValueError(f"{name} is not under {ROOT}")
    return d


def names() -> list:
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir() and ID.fullmatch(p.name)) if ROOT.is_dir() else []


def shared(p: Path) -> bool:
    n = p.name
    return not (n in NOT_LINKED or n.startswith((accounts.OWN, "auth")) or n.endswith(".lock") or ".sqlite" in n)


def sync(d: Path):
    """Link what ~/.codex has into account folder d (new entries too); a file codex replaced there by a write by
    rename is linked again (on Windows: a copy, refreshed), so the pc's own config holds for every account."""
    d.mkdir(parents=True, exist_ok=True)
    for src in MAIN.iterdir() if MAIN.is_dir() else []:
        if not shared(src):
            continue
        dst = d / src.name
        if src.is_file() and dst.is_file() and not accounts.is_link(dst):
            if os.name == "nt":
                a, b = src.stat(), dst.stat()
                if (a.st_size, a.st_mtime_ns) != (b.st_size, b.st_mtime_ns):
                    try:
                        shutil.copy2(src, dst)
                    except OSError:  # shortcut: open in another codex (Windows locks it): its older copy serves this run
                        pass
                continue
            dst.unlink()
        if not (dst.exists() or accounts.is_link(dst)):
            accounts.link(src, dst)


def clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in AUTH_ENV}


def env(name: str) -> dict:
    """What a Codex run under account `name` adds to its environment ({} for the pc's own); a caller drops AUTH_ENV."""
    if not name:
        return {}
    d = folder(name)
    if not d.is_dir():
        raise RuntimeError(f"there is no Codex account {name} on this pc (Machines > Codex > Accounts)")
    if accounts.info(d).get("removing"):
        raise RuntimeError(f"the Codex account {name} is removed once its running chats and workers end: pick another")
    sync(d)
    return {"CODEX_HOME": str(d)}


def run_env(name: str) -> dict:
    """The environment of a chat or worker under account `name`: its home, never the pc's own key."""
    return {**clean_env(), **env(name)} if name else dict(os.environ)


def may(name: str, by: str, pc_owner: bool, remove: bool = False):
    """The pc's own owner check, as accounts.may for Claude."""
    d = folder(name) if name else None
    if d is None or not d.is_dir():
        return
    owner = accounts.info(d).get("by", "")
    if not (owner and owner == by or pc_owner and (not owner or remove)):
        raise PermissionError(f"{name} is someone else's Codex account on this pc: pick another name")


def command(*args) -> list:
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex is not on PATH")
    cmd = [exe, *args]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        import workers
        return workers.shim_line(cmd)
    return cmd


def status(d: Path) -> dict:
    """{"kind", "email", "plan", "preview", "expires"} from the sign-in codex saved in folder d (auth.json; the email
    and plan are claims of its id token, read here without a network call)."""
    try:
        a = json.loads((d / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"kind": "", "email": "", "plan": "", "preview": "", "expires": 0}
    if a.get("OPENAI_API_KEY") and not (a.get("tokens") or {}).get("id_token"):
        return {"kind": "apikey", "email": "", "plan": "", "preview": accounts.token_preview(str(a["OPENAI_API_KEY"])), "expires": 0}
    claims = {}
    try:
        part = str((a.get("tokens") or {}).get("id_token") or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        pass
    auth = claims.get("https://api.openai.com/auth") or {}
    return {"kind": "login" if a.get("tokens") else "", "email": str(claims.get("email") or "")[:120],
            "plan": str(auth.get("chatgpt_plan_type") or "")[:40], "preview": "",
            "expires": accounts.parse_iso(auth.get("chatgpt_subscription_active_until")) if auth.get("chatgpt_subscription_active_until") else 0}


def _drop(name: str):
    _kill(LOGINS.pop(name, None))


def _kill(rec):
    if rec and rec["p"].poll() is None:
        if os.name == "nt":  # the .cmd shim's cmd.exe: its node child would go on
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(rec["p"].pid)], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rec["p"].kill()


def plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def login(name: str, wait: float = 30, by: str = "") -> str:
    """Start a ChatGPT sign-in for extra account `name` (made when new): `codex login --device-auth`; its link (the
    one-time code with it) goes to the page, and codex ends by itself once the person entered the code."""
    if not name:
        raise ValueError("the pc's own Codex signs in in codex itself")
    d = folder(name)
    new = not d.is_dir()
    sync(d)
    with LOCK:
        old = LOGINS.pop(name, None)
    _kill(old)  # outside LOCK: a taskkill on Windows takes seconds
    with LOCK:
        p = subprocess.Popen(command("login", "--device-auth"), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env={**clean_env(), "CODEX_HOME": str(d)}, text=True,
                             encoding="utf-8", errors="replace",
                             **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}))
        rec = LOGINS[name] = {"p": p, "url": "", "code": "", "out": [], "at": time.time(), "by": by}

        def read():
            for line in p.stdout:
                line = plain(line).rstrip()
                rec["out"].append(line)
                if not rec["url"] and (m := URL.search(line)):
                    rec["url"] = m.group(0)
                elif rec["url"] and not rec["code"] and (m := CODE.search(line)):
                    rec["code"] = m.group(0)
        rec["reader"] = threading.Thread(target=read, daemon=True)
        rec["reader"].start()
    end = time.time() + wait
    while not (rec["url"] and rec["code"]) and p.poll() is None and time.time() < end:
        time.sleep(0.1)
    if not (rec["url"] and rec["code"]):
        with LOCK:
            _drop(name)
        if new:
            remove(name)
        raise RuntimeError("codex login --device-auth gave no link and code: " + " ".join(rec["out"])[-200:])
    accounts.write(d / INFO, {**accounts.owned(d, by), "error": ""})
    return rec["url"]


def finished(name: str, rec: dict):
    """A device sign-in whose codex ended: signed in, or why not (kept for the page)."""
    rec["reader"].join(5)
    d = folder(name)
    if not d.is_dir():  # removed meanwhile
        return
    if rec["p"].returncode == 0:  # signed in (into auth.json, or the keyring when config.toml keeps it there)
        accounts.write(d / INFO, {**accounts.info(d), "kind": "login", "error": "", "at": int(time.time())})
    elif rec["p"].returncode is not None and rec["p"].returncode >= 0:  # a cancel kills it (negative or taskkill's 1)
        said = next((x for x in reversed(rec["out"]) if x.strip()), "no answer")
        accounts.write(d / INFO, {**accounts.info(d), "error": f"sign-in failed: {said}"[-300:]})


def cancel(name: str) -> str:
    with LOCK:
        rec = LOGINS.pop(name, None)
        if rec:
            rec["cancelled"] = True
    _kill(rec)  # outside LOCK: a taskkill on Windows takes seconds
    return f"{name}: sign-in cancelled"


def set_key(name: str, key: str, by: str = "") -> str:
    """An OpenAI API key for account `name` (made when new), saved by codex itself in its auth.json."""
    if not re.fullmatch(KEY_FORM, key or ""):
        raise ValueError("that is not an OpenAI API key (sk-...)")
    d = folder(name)
    sync(d)
    with LOCK:
        rec = LOGINS.pop(name, None)
    _kill(rec)  # outside LOCK: a taskkill on Windows takes seconds
    r = subprocess.run(command("login", "--with-api-key"), input=key, capture_output=True, text=True, timeout=60,
                       env={**clean_env(), "CODEX_HOME": str(d)}, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        said = plain(r.stdout + r.stderr).strip().splitlines()[-1:] or ["no answer"]
        accounts.write(d / INFO, {**accounts.owned(d, by), "error": said[0][-300:]})
        raise RuntimeError(f"codex login --with-api-key failed: {said[0][-200:]}")
    accounts.write(d / INFO, {**accounts.owned(d, by), "kind": "apikey", "error": "", "at": int(time.time())})
    return f"{name}: API key saved"


def remove_later(name: str) -> str:
    d = folder(name)
    if not d.is_dir():
        raise RuntimeError(f"there is no Codex account {name} on this pc")
    accounts.write(d / INFO, {**accounts.info(d), "removing": True})
    return f"{name}: removed once its running chats and workers end"


def removing() -> list:
    return [n for n in names() if accounts.info(ROOT / n).get("removing")]


def remove(name: str) -> str:
    """Sign account `name` out and delete its folder: links are unlinked first, what they point to stays."""
    d = folder(name)
    if not d.is_dir():
        raise RuntimeError(f"there is no Codex account {name} on this pc")
    with LOCK:
        rec = LOGINS.pop(name, None)
    _kill(rec)  # outside LOCK: a taskkill on Windows takes seconds
    if (d / "auth.json").exists():
        try:
            subprocess.run(command("logout"), capture_output=True, timeout=30, env={**clean_env(), "CODEX_HOME": str(d)})
        except (OSError, subprocess.SubprocessError, RuntimeError):
            pass
    for p in d.iterdir():
        if accounts.is_link(p):
            accounts.unlink(p)
    shutil.rmtree(d)
    forget_threads(name)
    return f"{name}: removed"


def keep_thread(thread: str, name: str):
    """A chat's thread on extra account `name`: its rollout (in the shared sessions) is not the pc's own usage."""
    if not (name and re.fullmatch(r"[\w-]{8,80}", thread or "")):
        return
    with LOCK:
        try:
            got = json.loads(THREADS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            got = {}
        if got.get(thread) != name:
            THREADS.parent.mkdir(parents=True, exist_ok=True)
            accounts.write(THREADS, {**dict(list(got.items())[-500:]), thread: name})


def threads() -> dict:
    try:
        return json.loads(THREADS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def forget_threads(name: str):
    with LOCK:
        got = threads()
        if any(v == name for v in got.values()):
            accounts.write(THREADS, {k: v for k, v in got.items() if v != name})


def listing(now: float = 0, windows_of=None) -> list:
    """The accounts for the Machines list: the pc's own first, then each extra one with who added it, its sign-in,
    a device sign-in waiting (its link and code) and its usage windows (windows_of(name))."""
    now = now or time.time()
    with LOCK:  # taken off here, waited for and ended below: LOCK is never held over a wait (a chat start needs it)
        ended = [(n, LOGINS.pop(n)) for n, rec in list(LOGINS.items()) if rec["p"].poll() is not None]
        stale = [LOGINS.pop(n) for n, rec in list(LOGINS.items()) if now - rec["at"] > LOGIN_TTL]
    for n, rec in ended:
        if not rec.get("cancelled"):
            finished(n, rec)
    for rec in stale:
        _kill(rec)
    win = lambda n: [list(w) for w in windows_of(n)] if windows_of else []
    out = [{"id": "", **status(MAIN), "windows": win("")}]
    for n in names():
        d, i = ROOT / n, accounts.info(ROOT / n)
        s = status(d)
        if not s["kind"] and i.get("kind") == "login":  # signed in into the keyring: no auth.json to read
            s["kind"] = "login"
        rec = LOGINS.get(n)
        out.append({"id": n, **s, "at": int(i.get("at") or 0), "error": str(i.get("error") or ""), "by": str(i.get("by") or ""),
                    "windows": win(n) if s["kind"] == "login" else [], **({"removing": True} if i.get("removing") else {}),
                    **({"login": rec["url"], "usercode": rec["code"]} if rec and rec["url"] else {})})
    return out


def self_test():
    global ROOT, MAIN, THREADS
    tmp, was_path = Path(tempfile.mkdtemp()), os.environ["PATH"]
    try:
        ROOT, MAIN = tmp / "accounts", tmp / "codex"
        THREADS = ROOT / ".threads.json"
        for sub in ("sessions/2026/11/14", "plugins", "skills", "log", "app-server-daemon"):
            (MAIN / sub).mkdir(parents=True)
        for f in ("config.toml", "AGENTS.md", "auth.json", "auth.json.lock", ".credentials.json", "state_5.sqlite", "state_5.sqlite-wal", "history.jsonl"):
            (MAIN / f).write_text("x", encoding="utf-8")
        for bad in ("", "Werk", "../x", "a/b", "x" * 32):
            try:
                folder(bad)
                raise AssertionError(f"{bad!r} must be refused")
            except ValueError:
                pass
        assert env("") == {}, "the pc's own changes nothing"
        try:
            env("werk")
            raise AssertionError("an unknown account must be refused")
        except RuntimeError:
            pass
        d = folder("werk")
        sync(d)
        assert all((d / f).exists() for f in ("sessions", "plugins", "skills", "config.toml", "AGENTS.md", "history.jsonl")), \
            "config, plugins, skills and the sessions are shared"
        assert not any((d / f).exists() for f in ("auth.json", "auth.json.lock", ".credentials.json", "state_5.sqlite", "state_5.sqlite-wal", "log", "app-server-daemon")), \
            "no sign-in (also not its MCP sign-ins), database, log or daemon of the pc's own"
        os.environ["OPENAI_API_KEY"] = "sk-own"
        assert "OPENAI_API_KEY" not in run_env("werk") and run_env("werk")["CODEX_HOME"] == str(d), "never the pc's own key"
        del os.environ["OPENAI_API_KEY"]
        claims = base64.urlsafe_b64encode(json.dumps({"email": "w@example.org", "https://api.openai.com/auth": {
            "chatgpt_plan_type": "plus", "chatgpt_subscription_active_until": "2027-01-01T00:00:00Z"}}).encode()).decode().rstrip("=")
        fake = tmp / "bin"
        fake.mkdir()
        script = fake / "fake_codex.py"
        script.write_text(
            "import json, os, sys, time\n"
            "d = os.environ['CODEX_HOME']\n"
            "assert 'OPENAI_API_KEY' not in os.environ\n"
            "if sys.argv[1:] == ['login', '--device-auth']:\n"
            "    if d.endswith('broken'):\n"
            "        print('Error: network'); sys.exit(1)\n"
            "    print('\\x1b[1mFollow these steps\\x1b[0m\\n1. Open this link\\n   \\x1b[94mhttps://auth.openai.com/codex/device\\x1b[0m\\n2. Enter this one-time code\\n   \\x1b[94mABCD-EFGH2\\x1b[0m', flush=True)\n"
            "    time.sleep(float(open(os.path.join(d, 'wait')).read()) if os.path.exists(os.path.join(d, 'wait')) else 0.5)\n"
            "    if os.path.exists(os.path.join(d, 'deny')):\n"
            "        print('Error: device code expired'); sys.exit(1)\n"
            "    if os.path.exists(os.path.join(d, 'keyring')):\n"
            "        print('Successfully logged in'); sys.exit(0)\n"
            f"    json.dump({{'auth_mode': 'chatgpt', 'tokens': {{'id_token': 'h.{claims}.s'}}}}, open(os.path.join(d, 'auth.json'), 'w'))\n"
            "elif sys.argv[1:] == ['login', '--with-api-key']:\n"
            "    json.dump({'auth_mode': 'apikey', 'OPENAI_API_KEY': sys.stdin.read().strip()}, open(os.path.join(d, 'auth.json'), 'w'))\n"
            "elif sys.argv[1:] == ['logout']:\n"
            "    os.remove(os.path.join(d, 'auth.json'))\n", encoding="utf-8")
        if os.name == "nt":
            (fake / "codex.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            (fake / "codex").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            (fake / "codex").chmod(0o755)
        os.environ["PATH"] = str(fake) + os.pathsep + was_path
        try:
            login("broken", by="eve")
            raise AssertionError("no link, no sign-in")
        except RuntimeError as e:
            assert "Error: network" in str(e), e
        assert "broken" not in names(), "a failed first sign-in leaves no account"
        assert login("thuis", by="bob") == "https://auth.openai.com/codex/device"
        a = {x["id"]: x for x in listing()}["thuis"]
        assert a["login"] == "https://auth.openai.com/codex/device" and a["usercode"] == "ABCD-EFGH2" and not a["kind"], a
        LOGINS["thuis"]["p"].wait(20)
        a = {x["id"]: x for x in listing(windows_of=lambda n: [[12.0, "5h", time.time() + 99]])}["thuis"]
        assert (a["kind"], a["email"], a["plan"], a["by"], a["error"]) == ("login", "w@example.org", "plus", "bob", "") and "login" not in a \
            and a["windows"] and a["expires"] > 0, a
        (folder("thuis") / "deny").write_text("", encoding="utf-8")
        login("thuis")
        LOGINS["thuis"]["p"].wait(20)
        assert "device code expired" in {x["id"]: x for x in listing()}["thuis"]["error"], "a sign-in that fails says why"
        (folder("thuis") / "deny").unlink()
        (folder("thuis") / "auth.json").unlink()
        (folder("thuis") / "keyring").write_text("", encoding="utf-8")  # config.toml keeps sign-ins in the keyring
        login("thuis")
        LOGINS["thuis"]["p"].wait(20)
        a = {x["id"]: x for x in listing()}["thuis"]
        assert (a["kind"], a["error"]) == ("login", ""), ("a sign-in into the keyring is one too", a)
        (folder("thuis") / "keyring").unlink()
        (folder("thuis") / "wait").write_text("30", encoding="utf-8")
        login("thuis")
        p = LOGINS["thuis"]["p"]
        assert cancel("thuis") == "thuis: sign-in cancelled" and p.wait(10) is not None
        assert "login" not in {x["id"]: x for x in listing()}["thuis"], "a cancelled sign-in leaves the page"
        try:
            set_key("thuis", "nope")
            raise AssertionError("not an OpenAI key")
        except ValueError:
            pass
        key = "sk-proj-" + "k" * 40
        assert set_key("sleutel", key, by="bob") == "sleutel: API key saved"
        a = {x["id"]: x for x in listing(windows_of=lambda n: [[1.0, "5h", None]])}["sleutel"]
        assert a["kind"] == "apikey" and a["preview"].endswith("kkkk") and a["windows"] == [] and a["by"] == "bob", a
        may("sleutel", "bob", False) or may("sleutel", "alice", True, remove=True) or may("new", "eve", False)
        for who, owner_pc, rm in (("eve", False, True), ("alice", True, False)):
            try:
                may("sleutel", who, owner_pc, remove=rm)
                raise AssertionError(f"{who} must not touch bob's account")
            except PermissionError:
                pass
        keep_thread("0199aaaa-bbbb-cccc", "thuis")
        keep_thread("0199dddd-eeee-ffff", "sleutel")
        assert threads() == {"0199aaaa-bbbb-cccc": "thuis", "0199dddd-eeee-ffff": "sleutel"}
        assert remove_later("thuis").startswith("thuis: removed once") and removing() == ["thuis"]
        try:
            env("thuis")
            raise AssertionError("no new run on an account waiting for removal")
        except RuntimeError as e:
            assert "removed once" in str(e), e
        for n in names():
            remove(n)
        assert names() == [] and threads() == {}, "its threads are forgotten with it"
        assert all((MAIN / f).exists() for f in ("sessions", "plugins", "config.toml", "auth.json", "state_5.sqlite")), \
            "the pc's own ~/.codex is untouched"
    finally:
        os.environ["PATH"] = was_path
        for n in list(LOGINS):
            _drop(n)
        shutil.rmtree(tmp, ignore_errors=True)
    print("codex_accounts self-test ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        print(__doc__)
