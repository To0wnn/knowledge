"""Uptime Kuma push heartbeat for the VM's scheduled jobs, so a job that does not run at all turns its monitor red.

push(<vault name of the push token>, up=True|False, msg). A no-op without KNOWLEDGE_KUMA_URL (tests, installs without
Kuma). Standard library only."""
import os
import shutil
import subprocess
import urllib.parse
import urllib.request


def push(secret_name: str, up: bool = True, msg: str = "OK") -> None:
    base = os.environ.get("KNOWLEDGE_KUMA_URL", "").rstrip("/")
    if not base:
        return
    secret = shutil.which("secret") or os.path.expanduser("~/.local/bin/secret")
    token = subprocess.run([secret, secret_name], capture_output=True, text=True, check=True).stdout.strip()
    query = urllib.parse.urlencode({"status": "up" if up else "down", "msg": msg[:200]})
    with urllib.request.urlopen(f"{base}/api/push/{token}?{query}", timeout=15) as r:
        r.read()
