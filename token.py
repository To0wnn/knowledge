#!/usr/bin/env python3
"""Manage device tokens for the knowledge server. Run on the VM.

  ./token.py list
  ./token.py add <device> [--read-only] [--watch] [--roots memory,homelab] [--redact]
                                                    prints the new token once (enter it in the plugin)
  ./token.py revoke <device>
  ./token.py push-secrets                           sends SHA-256 hashes of the known secrets to forge (redaction)

--roots limits a token to some roots; --redact masks known secrets in everything it reads (for agents and PA's,
which also read untrusted mail). Scoped tokens have no web UI access.

Readable tokens stay on the VM only (~/.config/knowledge-server/tokens-plain.json, 0600) so they can be handed out.
Forge gets only SHA-256 hashes (/root/docker/knowledge/config/tokens.json), so a leaked file there is useless.
Every change is pushed to forge right away; the server picks it up without a restart.
"""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile

PLAIN = os.path.expanduser("~/.config/knowledge-server/tokens-plain.json")
REMOTE = "root@<lan-ip>:/root/docker/knowledge/config/tokens.json"
# Optional local list of secrets (value<TAB>NAME per line). The Vaultwarden helper `secret tsv` is preferred.
SECRETS_TSV = os.path.expanduser(os.environ.get("KNOWLEDGE_SECRETS_TSV", "~/.config/kennis-sync/geheimen.tsv"))


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
        subprocess.run(["ssh", REMOTE.split(":")[0], "chown 1000:1000 /root/docker/knowledge/config/tokens.json && "
                        "chmod 600 /root/docker/knowledge/config/tokens.json"], check=True)
    finally:
        os.unlink(f.name)


def push_secrets():
    """Hashes only: the server can recognise a secret value in a text without knowing it."""
    values = set()
    try:  # Vaultwarden via the `secret` helper; else the local list
        lines = subprocess.run(["secret", "tsv"], check=True, capture_output=True, text=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        lines = open(SECRETS_TSV, encoding="utf-8").read().splitlines() if os.path.exists(SECRETS_TSV) else []
    for line in lines:
        if line.startswith("#") or "\t" not in line:
            continue
        value = line.split("\t", 1)[0].strip()
        if len(value) >= 6:  # shorter values would also mask ordinary words
            values.add(hashlib.sha256(value.encode()).hexdigest())
    for cfg in load().values():  # device tokens are secrets too
        values.add(hashlib.sha256(cfg["token"].encode()).hexdigest())
    local = os.path.join(os.path.dirname(PLAIN), "secret-hashes.json")  # for the local secret-guard hook
    with open(local, "w") as f:
        json.dump(sorted(values), f)
    os.chmod(local, 0o600)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
        json.dump(sorted(values), f)
    target = REMOTE.replace("tokens.json", "secrets.json")
    try:
        subprocess.run(["scp", "-q", f.name, target], check=True)
        subprocess.run(["ssh", target.split(":")[0], "chown 1000:1000 /root/docker/knowledge/config/secrets.json && "
                        "chmod 600 /root/docker/knowledge/config/secrets.json"], check=True)
    finally:
        os.unlink(f.name)
    print(f"pushed {len(values)} secret hashes")


def main(argv):
    tokens = load()
    if not argv or argv[0] == "list":
        for name, cfg in tokens.items():
            print(name, "write" if cfg.get("write") else "read-only", "watch" if cfg.get("watch") else "")
    elif argv[0] == "add" and len(argv) >= 2:
        name = argv[1]
        tok = secrets.token_urlsafe(32)
        tokens[name] = {"token": tok, "write": "--read-only" not in argv, "watch": "--watch" in argv}
        if "--roots" in argv:
            tokens[name]["roots"] = argv[argv.index("--roots") + 1].split(",")
        if "--redact" in argv:
            tokens[name]["redact"] = True
        save_and_push(tokens)
        label = f"KNOWLEDGE_TOKEN_{name.upper().replace('-', '_')}"
        try:  # keep it in the vault too, so redaction and push-secrets know it
            subprocess.run(["secret", "set", label, "--note", "knowledge server device token"], input=tok,
                           text=True, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            if os.path.exists(SECRETS_TSV):
                with open(SECRETS_TSV, "a") as f:
                    f.write(f"{tok}\t{label}\n")
        print(tok)
    elif argv[0] == "push-secrets":
        push_secrets()
    elif argv[0] == "revoke" and len(argv) == 2:
        tokens.pop(argv[1], None)
        save_and_push(tokens)
        print("revoked", argv[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
