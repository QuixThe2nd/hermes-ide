"""In-memory broker for the native Discord secure-login entry flow.

When a human Discord requester triggers ``browser_vault_save_login``, the
tool's ordinary ``SaveLoginPrompt`` callback (``agent/vault_backends/unlock``)
is — for that turn — bound to this broker. The flow:

1. The gateway sends a requester-only Discord interaction into the
   originating thread (``plugins/platforms/discord/secure_entry.py``).
2. Only a click by the initiating requester (``interaction.user.id``) mints a
   ≥256-bit capability and delivers the HTTPS form link EPHEMERALLY to that
   user. The capability travels as a URL *fragment* — it never reaches the
   server on the GET, never appears in a shared message, and is stored here
   only as a SHA-256 hash.
3. The short-lived form (served by the existing gateway API listener, see
   ``gateway/platforms/api_server_secure_entry.py``) POSTs the fragment
   capability plus identifier/password to the exact same-origin endpoint.
   Redemption is single-use and atomic; the request context (profile home,
   session, requester, site origin) is fixed server-side at mint time.
4. The identifier/password live ONLY in this process's memory until the
   owning tool (``tools/browser_vault_tool.browser_vault_save_login``)
   consumes them via the callback and hands them to the existing encrypted
   vault store. Nothing is written to disk, env, argv, URLs, or logs.

Disabled unless ``gateway.secure_entry.public_base_url`` is configured to an
exact HTTPS origin (operator provides TLS ingress). No parallel vault and no
standalone credential service: the ordinary save path stays the owner.

Cancellation/lifetime fencing: every request is owned by exactly one turn
(``owner_token`` minted by ``install_turn_prompt``) inside one profile home.
Turn end / /stop / shutdown cancels only that turn's own requests plus this
home's session requests — never a newer sibling turn of the same session key
and never another home that happens to reuse the session id. The pending wait
and the redemption boundary both poll the owning turn's authoritative
"still current" signal, so a cancellation that wins the race can never be
followed by a save.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Absolute TTL: mint → redemption deadline. Hard cap 5 minutes.
DEFAULT_TTL_S = 300
MAX_TTL_S = 300
# Pending (pre-mint) requests live a little longer than a minted capability so
# a requester who never clicks cannot pin registry slots forever.
PENDING_TTL_S = 360
# A redeemed-but-unconsumed result (tool thread died mid-save) must not retain
# the secret indefinitely: it is wiped after this window.
CONSUME_WINDOW_S = 90
# Bounded registry: a gateway is one operator, a handful of pending saves is abuse.
MAX_PENDING = 32
MAX_IDENTIFIER_LEN = 320
MAX_PASSWORD_LEN = 512
FORM_PATH = "/hermes-secure-entry/form"
SUBMIT_PATH = "/hermes-secure-entry/submit"
SITE_PATH = "/hermes-secure-entry/site"
CANCEL_PATH = "/hermes-secure-entry/cancel"
# Same rough cap the HTTP layer enforces on the JSON body.
MAX_BODY_BYTES = 16 * 1024

_LOCK = threading.RLock()
_REQUESTS: Dict[str, "_Request"] = {}
# (home, session_key, owner_token) -> request ids. owner_token is per-turn, so
# a session key alone never identifies a request: sibling turns and cross-home
# reuse of the same session id stay isolated.
_OWNED_INDEX: Dict[Tuple[str, str, str], List[str]] = {}
_warned_config = False


@dataclass
class _Request:
    request_id: str
    home: str                 # canonical profile home — request context is fixed at mint
    session_key: str
    owner_token: str          # per-turn token: only the owning turn cancels this request
    requester_user_id: str
    origin: str               # site origin the login is being saved for
    host: str
    cap_hash: str = ""        # sha256(capability); the raw capability is never stored
    status: str = "pending"   # pending -> delivered -> claimed | cancelled | expired
    expires_mono: float = 0.0
    result: Optional[Dict[str, str]] = field(default=None)
    event: threading.Event = field(default_factory=threading.Event)
    should_abort: Optional[Callable[[], bool]] = field(default=None)

    def owned_by(self, home: Optional[str], session_key: Optional[str],
                 owner_token: Optional[str]) -> bool:
        """Identity match where None means 'do not constrain on this field'."""
        if home is not None and self.home != home:
            return False
        if session_key is not None and self.session_key != session_key:
            return False
        if owner_token is not None and self.owner_token != owner_token:
            return False
        return True


def _now() -> float:
    return time.monotonic()


def _hash_capability(capability: str) -> str:
    return hashlib.sha256(capability.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONTROL_CHARS = set(
    "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
    "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f\x7f")
# Structural characters that must never appear inside a netloc (path/query/
# userinfo separators, or whitespace/controls smuggled into a host).
_FORBIDDEN_NETLOC_CHARS = _CONTROL_CHARS | set(" \t\r\n\v\f/?#[]@")


def _origin_from_config(raw: object) -> Optional[str]:
    """Fail-closed parse of the configured public origin.

    Accepts EXACTLY ``https://host[:port]`` — no path, query, fragment,
    userinfo (even an empty username), whitespace or control characters, and
    no unparseable port. Anything else is refused without being reflected.
    """
    try:
        original = str(raw or "")
    except Exception:
        return None
    # Leading/trailing whitespace is a config error, not something to trim silently.
    if original != original.strip() or not original.strip():
        return None
    text = original.strip()
    if any(c in _CONTROL_CHARS for c in text):
        return None
    try:
        parts = urlsplit(text)
        port = parts.port  # raises ValueError on an invalid port
    except Exception:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if parts.username is not None or parts.password is not None:
        return None  # userinfo forbidden — "https://@host" has username == ""
    if port is not None and not (0 < port < 65536):
        return None
    netloc = parts.netloc.rsplit("@", 1)[-1].lower()
    if not netloc or any(c in _FORBIDDEN_NETLOC_CHARS for c in netloc):
        return None
    return f"https://{netloc}"


def configured_origin() -> Optional[str]:
    """The operator-configured public HTTPS origin (``scheme://netloc``), or None.

    Refuses anything missing, malformed, or not exactly an https URL with a
    host and no path/query/fragment/userinfo. Warns once per process on a bad value.
    """
    global _warned_config
    try:
        from hermes_cli.config import cfg_get, load_config
        raw = cfg_get(load_config(), "gateway", "secure_entry", "public_base_url", default="")
    except Exception:
        return None
    origin = _origin_from_config(raw)
    if origin is None and str(raw or "").strip():
        if not _warned_config:
            _warned_config = True
            logger.warning(
                "gateway.secure_entry.public_base_url must be an exact HTTPS origin "
                "(e.g. https://host.example); secure login entry stays disabled.")
    return origin


def form_url(origin: str, capability: str) -> str:
    """The ephemeral form link: capability strictly in the fragment."""
    return f"{origin}{FORM_PATH}#{capability}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _purge_expired_locked(now: float) -> None:
    for rid in [rid for rid, r in _REQUESTS.items() if r.expires_mono and now > r.expires_mono]:
        req = _REQUESTS[rid]
        if req.status in ("pending", "delivered"):
            req.status = "expired"
        # Whatever the prior state, an expired request must not keep its secret.
        req.result = None
        req.event.set()
        _forget_locked(rid)


def _forget_locked(request_id: str) -> None:
    req = _REQUESTS.pop(request_id, None)
    if req is not None:
        key = (req.home, req.session_key, req.owner_token)
        bucket = _OWNED_INDEX.get(key)
        if bucket and request_id in bucket:
            bucket.remove(request_id)
            if not bucket:
                _OWNED_INDEX.pop(key, None)


def create_request(home: str, session_key: str, requester_user_id: str,
                   origin: str, host: str, *, owner_token: str = "",
                   should_abort: Optional[Callable[[], bool]] = None) -> Optional[_Request]:
    """Register a pending save-login request. Returns None when the registry is full."""
    with _LOCK:
        _purge_expired_locked(_now())
        if len(_REQUESTS) >= MAX_PENDING:
            return None
        req = _Request(
            request_id=secrets.token_urlsafe(16), home=str(home), session_key=str(session_key),
            owner_token=str(owner_token or secrets.token_urlsafe(8)),
            requester_user_id=str(requester_user_id), origin=str(origin), host=str(host),
            expires_mono=_now() + PENDING_TTL_S, should_abort=should_abort)
        _REQUESTS[req.request_id] = req
        _OWNED_INDEX.setdefault((req.home, req.session_key, req.owner_token), []).append(req.request_id)
        return req


def open_capability(request_id: str, *, ttl_s: int = DEFAULT_TTL_S) -> Optional[str]:
    """Mint the one-time capability for a pending request (requester clicked the
    authorized interaction). Marks it delivered with an absolute TTL. Single-use."""
    with _LOCK:
        req = _REQUESTS.get(request_id)
        if req is None or req.status != "pending":
            return None
        capability = secrets.token_urlsafe(32)  # 256 random bits
        req.cap_hash = _hash_capability(capability)
        req.status = "delivered"
        req.expires_mono = _now() + min(max(ttl_s, 1), MAX_TTL_S)
        return capability


def _find_delivered_by_capability_locked(cap_hash: str) -> Optional[_Request]:
    return next((r for r in _REQUESTS.values()
                 if r.status == "delivered" and r.cap_hash == cap_hash), None)


def redeem(capability: object, identifier: object, password: object) -> Tuple[bool, str]:
    """Atomic single redemption. Generic, secret-free error text only.

    Fails closed when the owning turn is no longer current (/stop, disconnect,
    generation change): the request is cancelled and nothing is retained.
    """
    with _LOCK:
        _purge_expired_locked(_now())
        if not isinstance(capability, str) or not isinstance(identifier, str) \
                or not isinstance(password, str):
            return False, "Invalid or expired link."
        cap_hash = _hash_capability(capability)
        req = _find_delivered_by_capability_locked(cap_hash)
        if req is None:
            return False, "Invalid or expired link."
        abort = req.should_abort
        if abort is not None:
            try:
                no_longer_current = bool(abort())
            except Exception:
                no_longer_current = True
            if no_longer_current:
                req.status = "cancelled"
                req.result = None
                req.event.set()
                _forget_locked(req.request_id)
                return False, "Invalid or expired link."
        # Claim exactly once: a second concurrent/racing POST finds status != delivered.
        req.status = "claimed"
        ident = identifier.strip()
        if not ident or not password or len(ident) > MAX_IDENTIFIER_LEN or len(password) > MAX_PASSWORD_LEN:
            req.result = None
            _forget_locked(req.request_id)
            return False, "Invalid or expired link."
        req.result = {"identifier": ident, "password": password}
        # Bounded consume window: a tool thread that never comes back must not
        # leave the secret sitting in memory past this deadline.
        req.expires_mono = _now() + CONSUME_WINDOW_S
        req.event.set()
        return True, ""


def wait_for_result(request_id: str, timeout_s: float,
                    should_abort: Optional[Callable[[], bool]] = None) -> Optional[Dict[str, str]]:
    """Block the calling tool thread until redemption, cancellation, or timeout.

    Returns ``{"identifier", "password"}`` (memory only; cleared on read) or None.
    The turn-lifecycle ``should_abort`` signal (as well as any per-request one)
    breaks the wait promptly: cancellation that wins the race always returns
    None — a late redemption cannot produce a save afterwards.
    """
    deadline = _now() + max(timeout_s, 0.0)
    while True:
        with _LOCK:
            req = _REQUESTS.get(request_id)
            if req is None or req.status in ("cancelled", "expired"):
                return None
            if req.status == "claimed" and req.result is not None:
                result, req.result = req.result, None
                _forget_locked(request_id)
                return result
            if req.should_abort is not None and should_abort is None:
                should_abort = req.should_abort
        if should_abort is not None:
            try:
                aborted = bool(should_abort())
            except Exception:
                aborted = True
            if aborted:
                cancel(request_id)
                return None
        remaining = deadline - _now()
        if remaining <= 0:
            cancel(request_id)
            return None
        # Poll in slices: responsive to cancel/expiry/turn interruption.
        time.sleep(min(0.25, remaining))


def cancel(request_id: str) -> bool:
    """Cancel a still-live request. Returns whether a live request was actually
    cancelled — False when it was already redeemed/consumed, cancelled, expired
    or never existed, so callers can word their UI truthfully (a consumed
    request's entry was already received and cannot be undone by a cancel)."""
    with _LOCK:
        req = _REQUESTS.get(request_id)
        if req is None:
            return False
        req.status = "cancelled"
        req.event.set()
        req.result = None
        _forget_locked(req.request_id)
        return True


def cancel_by_capability(capability: object) -> bool:
    """Form-side single-use cancellation: the capability itself retires the request."""
    if not isinstance(capability, str):
        return False
    with _LOCK:
        req = _find_delivered_by_capability_locked(_hash_capability(capability))
        if req is None:
            return False
        req.status = "cancelled"
        req.result = None
        req.event.set()
        _forget_locked(req.request_id)
        return True


def cancel_session(session_key: str, *, home: Optional[str] = None,
                   owner_token: Optional[str] = None) -> None:
    """Turn end / /stop / timeout / disconnect: drop this session's pending
    requests — scoped by home (profile) and optionally the owning turn, so a
    sibling turn of the same session that is still live is never cancelled."""
    with _LOCK:
        for req in [r for r in _REQUESTS.values()
                    if r.owned_by(home, session_key, owner_token)]:
            req.status = "cancelled"
            req.event.set()
            req.result = None
            _forget_locked(req.request_id)


def request_info(request_id: str) -> Optional[Dict[str, str]]:
    """Non-secret view of a request (for rendering the form; labels only)."""
    with _LOCK:
        req = _REQUESTS.get(request_id)
        if req is None:
            return None
        return {"status": req.status, "host": req.host, "origin": req.origin}


def find_by_capability(capability: object) -> Optional[Dict[str, str]]:
    """Non-secret info for the request a capability belongs to (form rendering).
    Non-consuming: the capability stays redeemable after this lookup."""
    if not isinstance(capability, str):
        return None
    with _LOCK:
        # An expired capability must fail closed even when no other broker call
        # has run the purge yet (the form's site lookup may be the only caller).
        _purge_expired_locked(_now())
        cap_hash = _hash_capability(capability)
        req = _find_delivered_by_capability_locked(cap_hash)
        if req is None:
            return None
        return {"status": req.status, "host": req.host, "origin": req.origin}


# ---------------------------------------------------------------------------
# Per-turn callback binding (gateway messaging surfaces)
# ---------------------------------------------------------------------------

def install_turn_prompt(runner, ctx) -> Optional[Callable[[], None]]:
    """Bind a save-login prompt for this turn when the surface supports it.

    Installs the thread-local ``SaveLoginPrompt`` on the calling (agent turn)
    thread and returns a restore callable, or returns None (and installs
    nothing — the tool keeps its precise prompt_unavailable refusal) when:
    the platform has no supported UI, the requester is unknown, the public
    HTTPS origin is unconfigured/malformed, or the API listener is down.

    The restore callable puts back the PREVIOUS callback (not None) and cancels
    every request this turn minted — nothing else's.
    """
    from agent.vault_backends.unlock import (
        get_save_login_prompt_callback, set_save_login_prompt_callback,
    )

    source = getattr(ctx, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None) or str(
        getattr(source, "platform", "") or "")
    if platform != "discord":
        return None
    requester = str(getattr(source, "user_id", "") or "").strip()
    if not requester:
        return None
    base = configured_origin()
    if base is None:
        return None
    adapter = getattr(ctx, "_status_adapter", None)
    if adapter is None or not callable(getattr(adapter, "_send_prompt", None)):
        return None
    # The form routes live on the API listener; without it the link would be dead.
    from gateway.config import Platform
    api = (getattr(runner, "adapters", {}) or {}).get(Platform.API_SERVER)
    if api is None or not getattr(api, "_app", None):
        return None

    from hermes_constants import get_hermes_home
    # Fixed at install time: later env churn cannot re-point this turn's saves.
    home = str(get_hermes_home())
    session_key = str(getattr(ctx, "session_key", "") or "")
    owner_token = secrets.token_urlsafe(8)
    # Authoritative turn-liveness signal (generation current + not interrupted):
    # the broker wants the inverse ("should this request be torn down?").
    still_current = getattr(ctx, "_run_still_current", None)
    if callable(still_current):
        def should_abort() -> bool:
            try:
                return not bool(still_current())
            except Exception:
                return True  # a broken liveness probe must fail closed
    else:
        should_abort = None

    def _prompt(origin: str, host: str) -> Optional[Dict[str, str]]:
        try:
            from plugins.platforms.discord.secure_entry import deliver_secure_entry_prompt
            return deliver_secure_entry_prompt(
                adapter=adapter,
                loop=getattr(ctx, "_loop_for_step", None),
                chat_id=getattr(ctx, "_status_chat_id", "") or "",
                metadata=dict(getattr(ctx, "_status_thread_metadata", {}) or {}),
                requester_user_id=requester,
                session_key=session_key,
                origin=origin,
                host=host,
                base_origin=base,
                home=home,
                owner_token=owner_token,
                should_abort=should_abort,
            )
        except Exception as exc:  # never leak secret material in exception text
            logger.warning("secure entry: save-login prompt delivery failed: %s", type(exc).__name__)
            return None

    previous_callback = get_save_login_prompt_callback()
    set_save_login_prompt_callback(_prompt)

    def _restore() -> None:
        # Put back exactly what was installed before this turn (may be another
        # surface's callback — never unconditionally None).
        set_save_login_prompt_callback(previous_callback)
        # Retire only THIS turn's requests: a still-live sibling turn of the
        # same session key keeps its pending requests.
        cancel_session(session_key, home=home, owner_token=owner_token)

    return _restore
