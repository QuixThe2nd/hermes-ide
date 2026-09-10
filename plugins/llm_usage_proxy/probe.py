"""Port occupancy and proxy identity probes.

Two separate concerns:

* **Port ownership** — reconcile must never fight a listener that is already
  serving on the configured port, and must never adopt or stop a listener
  that belongs to another profile or another service entirely.
* **Identity** — a generic ``{"ok": true}`` health answer is not sufficient
  identity to trust with bearer credentials. Before routing any traffic,
  ``/health`` must answer with this service's id/version, this profile's
  identity token, and exactly the route mapping the caller expects; anything
  else is a foreign listener and traffic stays direct (unmetered, visible).
"""

from __future__ import annotations

import errno
import http.client
import json
import socket
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

from plugins.llm_usage_proxy.server import BIND_HOST, PROTOCOL_VERSION, SERVICE_ID

_PROBE_TIMEOUT_SEC = 2.0

FOREIGN = "foreign"
HEALTHY = "healthy"
FREE = "free"
STALE = "stale"  # this profile's own proxy, but serving a superseded argv


@dataclass(frozen=True)
class PortState:
    """Outcome of probing the configured port."""

    status: str  # FREE | HEALTHY | FOREIGN | STALE
    detail: str = ""

    @property
    def occupied(self) -> bool:
        return self.status != FREE

    @property
    def healthy(self) -> bool:
        return self.status == HEALTHY

    @property
    def stale(self) -> bool:
        return self.status == STALE


def port_in_use(port: int, *, bind: str = BIND_HOST) -> bool:
    """True when something already listens on *port* at *bind*.

    A bind attempt (``SO_REUSEADDR``, no ``SO_REUSEPORT``) consults only the
    local socket table, so it cannot disturb an existing listener.
    """
    last_error: Optional[OSError] = None
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(_PROBE_TIMEOUT_SEC)
        sock.bind((bind, int(port)))
        return False
    except OSError as exc:
        last_error = exc
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return bool(
        last_error is not None
        and last_error.errno in (errno.EADDRINUSE, errno.EACCES)
    )


def classify_proxy_health(
    port: int,
    *,
    host: str = BIND_HOST,
    timeout: float = _PROBE_TIMEOUT_SEC,
    expect_identity: Optional[str] = None,
    expect_routes: Optional[Mapping[str, str]] = None,
    expect_manage_keys: Optional[bool] = None,
) -> Tuple[str, str]:
    """Classify the listener on *port*: HEALTHY, STALE, or FOREIGN.

    *HEALTHY* answers with this service's id and protocol version, this
    profile's identity token, and exactly the route table and key-manager
    mode the caller expects. *STALE* is the same proxy with a superseded
    argv — a route table or key-manager flag that changed after it started —
    which is this profile's own unit and therefore safe to restart, never a
    listener to fight. Anything else is *FOREIGN*.
    """
    try:
        conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            status = response.status
            body = response.read(16384)
        finally:
            conn.close()
    except OSError as exc:
        return FOREIGN, f"http probe failed: {exc.strerror or exc}"
    if status != 200:
        return FOREIGN, f"http status {status}"
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return FOREIGN, "health response is not JSON"
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return FOREIGN, "health response is not a healthy payload"
    if payload.get("service") != SERVICE_ID:
        return FOREIGN, f"listener service is {payload.get('service')!r}, not {SERVICE_ID!r}"
    if payload.get("version") != PROTOCOL_VERSION:
        return FOREIGN, (
            f"listener protocol version {payload.get('version')!r}"
            f" != {PROTOCOL_VERSION!r}"
        )
    if expect_identity is not None and payload.get("identity") != expect_identity:
        return FOREIGN, "listener belongs to a different profile"
    if expect_routes is not None:
        routes = payload.get("routes")
        if not isinstance(routes, dict) or routes != dict(expect_routes):
            return STALE, (
                "this profile's usage proxy is serving a superseded route table"
            )
    if expect_manage_keys is not None and bool(
        payload.get("manage_keys")
    ) != bool(expect_manage_keys):
        return STALE, (
            "this profile's usage proxy is running without the key-manager"
            " mode this profile's config asks for"
            if expect_manage_keys
            else "this profile's usage proxy is managing keys although this"
            " profile's config turned key-manager mode off"
        )
    return HEALTHY, "usage proxy /health verified (service, identity, routes)"


def probe_proxy_health(
    port: int,
    *,
    host: str = BIND_HOST,
    timeout: float = _PROBE_TIMEOUT_SEC,
    expect_identity: Optional[str] = None,
    expect_routes: Optional[Mapping[str, str]] = None,
    expect_manage_keys: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Return ``(is_our_verified_proxy, detail)`` for the listener on *port*.

    Verified means: 200, JSON, ``ok`` true, the right service id and protocol
    version, the expected profile identity token, and — when the caller
    passed one — exactly the expected route table. A listener that answers
    ``{"ok": true}`` without the rest is foreign by definition.
    """
    state, detail = classify_proxy_health(
        port,
        host=host,
        timeout=timeout,
        expect_identity=expect_identity,
        expect_routes=expect_routes,
        expect_manage_keys=expect_manage_keys,
    )
    return state == HEALTHY, detail


def probe_port_state(
    port: int,
    *,
    in_use_fn: Callable[[int], bool] = port_in_use,
    health_fn: Callable[..., Tuple[str, str]] = classify_proxy_health,
    expect_identity: Optional[str] = None,
    expect_routes: Optional[Mapping[str, str]] = None,
    expect_manage_keys: Optional[bool] = None,
) -> PortState:
    """Classify the configured port as free / verified / stale / foreign."""
    if not in_use_fn(port):
        return PortState(FREE, "port is free")
    state, detail = health_fn(
        port,
        expect_identity=expect_identity,
        expect_routes=expect_routes,
        expect_manage_keys=expect_manage_keys,
    )
    return PortState(state, detail)


def wait_for_verified_health(
    port: int,
    *,
    timeout: float = 8.0,
    poll_interval: float = 0.25,
    health_fn: Callable[..., Tuple[str, str]] = classify_proxy_health,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    expect_identity: Optional[str] = None,
    expect_routes: Optional[Mapping[str, str]] = None,
    expect_manage_keys: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Poll /health until the proxy verifies or *timeout* elapses."""
    deadline = monotonic() + timeout
    detail = ""
    while True:
        state, detail = health_fn(
            port,
            expect_identity=expect_identity,
            expect_routes=expect_routes,
            expect_manage_keys=expect_manage_keys,
        )
        if state == HEALTHY:
            return True, detail
        if monotonic() >= deadline:
            return False, detail
        sleep_fn(poll_interval)
