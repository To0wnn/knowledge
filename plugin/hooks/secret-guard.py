#!/usr/bin/env python3
"""PreToolUse hook (Edit|Write|MultiEdit): refuse writing a known secret value into a file.

Compares every word (and the parts of user/pass, KEY=value, url:token) of the new text with SHA-256 hashes of the
known secrets (~/.config/knowledge-server/secret-hashes.json, written by `token.py push-secrets`). On a hit the tool
call is denied with a hint to store the value with `secret set NAME` and write ‹secret:NAME› instead. Without a hash
file it allows everything. Standard library only, a few milliseconds.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

HASHES = Path.home() / ".config/knowledge-server/secret-hashes.json"
TOKEN = re.compile(r"[^\s`'\"<>()\[\]{},;|*]+")


def leaks(text: str, hashes: set) -> bool:
    for tok in TOKEN.findall(text):
        for part in [tok, *re.split(r"[=:@/]", tok)]:
            if part and hashlib.sha256(part.encode()).hexdigest() in hashes:
                return True
    return False


def main():
    if not HASHES.exists():
        return
    hook = json.load(sys.stdin)
    inp = hook.get("tool_input", {}) or {}
    texts = [inp.get("content", ""), inp.get("new_string", "")] + [e.get("new_string", "") for e in inp.get("edits", []) or []]
    if leaks("\n".join(t for t in texts if t), set(json.loads(HASHES.read_text()))):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "This text contains a known secret value. Store it with `secret set NAME` "
                                        "(value on stdin) and write ‹secret:NAME› in the file instead."}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as err:  # never block normal work, but say so: a silent failure would let secrets through
        print(json.dumps({"systemMessage": f"secret-guard could not check this edit ({type(err).__name__}: {err}); "
                                           "secrets are not being caught until it is fixed."}))
