#!/usr/bin/env python3
"""Admin rights for the agents of this pc's chats: switched on once per pc by its owner and kept until switched off.

Approving once is the point: one UAC "Yes" (Windows), one sudo password (Linux), one admin password (macOS). Nobody
approves within ASK_WAIT: it stays off and everything goes on as before (a UAC prompt on a locked screen waits for
ever, measured 13 Nov 2026; so the companion stops waiting itself).

- Windows: the "Yes" registers the task TASK (RunLevel HighestAvailable, this user, no trigger). A process of this user
  may run it (schtasks /Run) without a prompt, not change or delete it (measured on Windows 11 26H2). An admin chat's
  host (chat_host.py) is started by that task: the companion writes <live dir>/admin.req and runs the task, whose
  launch() starts the host elevated. Off: the task deletes itself (admin.off), no second prompt.
- Linux and macOS: the password installs SUDOERS_DIR/<file> "<user> ALL=(ALL) NOPASSWD: ALL", checked with visudo
  first; agents run sudo as usual. Off: sudo -n rm. Linux: a chat that may not have admin runs under
  setpriv --no-new-privs, so sudo cannot raise it; macOS has no such switch, so there every chat of the user can sudo.

UAC is no security boundary and NOPASSWD sudo makes the user root: with admin on, every program of this user can
become admin, not only the agents. That includes a guest's chat (it runs as the owner): without the "admin" share it
starts without admin, but one that can run commands can still reach it (systemd-run, the admin task). The pages say so.

  admin.py status | on | off   on in a terminal asks the sudo password there (Linux without a desktop)
  admin.py launch              run by the admin task (Windows, elevated)
  admin.py --self-test
Standard library only.
"""
import getpass
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
import workers  # noqa: E402

STATE = workers.DIR.parent
LIVE = STATE / "live"  # chat.LIVE_DIR
TASK = "Knowledge companion admin"
LAUNCHER = STATE / "admin_launch.py"  # the task's fixed action; it points at the plugin's current admin.py
OFF = STATE / "admin.off"
FLAG = STATE / "admin.on"
TASK_EXE = STATE / "admin_exe.txt"  # Windows: the Python the task was registered with  # Linux, macOS: switched on here (sudo itself is asked only at on and off: each try is logged)
ASK_WAIT = 150  # seconds: a UAC prompt with nobody there gives up after ~123 s by itself (measured), a locked screen never
REQ_TTL = 25  # an admin.req older than this is not started: the companion gives up on it after 30 s (chat.Hosted)
SUDOERS_DIR = "/etc/sudoers.d"
VISUDO = "/usr/sbin/visudo"


def user() -> str:
    if os.name == "nt":
        u = getpass.getuser()
    else:  # the account itself, not $USER / $LOGNAME
        import pwd
        u = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", u):  # goes into a sudoers line: nothing it could break
        raise RuntimeError(f"user name {u!r} cannot go into a sudoers file")
    return u


def sudoers_file() -> str:
    return f"{SUDOERS_DIR}/90-knowledge-admin-{user().replace('.', '_')}"  # sudo skips a file whose name has a dot


def run(argv, timeout=30, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)


# ---- status -----------------------------------------------------------------------------------------------------

_cache = [0.0, False]


def sudo_free() -> bool:
    """sudo runs without a password now (this or any other sudoers rule)."""
    try:
        return bool(shutil.which("sudo")) and run(["sudo", "-n", "true"]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def status(fresh: bool = False) -> bool:
    """Admin is on here: the task exists (Windows), or it was switched on and (fresh) sudo still runs without a password
    (Linux, macOS). Cached 30 s."""
    if not fresh and time.time() - _cache[0] < 30:
        return _cache[1]
    try:
        if os.name == "nt":
            on = run(["schtasks", "/Query", "/TN", TASK]).returncode == 0
        else:
            on = FLAG.exists() and (not fresh or sudo_free())
            if fresh and not on:
                FLAG.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        on = False
    _cache[:] = [time.time(), on]
    return on


def task_exe() -> str:
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return str(pyw if pyw.is_file() else Path(sys.executable))


def active() -> bool:
    """Admin is on and works: on Windows also the task's Python is still this one (a Python update moves it)."""
    if not status():
        return False
    try:
        return os.name != "nt" or TASK_EXE.read_text(encoding="utf-8") == task_exe()
    except OSError:
        return False


# ---- on ---------------------------------------------------------------------------------------------------------

def install_script() -> str:
    """The shell lines that install the sudoers file, run as root: checked with visudo before it goes in place."""
    line, dst = f"{user()} ALL=(ALL) NOPASSWD: ALL", sudoers_file()
    return (f"set -e; t=$(mktemp); printf '%s\\n' '{line}' > \"$t\"; {VISUDO} -cf \"$t\" >/dev/null; "
            f"install -m 0440 -o 0 -g 0 \"$t\" '{dst}'; rm -f \"$t\"")


def write_launcher():
    """The admin task's action (registered once, so a fixed path): runs the current plugin's admin.py launch."""
    text = ("# Written by the knowledge companion; run elevated by the task \"" + TASK + "\".\n"
            f"import runpy, sys\nsys.argv = [{str(HOOKS / 'admin.py')!r}, 'launch']\nrunpy.run_path(sys.argv[0], run_name='__main__')\n")
    if not LAUNCHER.exists() or LAUNCHER.read_text(encoding="utf-8") != text:
        LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
        LAUNCHER.write_text(text, encoding="utf-8")


def task_xml() -> str:
    from xml.sax.saxutils import escape
    exe = task_exe()
    who = windows_account()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts the knowledge companion's admin chats elevated (switched on once by its owner)</Description></RegistrationInfo>
  <Principals><Principal id="Author"><UserId>{escape(who)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><AllowStartOnDemand>true</AllowStartOnDemand></Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(f'"{LAUNCHER}"')}</Arguments>
    <WorkingDirectory>{escape(str(Path.home()))}</WorkingDirectory></Exec></Actions>
</Task>
"""


def windows_account() -> str:
    """DOMAIN\\user as Windows names it (as knowledge-agent.py's: schtasks cannot map %USERDOMAIN% on a workgroup pc)."""
    import ctypes
    size = ctypes.c_ulong(512)
    buf = ctypes.create_unicode_buffer(size.value)
    if ctypes.windll.secur32.GetUserNameExW(2, buf, ctypes.byref(size)):
        return buf.value
    return getpass.getuser()


def run_as_admin(exe: str, params: str, wait: float) -> str:
    """One UAC prompt for `exe params` (ShellExecuteEx runas): "ok", "declined", "timeout" (the prompt may stay up, a
    late "Yes" still runs it) or "failed: ..."."""
    import ctypes
    from ctypes import wintypes

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    out = ["timeout"]

    def ask():
        sei = SEI(cbSize=ctypes.sizeof(SEI), fMask=0x40 | 0x400, lpVerb="runas", lpFile=exe, lpParameters=params, nShow=0)  # NOCLOSEPROCESS | FLAG_NO_UI
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        if not shell.ShellExecuteExW(ctypes.byref(sei)):
            err = ctypes.get_last_error()
            out[0] = "declined" if err == 1223 else f"failed: Windows error {err}"  # 1223: No, or nobody answered (~123 s, measured)
            return
        k = ctypes.windll.kernel32
        k.WaitForSingleObject(sei.hProcess, 60_000)
        code = wintypes.DWORD()
        k.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        k.CloseHandle(sei.hProcess)
        out[0] = "ok" if code.value == 0 else f"failed: exit {code.value}"
    t = threading.Thread(target=ask, daemon=True)  # ShellExecuteEx waits for the answer; a locked screen never answers
    t.start()
    t.join(wait)
    return out[0]


def on(wait: float = ASK_WAIT) -> str:
    """Ask the owner once: "on", "declined", "timeout", "terminal: <command>" (Linux without a desktop) or "failed: ..."."""
    if status(fresh=True) and active():  # Windows: a task whose Python moved is registered again
        return "on"
    if os.name != "nt" and sudo_free():  # another sudoers rule already allows it: nothing to install
        FLAG.write_text(str(time.time()), encoding="utf-8")
        return "on" if status(fresh=True) else "failed: could not keep the switch"
    if os.name == "nt":
        write_launcher()
        xml = STATE / "admin_task.xml"
        xml.write_text(task_xml(), encoding="utf-16")
        got = run_as_admin("schtasks.exe", f'/Create /F /TN "{TASK}" /XML "{xml}"', wait)
    elif sys.platform == "darwin":
        script = install_script().replace("\\", "\\\\").replace('"', '\\"')
        got = timed(["osascript", "-e", f'do shell script "{script}" with administrator privileges'], wait,
                    declined=lambda r: "-128" in r.stderr)  # -128: the user cancelled
    elif sys.stdin.isatty():  # admin.py on, in a terminal: sudo asks there
        got = "ok" if subprocess.run(["sudo", "sh", "-c", install_script()]).returncode == 0 else "failed: sudo"
    elif shutil.which("pkexec") and (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        got = timed(["pkexec", "/bin/sh", "-c", install_script()], wait, declined=lambda r: r.returncode == 126)  # 126: dismissed
    else:
        return f"terminal: {sys.executable} {HOOKS / 'admin.py'} on"
    if got != "ok":
        return got
    if os.name != "nt":
        FLAG.write_text(str(time.time()), encoding="utf-8")
    else:
        TASK_EXE.write_text(task_exe(), encoding="utf-8")
    if not status(fresh=True):  # Linux: an /etc/sudoers without @includedir /etc/sudoers.d ignores the file
        return "failed: approved, but sudo still asks for a password" + ("" if os.name == "nt" else f" (does /etc/sudoers include {SUDOERS_DIR}?)")
    return "on"


def timed(argv, wait, declined) -> str:
    """Run a prompting command, given up after `wait` seconds (the prompt goes with it)."""
    try:
        r = run(argv, timeout=wait)
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError as e:
        return f"failed: {e}"
    return "ok" if r.returncode == 0 else "declined" if declined(r) else f"failed: {(r.stderr or r.stdout).strip()[:200]}"


# ---- off --------------------------------------------------------------------------------------------------------

def off() -> str:
    """Admin off without a prompt: "off" or "failed: ..."."""
    if not status(fresh=True):
        return "off"
    if os.name == "nt":  # this user may run the task, not delete it: the task deletes itself
        OFF.write_text(str(time.time()), encoding="utf-8")
        run(["schtasks", "/Run", "/TN", TASK])
        for _ in range(40):
            time.sleep(0.25)
            if not status(fresh=True):
                return "off"
        return "failed: the admin task did not remove itself"
    r = run(["sudo", "-n", "rm", "-f", sudoers_file()])
    if r.returncode:
        return f"failed: {r.stderr.strip()[:200]}"
    FLAG.unlink(missing_ok=True)
    status(fresh=True)
    return "off (sudo still runs without a password here through another sudoers rule)" if sudo_free() else "off"


# ---- chats ------------------------------------------------------------------------------------------------------

def no_raise(argv: list) -> list:
    """Linux: `argv` so that it can never gain rights (sudo refuses), for a chat without admin on a pc where admin is on."""
    if sys.platform.startswith("linux") and (sp := shutil.which("setpriv")):
        return [sp, "--no-new-privs", *argv]
    return argv


def ask_host(d: Path, argv: list, cwd: str, env: dict):
    """Windows: have the admin task start this chat's host elevated (launch() writes d/host.json)."""
    req = {"at": time.time(), "argv": argv, "cwd": cwd, "env": env}
    tmp = d / "admin.req.part"
    tmp.write_text(json.dumps(req), encoding="utf-8")
    tmp.replace(d / "admin.req")
    r = run(["schtasks", "/Run", "/TN", TASK])
    if r.returncode:
        (d / "admin.req").unlink(missing_ok=True)
        raise RuntimeError(f"the admin task did not start: {(r.stderr or r.stdout).strip()[:200]}")


def launch():
    """Run elevated by the admin task: switch off when asked, else start every chat host asked for (each once)."""
    if OFF.exists():
        OFF.unlink(missing_ok=True)
        run(["schtasks", "/Delete", "/F", "/TN", TASK])
        return
    for req in sorted(LIVE.glob("*/admin.req")) if LIVE.is_dir() else []:
        d, taken = req.parent, req.with_name("admin.taken")
        try:
            req.replace(taken)  # claimed: two launches at once start it once
            r = json.loads(taken.read_text(encoding="utf-8"))
            taken.unlink()
        except (OSError, ValueError):
            continue
        if time.time() - float(r.get("at") or 0) > REQ_TTL or (d / "host.json").exists():
            continue
        run_host = lambda **kw: subprocess.Popen(r["argv"], cwd=r.get("cwd") or None, env=r.get("env") or None, stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        try:
            p = run_host(creationflags=workers.WIN_FLAGS | workers.BREAKAWAY)
        except OSError:
            p = run_host(creationflags=workers.WIN_FLAGS)
        ident = ""
        for _ in range(40):
            if (ident := workers.identity(p.pid)) or p.poll() is not None:
                break
            time.sleep(0.05)
        (d / "host.json").write_text(json.dumps({"pid": p.pid, "ident": ident, "admin": True}), encoding="utf-8")


def self_test():
    global SUDOERS_DIR
    s = install_script()
    assert "NOPASSWD: ALL" in s and VISUDO + " -cf" in s and s.index("-cf") < s.index("install -m 0440"), s
    assert "." not in Path(sudoers_file()).name, sudoers_file()
    if sys.platform.startswith("linux") and shutil.which("setpriv"):
        assert no_raise(["x"])[1:] == ["--no-new-privs", "x"]
        r = subprocess.run(no_raise(["sudo", "-n", "true"]), capture_output=True, text=True) if shutil.which("sudo") else None
        assert r is None or r.returncode != 0, "sudo must not run under no_new_privs"
    else:
        assert no_raise(["x"]) == ["x"]
    with tempfile.TemporaryDirectory() as t:  # launch(): a stale request is dropped, a fresh one claimed once
        global LIVE, OFF
        LIVE, OFF = Path(t) / "live", Path(t) / "admin.off"
        (LIVE / "c1").mkdir(parents=True)
        (LIVE / "c1" / "admin.req").write_text(json.dumps({"at": time.time() - REQ_TTL - 5, "argv": ["x"]}), encoding="utf-8")
        launch()
        assert not (LIVE / "c1" / "admin.req").exists() and not (LIVE / "c1" / "host.json").exists()
        (LIVE / "c2").mkdir()
        (LIVE / "c2" / "admin.req").write_text(json.dumps({"at": time.time(), "argv": [sys.executable, "-c", "import time; time.sleep(1)"]}), encoding="utf-8")
        if os.name == "nt":
            launch()
            h = json.loads((LIVE / "c2" / "host.json").read_text(encoding="utf-8"))
            assert h["pid"] and h["admin"], h
    if os.name == "nt":
        assert "HighestAvailable" in task_xml() and str(LAUNCHER) in task_xml()
    print("admin self-test ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "--self-test":
        self_test()
    elif cmd == "launch":
        launch()
    elif cmd == "status":
        print("on" if status(fresh=True) else "off")
    elif cmd == "on":
        print(on())
    elif cmd == "off":
        print(off())
    else:
        sys.exit(__doc__)
