#!/usr/bin/env python3
"""Manage device tokens for the kennis server. Run on the VM.

  ./token.py list
  ./token.py add <device> [--read-only] [--watch]   prints the new token once (enter it in the plugin)
  ./token.py revoke <device>

Readable tokens stay on the VM only (~/.config/kennis-server/tokens-plain.json, 0600) so they can be handed out.
Forge gets only SHA-256 hashes (/root/docker/kennis/config/tokens.json), so a leaked file there is useless.
Every change is pushed to forge right away; the server picks it up without a restart.
"""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile

PLAIN = os.path.expanduser("~/.config/kennis-server/tokens-plain.json")
REMOTE = "root@<lan-ip>:/root/docker/kennis/config/tokens.json"
GEHEIMEN = os.path.expanduser("~/.config/kennis-sync/geheimen.tsv")


def load() -> dict:
    return json.load(open(PLAIN)) if os.path.exists(PLAIN) else {}


def save_and_push(tokens: dict):
    os.makedirs(os.path.dirname(PLAIN), mode=0o700, exist_ok=True)
    with open(PLAIN, "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(PLAIN, 0o600)
    hashed = {name: {"sha256": hashlib.sha256(cfg["token"].encode()).hexdigest(),
                     **{k: v for k, v in cfg.items() if k != "token"}} for name, cfg in tokens.items()}
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
        json.dump(hashed, f, indent=2)
    try:
        subprocess.run(["scp", "-q", f.name, REMOTE], check=True)
        subprocess.run(["ssh", REMOTE.split(":")[0], "chown 1000:1000 /root/docker/kennis/config/tokens.json && "
                        "chmod 600 /root/docker/kennis/config/tokens.json"], check=True)
    finally:
        os.unlink(f.name)


def main(argv):
    tokens = load()
    if not argv or argv[0] == "list":
        for name, cfg in tokens.items():
            print(name, "write" if cfg.get("write") else "read-only", "watch" if cfg.get("watch") else "")
    elif argv[0] == "add" and len(argv) >= 2:
        name = argv[1]
        tok = secrets.token_urlsafe(32)
        tokens[name] = {"token": tok, "write": "--read-only" not in argv, "watch": "--watch" in argv}
        save_and_push(tokens)
        with open(GEHEIMEN, "a") as f:  # kennis-sync redacts it if it ever ends up in a doc
            f.write(f"{tok}\tKENNIS_TOKEN_{name.upper().replace('-', '_')}\n")
        print(tok)
    elif argv[0] == "revoke" and len(argv) == 2:
        tokens.pop(argv[1], None)
        save_and_push(tokens)
        print("revoked", argv[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
