"""Single source of truth for claude-viewer public URLs and host allowlists.

``delegate_claude_agent`` (``tools/claude_agent_tool.py``) *emits* a
watch-live URL in its mid-tool status line, and the Discord adapter
(``plugins/platforms/discord/adapter.py``) *validates* that URL before
rendering it as a branded embed. Both sides used to carry frozen copies of
one homelab's addresses, so every other install either pointed its embed at
a stranger's LAN or had it dropped by the allowlist. This module owns the
address instead:

* ``public_base_url()`` — ``http://<host>:<port>`` for *this* machine, from
  config (``delegation.claude_viewer.public_host``) or auto-detection
  (Tailscale IPv4 → default-route IPv4 → loopback).
* ``watch_url(stem)`` — the per-run page ``…/#<stem>``.
* ``is_allowed_watch_url(url)`` — the Discord-side gate: private-network
  hosts only (loopback / RFC1918 / Tailscale CGNAT / configured extras),
  no userinfo, path ``/`` only, no query, fragment empty or a run stem.

Pure stdlib; importing it never touches the network and never raises — a
broken probe just falls through to the next candidate.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import subprocess
from typing import Callable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

# Run-log stem: <YYYYMMDD>-<HHMMSS>-<pid>. The delegate log writer guarantees
# this shape, so a fragment that fails to fullmatch is not a page this viewer
# would serve.
RUN_STEM_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]+")

DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8787

# Private networks a watch URL may point at. The viewer is unauthenticated,
# so the embed gate is the only thing keeping it off the public internet:
# loopback, RFC1918, and the Tailscale CGNAT range (100.64.0.0/10).
_ALLOWED_V4_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",  # loopback
        "10.0.0.0/8",  # RFC1918
        "172.16.0.0/12",  # RFC1918
        "192.168.0.0/16",  # RFC1918
        "100.64.0.0/10",  # Tailscale CGNAT
    )
)
_ALLOWED_V6_NETS = (ipaddress.ip_network("::1/128"),)

# A non-routable target: UDP connect() resolves the route locally, so this
# works with the interface table alone and no packet ever leaves the box.
_ROUTE_PROBE_TARGET = ("192.0.2.1", 9)  # TEST-NET-1, discard port
_TAILSCALE_TIMEOUT_SEC = 5.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_claude_viewer_config() -> dict:
    """Return the normalized ``delegation.claude_viewer`` config section."""
    raw: object = None
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        delegation = cfg.get("delegation") or {}
        raw = delegation.get("claude_viewer") if isinstance(delegation, dict) else None
    except Exception:
        logger.debug("claude_viewer config load failed; using defaults", exc_info=True)
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


def viewer_bind(cfg: Optional[dict] = None) -> str:
    raw = cfg if cfg is not None else load_claude_viewer_config()
    bind = str(raw.get("bind") or DEFAULT_BIND).strip()
    return bind or DEFAULT_BIND


def viewer_port(cfg: Optional[dict] = None) -> int:
    raw = cfg if cfg is not None else load_claude_viewer_config()
    return _coerce_int(raw.get("port"), DEFAULT_PORT)


def configured_public_host(cfg: Optional[dict] = None) -> str:
    """Return the explicit ``public_host`` override, or ``""`` (= auto)."""
    raw = cfg if cfg is not None else load_claude_viewer_config()
    return str(raw.get("public_host") or "").strip()


def extra_hosts(cfg: Optional[dict] = None) -> Tuple[str, ...]:
    """Additional hostnames/IPs the Discord embed gate should accept."""
    raw = cfg if cfg is not None else load_claude_viewer_config()
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
    cfg: Optional[dict] = None,
    tailscale_fn: Optional[Callable[[], Optional[str]]] = None,
    route_fn: Optional[Callable[[], Optional[str]]] = None,
) -> str:
    """Resolve the host a watch URL should carry, in precedence order.

    1. ``delegation.claude_viewer.public_host`` (explicit config)
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


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def public_base_url(
    *, cfg: Optional[dict] = None, host: Optional[str] = None
) -> str:
    """Return ``http://<host>:<port>`` for this machine's viewer."""
    resolved = host if host is not None else detect_public_host(cfg=cfg)
    return f"http://{resolved}:{viewer_port(cfg)}"


def watch_url(stem: str, *, cfg: Optional[dict] = None, host: Optional[str] = None) -> str:
    """Return the live page for one run: ``http://<host>:<port>/#<stem>``."""
    return f"{public_base_url(cfg=cfg, host=host)}/#{stem}"


# ---------------------------------------------------------------------------
# Allowlist (Discord embed gate)
# ---------------------------------------------------------------------------


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


def is_allowed_watch_url(
    url: str, *, cfg: Optional[dict] = None
) -> bool:
    """True when *url* is a viewer URL safe to render as a Discord embed.

    Constraints: http/https, no userinfo, path ``/`` only, no query, no path
    traversal, fragment empty or a run stem, and a private-network host.
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
    # slipping past the stem guard.
    if "#" in url and not RUN_STEM_RE.fullmatch(parsed.fragment):
        return False
    return _host_is_allowed(parsed.hostname, extra_hosts(cfg))
