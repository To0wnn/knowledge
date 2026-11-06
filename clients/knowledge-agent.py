#!/usr/bin/env python3
"""Connect Claude Code, Codex or agy to the knowledge server, and run the knowledge plugin's hooks for them.

Linux, macOS and Windows; standard library only. install asks for the device token (hidden) when none is stored, or
whether to replace the stored one, checks it with the server and saves it in the keyring of the OS (Linux secret-tool,
macOS Keychain, Windows Credential Manager; token_store.py). Without a terminal it uses KNOWLEDGE_TOKEN or the stored
token. The server address: KNOWLEDGE_URL, else the one install asks for (Enter keeps the shown one); install stores
it in ~/.config/knowledge/url for the hooks and sets the Claude plugin option. On the machine that holds the files, TOKEN can be a vault name. The session log falls back to ~/.config/knowledge-server/session.env.

  knowledge-agent.py install                       find Claude Code, Codex and agy on this computer, ask y/n for each,
                    then set up the chosen ones with one token
  knowledge-agent.py setup                         the one-line installer: install the first time; after that update,
                    offer agents found without the plugin, and ask for a token only when the stored one is refused
  knowledge-agent.py update                        update this checkout from GitHub, then the knowledge plugin in every
                    agent that has it, no questions
                    install, setup and update end with the companion (install-companion) unless --plugin-only is
                    given or an earlier choice in ~/.knowledge/install.json says so; in a terminal install and setup
                    ask once. --plugin-only on a pc with the companion removes it (as uninstall-companion)
  knowledge-agent.py import [--dry-run]            import what the agents here already know as memos (setup and install
                    offer it once): ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.gemini/GEMINI.md, Claude Code's local
                    memory (~/.claude/projects/*/memory) and CLAUDE.md/AGENTS.md of folders linked to a project; through
                    the server's knowledge_new_memo (known secrets refused, nothing replaced), once per file
  knowledge-agent.py install <claude|codex|agy> [TOKEN]   set up or update; safe to run again; never the companion
                    (so --plugin-only changes nothing). Claude Code: the plugin.
                    Codex and agy: the plugin (an older setup of this script is removed first); with a vault name,
                    KNOWLEDGE_SETUP=script, or agy on Windows without python3 on PATH: this script's setup (MCP relay,
                    hooks, rules in AGENTS.md/GEMINI.md, skills in ~/.agents/skills)
  knowledge-agent.py header [TOKEN]                 MCP header helper (Codex http_headers_helper): the token as JSON
  knowledge-agent.py context <agy|codex> TOKEN     SessionStart: startup context + docs mirror, as the agent's JSON
  knowledge-agent.py docs-sync TOKEN               PostToolUse: mirror an edited .md (folder with .knowledge-project)
  knowledge-agent.py hints TOKEN                   PostInvocation (agy): show the hint docs-sync left, once, and new tasks
  knowledge-agent.py tasks codex TOKEN             UserPromptSubmit: tasks handed to Codex since the session started
  knowledge-agent.py gate <codex|agy> TOKEN        PreToolUse on the subagent tool: refused from 95% of the usage limit
  knowledge-agent.py guard <codex|agy> TOKEN       PreToolUse on the edit tools: a known secret value is refused
  knowledge-agent.py wake <codex|agy> TOKEN        PostToolUse: wake the other agents here after a knowledge write
  knowledge-agent.py session-log TOKEN             SessionEnd/PreCompact (Codex), Stop (agy): post the session log
  knowledge-agent.py serve                         the local companion (alias: companion): wakes agents for ready tasks,
                    trusts the knowledge hooks, YOLO switches; page on http://127.0.0.1:47321
  knowledge-agent.py install-companion [--dry-run] [--os linux|darwin|windows]
                    start the companion at logon (systemd --user unit, LaunchAgent, or a per-user Scheduled Task that
                    runs pythonw.exe directly), a menu shortcut that opens its page in an app window, a first hook-trust
                    run; setup, install and update do this at the end, unless plugin only was chosen. --dry-run (or
                    KNOWLEDGE_COMPANION_DRY_RUN=1) only lists the steps, for this or another OS
  knowledge-agent.py uninstall-companion [--dry-run] [--os ...]   remove that autostart and shortcut again; both
                    save the choice, so update keeps the companion or leaves it out from then on
"""
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS))
import token_store  # noqa: E402  (KNOWLEDGE_TOKEN or the keyring, shared with the Claude plugin)
import agent_hooks  # noqa: E402  (the hook runtime, shipped in plugin/ for the Codex and agy plugins)
from agent_hooks import token  # noqa: E402
URL = token_store.url()  # KNOWLEDGE_URL, else what an earlier install stored; "" until the setup asks for it
CODEX = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")  # Codex's own override, as Codex reads it
CONFIG = {"agy": Path.home() / ".gemini" / "config" / "hooks.json", "codex": CODEX / "hooks.json"}
RULES = {"agy": Path.home() / ".gemini" / "GEMINI.md", "codex": CODEX / "AGENTS.md"}
BEGIN, END = "<!-- knowledge:house-rules (written by knowledge-agent.py install) -->", "<!-- /knowledge:house-rules -->"
MARKET = os.environ.get("KNOWLEDGE_MARKETPLACE", "To0wnn/knowledge")  # a local checkout in the tests
AGY_PLUGIN = os.environ.get("KNOWLEDGE_AGY_PLUGIN", "https://github.com/To0wnn/knowledge/tree/main/plugin")
KEYRING = "Windows Credential Manager" if os.name == "nt" else "macOS Keychain" if sys.platform == "darwin" else "keyring"
YES = ("y", "yes", "j", "ja")


def helper_line(name: str) -> str:
    """Codex config line: run this script's header command. json.dumps gives a valid TOML basic string (escapes)."""
    exe, me = ("py" if os.name == "nt" and shutil.which("py") else sys.executable), str(Path(__file__).resolve())
    q = lambda s: f'"{s}"' if " " in s else s
    return "http_headers_helper = " + json.dumps(" ".join([q(exe), q(me), "header", name]))


def relay(name: str) -> tuple:
    """(command, args) of the stdio MCP relay in plugin/hooks/agent_hooks.py: the token stays in its memory."""
    exe = "py" if os.name == "nt" and shutil.which("py") else sys.executable
    return exe, ["-X", "utf8", str(HOOKS / "agent_hooks.py"), "mcp", name]


def hook_config(agent: str, name: str) -> dict:
    # Windows: the py launcher (C:\Windows\py.exe, no spaces): a quoted interpreter path would not run in PowerShell.
    exe, me = ("py" if os.name == "nt" and shutil.which("py") else sys.executable), str(Path(__file__).resolve())
    q = lambda s: f'"{s}"' if " " in s else s  # unquoted where possible: PowerShell does not run a quoted path
    # Full paths: Codex runs hooks with a bare PATH. -X utf8: Windows pipes are cp1252 otherwise.
    cmd = lambda *a: " ".join([q(exe), "-X", "utf8", q(me), *a, "-" if a == ("session-log",) else name])
    # agy CLI edits with write_file/edit_file (arg Path); the documented names (TargetFile) are the app's and IDE's.
    if agent == "agy":  # agy has no session end: the log gets one entry per turn (Stop)
        return {"knowledge": {
            "SessionStart": [{"type": "command", "command": cmd("context", "agy"), "timeout": 30}],
            "PostToolUse": [{"matcher": "write_file|edit_file|write_to_file|replace_file_content|multi_replace_file_content",
                             "hooks": [{"type": "command", "command": cmd("docs-sync"), "timeout": 20}]},
                            {"matcher": "call_mcp_tool", "hooks": [{"type": "command", "command": cmd("wake", "agy"), "timeout": 40}]},
                            {"matcher": ".*", "hooks": [{"type": "command", "command": cmd("watch", "agy"), "timeout": 10}]}],
            "PreToolUse": [{"matcher": "invoke_subagent", "hooks": [{"type": "command", "command": cmd("gate", "agy"), "timeout": 10}]},
                           {"matcher": "write_file|edit_file|write_to_file|replace_file_content|multi_replace_file_content",
                            "hooks": [{"type": "command", "command": cmd("guard", "agy"), "timeout": 10}]}],
            "PostInvocation": [{"type": "command", "command": cmd("hints"), "timeout": 10}],  # docs-sync's hint, once
            "Stop": [{"type": "command", "command": cmd("session-log"), "timeout": 15}]}}
    # One SessionStart handler only: with several, Codex drops the context (openai/codex#45999).
    # SessionEnd hooks may take at most 3 seconds.
    return {"hooks": {
        "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [
            {"type": "command", "command": cmd("context", "codex"), "timeout": 30, "additionalContextLimit": 60000}]}],
        "PostToolUse": [{"matcher": "apply_patch", "hooks": [{"type": "command", "command": cmd("docs-sync"), "timeout": 20}]},
                        {"matcher": "mcp__.*knowledge_(edit|write|new_memo)",
                         "hooks": [{"type": "command", "command": cmd("wake", "codex"), "timeout": 40}]},
                        {"matcher": ".*", "hooks": [{"type": "command", "command": cmd("watch", "codex"), "timeout": 10}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd("tasks", "codex"), "timeout": 8}]}],
        "PreToolUse": [{"matcher": ".*spawn_agent", "hooks": [{"type": "command", "command": cmd("gate", "codex"), "timeout": 10}]},
                       {"matcher": "apply_patch", "hooks": [{"type": "command", "command": cmd("guard", "codex"), "timeout": 10}]}],
        "PreCompact": [{"hooks": [{"type": "command", "command": cmd("session-log"), "timeout": 15}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": cmd("session-log"), "timeout": 3}]}]}}


def fetch(path: str, tok: str) -> str:
    req = urllib.request.Request(f"{URL}{path}", headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def check_token(tok: str) -> tuple:
    """(house rules, "") when the server accepts the token, else (None, a plain reason)."""
    print(f"Contacting {URL} ...", flush=True)  # a server that does not answer takes the whole timeout
    try:
        return fetch("/house-rules", tok), ""
    except urllib.error.HTTPError as e:
        if e.code == 401 and os.environ.get("KNOWLEDGE_TOKEN") == tok:  # env wins over the keyring: often a stale export
            return None, ("The server refuses the token in KNOWLEDGE_TOKEN (401): copied wrongly or revoked. If the token "
                          "is in the keyring now, run 'unset KNOWLEDGE_TOKEN' (or open a new terminal) and try again.")
        return None, {401: "The server refuses this token (401): copied wrongly or revoked.",
                      403: f"knowledge server: {token_store.unreachable(URL, 'HTTP 403 Forbidden')}."}.get(e.code, f"Server answered HTTP {e.code}.")
    except OSError as e:
        return None, f"knowledge server: {token_store.unreachable(URL, getattr(e, 'reason', e))}."


def check(tok: str) -> str:
    """The house rules, which also proves the token and the connection; stops with a plain reason otherwise."""
    rules, err = check_token(tok)
    if rules is None:
        sys.exit(err)
    print(f"token ok ({URL})")
    return rules


def ensure_token(name: str) -> str:
    """The token for the setup. A vault name (the VM) is used as is. Otherwise the stored one; in a terminal the setup
    asks for it (hidden) when there is none or the person wants a new one, checks it and saves it in the keyring."""
    if name != "-":
        return token(name)
    env, stored = os.environ.get("KNOWLEDGE_TOKEN", ""), token_store.from_keyring()
    if env:
        print("Using KNOWLEDGE_TOKEN from this shell (it wins over the keyring).")
        return env
    if not sys.stdin.isatty():
        return stored
    if stored and input(f"A token is already stored in the {KEYRING}. Replace it? [y/N] ").strip().lower() not in YES:
        return stored
    import getpass
    for _ in range(3):
        tok = getpass.getpass("Token for this device (paste it; it is not shown): ").strip()
        rules, err = check_token(tok)
        if rules is not None:
            break
        print(err)
    else:
        sys.exit("No working token after three tries: ask for a new one on the admin page.")
    try:
        token_store.store(tok)
        print(f"token saved in the {KEYRING}")
    except OSError as e:
        print(f"Could not save the token in a keyring ({e}). This computer has none (a server without a desktop?): "
              "add 'export KNOWLEDGE_TOKEN=\"<the token>\"' to ~/.profile and log in again; Claude Code there takes it "
              "with 'claude plugin configure knowledge@knowledge'. Restart or stop the Codex daemon after adding the "
              "export: it keeps the environment it started with. This run goes on with the token.")
        os.environ["KNOWLEDGE_TOKEN"] = tok  # the agent CLIs this setup starts
    return tok


def cli(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"{name} is not on PATH: install it first, then run this again.")
    return path


def locked_hint(text: str) -> str:
    """Windows will not replace a plugin folder that a running agent or a terminal has open."""
    if any(x in text.lower() for x in ("used by another process", "access is denied")):
        return ("\nA file is in use: quit the agent (Codex, agy) completely, leave any terminal that is in its plugin "
                "folder (cd ~), and run this again.")
    return ""


def run_cli(argv: list, ok_if: tuple = ()) -> bool:
    """Run an agent CLI step and say what it did; stop loudly on a failure, unless its output holds one of ok_if."""
    print(f"{Path(argv[0]).stem} {' '.join(argv[1:4])}: ...", end=" ", flush=True)  # a step can take minutes (a download, a slow disk)
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = (r.stdout + r.stderr).strip()
    if r.returncode != 0 and not any(x in text.lower() for x in ok_if):
        print("failed")
        sys.exit(f"{Path(argv[0]).stem} {' '.join(argv[1:])} failed:\n{text[-1500:]}{locked_hint(text)}")
    print(text.splitlines()[-1] if text else "ok")
    return r.returncode == 0


def claude_settings() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def install_claude(tok: str):
    claude = cli("claude")
    run_cli([claude, "plugin", "marketplace", "add", MARKET], ok_if=("already",))
    run_cli([claude, "plugin", "marketplace", "update", "knowledge"])
    run_cli([claude, "plugin", "install", "knowledge@knowledge", "--scope", "user"], ok_if=("already",))
    run_cli([claude, "plugin", "update", "knowledge@knowledge"])  # install leaves an installed copy at its old version
    # Always written, also for the default: the plugin's MCP entry reads only its option, and the default will go.
    values = {"url": URL} if URL != token_store.claude_option("url") else {}
    keyring = bool(token_store.from_keyring())
    # No keyring: Linux without a desktop, or Windows over SSH (CredWrite fails with 1312). Then Claude's own option
    # (.credentials.json) is the only place a new session finds the token; with a keyring it stays empty, so no refused
    # old one wins. macOS: Claude keeps that option in the Keychain too, so it cannot help there.
    if not keyring and sys.platform != "darwin" and tok != token_store.claude_option("token"):
        values["token"] = tok
    if values:
        r = subprocess.run([claude, "plugin", "configure", "knowledge@knowledge", "--values-stdin"], input=json.dumps(values),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            sys.exit(f"claude plugin configure failed:\n{(r.stdout + r.stderr).strip()[-1500:]}")
        print(f"claude plugin options set: {', '.join(values)}")
    if keyring:
        print("(a note that options are not set is fine: the plugin takes the token from the keyring)")
    # The memory lives on the server: Claude's local auto memory would build a second one.
    path = claude_settings()
    data = load_json(path)
    changed = False
    if data.get("autoMemoryEnabled") is not False:
        data["autoMemoryEnabled"] = False
        changed = True
        print(f"settings: autoMemoryEnabled false in {path}")
    # Third-party marketplaces do not auto-update by default; this entry is where Claude Code reads it first.
    market = data.setdefault("extraKnownMarketplaces", {}).setdefault("knowledge", {})
    market.setdefault("source", {"source": "github", "repo": MARKET} if "/" in MARKET and not Path(MARKET).exists()
                      else {"source": "directory", "path": MARKET})
    if market.get("autoUpdate") is not True:
        market["autoUpdate"] = True
        changed = True
        print(f"settings: auto-update on for the knowledge marketplace in {path}")
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("done; start a new Claude Code session in your project folder")


TOML_TOKEN = re.compile(r'''"""|\'\'\'|"(?:[^"\\\n]|\\.)*"|'[^'\n]*'|#.*|[\[\]]''')
TOML_CLOSE = {'"""': re.compile(r'(?:[^\\]|\\.)*?"{3,5}', re.S), "'''": re.compile(r"(?s).*?'{3,5}")}


def toml_value_state(line: str, opened: str, depth: int) -> tuple:
    """(open multi-line string, array depth) after a line that is no table header, given the state before it."""
    i = 0
    while True:
        if opened:
            end = TOML_CLOSE[opened].match(line, i)
            if not end:
                return opened, depth
            i, opened = end.end(), ""
        m = TOML_TOKEN.search(line, i)
        if not m:
            return opened, depth
        t, i = m.group(0), m.end()
        if t in TOML_CLOSE:
            opened = t
        elif t in "[]":
            depth += 1 if t == "[" else -1


def toml_table_span(text: str, name: str):
    """(start, end) of table [name] in a TOML text with its subtables ([name.x], [[name.x]]), or None. It ends at the
    next other table header; comment lines above that header belong to that table. A line inside a multi-line string
    or array is a value, also when it looks like a header. Lines end at \\n only, as in TOML (no \\f, no U+2028)."""
    head = re.compile(r"\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(#.*)?")
    pos, start, comments, opened, depth = 0, None, None, "", 0
    for line in re.findall(r".*\n|.+", text):
        m = head.fullmatch(line.rstrip("\r\n")) if not opened and depth <= 0 else None
        in_value = bool(opened or depth > 0)
        if not m:
            opened, depth = toml_value_state(line, opened, depth)
        if start is None:
            if m and not line.lstrip().startswith("[[") and m.group(1) == name:
                start = pos
        elif m:
            if m.group(1) != name and not m.group(1).startswith(name + "."):
                return start, pos if comments is None else comments
            comments = None
        elif not in_value and line.lstrip().startswith("#"):
            comments = pos if comments is None else comments
        elif line.strip():
            comments = None
        pos += len(line)
    return None if start is None else (start, len(text))


def drop_toml_table(text: str, name: str):
    """The text without table [name] and its subtables, or None when it has none."""
    span = toml_table_span(text, name)
    if span:
        rest = (text[:span[0]].rstrip("\r\n") + "\n\n" + text[span[1]:].lstrip("\r\n")).strip("\r\n")
        return rest + "\n" if rest else ""


def remove_old_setup(agent: str):
    """What this script's own setup wrote (MCP server, hooks), so the plugin does not load it all twice."""
    if agent == "codex":
        conf, hooks = CODEX / "config.toml", CODEX / "hooks.json"
        text = conf.read_text(encoding="utf-8") if conf.exists() else ""
        rest = drop_toml_table(text, "mcp_servers.knowledge")
        if rest is not None:
            conf.write_text(rest, encoding="utf-8")
            print(f"removed the old [mcp_servers.knowledge] from {conf}")
        data = load_json(hooks)
        ours = lambda g: any("knowledge-agent.py" in h.get("command", "") or "agent_hooks.py" in h.get("command", "")
                             for h in g.get("hooks", []))
        events = data.get("hooks", {})
        if any(ours(g) for gs in events.values() for g in gs):
            data["hooks"] = {e: [g for g in gs if not ours(g)] for e, gs in events.items() if [g for g in gs if not ours(g)]}
            hooks.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(f"removed the old knowledge hooks from {hooks}")
        return
    for path, key in ((Path.home() / ".gemini" / "config" / "mcp_config.json", "mcpServers"),
                      (Path.home() / ".gemini" / "config" / "hooks.json", None)):
        data = load_json(path)
        holder = data.get(key, {}) if key else data
        if "knowledge" in holder:
            del holder["knowledge"]
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            print(f"removed the old knowledge entry from {path}")


CODEX_TRUST = ("the knowledge companion trusts the Codex hooks for you (install-companion; again after each plugin "
               "update); without the companion, open codex, type /hooks once and pick \"Trust all\" (the third choice "
               "turns the hooks off, and with them the shared memory)")
TRY_IT = ('Try it: tell your agent "save to knowledge: we deploy with ./deploy.sh", then start another agent, on this pc or '
          'another, and ask "how do we deploy?". It answers from the shared memory.')


def install_plugin(agent: str):
    remove_old_setup(agent)
    if agent == "codex":
        codex = cli("codex")
        run_cli([codex, "plugin", "marketplace", "add", MARKET], ok_if=("already",))
        run_cli([codex, "plugin", "marketplace", "upgrade", "knowledge"], ok_if=("not configured as a git",))  # a local checkout
        run_cli([codex, "plugin", "add", "knowledge@knowledge"])
        restart_codex()
        return print(f"done; {CODEX_TRUST}")
    run_cli([cli("agy"), "plugin", "install", AGY_PLUGIN])
    if not has_plugin("agy"):  # a failed copy can leave agy without the plugin at all
        sys.exit("agy plugin list does not show the knowledge plugin after the install: quit agy completely and run this again.")
    say_restart("agy")
    print("done; start agy and ask it to search the knowledge")


def has_python3() -> bool:
    """A working python3 on PATH, found the way agy (Go) finds it: PATH only (Windows itself would first look next to
    the running Python). The Store's python3 alias without Python installed fails the run."""
    exe = shutil.which("python3")
    try:
        return bool(exe) and subprocess.run([exe, "-c", "pass"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


AGENTS = ("claude", "codex", "agy")


def has_plugin(agent: str) -> bool:
    """The knowledge plugin is installed in this agent (as its own plugin list says)."""
    r = subprocess.run([shutil.which(agent), "plugin", "list"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    lines = (r.stdout + r.stderr).splitlines()
    if agent == "agy":
        return any(ln.strip().startswith("- knowledge (") for ln in lines)
    return any("knowledge@knowledge" in ln and (agent == "claude" or "installed" in ln) and "not installed" not in ln
               for ln in lines)


def choose(found: list, answer) -> list:
    """The agents the person says yes to; Enter means yes."""
    return [a for a in found if answer(f"Set up the knowledge plugin for {a}? [Y/n] ").strip().lower() in ("", *YES)]


def url_known() -> bool:
    return not token_store.url_is_default()


def ensure_url():
    """The server for a setup. A known one (KNOWLEDGE_URL, or stored by an earlier install) is offered with Enter to
    keep it; otherwise the person must type it: never a silent default, which would send their token to another server."""
    global URL
    known = url_known()
    if not sys.stdin.isatty():
        if not known:
            sys.exit("Which knowledge server? Set KNOWLEDGE_URL (e.g. https://knowledge.example.com) or run this in a terminal.")
        return
    for _ in range(3):
        typed = input(f"Knowledge server [{URL}]: " if known else
                      "Knowledge server address (e.g. https://knowledge.example.com): ").strip().rstrip("/")
        if typed.startswith(("http://", "https://")) or (known and not typed):
            URL = typed or URL
            return
        print("Type the address with http:// or https://.")
    sys.exit("No server address.")


def install_found():
    found = [a for a in AGENTS if shutil.which(a)]
    if not found:
        sys.exit("None of claude, codex or agy is on PATH: install one first, then run this again.")
    if not sys.stdin.isatty():
        sys.exit(f"Found {', '.join(found)}. Without a terminal, name one: install <claude|codex|agy>.")
    print(f"found: {', '.join(found)}")
    ensure_url()
    chosen = choose(found, input)
    if not chosen:
        sys.exit("nothing chosen")
    tok = ensure_token("-")  # asked once for all of them
    for agent in chosen:
        print(f"\n== {agent} ==")
        install(agent, "-", tok)


def setup():
    """The one-line installer (install.sh, install.ps1), the same line every time: the first time install; once set up,
    update the agents that have the plugin, offer the ones found without it (y/n), and ask for a token again only when
    the server refuses the stored one. The scripts already brought this checkout up to date."""
    tok = token_store.token()
    if tok:
        print("Looking for the agents on this computer ...", flush=True)
    have = [a for a in AGENTS if shutil.which(a) and has_plugin(a)] if tok else []
    if not have:
        return install_found()
    if not url_known():  # set up before the URL was stored: ask, never send the token to the default
        ensure_url()
    rules, err = check_token(tok)
    if rules is None and "(401)" in err:
        print(f"{err} Setting up again with a new token (answer y if asked to replace the stored one).")
        return install_found()
    print(f"Already set up ({', '.join(have)}): updating.")
    new = [a for a in AGENTS if shutil.which(a) and a not in have]
    add = choose(new, input) if new and sys.stdin.isatty() else []
    for agent in have + add:
        print(f"\n== {agent} ==")
        install(agent, "-", tok)


def update(argv: list = ()):
    """Every agent here that has the plugin gets the newest version; the stored token, no questions. A git checkout of
    this script is first brought to GitHub's main (self_update.sync), then the new script does the rest."""
    repo = HOOKS.parent.parent
    if (repo / ".git").exists() and not os.environ.get("KNOWLEDGE_SYNCED"):
        import self_update
        manifest = HOOKS.parent / ".claude-plugin" / "plugin.json"
        old = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        try:
            self_update.sync(repo)
        except RuntimeError as e:
            sys.exit(str(e))
        new = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        print(f"{repo}: " + (f"updated version {old} -> {new}" if new != old else f"up to date (version {new})"))
        sys.exit(subprocess.run([sys.executable, "-X", "utf8", __file__, "update", *argv],
                                env={**os.environ, "KNOWLEDGE_SYNCED": "1"}).returncode)
    if not token_store.token():
        sys.exit("No stored token: run install first.")
    todo = [a for a in AGENTS if shutil.which(a) and has_plugin(a)]
    if not todo:
        sys.exit("No agent here has the knowledge plugin: run install first.")
    if not url_known():
        ensure_url()
    for agent in todo:
        print(f"\n== {agent} ==")
        install(agent, "-", token_store.token())


def drop_agy_plugin():
    """The script setup replaces the plugin: left in place, an old version keeps running and every hook fires twice."""
    if shutil.which("agy") and has_plugin("agy"):
        run_cli([cli("agy"), "plugin", "uninstall", "knowledge"])


def install(agent: str, name: str, tok: str = ""):
    """Set up or update; every step says what it did. Fails loudly: a wrong token or no connection stops here."""
    tok = tok or ensure_token(name)
    if not tok:
        sys.exit("No token: run this in a terminal so it can ask for it, or set KNOWLEDGE_TOKEN.")
    rules = check(tok)
    token_store.store_url(URL)  # the hooks and the MCP relay read it; this address just answered
    if agent == "claude":
        return install_claude(tok)
    if name == "-" and os.environ.get("KNOWLEDGE_SETUP") != "script":
        if agent != "agy" or os.name != "nt" or has_python3():
            return install_plugin(agent)
        print("agy starts the plugin's MCP server with python3, which is not on PATH here (Python from the python.org "
              "install manager or the Microsoft Store has it): using the script setup")
        drop_agy_plugin()
    if install_mcp(agent, name):
        restart_codex() if agent == "codex" else say_restart("agy")
    install_hooks(agent, name)
    install_rules(agent, rules)
    install_skills()
    print("done" + (f"; {CODEX_TRUST}" if agent == "codex" else "; start agy and ask it to search the knowledge"))


def codex_block(name: str) -> str:
    """[mcp_servers.knowledge] without a token: the header helper on Linux/macOS, the stdio relay on Windows (a
    helper command line there is split in ways we could not test). json.dumps gives valid TOML strings and arrays."""
    if os.name != "nt":
        how = f'url = "{URL}/mcp"\n{helper_line(name)}'
    else:
        exe, args = relay(name)
        how = f'command = {json.dumps(exe)}\nargs = {json.dumps(args)}\nenv_vars = ["KNOWLEDGE_TOKEN", "KNOWLEDGE_URL"]'
    return f'[mcp_servers.knowledge]\n{how}\ndefault_tools_approval_mode = "writes"\nstartup_timeout_sec = 15\n'


def load_json(path: Path) -> dict:
    """A missing or empty config counts as {}: agy creates an empty mcp_config.json on Windows."""
    text = path.read_text(encoding="utf-8-sig").strip() if path.exists() else ""
    return json.loads(text) if text else {}


def install_mcp(agent: str, name: str = "-"):
    if agent == "codex":  # no token is written to a file: the header helper or the stdio relay fetches it
        path = CODEX / "config.toml"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        old = 'bearer_token_env_var = "KNOWLEDGE_TOKEN"'
        span = toml_table_span(text, "mcp_servers.knowledge")
        if span:
            start, end = span
            block = text[start:end]
            if old in block:  # older setup that read KNOWLEDGE_TOKEN: replace this server's block only
                path.write_text(text[:start] + codex_block(name) + text[end:], encoding="utf-8")
                print(f"MCP: {path} now gets the token without KNOWLEDGE_TOKEN in a config")
                return True
            print(f"MCP: {path} already has [mcp_servers.knowledge], left as it is")
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.rstrip("\n") + ("\n\n" if text.strip() else "") + codex_block(name), encoding="utf-8")
        print(f"MCP: added the knowledge server to {path} (Codex asks before it writes)")
        return True
    # agy sends a ${VAR} header as is, so the stdio relay fetches the token; an older entry held it in plain text.
    path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    data = load_json(path)
    had = data.get("mcpServers", {}).get("knowledge")
    if had and "headers" not in had:
        print(f"MCP: {path} already has 'knowledge', left as it is")
        return False
    exe, args = relay(name)
    data.setdefault("mcpServers", {})["knowledge"] = {"command": exe, "args": args}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"MCP: {path} " + ("no longer holds the token (it runs the relay)" if had else "runs the knowledge server's relay"))
    return True


def say_restart(agent: str):
    """A running agy session keeps its old MCP settings. It is the person's own work, so only say so, never stop it."""
    try:
        running = subprocess.run(["pgrep", "-x", agent], capture_output=True).returncode == 0
    except OSError:  # shortcut: no pgrep (Windows): nothing said
        running = False
    if running:
        print(f"{agent} is running with the old settings: restart it so the knowledge server is used")


def same_codex_home(environ: bytes, mine: Path) -> bool:
    """Whether a process with this /proc environ uses this CODEX_HOME: another home holds other settings."""
    env = dict(v.split(b"=", 1) for v in environ.split(b"\0") if b"=" in v)
    home = env.get(b"CODEX_HOME") or (env.get(b"HOME", b"") + b"/.codex")
    return os.path.normpath(os.fsdecode(home)) == os.path.normpath(str(mine))


def codex_servers() -> list:
    """This user's `codex app-server` processes (the program, not a shell whose command line names it). On Linux only
    those of this CODEX_HOME. shortcut: macOS cannot read another process's environment simply, so there every
    CODEX_HOME of this user counts; the question says how many it stops."""
    try:
        pids = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", r"(^|/)codex(\.js)? app-server"], capture_output=True, text=True).stdout.split()
    except (OSError, AttributeError):  # no pgrep, or no getuid: Windows
        return []
    if not sys.platform.startswith("linux"):
        return pids
    keep = []
    for pid in pids:
        try:
            if same_codex_home(Path(f"/proc/{pid}/environ").read_bytes(), CODEX):
                keep.append(pid)
        except OSError:  # gone meanwhile
            pass
    return keep


def restart_codex():
    """Codex keeps a background app-server that holds the MCP settings it started with, so a changed config only
    counts after it restarts. Offer to stop it (Codex starts a new one on its next run); never without asking."""
    pids = codex_servers()
    if not pids:
        return
    msg = "Codex is still running with the old settings."
    if not sys.stdin.isatty():
        return print(f"{msg} Quit Codex (and its background server) so the new settings apply.")
    what = (f"{len(pids)} Codex background server(s) of this user ({CODEX}), pid {', '.join(pids)}; any Codex session or "
            "web UI chat on them stops too")
    if input(f"{msg} Stop {what}? [y/N] ").strip().lower() not in ("y", "yes", "j", "ja"):
        return print("left running: quit Codex yourself so the new settings apply")
    for pid in pids:
        try:
            os.kill(int(pid), 15)
        except (OSError, ValueError):
            pass
    print("stopped; Codex starts it again on its next run")


def install_rules(agent: str, rules: str):
    """The always-loaded rules file: a marked block, so a rerun updates it and the rest of the file stays yours."""
    path = RULES[agent]
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    if text.startswith("<!-- Generated by knowledge-watch"):
        return print(f"rules: {path} is generated on this machine (it already holds the house rules)")
    block = f"{BEGIN}\n{rules.strip()}\n{END}"
    if BEGIN in text and END in text:
        new = text[:text.index(BEGIN)] + block + text[text.index(END) + len(END):]
    else:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    print(f"rules: house rules in {path}")


def install_skills():
    """The shared development skills (plugin/skills) into ~/.agents/skills, the folder Codex and agy read; ours are
    replaced on a rerun, other skills there stay."""
    dest = Path.home() / ".agents" / "skills"
    names = []
    for src in sorted((HOOKS.parent / "skills").glob("*/SKILL.md")):
        target = dest / src.parent.name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
        names.append(src.parent.name)
    print(f"skills: {', '.join(names)} in {dest}" if names else "skills: none found next to this script")


def install_hooks(agent: str, name: str):
    path, new = CONFIG[agent], json.dumps(hook_config(agent, name), indent=2) + "\n"
    bak = path.with_suffix(".json.bak")
    if path.exists() and path.read_text(encoding="utf-8-sig") != new and not bak.exists():  # the first original stays
        bak.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        print(f"kept the old file as {bak}; merge your own hooks from it if it had any")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    print(f"hooks: context at start, session log and docs mirror in {path}")


# ---------------------------------------------------------------- import what the agents here already know

IMPORTED = token_store.KN_HOME / "imported.json"  # what an import did, per file: a rerun imports nothing twice
FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
TYPES = ("project", "reference", "feedback", "user")
# Secrets the server cannot know (not in the vault): never uploaded. Values must look random, so prose passes.
LIKELY_SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|"
                           r"github_pat_\w{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})")
KEY_VALUE = re.compile(r"(?i)(?:api[-_]?key|key|token|secret|passw(?:or)?d|pwd)[\"']?\s*[:=]\s*[\"']?([^\s\"'`<>‹›]{16,})")


def high_entropy(value: str) -> bool:
    """Random-looking: at least 3 bits per character and letters mixed with digits (no word or path is)."""
    import math
    n = len(value)
    bits = -sum(c / n * math.log2(c / n) for c in (value.count(ch) for ch in set(value)))
    return bits >= 3.0 and bool(re.search(r"\d", value)) and bool(re.search(r"[A-Za-z]", value)) and "/" not in value


def likely_secret(text: str) -> bool:
    return bool(LIKELY_SECRET.search(text)) or any(high_entropy(v) for v in KEY_VALUE.findall(text))


def claude_folder(encoded: str, folders: set) -> str:
    """The working folder of ~/.claude/projects/<encoded>: Claude Code names it after the folder's path with every
    character other than a letter or digit as '-'. That is not reversible, so it is matched against the known folders."""
    return next((f for f in sorted(folders) if re.sub(r"[^A-Za-z0-9]", "-", f) == encoded), "")


def import_sources() -> tuple:
    """([(file, title, project)], [skip reasons]) of what the agents here already know, from these known places only:
    the user-level instruction files, Claude Code's local auto memory and CLAUDE.md/AGENTS.md at the root of folders
    linked to a project. project "" is the token's own; None: no project known. A file whose real path is outside its
    expected folder (a symlink to elsewhere) is never read."""
    import companion, project_marker  # the folders the agents worked in, and their .knowledge-project links
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    cands = [(claude / "CLAUDE.md", claude, "Claude Code user instructions", ""), (CODEX / "AGENTS.md", CODEX, "Codex user instructions", ""),
             (RULES["agy"], RULES["agy"].parent, "agy user instructions", "")]  # agy (Antigravity) reads ~/.gemini/GEMINI.md
    try:
        folders, linked = companion.known_folders(), companion.project_folders()
    except Exception as e:  # an unreadable agent config must not stop the rest
        print(f"import: could not list the known folders ({e})")
        folders, linked = set(), {}
    home = Path.home().resolve()
    for mem in sorted(claude.glob("projects/*/memory")):
        folder = claude_folder(mem.parent.name, folders)
        root, name = project_marker.project_root(Path(folder)) if folder else (None, None)
        project = name if root and name and root.resolve() != home else None
        cands += [(f, mem, "", project) for f in sorted(mem.glob("*.md")) if f.name != "MEMORY.md"]  # the index: the server writes its own
    for project, folder in sorted(linked.items()):
        cands += [(Path(folder) / n, Path(folder), f"{Path(folder).name} {n}", project) for n in ("CLAUDE.md", "AGENTS.md")]
    out, skipped = [], []
    for f, base, title, project in cands:
        if not (f.is_file() or f.is_symlink()):
            continue
        if not f.resolve().is_relative_to(base.resolve()):
            skipped.append(f"{f}: a link to a file elsewhere, not read")
        elif f.is_file():
            out.append((f, title, project))
    return out, skipped


def import_memo(path: Path, title: str, project: str) -> dict:
    """The knowledge_new_memo arguments for one file, or {} when nothing of it is worth importing."""
    text = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    if text.startswith("<!-- Generated by knowledge-watch"):
        return {}  # the house rules this server wrote there
    if BEGIN in text and END in text:  # the house-rules block install_rules wrote: the server's own text
        text = text[:text.index(BEGIN)] + text[text.index(END) + len(END):]
    meta = {}
    if m := FRONT.match(text):
        meta = dict(re.findall(r"^[ \t]*(\w+):[ \t]*(.+?)[ \t]*$", m.group(1), re.M))
        text = text[m.end():]
    if not text.strip():
        return {}
    kind = meta.get("type", "").strip("'\"") if meta.get("type", "").strip("'\"") in TYPES else "reference"
    name = re.sub(rf"^{kind}_", "", meta.get("name", "").strip("'\"") or path.stem)
    shown = f"~/{path.relative_to(Path.home()).as_posix()}" if path.is_relative_to(Path.home()) else str(path)
    # a memo that already has both sections keeps them; any other heading drops one level under ## Status
    parts = re.split(r"^## (?:Decisions|Keuzes)\s*$", re.sub(r"^## (?:Status|Stand)\s*$", "", text, count=1, flags=re.M), maxsplit=1, flags=re.M)
    status, decisions = (parts + [""])[:2]
    demote = lambda s: re.sub(r"^(#{1,5}) ", r"#\1 ", s.strip(), flags=re.M)
    name = name.replace("_", " ")
    return {"title": title or name[:1].upper() + name[1:], "type": kind, "heading": "Imported", **({"project": project} if project else {}),
            "description": " ".join(meta.get("description", "").strip("'\"").split()) or f"Imported from {shown}",
            "status": f"Imported from `{shown}` (knowledge-agent.py import).\n\n{demote(status)}", "decisions": demote(decisions)}


def call_tool(tok: str, name: str, args: dict) -> tuple:
    """(ok, text) of one MCP tool call; the server is stateless, so one POST is the whole call."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}).encode()
    req = urllib.request.Request(f"{URL}/mcp", body, {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                                      **token_store.headers(tok)})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode("utf-8")
    reply = json.loads(([ln[5:] for ln in raw.splitlines() if ln.startswith("data:")] or [raw])[-1])
    if "error" in reply:
        return False, reply["error"].get("message", "")
    res = reply["result"]
    return not res.get("isError"), "".join(c.get("text", "") for c in res.get("content", []))


def load_record() -> dict:
    try:
        return json.loads(IMPORTED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_record(rec: dict):
    IMPORTED.parent.mkdir(parents=True, exist_ok=True)
    IMPORTED.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")


def server_view(tok: str) -> tuple:
    """({project: memory root} this token may write, True when this device holds the server's own files): the server's
    MEMORY.md or CLAUDE.md is byte for byte a local one, as on the machine the server reads its files from."""
    ok, text = call_tool(tok, "knowledge_projects", {})
    if not ok:
        sys.exit(f"import: the server did not list this token's projects: {text[:300]}")
    rows = re.findall(r"^(\S+): docs \S+/, memory (\S+)/, (write|read)$", text, re.M)
    writable = {p: m for p, m, w in rows if w == "write"}
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    local = {hashlib.sha256(f.read_bytes()).hexdigest()[:16] for f in [claude / "CLAUDE.md", *claude.glob("projects/*/memory/MEMORY.md")]
             if f.is_file()}
    for rel in ["claude/CLAUDE.md", *(f"{m}/MEMORY.md" for _, m, _ in rows)]:
        ok, text = call_tool(tok, "knowledge_read", {"path": rel, "limit": 1})
        if ok and (v := re.search(r"\(version (\w+),", text)) and v.group(1) in local:
            return writable, True
    return writable, False


def import_todo(tok: str) -> list:
    """(file, memo arguments) not imported before, numbered as a dry-run list (file, size, target); says what is
    skipped and why. Nothing at all on the device that holds the server's own files."""
    writable, source = server_view(tok)
    if source:
        print("import: this device holds the server's own files (its memory or CLAUDE.md is the server's copy): nothing to import")
        return []
    done = load_record().get("files", {})
    sources, skipped = import_sources()
    todo = []
    for p, title, project in sources:
        if str(p) in done:
            continue
        if project is None:  # Claude memory of a folder not linked: only a one-project token says where it belongs
            if len(writable) != 1:
                skipped.append(f"{p}: its folder is not linked to a project: link the folder first (skill knowledge-init)")
                continue
            project = next(iter(writable))
        if project and project not in writable:
            skipped.append(f"{p}: this token may not write project {project}")
            continue
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            skipped.append(f"{p}: unreadable ({e})")
            continue
        if likely_secret(text):
            skipped.append(f"{p}: looks like it holds a secret (a key, token or password): not uploaded; put it in your vault, "
                           "write ‹secret:NAME› instead")
        elif a := import_memo(p, title, project):
            todo.append((p, a))
    for i, (p, a) in enumerate(todo, 1):
        print(f"  {i}. {p}  ({p.stat().st_size} bytes) -> {a['type']} memo \"{a['title']}\" in "
              + (f"project {a['project']}" if "project" in a else "this token's project"))
    for s in skipped:
        print(f"  skipped: {s}")
    print(f"import: {len(todo)} file(s) to import" + (f", {len(done)} imported before" if done else ""))
    return todo


def pick(todo: list, answer: str) -> list:
    """The files chosen: 'all', 'none' (or Enter), or numbers such as '1,3,5'."""
    a = answer.strip().lower()
    if a in ("all", "a", *YES):
        return todo
    nums = {int(n) for n in re.findall(r"\d+", a)}
    return [t for i, t in enumerate(todo, 1) if i in nums]


def run_import(tok: str, todo: list) -> int:
    """Each file through the server's own knowledge_new_memo: it refuses a known secret, writes frontmatter, sections
    and index line, never replaces a memo, and names similar ones. Returns the number of files that failed."""
    rec, failed = load_record(), 0
    files = rec.setdefault("files", {})
    for p, args in todo:
        try:
            ok, text = call_tool(tok, "knowledge_new_memo", args)
        except (OSError, ValueError, KeyError) as e:
            ok, text = False, f"server: {token_store.unreachable(URL, getattr(e, 'reason', e))}"
        if ok or " exists: " in text:  # an existing memo is never replaced: leave it and do not try again
            files[str(p)] = text.splitlines()[0] if text else "imported"
            print(f"import: {p}: {'imported' if ok else 'a memo with this name exists, left as it is'}"
                  + "".join(f"\n    {ln}" for ln in text.splitlines()[1:] if "Similar existing memos" in ln))
        elif "known secret" in text:  # never imported; said again on a rerun until the file holds ‹secret:NAME› instead
            print(f"import: {p}: SKIPPED, it contains a known secret value (replace it with ‹secret:NAME›, then run import again)")
        else:
            failed += 1
            print(f"import: {p}: FAILED: {text[:500]}")
        save_record(rec)  # after each file: a stop halfway never imports a file twice
    return failed


CHOOSE = "Import which? all, none, or numbers like 1,3,5 [none]: "


def import_cmd(argv: list):
    """knowledge-agent.py import [--dry-run]: in a terminal the person picks the files; without one, all of them."""
    tok = token_store.token()
    if not tok or not url_known():
        sys.exit("No token or server address stored: run install first.")
    todo = import_todo(tok)
    if "--dry-run" in argv or not todo:
        return
    if sys.stdin.isatty() and not (todo := pick(todo, input(CHOOSE))):
        return print("nothing imported")
    if failed := run_import(tok, todo):
        sys.exit(f"import: {failed} file(s) failed (see above); run knowledge-agent.py import again")


def offer_import():
    """After setup/install: offer the import once (no terminal: no). The answer is kept, so a later setup does not ask
    again; knowledge-agent.py import does it later."""
    rec = load_record()
    if rec.get("asked") or not sys.stdin.isatty() or not (tok := token_store.token()):
        return
    print("\n== import ==\nWhat your agents here already know (instruction files, Claude Code's local memory, linked folders):")
    todo = import_todo(tok)
    if not todo:
        return
    rec["asked"] = True
    save_record(rec)
    chosen = pick(todo, input("A file with a known secret is refused, an existing memo never replaced. " + CHOOSE))
    if not chosen:
        return print("not imported; later: knowledge-agent.py import (--dry-run lists first)")
    if failed := run_import(tok, chosen):
        print(f"import: {failed} file(s) failed; run knowledge-agent.py import again")


COMPANION_URL = "http://127.0.0.1:47321"
COMPANION_NAME = "Knowledge companion"
UNIT = "knowledge-companion.service"
AGENT_LABEL = "io.github.knowledge.companion"


def companion_paths(system: str) -> dict:
    home = Path.home()
    # the page that hands the companion page its key: a private file, so the key is never on a browser's command line
    opener = {"opener": Path(os.environ.get("KNOWLEDGE_HOME") or home / ".knowledge") / "companion" / "open.html"}
    if system == "windows":
        start = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        return {"task_xml": home / ".knowledge" / "companion" / "autostart.xml",
                "lnk": start / f"{COMPANION_NAME}.lnk", "url": start / f"{COMPANION_NAME}.url", **opener}
    if system == "darwin":
        app = home / "Applications" / f"{COMPANION_NAME}.app" / "Contents"
        return {"plist": home / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist", "app": app.parent,
                "app_plist": app / "Info.plist", "app_exe": app / "MacOS" / "knowledge-companion", **opener}
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    conf = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return {"unit": conf / "systemd" / "user" / UNIT, "desktop": data / "applications" / "knowledge-companion.desktop", **opener}


def windows_account() -> str:
    """DOMAIN\\user of this logon as Windows itself names it. %USERDOMAIN% is no source: an OpenSSH session on a
    workgroup pc says WORKGROUP, an account schtasks cannot map ("No mapping between account names and security IDs")."""
    if os.name == "nt":
        import ctypes
        size = ctypes.c_ulong(512)
        buf = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.secur32.GetUserNameExW(2, buf, ctypes.byref(size)):  # 2 = NameSamCompatible
            return buf.value
    return getpass.getuser()


def app_browser(system: str) -> str:
    """Edge or Chrome, whose --app= opens the page in a window of its own; '' means the default browser."""
    if system == "windows":
        roots = [os.environ.get(v) for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        cands = [Path(r) / sub for r in roots if r for sub in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe")]
        return next((str(c) for c in cands if c.is_file()), "")
    if system == "darwin":
        return next((n for n in ("Microsoft Edge", "Google Chrome") if Path(f"/Applications/{n}.app").is_dir()), "")
    return next((shutil.which(n) for n in ("microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
                                           "chromium", "chromium-browser") if shutil.which(n)), "")


def companion_steps(system: str, key: str = "<key>") -> list:
    """What install-companion does, in order: ("write", path, text[, mode]) or ("run", argv, may_fail). The shortcut
    opens a private file that forwards to the page with the local API's key: a key on the browser's command line
    could be read by every other account on the pc (/proc, ps), and the companion's port is open to them."""
    from xml.sax.saxutils import escape
    me, p, browser = str(Path(__file__).resolve()), companion_paths(system), app_browser(system)
    target = escape(f"{COMPANION_URL}/#key={key}", {'"': "&quot;"})
    forward = ("<!doctype html>\n<meta charset=\"utf-8\">\n<title>Knowledge companion</title>\n"
               f"<meta http-equiv=\"refresh\" content=\"0;url={target}\">\n<a href=\"{target}\">Open the knowledge companion</a>\n")
    page = p["opener"].as_uri()
    first = [("write", p["opener"], forward, 0o600)]
    if system == "windows":
        # pythonw.exe runs without a console; started directly, never through (hidden or encoded) PowerShell,
        # which corporate EDRs flag.
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw) if os.name != "nt" or pyw.is_file() else sys.executable
        user = windows_account()
        xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{COMPANION_NAME} ({COMPANION_URL})</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(f'"{me}" serve')}</Arguments>
    <WorkingDirectory>{escape(str(Path.home()))}</WorkingDirectory></Exec></Actions>
</Task>
"""
        steps = first + [("write", p["task_xml"], xml),
                 ("run", ["schtasks", "/Create", "/F", "/TN", COMPANION_NAME, "/XML", str(p["task_xml"])], False),
                 ("run", ["schtasks", "/End", "/TN", COMPANION_NAME], True),  # an update: the running one has old code
                 ("run", ["schtasks", "/Run", "/TN", COMPANION_NAME], False)]
        if browser:  # a .lnk needs the shell's COM object: a plain, visible PowerShell line, values passed as env vars
            steps.append(("run", ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:KN_LNK); $s.TargetPath = $env:KN_TARGET; "
                                  "$s.Arguments = $env:KN_ARGS; $s.Description = $env:KN_DESC; $s.Save()"], False,
                          {"KN_LNK": str(p["lnk"]), "KN_TARGET": browser, "KN_ARGS": f"--app={page}", "KN_DESC": COMPANION_NAME}))
            steps.append(("remove", p["url"]))
        else:
            steps += [("write", p["url"], f"[InternetShortcut]\nURL={page}\n"), ("remove", p["lnk"])]
    elif system == "darwin":
        # systemd and launchd start with a bare PATH: the setup's PATH is passed so the companion finds claude, codex and agy.
        uid = getattr(os, "getuid", lambda: 501)()
        arg = lambda s: f"<string>{escape(s)}</string>"
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{AGENT_LABEL}</string>
  <key>ProgramArguments</key><array>{arg(sys.executable)}{arg(me)}<string>serve</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key>{arg(os.environ.get("PATH", ""))}</dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardErrorPath</key>{arg(str(Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge") / "companion" / "serve.log"))}
</dict></plist>
"""
        opener = f'exec open -na "{browser}" --args "--app={page}"' if browser else f'exec open "{page}"'
        info = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>{COMPANION_NAME}</string>
  <key>CFBundleIdentifier</key><string>{AGENT_LABEL}.page</string>
  <key>CFBundleExecutable</key><string>knowledge-companion</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
"""
        steps = first + [("write", p["plist"], plist),
                 ("run", ["launchctl", "bootout", f"gui/{uid}", str(p["plist"])], True),
                 ("run", ["launchctl", "bootstrap", f"gui/{uid}", str(p["plist"])], False),
                 ("write", p["app_plist"], info), ("write", p["app_exe"], f"#!/bin/sh\n{opener}\n", 0o700)]
    else:
        q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'  # systemd and .desktop quoting
        unit = f"""[Unit]
Description={COMPANION_NAME} ({COMPANION_URL})
After=network-online.target

[Service]
Environment={q("PATH=" + os.environ.get("PATH", ""))}
ExecStart={q(sys.executable)} {q(me)} serve
Restart=on-failure
RestartSec=10
# only the companion itself: the tmux servers and codex exec runs it starts (where you work) live on
KillMode=process

[Install]
WantedBy=default.target
"""
        desktop = f"""[Desktop Entry]
Type=Application
Name={COMPANION_NAME}
Comment=Wakes your agents for ready knowledge tasks
Exec={q(browser) + " " + q(f"--app={page}") if browser else "xdg-open " + q(page)}
Icon=applications-internet
Terminal=false
Categories=Network;Development;
"""
        steps = first + [("write", p["unit"], unit),
                 ("run", ["systemctl", "--user", "daemon-reload"], False),
                 ("run", ["systemctl", "--user", "enable", UNIT], False),
                 ("run", ["systemctl", "--user", "restart", UNIT], False),  # starts it, or loads new code after an update
                 ("write", p["desktop"], desktop, 0o600)]
    return steps + [("run", [sys.executable, str(HOOKS / "companion.py"), "trust"], False)]


def companion_undo_steps(system: str) -> list:
    """YOLO off first: once the companion is gone nothing could switch the agents' settings back."""
    p, off = companion_paths(system), [("run", [sys.executable, str(HOOKS / "companion.py"), "yolo-off"], False)]
    if system == "windows":
        return off + [("run", ["schtasks", "/End", "/TN", COMPANION_NAME], True),
                ("run", ["schtasks", "/Delete", "/F", "/TN", COMPANION_NAME], True),
                ("remove", p["task_xml"]), ("remove", p["lnk"]), ("remove", p["url"]), ("remove", p["opener"])]
    if system == "darwin":
        uid = getattr(os, "getuid", lambda: 501)()
        return off + [("run", ["launchctl", "bootout", f"gui/{uid}", str(p["plist"])], True), ("remove", p["plist"]), ("remove", p["app"]),
                      ("remove", p["opener"])]
    return off + [("run", ["systemctl", "--user", "disable", "--now", UNIT], True), ("remove", p["unit"]),
            ("run", ["systemctl", "--user", "daemon-reload"], True), ("remove", p["desktop"]), ("remove", p["opener"])]


def run_steps(steps: list, dry: bool) -> list:
    """Every step says what it did; returns the steps that failed (a may-fail step never does)."""
    failed = []
    for step in steps:
        kind, what = step[0], step[1]
        if kind == "write":
            new = step[2]
            enc = "utf-16" if what.suffix == ".xml" else "utf-8"  # schtasks /XML reads UTF-16
            old = what.read_text(encoding=enc) if what.is_file() else None
            if old == new:
                print(f"companion: {what} unchanged")
            elif dry:
                print(f"companion: would write {what}:\n" + "".join(f"    {ln}\n" for ln in new.splitlines()), end="")
            else:
                what.parent.mkdir(parents=True, exist_ok=True)
                if len(step) > 3:  # private from its first byte (the opener holds the key), also when it was there before
                    with os.fdopen(os.open(what, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, step[3]), "w", encoding=enc, newline="\n") as f:
                        os.chmod(what, step[3])
                        f.write(new)
                else:
                    what.write_text(new, encoding=enc, newline="\n" if enc == "utf-8" else None)
                print(f"companion: {'updated' if old is not None else 'wrote'} {what}")
        elif kind == "remove":
            if not what.exists():
                continue
            if dry:
                print(f"companion: would remove {what}")
            else:
                shutil.rmtree(what) if what.is_dir() else what.unlink()
                print(f"companion: removed {what}")
        else:
            may_fail, env = step[2], (step[3] if len(step) > 3 else {})
            shown = " ".join(f'"{a}"' if " " in a else a for a in what) + "".join(f"  ({k}={v})" for k, v in env.items())
            shown = re.sub(r"#key=[\w-]+", "#key=...", shown)  # output gets pasted into bug reports
            if dry:
                print(f"companion: would run {shown}")
                continue
            exe = shutil.which(what[0]) or what[0]
            print(f"companion: {shown} ...", end=" ", flush=True)
            try:
                r = subprocess.run([exe, *what[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   env={**os.environ, **env}, stdin=subprocess.DEVNULL, timeout=180)
                ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, out = False, str(e)
            if ok or may_fail:
                print("ok" if ok else "nothing to do")
            else:
                print(f"FAILED: {out[-500:]}")
                failed.append(shown)
    return failed


def run_quiet(argv: list) -> bool:
    try:
        return subprocess.run([shutil.which(argv[0]) or argv[0], *argv[1:]], capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def this_system() -> str:
    return "windows" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux"


def install_companion(argv: list = (), undo: bool = False):
    """The companion at logon, its menu shortcut and a first hook-trust run; again only updates. Fails loudly."""
    dry = "--dry-run" in argv or os.environ.get("KNOWLEDGE_COMPANION_DRY_RUN") == "1"
    system = argv[argv.index("--os") + 1] if "--os" in argv[:-1] else this_system()
    if system not in ("linux", "darwin", "windows"):
        sys.exit("--os is linux, darwin or windows")
    print(f"\n== companion ({system}{', dry run' if dry else ''}) ==")
    skipped = False
    if undo:
        steps = companion_undo_steps(system)
    else:
        import companion  # the local API's key, made once; not in a dry run, which writes nothing
        steps = companion_steps(system, "<key>" if dry else companion.api_key())
        uid = getattr(os, "getuid", lambda: 501)()
        probe = {"linux": ["systemctl", "--user", "show-environment"], "darwin": ["launchctl", "print", f"gui/{uid}"]}.get(system)
        if probe and not dry and system == this_system() and not run_quiet(probe):
            # SSH without lingering, a container, WSL: no user session to start it in; the agents work without it
            print(f"companion: no user session for an autostart here ({' '.join(probe)} failed): skipped. "
                  "Start it by hand with: knowledge-agent.py serve")
            steps, skipped = steps[-1:], True  # still trust the hooks
    failed = run_steps(steps, dry)
    if failed:
        sys.exit(f"companion: {len(failed)} step(s) failed (see above); the agents themselves are set up. "
                 f"Run again: knowledge-agent.py {'uninstall' if undo else 'install'}-companion")
    if not dry and not skipped:
        print("companion: autostart and shortcut removed" if undo else f"companion: runs at logon; page on {COMPANION_URL}")


CHOICE = token_store.KN_HOME / "install.json"  # {"companion": bool}: what install/setup chose, so update keeps it
ASK_COMPANION = "Also install the companion (chats and workers from the web UI, an agent runner that starts at logon)? [Y/n] "


def saved_choice():
    try:
        return json.loads(CHOICE.read_text(encoding="utf-8")).get("companion")
    except (OSError, ValueError, AttributeError):
        return None


def save_choice(want: bool):
    CHOICE.parent.mkdir(parents=True, exist_ok=True)
    CHOICE.write_text(json.dumps({"companion": want}), encoding="utf-8")


def want_companion(cmd: str, flags: list, saved, tty: bool, answer=input) -> bool:
    """--plugin-only decides; else a saved choice; else install/setup in a terminal ask; else the companion (as before)."""
    if "--plugin-only" in flags:
        return False
    if saved is not None:
        return saved is True
    if tty and cmd != "update":
        return answer(ASK_COMPANION).strip().lower() in ("", *YES)
    return True


def finish_companion(cmd: str, flags: list):
    """The end of install, setup and update: the companion, or the plugin only (an existing companion removed)."""
    want = want_companion(cmd, flags, saved_choice(), sys.stdin.isatty() and sys.stdout.isatty())
    save_choice(want)
    if want:
        return install_companion()
    p = companion_paths(this_system())
    if any(p[k].exists() for k in ("unit", "plist", "task_xml") if k in p):
        print("\ncompanion: plugin only chosen, so the companion set up here is removed (YOLO off first)")
        install_companion(undo=True)
    print("companion: not installed (plugin only); add it later with: knowledge-agent.py install-companion")


def install_args(args: list):
    """The words after `install <agent>`: [] or [TOKEN], with --plugin-only dropped (one agent's install never sets up
    the companion), or None for another flag or more words (never taken as a token or vault name)."""
    rest = [a for a in args if a != "--plugin-only"]
    return rest if len(rest) <= 1 and not any(a.startswith("-") and a != "-" for a in rest) else None


def main(argv: list):
    cmd = argv[0] if argv else ""
    if cmd == "header" and len(argv) <= 2:
        tok = token(argv[1] if len(argv) == 2 else "-")
        print(json.dumps(token_store.headers(tok) if tok else {}))
    elif cmd in ("install", "setup", "update") and set(argv[1:]) <= {"--plugin-only"}:
        if cmd == "update":
            update(argv[1:])
        else:
            install_found() if cmd == "install" else setup()
            offer_import()
        finish_companion(cmd, argv[1:])
        if cmd != "update":
            print(f"\n{TRY_IT}")
    elif cmd == "import" and set(argv[1:]) <= {"--dry-run"}:
        import_cmd(argv[1:])
    elif cmd in ("install-companion", "uninstall-companion"):
        if "--dry-run" not in argv:
            save_choice(not cmd.startswith("un"))  # update follows this choice from now on
        install_companion(argv[1:], undo=cmd.startswith("un"))
    elif cmd == "install" and len(argv) >= 2 and argv[1] in (*CONFIG, "claude") and (rest := install_args(argv[2:])) is not None:
        if not rest:  # a vault name (the machine that holds the files) keeps the URL it is given
            ensure_url()
        install(argv[1], rest[0] if rest else "-")
    elif cmd in ("context", "docs-sync", "session-log", "hints", "tasks", "wake", "gate", "guard", "watch", "take", "register", "serve", "companion"):  # hooks written by the setup call this script
        agent_hooks.SETUP = "script"  # its auto-update: git pull + install, not the plugin CLI
        agent_hooks.main(argv)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    if sys.argv[1:2] in (["install"], ["update"], ["setup"], ["import"], ["install-companion"], ["uninstall-companion"]):  # setup is run by a person: errors go to them, loudly
        sys.stdout.reconfigure(errors="replace")  # agent CLIs print ✔ and such; a Windows pipe is cp1252
        main(sys.argv[1:])
    elif "--selftest" in sys.argv:
        ran, kept = [], (shutil.which, has_plugin, run_cli)  # falling back to the script setup takes the plugin away
        shutil.which, has_plugin, run_cli = (lambda n: n), (lambda a: a == "agy"), (lambda argv, ok_if=(): ran.append(argv[1:]))
        drop_agy_plugin()
        assert ran == [["plugin", "uninstall", "knowledge"]], ran
        has_plugin, ran[:] = (lambda a: False), []
        drop_agy_plugin()
        assert not ran, ran
        shutil.which, has_plugin, run_cli = kept
        c = hook_config("codex", "-")["hooks"]
        assert len(c["SessionStart"]) == 1 and c["SessionEnd"][0]["hooks"][0]["timeout"] <= 3
        a = hook_config("agy", "X")["knowledge"]
        assert a["Stop"][0]["command"].endswith("session-log -") and a["SessionStart"][0]["command"].endswith("context agy X")
        assert helper_line("-").startswith('http_headers_helper = "') and helper_line("-").endswith(' header -"')
        replies = iter(["", "n", "J"])
        assert choose(["claude", "codex", "agy"], lambda q: next(replies)) == ["claude", "agy"]
        assert locked_hint("unlinkat x: The process cannot access the file because it is being used by another process.")
        assert locked_hint("token ok") == ""
        assert install_args(["--plugin-only"]) == [] and install_args(["tok", "--plugin-only"]) == ["tok"] and install_args(["-"]) == ["-"]
        assert install_args(["--yolo"]) is None and install_args(["a", "b"]) is None, "no flag or second word is a token"
        try:
            import tomllib
        except ImportError:  # Python < 3.11 (macOS's python3 is 3.9): the text checks below still run
            tomllib = None
        k = "[mcp_servers.knowledge]\nurl = \"u\"\n"
        for before, after, want in (  # the knowledge table goes, everything else stays (review 31 Oct 2026)
                ("", "", ""), ('model = "x"\n', "", 'model = "x"\n'),
                ("", '[mcp_servers.knowledge.env]\nA = "1"\n[mcp_servers.knowledge.tools.t]\nb = 1\n[mcp_servers.other]\nurl = "o"\n',
                 '[mcp_servers.other]\nurl = "o"\n'),
                ("", '[[mcp_servers.knowledge.x]]\nn = 1\n[[profiles]]\nx = 1\n', '[[profiles]]\nx = 1\n'),
                ("# [mcp_servers.knowledge] once\n[a]\n", '\n# my other server\n[mcp_servers.other] # here\nurl = "o"\n',
                 '# [mcp_servers.knowledge] once\n[a]\n\n# my other server\n[mcp_servers.other] # here\nurl = "o"\n'),
                ("[b]\r\n", '# note\r\nc = 1\r\n[mcp_servers.knowledgex]\r\n', "[b]\r\n\n[mcp_servers.knowledgex]\r\n")):
            for t in (before + k + after, (before + k + after).replace("\n", "\r\n").replace("\r\r", "\r")):
                rest = drop_toml_table(t, "mcp_servers.knowledge")
                assert not tomllib or "knowledge" not in tomllib.loads(rest).get("mcp_servers", {}), t
                assert not tomllib or tomllib.loads(rest) == tomllib.loads(want), (t, rest)
                assert ("# my other" in rest) == ("# my other" in want) and ("once" in rest) == ("once" in want), (t, rest)
        assert toml_table_span("[[mcp_servers.knowledge]]\n# [mcp_servers.knowledge]\n", "mcp_servers.knowledge") is None
        os.environ["CLAUDE_CONFIG_DIR"] = "elsewhere"
        assert claude_settings() == Path("elsewhere", "settings.json")
        del os.environ["CLAUDE_CONFIG_DIR"]
        mine = Path(os.path.join(os.sep, "h", "me", ".codex"))
        for env, same in ((b"HOME=/h/me\0A=b=c\0", True), (b"HOME=/h/other\0", False), (b"HOME=/h/me\0CODEX_HOME=/x\0", False),
                          (b"HOME=/x\0CODEX_HOME=/h/me/.codex/\0", True)):
            assert same_codex_home(env, mine) == same or os.name == "nt", env
        for t, want in (  # a look-alike in a value is no table; the real one goes whole (stopped review 23 Oct 2026, reproduced 5 Nov 2026)
                ('note = "[mcp_servers.knowledge]"\n[z]\nq = 1\n', None),
                ('note = """\n[mcp_servers.knowledge]\nurl = 1\n"""\n[z]\nq = 1\n', None),
                ("note = '''\n[mcp_servers.knowledge]\n'''\n[z]\nq = 1\n", None),
                ('note = "a [mcp_servers.knowledge] b"\n', None),
                ('[z]\nnote = """\n[mcp_servers.knowledge]\n"""\nq = 1\n' + k, '[z]\nnote = """\n[mcp_servers.knowledge]\n"""\nq = 1\n'),
                ('a = """x\\"""\n[mcp_servers.knowledge]\n"""\n' + k, 'a = """x\\"""\n[mcp_servers.knowledge]\n"""\n'),
                ('a = """x"""\n' + k + '[z]\nq = 1\n', 'a = """x"""\n\n[z]\nq = 1\n'),
                (k + 'd = """\n[other]\n"""\n[z]\nq = 1\n', '[z]\nq = 1\n'),
                (k + 'm = [\n  ["a"]\n]\n[z]\nq = 1\n', '[z]\nq = 1\n'),
                (k + '# c\nm = [\n  # ]\n  "]"\n]\n[z]\nq = 1\n', '[z]\nq = 1\n'),
                ('m = "x"\n[mcp_servers.knowledge]\nurl = "u"', 'm = "x"\n'),
                (k + "[[profiles]]\nx = 1\n", "[[profiles]]\nx = 1\n"),
                ('[mcp_servers.knowledge]\r\nurl = "u"\r\n[z]\r\nq = 1', "[z]\r\nq = 1\n")):
            assert drop_toml_table(t, "mcp_servers.knowledge") == want, (t, drop_toml_table(t, "mcp_servers.knowledge"))
            assert not tomllib or tomllib.loads(want if want is not None else t), t
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "e.json").write_text("\ufeff \n", encoding="utf-8")
            assert load_json(Path(d) / "e.json") == {} and load_json(Path(d) / "missing.json") == {}
            f = Path(d) / "feedback_short.md"
            f.write_text("---\r\nname: feedback_short\r\ndescription: Keep it short\r\nmetadata:\r\n  type: feedback\r\n---\r\n"
                         f"# Short\r\n## Status\r\nrule\r\n{BEGIN}\r\nhouse\r\n{END}\r\n## Decisions\r\n- **x** (1 Jan 2026, me): y\r\n", encoding="utf-8")
            a = import_memo(f, "", "")
            assert (a["type"], a["title"], a["description"], a["heading"]) == ("feedback", "Short", "Keep it short", "Imported"), a
            assert "## Short" in a["status"] and "rule" in a["status"] and "house" not in a["status"] and "## " not in a["status"].replace("## Short", "")
            assert a["decisions"] == "- **x** (1 Jan 2026, me): y" and "project" not in a
            f.write_text(f"{BEGIN}\nhouse\n{END}\n", encoding="utf-8")
            assert import_memo(f, "t", "garden") == {}
        rnd = hashlib.sha256(b"selftest").hexdigest()[:24]  # built at run time: no secret-looking literal in the repo
        for text in (f"api_key = {rnd}", f"TOKEN: '{rnd}'", "-----BEGIN OPENSSH " + "PRIVATE KEY-----", "sk-" + rnd, "AKIA" + "Q7" * 8):
            assert likely_secret(text), text
        for text in ("password: ‹secret:HOMELAB_ROOT_PASSWORD›", "token=see_the_vault_for_this", "key: /home/user/.ssh/id_ed25519.pub",
                     "the token is refused", "secret_hashes() of every known value"):
            assert not likely_secret(text), text
        assert [n for n, _ in pick([(1, 0), (2, 0), (3, 0)], "1, 3")] == [1, 3] and pick([(1, 0)], "") == [] and pick([(1, 0)], "all") == [(1, 0)]
        assert claude_folder("-home-u-My-Proj", {"/home/u/My Proj", "/home/u/My"}) == "/home/u/My Proj"
        for flags, saved, tty, reply, want in ((["--plugin-only"], True, True, "", False), ([], False, True, "y", False),
                                               ([], None, True, "n", False), ([], None, True, "", True), ([], None, False, "n", True)):
            assert want_companion("install", flags, saved, tty, lambda q: reply) is want, (flags, saved, tty, reply)
        assert want_companion("update", [], None, True, lambda q: "n") is True  # update never asks
        assert claude_folder("C--Users-u-x", {"C:\\Users\\u\\x"}) == "C:\\Users\\u\\x" and claude_folder("-y", {"/x"}) == ""
        print("ok")
    else:
        try:
            main(sys.argv[1:])
        except Exception as e:  # locked vault, bad stdin, network: say so, never fail the session
            print(f"knowledge hook skipped: {e}", file=sys.stderr)
