"""Where a device keeps its knowledge-server token, so it sits in no plain file (shell rc, ~/.claude/.credentials.json).

Lookup order: KNOWLEDGE_TOKEN (Windows guests via setx, the VM, CI), the Linux keyring (libsecret via secret-tool,
entry service=knowledge; one token per device), then the Claude plugin option (installs from before the keyring).
The MCP headersHelper sees no credential env vars (Claude Code strips them), so there the keyring comes first too.
`token_store.py header` prints the MCP Authorization header as JSON for Claude Code's headersHelper and Codex's
http_headers_helper; `{}` when there is no token, so a static header still applies. Standard library only.
"""
import json
import os
import subprocess
import sys

SERVICE = "knowledge"


def from_keyring() -> str:
    # shortcut: Linux only (secret-tool); Windows/macOS keep KNOWLEDGE_TOKEN until a device there needs the keyring.
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and not env.get("XDG_RUNTIME_DIR") and hasattr(os, "getuid"):
        # Codex runs its header helper with a bare environment; libsecret finds the session bus via XDG_RUNTIME_DIR.
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    try:
        r = subprocess.run(["secret-tool", "lookup", "service", SERVICE], capture_output=True, text=True, timeout=5,
                           env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def token() -> str:
    return os.environ.get("KNOWLEDGE_TOKEN") or from_keyring() \
        or os.environ.get("CLAUDE_PLUGIN_OPTION_TOKEN") or os.environ.get("CLAUDE_PLUGIN_OPTION_token", "")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "token"
    tok = token()
    if cmd == "header":
        print(json.dumps({"Authorization": f"Bearer {tok}"} if tok else {}))
    elif cmd == "token":  # for shell hooks (context.sh); prints nothing when there is none
        print(tok)
    else:
        sys.exit("usage: token_store.py [header|token]")
