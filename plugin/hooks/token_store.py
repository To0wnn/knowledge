"""Where a device keeps its knowledge-server token, so it sits in no plain file (shell rc, ~/.claude/.credentials.json).

Lookup order: KNOWLEDGE_TOKEN (the VM, CI, servers without a keyring), the keyring (Linux: libsecret via secret-tool,
entry service=knowledge; Windows: Credential Manager, generic credential "knowledge", stored with
`cmdkey /generic:knowledge /user:knowledge /pass`; one token per device), then the Claude plugin option (older installs).
The MCP headersHelper sees no credential env vars (Claude Code strips them), so there the keyring comes first too.
`token_store.py header` prints the MCP Authorization header as JSON for Claude Code's headersHelper and Codex's
http_headers_helper; `{}` when there is no token, so a static header still applies. Standard library only.
"""
import json
import os
import subprocess
import sys

SERVICE = "knowledge"


def from_windows() -> str:
    """The generic credential "knowledge" from the Windows Credential Manager (cmdkey stores it as UTF-16)."""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]
    advapi = ctypes.WinDLL("advapi32")
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    cred = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(SERVICE, 1, 0, ctypes.byref(cred)):  # 1 = CRED_TYPE_GENERIC; not found: no token
        return ""
    try:
        blob = ctypes.string_at(cred.contents.CredentialBlob, cred.contents.CredentialBlobSize)
    finally:
        advapi.CredFree(cred)
    return blob.decode("utf-16-le", "replace").strip("\x00").strip()


def from_keyring() -> str:
    if os.name == "nt":
        try:
            return from_windows()
        except (OSError, AttributeError, ValueError):
            return ""
    # shortcut: no macOS Keychain yet (no Mac in use); there KNOWLEDGE_TOKEN.
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
