"""Where a device keeps its knowledge-server token, so it sits in no plain file (shell rc, ~/.claude/.credentials.json).

Lookup order: KNOWLEDGE_TOKEN (the VM, CI, servers without a keyring), the keyring (Linux: libsecret via secret-tool,
entry service=knowledge; macOS: Keychain, service "knowledge"; Windows: Credential Manager, generic credential
"knowledge"; one token per device), then the Claude plugin option (older installs, and machines without a keyring;
outside Claude read where Claude Code stored it, claude_option). store() writes the keyring of the
OS it runs on (clients/knowledge-agent.py install asks for the token and calls it).
The MCP headersHelper sees no credential env vars (Claude Code strips them), so there the keyring comes first too.
url() is the server address the same way: the Claude plugin option, KNOWLEDGE_URL, the file the setup wrote, the option
as Claude stored it, the default.
`token_store.py header` prints the MCP Authorization header as JSON for Claude Code's headersHelper and Codex's
http_headers_helper; `{}` when there is no token, so a static header still applies. Standard library only.
"""
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

SERVICE = "knowledge"
NO_URL = ("knowledge: no server address is set on this device, so the knowledge server is not used. Run "
          "`knowledge-agent.py setup` (or set KNOWLEDGE_URL) to connect it.")
KN_HOME = Path(os.environ.get("KNOWLEDGE_HOME") or Path.home() / ".knowledge")  # KNOWLEDGE_HOME relocates ~/.knowledge (tests)
URL_FILE = Path.home() / ".config" / "knowledge" / "url"  # written by knowledge-agent.py install; not a secret


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


def claude_option(name: str) -> str:
    """The Claude plugin's option as Claude Code stored it, for code that runs outside Claude (the setup, Codex and agy
    hooks): on a machine without a keyring the token is only there (a server, a container). Secrets live in
    .credentials.json (on macOS in the Keychain instead, then this finds nothing), the rest in settings.json."""
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        if name == "token":
            return json.loads((home / ".credentials.json").read_text(encoding="utf-8"))["pluginSecrets"]["knowledge@knowledge"]["token"]
        return json.loads((home / "settings.json").read_text(encoding="utf-8"))["pluginConfigs"]["knowledge@knowledge"]["options"][name]
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def token() -> str:
    return os.environ.get("KNOWLEDGE_TOKEN") or from_keyring() \
        or os.environ.get("CLAUDE_PLUGIN_OPTION_TOKEN") or os.environ.get("CLAUDE_PLUGIN_OPTION_token") or claude_option("token")


def url() -> str:
    """The server: the Claude plugin option (what Claude's MCP entry uses too), KNOWLEDGE_URL, the URL the setup
    stored; "" when none is set (there is no built-in server)."""
    try:
        stored = URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        stored = ""
    return (os.environ.get("CLAUDE_PLUGIN_OPTION_URL") or os.environ.get("CLAUDE_PLUGIN_OPTION_url")
            or os.environ.get("KNOWLEDGE_URL") or stored or claude_option("url") or "").rstrip("/")


def url_is_default(claude: bool = False) -> bool:
    """Nothing names the server on this device (url() is ""). Claude's MCP entry reads only its plugin option."""
    if claude:
        return not claude_option("url")
    return not (os.environ.get("KNOWLEDGE_URL") or URL_FILE.exists() or claude_option("url"))


def machine() -> dict:
    """This pc's identity, {"id", "label"}: a random id made once in KNOWLEDGE_HOME/machine.json with the host name as
    label (the companion makes it, a hook makes it when missing). The server keys task leases on token + machine, so
    one token used on two pcs is two machines. {} when the file cannot be read or written (the server then falls back
    to the token alone)."""
    f = KN_HOME / "machine.json"
    for _ in range(2):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict) and re.fullmatch(r"[0-9a-f]{16}", str(d.get("id", ""))):
                return {"id": d["id"], "label": re.sub(r"[^\w.-]", "-", str(d.get("label") or ""))[:60] or d["id"]}
        except (OSError, ValueError):
            pass
        try:
            KN_HOME.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": secrets.token_hex(8), "label": socket.gethostname(), "created": time.time()}))
        except FileExistsError:  # made by another hook meanwhile, or not valid: read it (again)
            continue
        except OSError:
            return {}
    return {}


def headers(tok: str) -> dict:
    """What every request to the server carries: the token and this pc's identity; a worker the companion started
    also names its task (KNOWLEDGE_WORKER), so the server can hold its hand-back lines to the worker rules."""
    m = machine()
    return {**({"Authorization": f"Bearer {tok}"} if tok else {}),
            **({"X-Knowledge-Machine": m["id"], "X-Knowledge-Machine-Label": m["label"]} if m else {}),
            **({"X-Knowledge-Worker": w} if (w := re.sub(r"[^\w.-]", "", os.environ.get("KNOWLEDGE_WORKER", ""))[:60]) else {})}


def store_url(u: str) -> None:
    URL_FILE.parent.mkdir(parents=True, exist_ok=True)
    URL_FILE.write_text(u.rstrip("/") + "\n", encoding="utf-8")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "token"
    tok = token()
    if cmd == "header":
        print(json.dumps(headers(tok) if tok else {}))
    elif cmd == "url":  # for context.sh
        print(url())
    elif cmd == "url-default":  # for context.sh: "1" when Claude's url option is not set
        print("1" if url_is_default(claude=True) else "")
    elif cmd == "token":  # for shell hooks (context.sh); prints nothing when there is none
        print(tok)
    else:
        sys.exit("usage: token_store.py [header|token|url|url-default]")
