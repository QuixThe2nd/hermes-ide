"""Host binding and trust helpers for the mission-control HTTP server:
loopback detection, hostname validation and the default trusted-host
set.

Moved byte-identical from server.py.
"""

import ipaddress
import re
import socket
def _is_loopback(host):
    """True when host binds a loopback interface only (this machine).

    An empty host is a wildcard bind (every interface), NOT loopback —
    only the explicit names count: "localhost", 127.0.0.0/8, ::1."""
    name = str(host or "").strip()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:
        return False  # a hostname, wildcard, or non-loopback address


# ---- trusted Host and origin binding ---------------------------------
# DNS-rebinding defense: a request's own Host header proves nothing,
# because the connection arriving on our socket may be an attacker
# domain rebound to our address. Every HTML page this server emits
# carries the CSRF token and every state-changing route accepts it, so
# both token-bearing pages and POSTs are refused unless the Host names
# an address this server deliberately serves. The default trusted set
# is derived from the configured bind address; Forwarded /
# X-Forwarded-* are never consulted — without an explicitly trusted
# proxy those headers are client-controlled, and this server has no
# proxy-trust mechanism at all.

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
WILDCARD_BINDS = ("", "*", "::", "0.0.0.0")
# One hostname label: letters/digits/hyphen/underscore, no leading or
# trailing hyphen; dots join labels. Anything else a Host header might
# carry is refused before it can match anything.
HOSTNAME_RE = re.compile(
    r"^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
    r"(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*$")

# The themed 421 page: Host refused before anything token-bearing or
# state-changing is considered.
MISDIRECTED_HTML = (
    "<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
    "<title>Misdirected request</title>\n</head>\n<body>\n"
    "<h1>This address is not served here.</h1>\n"
    "<p>The request named a host this server was not configured to "
    "serve.</p>\n</body>\n</html>\n").encode("utf-8")


def _normalize_host(value):
    """Host header / bind spelling -> one comparable host string, or
    None.

    Strips the port (an IPv6 literal only in its bracketed form, per
    RFC 7230 — a bare unbracketed one is refused), lowercases, drops
    one trailing dot, and canonicalizes IP literals through
    ipaddress so ::1, [::1] and 0:0:0:0:0:0:0:1 all compare equal.
    None means the value is not a host this parser accepts — garbage,
    whitespace, delimiters — and can never match a trusted entry."""
    if not isinstance(value, str):
        return None
    host = value.strip()
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        name = host[1:end]
        rest = host[end + 1:]
        if rest and not rest.startswith(":"):
            return None
    else:
        host_part, sep, port_part = host.partition(":")
        if sep:
            # An explicit port: exactly one, digits only. Anything else
            # where the port belongs — including a second colon, i.e. a
            # bare unbracketed IPv6 literal — is refused rather than
            # silently truncated to its first segment.
            if ":" in port_part or not port_part.isdigit():
                return None
        name = host_part
    name = name.strip().rstrip(".").lower()
    if not name or any(ch.isspace() or ch in '/\\?#@,%"' for ch in name):
        return None
    try:
        return str(ipaddress.ip_address(name))
    except ValueError:
        return name if HOSTNAME_RE.fullmatch(name) else None


def _local_interface_ips():
    """Best-effort set of this machine's own interface addresses.

    Used only to size the default trusted set for a wildcard bind —
    which genuinely answers on every one of them. Any failure (no
    resolver, odd platform) yields an empty set: the trusted set stays
    smaller and access uses a name that is listed."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            raw = info[4][0].split("%", 1)[0]
            try:
                ips.add(str(ipaddress.ip_address(raw)))
            except ValueError:
                pass
    except OSError:
        pass
    return ips


def _default_trusted_hosts(bind_host):
    """Trusted Host set for one configured bind address.

    A loopback bind (the default) trusts the loopback spellings —
    localhost, 127.0.0.1, ::1 — since any of them may be how the user
    reaches the socket and none can name another machine. A wildcard
    bind answers on every interface, so it also trusts this machine's
    own interface addresses. An explicit non-loopback address trusts
    exactly itself: binding a LAN IP is a deliberate act, and reaching
    that socket under any other name needs an explicit --trusted-host
    entry."""
    name = _normalize_host(bind_host)
    if name is None or name in WILDCARD_BINDS:
        return set(LOOPBACK_HOSTS) | _local_interface_ips()
    if name == "localhost":
        return set(LOOPBACK_HOSTS)
    try:
        if ipaddress.ip_address(name).is_loopback:
            return set(LOOPBACK_HOSTS) | {name}
    except ValueError:
        pass
    return {name}


def _server_trusted_hosts(server):
    """The trusted Host set in force on one HTTP server instance.

    main() pins the CLI-derived set (bind-derived defaults plus any
    explicit --trusted-host entries) onto the instance; a
    directly-constructed server — as the tests build — derives the set
    from its own bound address, cached on the instance after the first
    ask so a request never re-runs interface enumeration."""
    pinned = getattr(server, "trusted_hosts", None)
    if pinned is not None:
        return pinned
    cached = getattr(server, "_trusted_hosts_cache", None)
    if cached is None:
        try:
            bind = server.server_address[0]
        except (AttributeError, IndexError):
            bind = "127.0.0.1"
        cached = _default_trusted_hosts(bind)
        try:
            server._trusted_hosts_cache = cached
        except Exception:
            pass
    return cached
