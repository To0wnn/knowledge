#!/usr/bin/env python3
"""PreToolUse hook on the file-edit tools: refuse writing a known secret value into a file, and refuse a session the
companion started (a web UI chat or a worker: KNOWLEDGE_CHAT / KNOWLEDGE_WORKER) a file outside its project folder.

  secret-guard.py [claude|codex|agy]   (default claude)

Compares every word (and the parts of user/pass, KEY=value, url:token) of the new text with SHA-256 hashes of the
known secrets (~/.config/knowledge-server/secret-hashes.json, written by `token.py push-secrets`). On a hit the tool
call is denied with a hint to store the value with `secret set NAME` and write ‹secret:NAME› instead. Without a hash
file it allows everything. Only new text counts, so removing a secret is allowed. Standard library only, a few
milliseconds.
New text per agent: Claude Edit|Write|MultiEdit (content, new_string, edits), Codex apply_patch (the + lines of the
patch), agy (every string in toolCall.args except the old text and the path: CLI write_file Content, edit_file
Replacement; app write_to_file CodeContent, replace_file_content ReplacementContent).
The project folder: the git checkout around the session's folder plus, for a linked worktree, its main checkout; the
agents' own folders (~/.claude, ~/.codex, ~/.gemini, the knowledge home) and the temp folder stay writable too. Outside
those only a file or folder the owner named in this chat or task (KNOWLEDGE_ASKS, written by the companion), never a
drive root, the home folder or above it.
shortcut: only the edit tools are checked; a shell command can still write elsewhere (Codex without YOLO has its sandbox).
"""
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HASHES = Path.home() / ".config/knowledge-server/secret-hashes.json"
PENDING = Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge") / "pending-hints.txt"  # agy: said at the next turn by agent_hooks.py hints
TOKEN = re.compile(r"[^\s`'\"<>()\[\]{},;|*]+")
# agy args that are not new file text: old text, paths, and prose about the edit ("remove the password x")
OLD = {"Target", "TargetContent", "Path", "TargetFile", "AbsolutePath", "Instruction", "Description"}
REASON = ("This text contains a known secret value. Store it with `secret set NAME` (value on stdin) and write "
          "‹secret:NAME› in the file instead.")


def leaks(text: str, hashes: set) -> bool:
    for tok in TOKEN.findall(text):
        candidates = [tok]
        if "=" in tok:
            candidates.append(tok.split("=", 1)[1])
        if ":" in tok:
            candidates.append(tok.split(":", 1)[1])
        candidates.extend(re.split(r"[=:@/&?#]|\\[nrt]", tok))
        for cand in candidates:
            # a sentence may end right after the value: "the key is abc123."
            for p in {cand, cand.strip("'\""), cand.rstrip(".!?"), cand.strip("'\"").rstrip(".!?")}:
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


def deny(agent: str, reason: str = REASON) -> str:
    if agent == "agy":
        return json.dumps({"decision": "deny", "reason": reason})
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}})


PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.M)
AGY_PATHS = ("Path", "TargetFile", "AbsolutePath", "path", "file_path")


def targets(agent: str, hook: dict) -> list:
    """The files an edit writes: Claude's file_path, the files a Codex patch names, agy's path arguments."""
    if agent == "agy":
        args = (hook.get("toolCall") or {}).get("args") or {}
        return [str(args[k]).strip('"') for k in AGY_PATHS if isinstance(args.get(k), str) and args[k].strip('"')]
    inp = hook.get("tool_input") or {}
    if isinstance(inp, str):
        inp = {"command": inp}
    patch = inp.get("command") or inp.get("patch")
    if isinstance(patch, str):
        return [m.strip() for m in PATCH_FILE.findall(patch)]
    return [str(inp[k]) for k in ("file_path", "notebook_path") if isinstance(inp.get(k), str) and inp[k]]


# what an agent itself writes with its edit tools in its own folders (plans, memory, todos, agy's artifacts); never
# their settings, hooks, plugins or skills, nor the knowledge home (the asks file, the companion's state): a session
# that could write those could switch this guard off
AGENT_DIRS = (".claude/plans", ".claude/projects", ".claude/todos", ".claude/tasks", ".codex/memories",
              ".gemini/antigravity-cli/brain", ".gemini/antigravity-cli/scratch", ".gemini/antigravity-cli/knowledge")


def roots(cwd: str, env) -> list:
    """Where a companion-started session may write: its checkout (the main one too, for a linked worktree, except a
    guest's), the agents' own working folders and the temp folder."""
    home = Path.home()
    out = [cwd, *(str(home / d) for d in AGENT_DIRS), tempfile.gettempdir()]
    if env.get("CLAUDE_CONFIG_DIR"):  # another Claude account: its own folder (its projects/ links to ~/.claude's)
        out += [str(Path(env["CLAUDE_CONFIG_DIR"]) / d.split("/", 1)[1]) for d in AGENT_DIRS if d.startswith(".claude/")]
    for d in (Path(cwd), *Path(cwd).parents):
        git = d / ".git"
        if git.is_dir():
            out.append(str(d))
            break
        if git.is_file():  # a linked worktree: "gitdir: <main>/.git/worktrees/<name>"
            out.append(str(d))
            m = re.match(r"gitdir:\s*(.+)", git.read_text(encoding="utf-8", errors="replace").strip())
            parts = Path(m[1]).parts if m else ()
            if ".git" in parts and not env.get("KNOWLEDGE_GUEST"):  # a guest never works in the owner's own checkout
                out.append(str(Path(*parts[:parts.index(".git")])))
            break
    return out


def within(path: str, root: str) -> bool:
    # realpath: a symlink or junction in the project (a cloned repo can carry one) points where it really writes
    p, r = (os.path.normcase(os.path.realpath(x)) for x in (path, root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def asked_for(path: str, asks: str) -> bool:
    """Whether the owner named `path` or a folder holding it (deeper than the home folder, never a root) in `asks`."""
    flat = lambda x: (x.lower() if os.name == "nt" else x).replace("\\", "/").rstrip("/")
    text, home = flat(asks), flat(str(Path.home()))
    p = os.path.realpath(path)
    while True:
        up = os.path.dirname(p)
        f = flat(p)
        if up == p or f == home or home.startswith(f + "/"):  # a root, the home folder or above: too broad to count
            return False
        for name in {f, "~" + f[len(home):]} if f.startswith(home + "/") else {f}:
            # whole: "/opt/app" inside "/mnt/backup/opt/app" names another folder
            if re.search(r"(?<![\w.~/-])" + re.escape(name) + r"(?![\w.-]|/[\w.-])", text, re.I if os.name == "nt" else 0):
                return True
        p = up


def scope(agent: str, hook: dict, env) -> str:
    """The deny JSON when a companion-started session writes outside its project folder, else ''."""
    if not (env.get("KNOWLEDGE_CHAT") or env.get("KNOWLEDGE_WORKER")):
        return ""  # a person's own terminal session: theirs to steer
    cwd = env.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd()
    ok, asks = roots(cwd, env), None
    for t in targets(agent, hook):
        path = os.path.join(cwd, os.path.expanduser(t))  # a relative path is relative to the session's folder
        if any(within(path, r) for r in ok):
            continue
        if asks is None:
            try:
                asks = Path(env["KNOWLEDGE_ASKS"]).read_text(encoding="utf-8", errors="replace") if env.get("KNOWLEDGE_ASKS") else ""
            except OSError:
                asks = ""
        if not asked_for(path, asks):
            return deny(agent, f"{t} is outside this session's project folder ({cwd}). A chat or worker the companion "
                               "started writes outside its project only where the user explicitly named that folder or file "
                               "in this chat or task. Put the file in the project, or ask the user and have them name the full "
                               "folder; do not work around this with a shell command.")
    return ""


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
    for text in f"https://x/api?token={secret}&page=2", f"pw={secret}&x=1", f'"pw": "{secret}\\n"':  # & ? # and a literal \n split the word too
        assert check("claude", {"tool_input": {"content": text}}, hashes), f"a secret in {text!r}"
    b64_secret = "b64sec=="
    slash_secret = "slash/token/sec"
    extra_hashes = {hashlib.sha256(b64_secret.encode()).hexdigest(), hashlib.sha256(slash_secret.encode()).hexdigest()}
    assert leaks(f"KEY={b64_secret}", extra_hashes)
    assert leaks(f"KEY={slash_secret}", extra_hashes)
    assert leaks(f"TOKEN:{b64_secret}", extra_hashes)
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
    with tempfile.TemporaryDirectory() as t:  # a chat in a linked worktree of <main>: both checkouts, nothing else
        main, wt = Path(t) / "main", Path(t) / "main" / ".worktrees" / "c1"
        (main / ".git" / "worktrees" / "c1").mkdir(parents=True)
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'c1'}\n")
        outer = Path(os.path.abspath(os.sep)) / "elsewhere-x" / "a.txt"  # not under the temp folder, which is allowed
        env = {"KNOWLEDGE_CHAT": "c1", "CLAUDE_PROJECT_DIR": str(wt / "sub")}
        for ok in ("a.txt", str(wt / "b.py"), str(main / "c.md"), str(Path.home() / ".claude" / "plans" / "p.md")):
            assert scope("claude", {"tool_input": {"file_path": ok}}, env) == "", f"inside: {ok}"
        for bad in (str(outer), "../" * 40 + "x.txt"):
            assert json.loads(scope("claude", {"tool_input": {"file_path": bad}}, env))["hookSpecificOutput"]["permissionDecision"] == "deny", bad
        assert scope("claude", {"tool_input": {"file_path": str(outer)}}, {}) == "", "a person's own session is not limited"
        for bad in (Path.home() / ".claude" / "settings.json", Path.home() / ".claude" / "plugins" / "x" / "secret-guard.py",
                    Path.home() / ".knowledge" / "asks" / "c1.txt", Path.home() / ".knowledge" / "companion" / "admin_launch.py",
                    Path.home() / ".codex" / "config.toml"):
            assert scope("claude", {"tool_input": {"file_path": str(bad)}}, env), f"the guard's own ground: {bad}"
        assert str(main) in roots(str(wt / "sub"), env) and str(main) not in roots(str(wt / "sub"), {**env, "KNOWLEDGE_GUEST": "1"}), \
            "a guest: not the owner's checkout"
        if os.name != "nt":
            (wt / "out").symlink_to(Path.home())
            assert scope("claude", {"tool_input": {"file_path": str(wt / "out" / ".bashrc")}}, env), "a link out of the project"
        patch = f"*** Begin Patch\n*** Update File: a.py\n*** Move to: {outer}\n*** End Patch"
        assert scope("codex", {"tool_input": {"command": patch}}, env), "codex: a move out of the project"
        assert scope("codex", {"tool_input": {"command": "*** Add File: x/y.py\n+1"}}, env) == ""
        assert json.loads(scope("agy", {"toolCall": {"args": {"TargetFile": str(outer)}}}, env))["decision"] == "deny"
        assert scope("agy", {"toolCall": {"args": {"TargetFile": json.dumps(str(main / "x"))}}}, env) == "", "agy: a JSON-encoded path"
        asks = Path(t) / "asks.txt"
        named = Path(os.path.abspath(os.sep)) / "Games" / "Site"
        asks.write_text(f"zet het in {named} graag\nen {Path.home() / 'Desktop'}\n", encoding="utf-8")
        env = {**env, "KNOWLEDGE_ASKS": str(asks)}
        for ok in (named / "a.html", named / "css" / "b.css", Path.home() / "Desktop" / "x.txt"):
            assert scope("claude", {"tool_input": {"file_path": str(ok)}}, env) == "", f"named by the owner: {ok}"
        for bad in (named.parent / "Other" / "a.html", Path(str(named) + "2") / "a", Path.home() / "Documents" / "x", Path.home() / "x"):
            assert scope("claude", {"tool_input": {"file_path": str(bad)}}, env), f"not named: {bad}"
        asks.write_text(f"alles mag in {Path.home()} en {Path(os.path.abspath(os.sep))}\n", encoding="utf-8")
        assert scope("claude", {"tool_input": {"file_path": str(Path.home() / "Documents" / "x")}}, env), "the home folder is too broad"
        assert scope("claude", {"tool_input": {"file_path": str(named / "a")}}, env), "a drive root is too broad"
        asks.write_text(f"copy it from /mnt/backup{named}\n", encoding="utf-8")
        assert scope("claude", {"tool_input": {"file_path": str(named / "a")}}, env), "a longer path names another folder"
        asks.write_text("put it in ~/Desktop please\n", encoding="utf-8")
        assert scope("claude", {"tool_input": {"file_path": str(Path.home() / "Desktop" / "y.txt")}}, env) == "", "~/ counts"
    print("ok")


def main(agent: str):
    hook = json.load(sys.stdin)
    out = scope(agent, hook, os.environ)
    if not out and HASHES.exists():
        out = check(agent, hook, set(json.loads(HASHES.read_text())))
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
        msg = f"secret-guard could not check this edit ({type(err).__name__}: {err}); secrets and writes outside the project are not being caught until it is fixed."
        if agent == "agy":  # any output is a deny for agy: the hint goes out at the next turn instead
            try:
                PENDING.parent.mkdir(parents=True, exist_ok=True)
                with PENDING.open("a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except OSError:
                print(msg, file=sys.stderr)
        else:
            print(json.dumps({"systemMessage": msg}))
