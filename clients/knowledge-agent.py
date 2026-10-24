#!/usr/bin/env python3
"""Connect Claude Code, Codex or agy to the knowledge server, and run the knowledge plugin's hooks for them.

Linux, macOS and Windows; standard library only. install asks for the device token (hidden) when none is stored, or
whether to replace the stored one, checks it with the server and saves it in the keyring of the OS (Linux secret-tool,
macOS Keychain, Windows Credential Manager; token_store.py). Without a terminal it uses KNOWLEDGE_TOKEN or the stored
token. On the machine that holds the files, TOKEN can be a vault name. The session log falls back to ~/.config/knowledge-server/session.env.

  knowledge-agent.py install <claude|codex|agy> [TOKEN]   set up or update; safe to run again. Claude Code: the plugin.
                    Codex and agy: the plugin (an older setup of this script is removed first); with a vault name,
                    KNOWLEDGE_SETUP=script, or agy on Windows without python3 on PATH: this script's setup (MCP relay,
                    hooks, rules in AGENTS.md/GEMINI.md, skills in ~/.agents/skills)
  knowledge-agent.py header [TOKEN]                 MCP header helper (Codex http_headers_helper): the token as JSON
  knowledge-agent.py context <agy|codex> TOKEN     SessionStart: startup context + docs mirror, as the agent's JSON
  knowledge-agent.py docs-sync TOKEN               PostToolUse: mirror an edited .md (folder with .knowledge-project)
  knowledge-agent.py session-log TOKEN             SessionEnd/PreCompact (Codex), Stop (agy): post the session log
"""
import json
import os
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
URL = os.environ.get("KNOWLEDGE_URL", "https://knowledge.to0wnn.com").rstrip("/")
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
                             "hooks": [{"type": "command", "command": cmd("docs-sync"), "timeout": 20}]}],
            "Stop": [{"type": "command", "command": cmd("session-log"), "timeout": 15}]}}
    # One SessionStart handler only: with several, Codex drops the context (openai/codex#45999).
    # SessionEnd hooks may take at most 3 seconds.
    return {"hooks": {
        "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [
            {"type": "command", "command": cmd("context", "codex"), "timeout": 30, "additionalContextLimit": 60000}]}],
        "PostToolUse": [{"matcher": "apply_patch", "hooks": [{"type": "command", "command": cmd("docs-sync"), "timeout": 20}]}],
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


def run_cli(argv: list, ok_if: tuple = ()) -> bool:
    """Run an agent CLI step and say what it did; stop loudly on a failure, unless its output holds one of ok_if."""
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = (r.stdout + r.stderr).strip()
    if r.returncode != 0 and not any(x in text.lower() for x in ok_if):
        sys.exit(f"{Path(argv[0]).stem} {' '.join(argv[1:])} failed:\n{text[-1500:]}")
    print(f"{Path(argv[0]).stem} {' '.join(argv[1:3])}: {text.splitlines()[-1] if text else 'ok'}")
    return r.returncode == 0


def install_claude():
    claude = cli("claude")
    run_cli([claude, "plugin", "marketplace", "add", MARKET], ok_if=("already",))
    run_cli([claude, "plugin", "marketplace", "update", "knowledge"])
    run_cli([claude, "plugin", "install", "knowledge@knowledge", "--scope", "user"], ok_if=("already",))
    run_cli([claude, "plugin", "update", "knowledge@knowledge"])  # install leaves an installed copy at its old version
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


def install_plugin(agent: str):
    remove_old_setup(agent)
    if agent == "codex":
        codex = cli("codex")
        run_cli([codex, "plugin", "marketplace", "add", MARKET], ok_if=("already",))
        run_cli([codex, "plugin", "marketplace", "upgrade", "knowledge"], ok_if=("not configured as a git",))  # a local checkout
        run_cli([codex, "plugin", "add", "knowledge@knowledge"])
        restart_codex()
        return print("done; open codex and type /hooks once to trust the four knowledge hooks")
    run_cli([cli("agy"), "plugin", "install", AGY_PLUGIN])
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


def install(agent: str, name: str):
    """Set up or update; every step says what it did. Fails loudly: a wrong token or no connection stops here."""
    tok = ensure_token(name)
    if not tok:
        sys.exit("No token: run this in a terminal so it can ask for it, or set KNOWLEDGE_TOKEN.")
    rules = check(tok)
    if agent == "claude":
        return install_claude()
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
    print("done" + ("; open codex and type /hooks once to trust the hooks" if agent == "codex" else "; start agy and ask it to search the knowledge"))


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


def main(argv: list):
    cmd = argv[0] if argv else ""
    if cmd == "header" and len(argv) <= 2:
        tok = token(argv[1] if len(argv) == 2 else "-")
        print(json.dumps({"Authorization": f"Bearer {tok}"} if tok else {}))
    elif cmd == "install" and len(argv) >= 2 and argv[1] in (*CONFIG, "claude"):
        install(argv[1], argv[2] if len(argv) > 2 else "-")
    elif cmd in ("context", "docs-sync", "session-log"):  # hooks written by older setups call this script
        agent_hooks.SETUP = "script"  # its auto-update: git pull + install, not the plugin CLI
        agent_hooks.main(argv)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    if sys.argv[1:2] == ["install"]:  # setup is run by a person: errors go to them, loudly
        sys.stdout.reconfigure(errors="replace")  # agent CLIs print ✔ and such; a Windows pipe is cp1252
        main(sys.argv[1:])
    elif "--selftest" in sys.argv:
        c = hook_config("codex", "-")["hooks"]
        assert len(c["SessionStart"]) == 1 and c["SessionEnd"][0]["hooks"][0]["timeout"] <= 3
        a = hook_config("agy", "X")["knowledge"]
        assert a["Stop"][0]["command"].endswith("session-log -") and a["SessionStart"][0]["command"].endswith("context agy X")
        assert helper_line("-").startswith('http_headers_helper = "') and helper_line("-").endswith(' header -"')
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
