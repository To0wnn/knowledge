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
  knowledge-agent.py install <claude|codex|agy> [TOKEN]   set up or update; safe to run again. Claude Code: the plugin.
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
                    run; setup, install and update do this at the end. --dry-run (or KNOWLEDGE_COMPANION_DRY_RUN=1) only
                    lists the steps, for this or another OS
  knowledge-agent.py uninstall-companion [--dry-run] [--os ...]   remove that autostart and shortcut again
"""
import getpass
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
URL = token_store.url()  # KNOWLEDGE_URL, else what an earlier install stored, else the default
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
    try:
        return fetch("/house-rules", tok), ""
    except urllib.error.HTTPError as e:
        if e.code == 401 and os.environ.get("KNOWLEDGE_TOKEN") == tok:  # env wins over the keyring: often a stale export
            return None, ("The server refuses the token in KNOWLEDGE_TOKEN (401): copied wrongly or revoked. If the token "
                          "is in the keyring now, run 'unset KNOWLEDGE_TOKEN' (or open a new terminal) and try again.")
        return None, {401: "The server refuses this token (401): copied wrongly or revoked.",
                      403: "The server is only reachable from the home network or WireGuard (403)."}.get(e.code, f"Server answered HTTP {e.code}.")
    except OSError as e:
        return None, f"Cannot reach {URL}: {e}"


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
              "with 'claude plugin configure knowledge@knowledge'. This run goes on with the token.")
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
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = (r.stdout + r.stderr).strip()
    if r.returncode != 0 and not any(x in text.lower() for x in ok_if):
        sys.exit(f"{Path(argv[0]).stem} {' '.join(argv[1:])} failed:\n{text[-1500:]}{locked_hint(text)}")
    print(f"{Path(argv[0]).stem} {' '.join(argv[1:3])}: {text.splitlines()[-1] if text else 'ok'}")
    return r.returncode == 0


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
    path = Path.home() / ".claude" / "settings.json"
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


def remove_old_setup(agent: str):
    """What this script's own setup wrote (MCP server, hooks), so the plugin does not load it all twice."""
    if agent == "codex":
        conf, hooks = CODEX / "config.toml", CODEX / "hooks.json"
        text = conf.read_text(encoding="utf-8") if conf.exists() else ""
        if "[mcp_servers.knowledge]" in text:
            start = text.index("[mcp_servers.knowledge]")
            end = text.find("\n[", start + 1)
            end = len(text) if end < 0 else end + 1
            conf.write_text((text[:start].rstrip("\n") + "\n\n" + text[end:]).strip("\n") + "\n", encoding="utf-8")
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
               "update); without the companion, open codex and type /hooks once")


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


def update():
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
        sys.exit(subprocess.run([sys.executable, "-X", "utf8", __file__, "update"],
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
        if "[mcp_servers.knowledge]" in text:
            start = text.index("[mcp_servers.knowledge]")
            end = text.find("\n[", start + 1)  # the next table, or the end of the file
            end = len(text) if end < 0 else end
            block = text[start:end]
            if old in block:  # older setup that read KNOWLEDGE_TOKEN: replace this server's block only
                path.write_text(text[:start] + codex_block(name).rstrip("\n") + text[end:], encoding="utf-8")
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


def restart_codex():
    """Codex keeps a background app-server that holds the MCP settings it started with, so a changed config only
    counts after it restarts. Offer to stop it (Codex starts a new one on its next run); never without asking."""
    # shortcut: Linux/macOS only (pgrep); on Windows the setup only says to restart Codex.
    try:
        pids = subprocess.run(["pgrep", "-f", "codex app-server"], capture_output=True, text=True).stdout.split()
    except OSError:
        pids = []
    if not pids:
        return
    msg = "Codex is still running with the old settings."
    if not sys.stdin.isatty():
        return print(f"{msg} Quit Codex (and its background server) so the new settings apply.")
    if input(f"{msg} Stop it now? [y/N] ").strip().lower() not in ("y", "yes", "j", "ja"):
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
            try:
                r = subprocess.run([exe, *what[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   env={**os.environ, **env}, stdin=subprocess.DEVNULL, timeout=180)
                ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, out = False, str(e)
            if ok or may_fail:
                print(f"companion: ran {shown}" + ("" if ok else " (nothing to do)"))
            else:
                print(f"companion: FAILED {shown}: {out[-500:]}")
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


def main(argv: list):
    cmd = argv[0] if argv else ""
    if cmd == "header" and len(argv) <= 2:
        tok = token(argv[1] if len(argv) == 2 else "-")
        print(json.dumps(token_store.headers(tok) if tok else {}))
    elif cmd == "install" and len(argv) == 1:
        install_found()
        install_companion()
    elif cmd == "update" and len(argv) == 1:
        update()
        install_companion()
    elif cmd == "setup" and len(argv) == 1:
        setup()
        install_companion()
    elif cmd in ("install-companion", "uninstall-companion"):
        install_companion(argv[1:], undo=cmd.startswith("un"))
    elif cmd == "install" and len(argv) >= 2 and argv[1] in (*CONFIG, "claude"):
        if len(argv) == 2:  # a vault name (the machine that holds the files) keeps the URL it is given
            ensure_url()
        install(argv[1], argv[2] if len(argv) > 2 else "-")
    elif cmd in ("context", "docs-sync", "session-log", "hints", "tasks", "wake", "gate", "guard", "watch", "take", "register", "serve", "companion"):  # hooks written by the setup call this script
        agent_hooks.SETUP = "script"  # its auto-update: git pull + install, not the plugin CLI
        agent_hooks.main(argv)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    if sys.argv[1:2] in (["install"], ["update"], ["setup"], ["install-companion"], ["uninstall-companion"]):  # setup is run by a person: errors go to them, loudly
        sys.stdout.reconfigure(errors="replace")  # agent CLIs print ✔ and such; a Windows pipe is cp1252
        main(sys.argv[1:])
    elif "--selftest" in sys.argv:
        c = hook_config("codex", "-")["hooks"]
        assert len(c["SessionStart"]) == 1 and c["SessionEnd"][0]["hooks"][0]["timeout"] <= 3
        a = hook_config("agy", "X")["knowledge"]
        assert a["Stop"][0]["command"].endswith("session-log -") and a["SessionStart"][0]["command"].endswith("context agy X")
        assert helper_line("-").startswith('http_headers_helper = "') and helper_line("-").endswith(' header -"')
        replies = iter(["", "n", "J"])
        assert choose(["claude", "codex", "agy"], lambda q: next(replies)) == ["claude", "agy"]
        assert locked_hint("unlinkat x: The process cannot access the file because it is being used by another process.")
        assert locked_hint("token ok") == ""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "e.json").write_text("\ufeff \n", encoding="utf-8")
            assert load_json(Path(d) / "e.json") == {} and load_json(Path(d) / "missing.json") == {}
        print("ok")
    else:
        try:
            main(sys.argv[1:])
        except Exception as e:  # locked vault, bad stdin, network: say so, never fail the session
            print(f"knowledge hook skipped: {e}", file=sys.stderr)
