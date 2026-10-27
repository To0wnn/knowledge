#!/usr/bin/env python3
"""PreToolUse hook on the file-edit tools: refuse writing a known secret value into a file.

  secret-guard.py [claude|codex|agy]   (default claude)

Compares every word (and the parts of user/pass, KEY=value, url:token) of the new text with SHA-256 hashes of the
known secrets (~/.config/knowledge-server/secret-hashes.json, written by `token.py push-secrets`). On a hit the tool
call is denied with a hint to store the value with `secret set NAME` and write ‹secret:NAME› instead. Without a hash
file it allows everything. Only new text counts, so removing a secret is allowed. Standard library only, a few
milliseconds.
New text per agent: Claude Edit|Write|MultiEdit (content, new_string, edits), Codex apply_patch (the + lines of the
patch), agy (every string in toolCall.args except the old text and the path: CLI write_file Content, edit_file
Replacement; app write_to_file CodeContent, replace_file_content ReplacementContent).
"""
import hashlib
import json
import re
import sys
from pathlib import Path

HASHES = Path.home() / ".config/knowledge-server/secret-hashes.json"
PENDING = Path.home() / ".knowledge" / "pending-hints.txt"  # agy: said at the next turn by agent_hooks.py hints
TOKEN = re.compile(r"[^\s`'\"<>()\[\]{},;|*]+")
# agy args that are not new file text: old text, paths, and prose about the edit ("remove the password x")
OLD = {"Target", "TargetContent", "Path", "TargetFile", "AbsolutePath", "Instruction", "Description"}
REASON = ("This text contains a known secret value. Store it with `secret set NAME` (value on stdin) and write "
          "‹secret:NAME› in the file instead.")


def leaks(text: str, hashes: set) -> bool:
    for tok in TOKEN.findall(text):
        for part in [tok, *re.split(r"[=:@/]", tok)]:
            # a sentence may end right after the value: "the key is abc123."
            for p in {part, part.rstrip(".!?")}:
                if p and hashlib.sha256(p.encode()).hexdigest() in hashes:
                    return True
    return False


def strings(value, key=""):
    if key in OLD:
        return
    if isinstance(value, str):
        if value[:1] in ('"', "[", "{"):  # agy sends some args JSON-encoded ("\"text\""): read what it stands for
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = None
            if decoded is not None and decoded != value:
                yield from strings(decoded, key)
                return
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from strings(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v, key)


def new_text(agent: str, hook: dict) -> str:
    if agent == "agy":
        return "\n".join(strings((hook.get("toolCall") or {}).get("args") or {}))
    inp = hook.get("tool_input") or {}
    if isinstance(inp, str):  # Codex: a raw patch instead of {"command": patch}
        inp = {"command": inp}
    patch = inp.get("command") or inp.get("patch")
    if agent == "codex" or isinstance(patch, str):
        return "\n".join(line[1:] for line in str(patch or "").splitlines() if line.startswith("+"))
    texts = [inp.get("content", ""), inp.get("new_string", "")] + [e.get("new_string", "") for e in inp.get("edits", []) or []]
    return "\n".join(t for t in texts if isinstance(t, str) and t)


def deny(agent: str) -> str:
    if agent == "agy":
        return json.dumps({"decision": "deny", "reason": REASON})
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": REASON}})


def check(agent: str, hook: dict, hashes: set) -> str:
    """The deny JSON on a leak, else '' (agy reads any output, even {}, as a deny)."""
    return deny(agent) if leaks(new_text(agent, hook), hashes) else ""


def self_test():
    secret = "gategategate"
    hashes = {hashlib.sha256(secret.encode()).hexdigest()}
    leak = {"claude": {"tool_input": {"file_path": "a", "content": f"pw={secret}"}},
            "codex": {"tool_input": {"command": f"*** Begin Patch\n*** Add File: a.txt\n+user:{secret}\n*** End Patch"}},
            "agy": {"toolCall": {"name": "edit_file", "args": {"Path": "a", "Target": "x", "Replacement": f"t {secret}"}}}}
    for agent, hook in leak.items():
        assert check(agent, hook, hashes), f"{agent}: a new secret is refused"
    assert json.loads(check("agy", leak["agy"], hashes))["decision"] == "deny"
    assert check("claude", {"tool_input": {"command": f"+k={secret}"}}, hashes), "Claude with a Codex-style patch"
    for end in ".", "?", "!", "...":  # missed live with Codex on 27 Oct 2026: "the cat says <secret>."
        assert check("claude", {"tool_input": {"content": f"it is {secret}{end}"}}, hashes), f"a secret before {end!r}"
    removed = {"claude": {"tool_input": {"old_string": secret, "new_string": "‹secret:X›"}},
               "codex": {"tool_input": {"command": f"*** Update File: a\n@@\n-{secret}\n+‹secret:X›"}},
               "agy": {"toolCall": {"args": {"Path": "a", "Target": secret, "Replacement": "‹secret:X›"}}}}
    for agent, hook in removed.items():
        assert check(agent, hook, hashes) == "", f"{agent}: removing a secret is allowed"
    assert check("agy", {"toolCall": {"args": {"Content": "hello", "Path": "a"}}}, hashes) == "", "agy: silent when clean"
    assert check("agy", {"toolCall": {"args": {"ReplacementChunks": [{"ReplacementContent": secret}]}}}, hashes)
    for encoded in json.dumps(f'export PW="{secret}"'), json.dumps(f"a\n{secret}"):  # as agy stores CodeContent
        assert check("agy", {"toolCall": {"args": {"CodeContent": encoded}}}, hashes), f"agy: encoded {encoded}"
    chunks = json.dumps([{"TargetContent": secret, "ReplacementContent": "‹secret:X›"}])
    assert check("agy", {"toolCall": {"args": {"ReplacementChunks": chunks, "Instruction": f"remove {secret}"}}}, hashes) == "", \
        "agy: removing a secret, chunks encoded and named in the instruction, is allowed"
    print("ok")


def main(agent: str):
    if not HASHES.exists():
        return
    out = check(agent, json.load(sys.stdin), set(json.loads(HASHES.read_text())))
    if out:
        print(out)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        sys.exit()
    agent = (sys.argv[1:] or ["claude"])[0]
    try:
        main(agent)
    except Exception as err:  # never block normal work, but say so: a silent failure would let secrets through
        msg = f"secret-guard could not check this edit ({type(err).__name__}: {err}); secrets are not being caught until it is fixed."
        if agent == "agy":  # any output is a deny for agy: the hint goes out at the next turn instead
            try:
                PENDING.parent.mkdir(parents=True, exist_ok=True)
                with PENDING.open("a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except OSError:
                print(msg, file=sys.stderr)
        else:
            print(json.dumps({"systemMessage": msg}))
