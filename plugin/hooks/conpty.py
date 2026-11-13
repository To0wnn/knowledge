#!/usr/bin/env python3
"""A program in a pseudo console on Windows (ConPTY, Windows 10 1809+), the Windows side of Python's pty: for a
terminal program (claude setup-token) that prints nothing and waits forever when its output is a pipe.

  p = Spawn(command_line, env, cols, rows); p.read() -> bytes ("" at the end); p.write(bytes); p.poll(); p.kill()
Standard library only (ctypes).
"""
import ctypes
import os
import subprocess
from ctypes import wintypes

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
STILL_ACTIVE = 259


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD]
    k.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    k.CreatePseudoConsole.restype = ctypes.c_long  # HRESULT
    k.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
    k.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    k.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                                            ctypes.c_void_p, ctypes.c_void_p]
    k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                                 wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW),
                                 ctypes.POINTER(PROCESS_INFORMATION)]
    k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _ok(r):
    if not r:
        raise ctypes.WinError(ctypes.get_last_error())


class Spawn:
    """`cmd` (a list, or a whole command line) in a new pseudo console of cols x rows; Popen-like: pid, poll, wait, kill."""

    def __init__(self, cmd, env: dict, cols: int = 500, rows: int = 50):
        k = self.k = _k32()
        in_r, in_w, out_r, out_w = (wintypes.HANDLE() for _ in range(4))
        _ok(k.CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0))
        _ok(k.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0))
        self.hpc = ctypes.c_void_p()
        hr = k.CreatePseudoConsole(COORD(cols, rows), in_r, out_w, 0, ctypes.byref(self.hpc))
        k.CloseHandle(in_r)  # the console holds its own copies
        k.CloseHandle(out_w)
        if hr != 0:
            raise OSError(f"CreatePseudoConsole failed (0x{hr & 0xffffffff:08x}): Windows 10 1809 or newer is needed")
        self.inp, self.out = in_w, out_r
        size = ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        _ok(k.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)))
        # the console handle itself is the value here, not a pointer to it
        _ok(k.UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, self.hpc, ctypes.sizeof(self.hpc), None, None))
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES  # with no handles: never the companion's own (redirected) ones
        si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
        block = ctypes.create_unicode_buffer("".join(f"{k_}={v}\0" for k_, v in sorted(env.items(), key=lambda x: x[0].upper())) + "\0")
        line = ctypes.create_unicode_buffer(cmd if isinstance(cmd, str) else subprocess.list2cmdline(cmd))
        pi = PROCESS_INFORMATION()
        try:
            _ok(k.CreateProcessW(None, line, None, None, False, EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                                 block, None, ctypes.byref(si), ctypes.byref(pi)))
        except OSError:
            self.close()
            raise
        finally:
            k.DeleteProcThreadAttributeList(attrs)
        k.CloseHandle(pi.hThread)
        self.proc, self.pid, self.returncode = pi.hProcess, pi.dwProcessId, None

    def read(self, n: int = 4096) -> bytes:
        """What the console drew; b"" once it is closed."""
        buf, got = ctypes.create_string_buffer(n), wintypes.DWORD()
        if not self.out or not self.k.ReadFile(self.out, buf, n, ctypes.byref(got), None):
            if self.out:
                self.k.CloseHandle(self.out)
                self.out = None
            return b""
        return buf.raw[:got.value]

    def write(self, data: bytes):
        done = wintypes.DWORD()
        if not self.k.WriteFile(self.inp, data, len(data), ctypes.byref(done), None):
            raise ctypes.WinError(ctypes.get_last_error())

    def poll(self):
        if self.returncode is None:
            code = wintypes.DWORD()
            if self.k.GetExitCodeProcess(self.proc, ctypes.byref(code)) and code.value != STILL_ACTIVE:
                self.returncode = code.value
                self.close()  # the console ends: read() then returns b""
        return self.returncode

    def wait(self, timeout=None):
        self.k.WaitForSingleObject(self.proc, 0xFFFFFFFF if timeout is None else int(timeout * 1000))
        if self.poll() is None:
            raise subprocess.TimeoutExpired("conpty", timeout)
        return self.returncode

    def kill(self):
        if self.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.pid)], capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.wait(10)
        self.close()

    def close(self):
        if self.hpc:
            self.k.ClosePseudoConsole(self.hpc)  # also ends a reader blocked in read()
            self.hpc = ctypes.c_void_p()
        for h in ("inp",):
            if getattr(self, h, None):
                self.k.CloseHandle(getattr(self, h))
                setattr(self, h, None)
