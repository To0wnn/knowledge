"""Where a device keeps its knowledge-server token, so it sits in no plain file (shell rc, ~/.claude/.credentials.json).

Lookup order: KNOWLEDGE_TOKEN (the VM, CI, servers without a keyring), the keyring (Linux: libsecret via secret-tool,
entry service=knowledge; macOS: Keychain, service "knowledge"; Windows: Credential Manager, generic credential
"knowledge"; one token per device), then the Claude plugin option (older installs). store() writes the keyring of the
OS it runs on (clients/knowledge-agent.py install asks for the token and calls it).
The MCP headersHelper sees no credential env vars (Claude Code strips them), so there the keyring comes first too.
`token_store.py header` prints the MCP Authorization header as JSON for Claude Code's headersHelper and Codex's
http_headers_helper; `{}` when there is no token, so a static header still applies. Standard library only.
"""
import json
import os
import subprocess
import sys

SERVICE = "knowledge"


def _windows():
    """advapi32 with CredReadW/CredWriteW/CredFree typed, and the CREDENTIAL structure."""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, advapi, CREDENTIAL


def from_windows() -> str:
    """The generic credential "knowledge" from the Windows Credential Manager (stored as UTF-16, as cmdkey does)."""
    ctypes, advapi, CREDENTIAL = _windows()
    cred = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(SERVICE, 1, 0, ctypes.byref(cred)):  # 1 = CRED_TYPE_GENERIC; not found: no token
        return ""
    try:
        blob = ctypes.string_at(cred.contents.CredentialBlob, cred.contents.CredentialBlobSize)
    finally:
        advapi.CredFree(cred)
    return blob.decode("utf-16-le", "replace").strip("\x00").strip()


def from_keyring() -> str:
    """The stored token of this device, or "" (no keyring, nothing stored, or the keyring is locked)."""
    if os.name == "nt":
        try:
            return from_windows()
        except (OSError, AttributeError, ValueError):
            return ""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-w"], capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""
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


def store(tok: str) -> None:
    """Save the token in this OS's keyring, replacing an older one; raises OSError when there is no keyring."""
    if os.name == "nt":
        ctypes, advapi, CREDENTIAL = _windows()
        blob = tok.encode("utf-16-le")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = CREDENTIAL(Type=1, TargetName=SERVICE, CredentialBlobSize=len(blob),  # 1 = CRED_TYPE_GENERIC
                          CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), Persist=2, UserName=SERVICE)  # 2 = local machine
        if not advapi.CredWriteW(ctypes.byref(cred), 0):
            raise OSError(f"CredWriteW failed ({ctypes.get_last_error() or 'no detail'})")
        return
    if sys.platform == "darwin":
        # shortcut: security takes the password as an argument (briefly visible in ps); its prompt cannot be scripted.
        cmd, stdin = ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", SERVICE, "-w", tok], None
    else:
        cmd, stdin = ["secret-tool", "store", "--label=Knowledge server token", "service", SERVICE], tok
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(f"no keyring: {e}") from e
    if r.returncode != 0:
        raise OSError(f"no keyring: {(r.stderr or r.stdout).strip()[:200]}")


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
