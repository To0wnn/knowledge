#!/usr/bin/env python3
"""Push the SHA-256 hashes of the known secrets to the knowledge server (redaction and the write guard). Run on the VM.

  ./token.py push-secrets

Device tokens are created and revoked in the web UI (https://knowledge.to0wnn.com/ui/admin); the server stores only
their hashes and treats them as secrets too. tokens-plain.json still holds the tokens made before the web UI, so
push-secrets keeps masking those.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

PLAIN = os.path.expanduser("~/.config/knowledge-server/tokens-plain.json")
REMOTE = "root@<lan-ip>:/root/docker/knowledge/config/secrets.json"
# Optional local list of secrets (value<TAB>NAME per line). The Vaultwarden helper `secret tsv` is preferred.
SECRETS_TSV = os.path.expanduser(os.environ.get("KNOWLEDGE_SECRETS_TSV", "~/.config/kennis-sync/geheimen.tsv"))


def load() -> dict:
    return json.load(open(PLAIN)) if os.path.exists(PLAIN) else {}


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
    target = REMOTE
    try:
        subprocess.run(["scp", "-q", f.name, target], check=True)
        subprocess.run(["ssh", target.split(":")[0], "chown 1000:1000 /root/docker/knowledge/config/secrets.json && "
                        "chmod 600 /root/docker/knowledge/config/secrets.json"], check=True)
    finally:
        os.unlink(f.name)
    print(f"pushed {len(values)} secret hashes")


def main(argv):
    if argv[:1] == ["push-secrets"]:
        push_secrets()
    elif argv[:1] in (["add"], ["revoke"], ["list"]):
        sys.exit("Tokens are managed in the web UI now: https://knowledge.to0wnn.com/ui/admin")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
