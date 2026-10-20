"""SSRF-safe fetch of a user-supplied URL for ingest.

The server sits inside the homelab (forge, LAN access), so a URL must never reach anything internal:
  - only http/https on ports 80/443, no user:password@ in the URL;
  - every address the host resolves to must be public (no private, loopback, link-local, CGNAT 100.64/10,
    reserved or multicast), IPv4-mapped IPv6 normalised first;
  - we connect to the validated IP ourselves (TLS still checks the real hostname), so DNS rebinding cannot swap
    the address between check and connect;
  - redirects are followed by hand (max 5), each hop validated again;
  - identity encoding only (no decompression bombs), body capped at 10 MB, 5 s connect / 20 s total.
"""
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 10_000_000
ALLOWED_TYPES = ("text/html", "text/plain", "text/markdown", "application/xhtml+xml", "application/pdf",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
CGNAT = ipaddress.ip_network("100.64.0.0/10")


class FetchError(Exception):
    pass


def public_ip(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve {host}: {e}")
    addrs = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip in CGNAT or ip.is_multicast:
            raise FetchError(f"{host} resolves to a non-public address ({ip}); internal addresses are not allowed")
        addrs.append(str(ip))
    if not addrs:
        raise FetchError(f"{host} has no address")
    return addrs[0]


def check_url(url: str) -> tuple[str, str, int, str]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("only http and https URLs can be ingested")
    if parts.username or parts.password:
        raise FetchError("URLs with user:password@ are not allowed")
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in (80, 443):
        raise FetchError("only ports 80 and 443 are allowed")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.scheme, host, port, path


def fetch(url: str, deadline_s: float = 20.0) -> tuple[bytes, str, str]:
    """Returns (body, content_type, final_url)."""
    end = time.monotonic() + deadline_s
    for _ in range(6):
        scheme, host, port, path = check_url(url)
        ip = public_ip(host, port)
        sock = socket.create_connection((ip, port), timeout=5)
        try:
            if scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            conn = http.client.HTTPConnection(host, port, timeout=max(1.0, end - time.monotonic()))
            conn.sock = sock  # use our validated connection, never a fresh lookup
            conn.request("GET", path, headers={"Host": host, "User-Agent": "knowledge-ingest/1.0",
                                               "Accept-Encoding": "identity", "Accept": "*/*"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    raise FetchError(f"redirect without Location from {url}")
                url = urljoin(url, loc)
                continue
            if resp.status != 200:
                raise FetchError(f"{url} answered HTTP {resp.status}")
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith(ALLOWED_TYPES):
                raise FetchError(f"content type {ctype or 'unknown'} is not supported")
            body = bytearray()
            # read1 returns whatever arrived, so the deadline is checked even when a server trickles a byte at a time.
            while chunk := resp.read1(65536):
                body += chunk
                sock.settimeout(max(0.5, min(5.0, end - time.monotonic())))
                if len(body) > MAX_BYTES:
                    raise FetchError("document larger than 10 MB")
                if time.monotonic() > end:
                    raise FetchError("download took longer than 20 s")
            return bytes(body), ctype, url
        finally:
            sock.close()
    raise FetchError("too many redirects")


if __name__ == "__main__":  # self-check of the address rules (no network needed for most)
    for bad in ("ftp://x.org/", "http://user:pw@example.org/", "http://example.org:8080/"):
        try:
            check_url(bad)
            raise SystemExit(f"accepted {bad}")
        except FetchError:
            pass
    for host in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254", "100.64.0.1", "::1", "::ffff:10.0.0.1", "localhost"):
        try:
            public_ip(host, 80)
            raise SystemExit(f"accepted internal {host}")
        except FetchError:
            pass
    print("ok")
