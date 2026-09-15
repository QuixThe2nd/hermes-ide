"""Port occupancy and viewer health probes.

Reconcile must never fight a viewer that is already serving on the configured
port: this homelab (and any other box where someone started ``claude-viewer``
by hand) may already run one under a foreign unit name. Starting a second
copy would just crash-loop on ``Address already in use``, so the probe result
decides whether reconcile installs a unit at all.
"""

from __future__ import annotations

import errno
import http.client
import socket
from dataclasses import dataclass
from typing import Optional, Tuple

from tools.claude_viewer_url import viewer_bind, viewer_port

# ui.html's <title>; a root response carrying it is a real claude-viewer.
_UI_TITLE_MARKER = b"Claude viewer"
_PROBE_TIMEOUT_SEC = 2.0

FOREIGN = "foreign"
HEALTHY = "healthy"
FREE = "free"


@dataclass(frozen=True)
class PortState:
    """Outcome of probing the configured bind address."""

    status: str  # FREE | HEALTHY | FOREIGN
    detail: str = ""

    @property
    def occupied(self) -> bool:
        return self.status != FREE

    @property
    def healthy(self) -> bool:
        return self.status == HEALTHY


def _probe_bind_address(bind: Optional[str], port: int) -> Tuple[str, ...]:
    """Addresses to bind-probe, most specific first."""
    if bind and bind not in ("0.0.0.0", "::", ""):
        return (bind,)
    return ("127.0.0.1", "0.0.0.0")


def port_in_use(
    port: Optional[int] = None,
    *,
    bind: Optional[str] = None,
) -> bool:
    """True when something already listens on *port* at *bind*.

    A bind attempt (``SO_REUSEADDR``, no ``SO_REUSEPORT``) consults only the
    local socket table, so it cannot disturb an existing listener.
    """
    port = int(port if port is not None else viewer_port())
    last_error: Optional[OSError] = None
    for addr in _probe_bind_address(bind or viewer_bind(), port):
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(_PROBE_TIMEOUT_SEC)
            sock.bind((addr, port))
            return False  # a bind succeeded somewhere -> port is free
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES, errno.EADDRNOTAVAIL):
                last_error = exc
                continue
            last_error = exc
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    if last_error is not None and last_error.errno in (
        errno.EADDRINUSE,
        errno.EACCES,
    ):
        return True
    # Nothing bound and nothing definitively refused: treat as free. A
    # permission failure on a privileged bind must not wedge gateway start.
    return False


def probe_viewer_health(
    port: Optional[int] = None,
    *,
    host: str = "127.0.0.1",
    timeout: float = _PROBE_TIMEOUT_SEC,
) -> Tuple[bool, str]:
    """Return ``(is_healthy_viewer, detail)`` for the listener on *port*."""
    port = int(port if port is not None else viewer_port())
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", "/")
            response = conn.getresponse()
            status = response.status
            body = response.read(4096)
        finally:
            conn.close()
    except OSError as exc:
        return False, f"http probe failed: {exc.strerror or exc}"
    if status != 200:
        return False, f"http status {status}"
    if _UI_TITLE_MARKER not in body:
        return False, "response is not the claude-viewer UI"
    return True, "claude-viewer UI served"


def probe_port_state(
    port: Optional[int] = None,
    *,
    bind: Optional[str] = None,
    in_use_fn=None,
    health_fn=None,
) -> PortState:
    """Classify the configured port as free / healthy viewer / foreign listener."""
    port = int(port if port is not None else viewer_port())
    in_use_fn = in_use_fn or port_in_use
    if not in_use_fn(port, bind=bind):
        return PortState(FREE, "port is free")
    health_fn = health_fn or probe_viewer_health
    healthy, detail = health_fn(port)
    if healthy:
        return PortState(HEALTHY, detail)
    return PortState(FOREIGN, detail)
