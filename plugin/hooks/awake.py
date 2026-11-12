"""Keep the pc from going to sleep on its own while an agent works here (a chat turn or a worker): a laptop that
suspends after 15 idle minutes would otherwise cut a turn off without a word. Only idle sleep is held off: closing the
lid, the power button or a sleep chosen by hand still work (logind's LidSwitchIgnoreInhibited=yes, Windows' and
macOS' own rules), and the hold ends with the work or with the companion.

  Linux    systemd-inhibit --what=idle (polkit lets any user hold an idle lock), a child that ends once the
           companion is gone, so a crash never leaves the lock behind
  Windows  PowerCreateRequest + PowerSetRequest(PowerRequestSystemRequired): owned by the process, not a thread, so it
           holds from any thread and ends with the process (as Zed's agent.prevent_idle_sleep)
  macOS    caffeinate -i -w <companion pid>

Off on a pc with KNOWLEDGE_KEEP_AWAKE=0.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time

WHY = "An agent is working (knowledge companion)"
_held = None  # the Popen (Linux, macOS) or the power request handle (Windows) while held


def off() -> bool:
    return os.environ.get("KNOWLEDGE_KEEP_AWAKE") == "0"


def hold(want: bool) -> str:
    """Hold or release; "" when that worked or was already so, else why it could not (the caller says it once)."""
    global _held
    if want == (_held is not None):
        if _held is not None and not sys.platform.startswith("win") and _held.poll() is not None:
            _held = None  # the inhibitor died (logind restarted): take it again
            return hold(want)
        return ""
    try:
        if want:
            _held = _take()
        else:
            _give(_held)
            _held = None
        return ""
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:300]


def _take():
    if sys.platform.startswith("win"):
        return _win_take()
    me = str(os.getpid())
    if sys.platform == "darwin":
        cmd = ["caffeinate", "-i", "-w", me]
    else:
        if not shutil.which("systemd-inhibit"):
            raise RuntimeError("systemd-inhibit not found (no systemd on this pc)")
        cmd = ["systemd-inhibit", "--what=idle", "--who=Knowledge", f"--why={WHY}", "--mode=block",
               "sh", "-c", f"while kill -0 {me} 2>/dev/null; do sleep 15; done"]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         start_new_session=True)
    time.sleep(0.3)  # a refused lock ends at once, with logind's reason on stderr
    if p.poll() is not None:
        raise RuntimeError((p.stderr.read() or b"").decode(errors="replace").strip()[:200] or f"exit {p.returncode}")
    return p


def _give(h):
    if sys.platform.startswith("win"):
        return _win_give(h)
    try:  # the whole group: systemd-inhibit and the sh it runs
        os.killpg(h.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    h.wait(5)


if sys.platform.startswith("win"):
    import ctypes
    from ctypes import wintypes

    class _Detailed(ctypes.Structure):
        _fields_ = [("LocalizedReasonModule", wintypes.HMODULE), ("LocalizedReasonId", wintypes.ULONG),
                    ("ReasonStringCount", wintypes.ULONG), ("ReasonStrings", ctypes.POINTER(wintypes.LPWSTR))]

    class _Reason(ctypes.Union):
        _fields_ = [("Detailed", _Detailed), ("SimpleReasonString", wintypes.LPWSTR)]

    class _ReasonContext(ctypes.Structure):  # REASON_CONTEXT
        _fields_ = [("Version", wintypes.ULONG), ("Flags", wintypes.DWORD), ("Reason", _Reason)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.PowerCreateRequest.restype = wintypes.HANDLE
    _k32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
    _k32.PowerSetRequest.argtypes = _k32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    _k32.PowerSetRequest.restype = _k32.PowerClearRequest.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _SYSTEM_REQUIRED, _SIMPLE_STRING, _INVALID = 1, 0x1, ctypes.c_void_p(-1).value

    def _win_take():
        ctx = _ReasonContext(0, _SIMPLE_STRING)  # POWER_REQUEST_CONTEXT_VERSION 0
        ctx.Reason.SimpleReasonString = WHY
        h = _k32.PowerCreateRequest(ctypes.byref(ctx))
        if not h or h == _INVALID:
            raise OSError(ctypes.get_last_error(), "PowerCreateRequest failed")
        if not _k32.PowerSetRequest(h, _SYSTEM_REQUIRED):
            err = ctypes.get_last_error()
            _k32.CloseHandle(h)
            raise OSError(err, "PowerSetRequest failed")
        return h

    def _win_give(h):
        _k32.PowerClearRequest(h, _SYSTEM_REQUIRED)
        _k32.CloseHandle(h)


def _listed(pid: int = 0) -> bool | None:
    """Whether the OS lists our hold now; None when this pc cannot tell (no logind, powercfg needs an admin)."""
    try:
        if sys.platform.startswith("win"):
            r = subprocess.run(["powercfg", "/requests"], capture_output=True, text=True, timeout=20)
            return WHY in r.stdout if r.returncode == 0 else None
        if sys.platform == "darwin":
            r = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=20)
            return f"pid {pid}(caffeinate)" in r.stdout if r.returncode == 0 else None
        r = subprocess.run(["systemd-inhibit", "--list", "--no-pager"], capture_output=True, text=True, timeout=20)
        return WHY in r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


if __name__ == "__main__" and "--self-test" in sys.argv:
    WHY = f"{WHY} self-test {os.getpid()}"  # its own words: a companion running beside it (the e2e test) holds one too
    can = _listed() is not None  # a container or CI box without logind cannot hold one: then it must say why, not crash
    err = hold(True)
    assert (err == "") if can else (err == "" or isinstance(err, str)), err
    if not err:
        assert hold(True) == "", "a second hold is a no-op"
        pid = getattr(_held, "pid", 0)
        seen = _listed(pid)
        assert seen in (True, None), "held, but the OS does not list it"
        assert hold(False) == "" and _held is None
        time.sleep(0.5)
        assert _listed(pid) in (False, None), "released, but the OS still lists it"
    else:
        assert _held is None
    assert hold(False) == ""
    print("ok")
