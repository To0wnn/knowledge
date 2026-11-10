#!/usr/bin/env python3
"""CPU, GPU, RAM and VRAM use of this pc for the Machines list: the companion calls sample() once a round (a minute)
and keeps the last 24 hours, summary() turns them into min, max, average, now and a 10-minute series per metric.

CPU and RAM on Linux (/proc), Windows (kernel32) and macOS (ps, sysctl, vm_stat). GPU and VRAM from nvidia-smi
(NVIDIA, any OS) or the amdgpu sysfs files (AMD on Linux); a pc without either reports none. Standard library only.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import sys

KEEP = 24 * 3600
BUCKET = 600
_PREV: dict = {}  # per caller the last CPU counters [busy, total]: the CPU share is the change since its previous call


def _run(cmd: list) -> str:
    try:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flashing up every minute
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=flags).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _cpu_counters():
    """[busy, total] time since boot, or None where the OS has no cheap counter (macOS: ps instead)."""
    if sys.platform.startswith("linux"):
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
        return [sum(v[:8]) - idle, sum(v[:8])]
    if os.name == "nt":
        import ctypes
        idle, kern, user = (ctypes.c_ulonglong() for _ in range(3))
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        return [kern.value + user.value - idle.value, kern.value + user.value]  # kernel time includes idle time
    return None


def cpu(key: str = "round") -> float | None:
    c = _cpu_counters()
    if c is None:  # macOS: the processes' share, summed over all cores
        out = _run(["ps", "-A", "-o", "%cpu="])
        return min(100.0, sum(float(x) for x in out.split() if re.fullmatch(r"[\d.]+", x)) / (os.cpu_count() or 1)) if out else None
    prev, _PREV[key] = _PREV.get(key), c
    if not prev or c[1] <= prev[1]:
        return None  # the first round has nothing to compare with
    return round(100 * (c[0] - prev[0]) / (c[1] - prev[1]), 1)


def ram() -> list | None:
    """[used MB, total MB]."""
    if sys.platform.startswith("linux"):
        with open("/proc/meminfo") as f:
            m = {k: int(v.split()[0]) for k, v in (line.split(":", 1) for line in f)}
        return [(m["MemTotal"] - m["MemAvailable"]) // 1024, m["MemTotal"] // 1024]
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in ("total", "avail", "pt", "pa", "vt", "va", "ve")]
        s = Status(len=ctypes.sizeof(Status))
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
        return [(s.total - s.avail) >> 20, s.total >> 20]
    if sys.platform == "darwin":
        total = _run(["sysctl", "-n", "hw.memsize"]).strip()
        vm = _run(["vm_stat"])
        page = int(m.group(1)) if (m := re.search(r"page size of (\d+)", vm)) else 4096
        pages = {k: int(v) for k, v in re.findall(r"Pages (free|speculative|purgeable|inactive)[^:]*:\s+(\d+)", vm)}
        if total.isdigit() and pages:
            return [(int(total) - sum(pages.values()) * page) >> 20, int(total) >> 20]
    return None


def _nvidia_asleep() -> bool:
    """A laptop's NVIDIA GPU in runtime suspend: nvidia-smi would wake it every minute and drain the battery."""
    root = "/sys/bus/pci/devices"
    if not os.path.isdir(root):
        return False
    for dev in os.listdir(root):
        try:
            with open(f"{root}/{dev}/vendor") as v, open(f"{root}/{dev}/power/runtime_status") as st:
                if v.read().strip() == "0x10de" and st.read().strip() == "suspended":
                    return True
        except OSError:
            continue
    return False


def gpu() -> tuple:
    """(busy %, [VRAM used MB, total MB]) of the busiest GPU, or (None, None)."""
    if shutil.which("nvidia-smi") and not _nvidia_asleep():
        rows = [r.split(",") for r in _run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]).splitlines()]
        rows = [[float(x) for x in r] for r in rows if len(r) == 3 and all(re.fullmatch(r"\s*[\d.]+\s*", x) for x in r)]
        if rows:
            b = max(rows)
            return b[0], [int(b[1]), int(b[2])]
    if sys.platform.startswith("linux") and os.path.isdir("/sys/class/drm"):
        best = (None, None)
        for card in sorted(os.listdir("/sys/class/drm")):
            d = f"/sys/class/drm/{card}/device"
            try:
                with open(f"{d}/gpu_busy_percent") as f:
                    busy = float(f.read())
                with open(f"{d}/mem_info_vram_used") as u, open(f"{d}/mem_info_vram_total") as t:
                    vram = [int(u.read()) >> 20, int(t.read()) >> 20]
            except (OSError, ValueError):
                continue
            if re.fullmatch(r"card\d+", card) and (best[0] is None or busy > best[0]):
                best = (busy, vram)
        return best
    return None, None


def sample(now: float, key: str = "round") -> list:
    """[time, cpu %, ram MB, ram total, gpu %, vram MB, vram total]; None where unknown."""
    def safe(f):
        try:
            return f()
        except (OSError, ValueError, KeyError, AttributeError, IndexError):
            return None
    r, (g, v) = safe(ram) or [None, None], safe(gpu) or (None, None)
    return [int(now), safe(lambda: cpu(key)), *r, g, *(v or [None, None])]


def summary(samples: list, now: float) -> dict:
    """Per metric [min, max, avg, now] over the last 24 hours and its 10-minute averages, oldest first (None: no
    sample then); totals from the newest sample. Metrics no sample knows are left out."""
    samples = [s for s in samples if now - s[0] < KEEP]
    out, last = {"bucket": BUCKET, "total": {}}, samples[-1] if samples else None
    for name, i, ti in (("cpu", 1, None), ("ram", 2, 3), ("gpu", 4, None), ("vram", 5, 6)):
        vals = [(s[0], s[i]) for s in samples if isinstance(s[i], (int, float))]
        if not vals:
            continue
        series = [[] for _ in range(KEEP // BUCKET)]
        for t, x in vals:
            series[min(len(series) - 1, int((KEEP - (now - t)) // BUCKET))].append(x)
        xs = [x for _, x in vals]
        hour = [None] * 60  # the last hour per minute, for zooming in
        for t, x in vals:
            if now - t < 3600:
                hour[min(59, int((3600 - (now - t)) // 60))] = x
        win = {w: [x for t, x in vals if now - t < sec] for w, sec in (("1h", 3600), ("6h", 6 * 3600))}
        out[name] = {"min": min(xs), "max": max(xs), "avg": round(sum(xs) / len(xs), 1), "now": last[i] if last[i] is not None else xs[-1],
                     "series": [round(sum(b) / len(b), 1) if b else None for b in series], "hour": hour,
                     "windows": {w: [min(v), max(v), round(sum(v) / len(v), 1)] for w, v in win.items() if v}}
        if ti is not None and last[ti]:
            out["total"][name] = last[ti]
    return out


if __name__ == "__main__" and sys.argv[1:] == ["--self-test"]:
    import time
    now = time.time()
    s1 = sample(now)
    assert len(s1) == 7 and s1[0] == int(now)
    s2 = sample(now + 1)
    assert s2[1] is None or 0 <= s2[1] <= 100, s2
    assert sample(now, "live")[1] is None or not sys.platform.startswith(("linux", "win")), "the live sampler keeps its own counters"
    if sys.platform.startswith("linux"):
        assert s1[2] and s1[3] and s1[2] <= s1[3], s1  # /proc/meminfo always answers
    # the summary: min/max/avg over raw samples, a 10-minute series, old samples dropped, missing metrics left out
    fake = [[now - 90000, 99, 1, 8, None, None, None], [now - 1200, 10, 2000, 8000, None, None, None],
            [now - 60, 30, 4000, 8000, None, None, None], [now - 1, 20, 3000, 8000, None, None, None]]
    got = summary(fake, now)
    assert got["cpu"]["min"] == 10 and got["cpu"]["max"] == 30 and got["cpu"]["avg"] == 20.0 and got["cpu"]["now"] == 20, got
    assert len(got["cpu"]["series"]) == 144 and got["cpu"]["series"][-1] == 25.0 and got["cpu"]["series"][-2] == 10, got["cpu"]["series"][-3:]
    assert got["cpu"]["hour"][-1] == 20 and got["cpu"]["hour"][40] == 10 and got["cpu"]["hour"].count(None) == 58, got["cpu"]["hour"][-3:]
    assert got["cpu"]["windows"] == {"1h": [10, 30, 20.0], "6h": [10, 30, 20.0]}, got["cpu"]["windows"]
    assert got["total"] == {"ram": 8000} and "gpu" not in got and "vram" not in got, got
    assert summary([], now) == {"bucket": BUCKET, "total": {}}
    print("sysstats self-test ok", s1)
