#!/usr/bin/env python3
"""Connect agy (Antigravity CLI) or Codex to the knowledge server, and run the knowledge plugin's hooks for them.

Linux, macOS and Windows; standard library only. The token (TOKEN "-") comes from KNOWLEDGE_TOKEN or the Linux keyring
(secret-tool store --label="Knowledge server token" service knowledge), or on the owner's VM from the vault (TOKEN = a vault name). The session log falls back to ~/.config/knowledge-server/session.env, so the
agy/Codex tokens on the VM stay read-only (install always gives session-log "-").

  knowledge-agent.py header [TOKEN]                 MCP header helper (Codex http_headers_helper): the token as JSON
  knowledge-agent.py install <agy|codex> [TOKEN]   one-time setup: checks the token, adds the MCP server, the hooks,
                                                   the house and development rules (GEMINI.md / AGENTS.md) and the
                                                   development skills (~/.agents/skills); safe to run again
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


def check(tok: str) -> str:
    """The house rules, which also proves the token and the connection; stops with a plain reason otherwise."""
    try:
        rules = fetch("/house-rules", tok)
    except urllib.error.HTTPError as e:
        if e.code == 401 and os.environ.get("KNOWLEDGE_TOKEN"):  # env wins over the keyring: often a stale export
            sys.exit("The server refuses the token in KNOWLEDGE_TOKEN (401): copied wrongly or revoked. If the token "
                     "is in the keyring now, run 'unset KNOWLEDGE_TOKEN' (or open a new terminal) and try again.")
        sys.exit({401: "The server refuses this token (401): copied wrongly or revoked.",
                  403: "The server is only reachable from the home network or WireGuard (403)."}.get(e.code, f"Server answered HTTP {e.code}."))
    except OSError as e:
        sys.exit(f"Cannot reach {URL}: {e}")
    print(f"token ok ({URL})")
    return rules


def install(agent: str, name: str):
    """One-time setup; every step says what it did. Fails loudly: a wrong token or no connection stops here."""
    tok = token(name)
    if not tok:
        sys.exit("No token: put the token in the keyring first (Windows: cmdkey /generic:knowledge /user:knowledge /pass; see the help page), then run this again.")
    rules = check(tok)
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
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
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
    elif cmd == "install" and len(argv) >= 2 and argv[1] in CONFIG:
        install(argv[1], argv[2] if len(argv) > 2 else "-")
    elif cmd in ("context", "docs-sync", "session-log"):  # hooks written by older setups call this script
        agent_hooks.main(argv)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    if sys.argv[1:2] == ["install"]:  # setup is run by a person: errors go to them, loudly
        main(sys.argv[1:])
    elif "--selftest" in sys.argv:
        c = hook_config("codex", "-")["hooks"]
        assert len(c["SessionStart"]) == 1 and c["SessionEnd"][0]["hooks"][0]["timeout"] <= 3
        a = hook_config("agy", "X")["knowledge"]
        assert a["Stop"][0]["command"].endswith("session-log -") and a["SessionStart"][0]["command"].endswith("context agy X")
        assert helper_line("-").startswith('http_headers_helper = "') and helper_line("-").endswith(' header -"')
        print("ok")
    else:
        try:
            main(sys.argv[1:])
        except Exception as e:  # locked vault, bad stdin, network: say so, never fail the session
            print(f"knowledge hook skipped: {e}", file=sys.stderr)
