"""Single source of truth for omp-viewer bind/port and public URLs.

Mirrors ``tools.claude_viewer_url`` for the OMP lane viewer, scoped to the
``delegation.omp.viewer`` config section. The viewer is unauthenticated, so
it binds loopback by default; a ``bind``/``public_host`` override is the
operator's explicit choice to put it on a LAN/tailnet address.

Pure stdlib; importing it never touches the network and never raises — a
broken probe just falls through to the next candidate.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
from typing import Callable, List, Mapping, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

logger = logging.getLogger(__name__)

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8790
# Discovery is registry-first (plugins/omp_viewer/runs.py records every
# run's workdir as it starts), so the defaults carry no path at all;
# scan_roots stays an additive option for runs that predate the registry.
DEFAULT_SCAN_ROOTS: Tuple[str, ...] = ()

# A non-routable target: UDP connect() resolves the route locally, so this
# works with the interface table alone and no packet ever leaves the box.
_ROUTE_PROBE_TARGET = ("192.0.2.1", 9)  # TEST-NET-1, discard port
_TAILSCALE_TIMEOUT_SEC = 5.0

# Private networks a watch URL may point at. The viewer is unauthenticated,
# so the embed gate is the only thing keeping it off the public internet:
# loopback, RFC1918, and the Tailscale CGNAT range (100.64.0.0/10).
_ALLOWED_V4_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_ALLOWED_V6_NETS = (ipaddress.ip_network("::1/128"),)


def load_omp_viewer_section() -> dict:
    """Return the raw ``delegation.omp.viewer`` config section."""
    raw: object = None
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        delegation = cfg.get("delegation") or {}
        omp = delegation.get("omp") if isinstance(delegation, dict) else None
        raw = omp.get("viewer") if isinstance(omp, dict) else None
    except Exception:
        logger.debug("omp_viewer config load failed; using defaults", exc_info=True)
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    return raw


def _coerce_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def viewer_bind(cfg: Optional[Mapping] = None) -> str:
    raw = cfg if cfg is not None else load_omp_viewer_section()
    bind = str(raw.get("bind") or DEFAULT_BIND).strip()
    return bind or DEFAULT_BIND


def viewer_port(cfg: Optional[Mapping] = None) -> int:
    raw = cfg if cfg is not None else load_omp_viewer_section()
    return _coerce_int(raw.get("port"), DEFAULT_PORT)


def viewer_scan_roots(cfg: Optional[Mapping] = None) -> List[str]:
    """ADDITIVE extra directories scanned for ``.hermes-lane`` sessions.

    Empty by default: primary discovery is the per-home lane-run registry,
    so the viewer works out of the box on any install; scan roots keep
    pre-registry runs findable.
    """
    raw = cfg if cfg is not None else load_omp_viewer_section()
    listed = raw.get("scan_roots") or list(DEFAULT_SCAN_ROOTS)
    if isinstance(listed, str):
        listed = [listed]
    roots: List[str] = []
    for item in listed:
        root = str(item or "").strip()
        if root and root not in roots:
            roots.append(root)
    return roots or list(DEFAULT_SCAN_ROOTS)


def configured_public_host(cfg: Optional[Mapping] = None) -> str:
    """Return the explicit ``public_host`` override, or ``""`` (= auto)."""
    raw = cfg if cfg is not None else load_omp_viewer_section()
    return str(raw.get("public_host") or "").strip()


def extra_hosts(cfg: Optional[Mapping] = None) -> Tuple[str, ...]:
    """Additional hostnames/IPs the public URL may legitimately carry."""
    raw = cfg if cfg is not None else load_omp_viewer_section()
    listed = raw.get("extra_hosts") or []
    if isinstance(listed, str):
        listed = [listed]
    hosts: List[str] = []
    for item in listed:
        host = str(item or "").strip().lower()
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


# ---------------------------------------------------------------------------
# Host auto-detection
# ---------------------------------------------------------------------------


def _tailscale_ipv4(
    run_command: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Optional[str]:
    """Return this machine's Tailscale IPv4, or None when unavailable."""
    try:
        proc = run_command(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=_TAILSCALE_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if addr.version == 4:
            return candidate
    return None


def _default_route_ipv4(
    socket_factory: Callable[[], socket.socket] = socket.socket,
) -> Optional[str]:
    """Return the IPv4 of the default-route interface (never loopback).

    A UDP ``connect`` only consults the local routing table — no packet is
    sent — so this is safe to call at emit time on any box, online or not.
    """
    sock: Optional[socket.socket] = None
    try:
        sock = socket_factory()
        sock.settimeout(2.0)
        sock.connect(_ROUTE_PROBE_TARGET)
        host = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    if not host or host in ("0.0.0.0", "127.0.0.1"):
        return None
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    if addr.version != 4 or addr.is_loopback:
        return None
    return host


def detect_public_host(
    *,
    cfg: Optional[Mapping] = None,
    tailscale_fn: Optional[Callable[[], Optional[str]]] = None,
    route_fn: Optional[Callable[[], Optional[str]]] = None,
) -> str:
    """Resolve the host a viewer URL should carry, in precedence order.

    1. ``delegation.omp.viewer.public_host`` (explicit config)
    2. Tailscale IPv4 (reachable from a phone/laptop off-LAN)
    3. IPv4 of the default-route interface
    4. ``127.0.0.1``
    """
    explicit = configured_public_host(cfg)
    if explicit:
        return explicit
    if tailscale_fn is None:
        tailscale_fn = _tailscale_ipv4
    if route_fn is None:
        route_fn = _default_route_ipv4
    for probe in (tailscale_fn, route_fn):
        try:
            host = probe()
        except Exception:
            host = None
        if host:
            return str(host).strip()
    return "127.0.0.1"


def public_base_url(
    *, cfg: Optional[Mapping] = None, host: Optional[str] = None
) -> str:
    """Return ``http://<host>:<port>`` for this machine's viewer."""
    resolved = host if host is not None else detect_public_host(cfg=cfg)
    return f"http://{resolved}:{viewer_port(cfg)}"


# ---------------------------------------------------------------------------
# Watch URLs (lane progress links) + allowlist (Discord embed gate)
# ---------------------------------------------------------------------------


def watch_url(selector: str, *, cfg: Optional[Mapping] = None, host: Optional[str] = None) -> str:
    """Live page selecting one lane session dir: ``http://<host>:<port>/#<dir>``."""
    return f"{public_base_url(cfg=cfg, host=host)}/#{quote(str(selector), safe='/')}"


def _selector_ok(fragment: str) -> bool:
    """True when *fragment* decodes to an absolute, traversal-free directory.

    Raw whitespace is already refused at the URL level; a percent-escaped
    space (``%20``) is a legitimate directory byte the producer itself
    emits, so the decoded check targets control characters, not spaces.
    """
    decoded = unquote(fragment)
    if not decoded or "\x00" in decoded:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded):
        return False
    if not decoded.startswith("/"):
        return False
    return ".." not in decoded.split("/")


def _host_is_allowed(hostname: Optional[str], extras: Tuple[str, ...]) -> bool:
    if not hostname:
        return False
    if hostname.lower() in extras:
        return True
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return False  # bare DNS names are not accepted without config
    nets = _ALLOWED_V4_NETS if addr.version == 4 else _ALLOWED_V6_NETS
    return any(addr in net for net in nets)


def is_allowed_watch_url(url: str, *, cfg: Optional[Mapping] = None) -> bool:
    """True when *url* is a viewer URL safe to render as a platform card.

    Constraints: http/https, no userinfo, path ``/`` only, no query,
    fragment empty or an absolute traversal-free session directory, and a
    private-network host. The viewer is unauthenticated, so that host gate
    is what keeps a rendered card from pointing off the LAN/tailnet.
    """
    if not url or any(ch.isspace() for ch in url):
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return False
    decoded_path = unquote(parsed.path)
    if ".." in decoded_path or decoded_path != "/":
        return False
    if parsed.query:
        return False
    # urlparse drops a bare trailing "#", leaving fragment empty — test the
    # raw URL so a present-but-empty fragment is rejected rather than
    # slipping past the selector guard.
    if "#" in url and not _selector_ok(parsed.fragment):
        return False
    return _host_is_allowed(parsed.hostname, extra_hosts(cfg))
