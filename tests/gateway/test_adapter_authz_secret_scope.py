"""Adapter authz gates must read GATEWAY_/PLATFORM_ allow-all + allowlists through the profile
secret scope (#77548 cluster; #72348 / #86905 precedent).

Under ``gateway.multiplex_profiles`` ``os.environ`` holds the DEFAULT profile's values. A raw
``os.getenv`` in an adapter's own gate let the default's ``GATEWAY_ALLOW_ALL_USERS=true`` open every
secondary WhatsApp/Matrix bot, and the default's allowlist decide
who may approve on a secondary bot. Two invariants per adapter: the default env never answers a
scoped gate; the scope's own opt-in/allowlist does.
"""

import contextlib
from types import SimpleNamespace

import pytest

from agent import secret_scope as ss
from gateway.config import PlatformConfig

_DEFAULT_ENV = {
    "GATEWAY_ALLOW_ALL_USERS": "true", "GATEWAY_ALLOWED_USERS": "default-admin",
    "MATRIX_ALLOWED_USERS": "@default-admin:example.org", "MATRIX_IGNORE_USER_PATTERNS": r"^@spam:.*",
    "WHATSAPP_ALLOWED_USERS": "+15550001111", }


@pytest.fixture
def default_env(monkeypatch):
    """Multiplex on; os.environ carries the DEFAULT profile's permissive authz."""
    for name, value in _DEFAULT_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(ss, "_MULTIPLEX_ACTIVE", True)


@contextlib.contextmanager
def _scope(secrets):
    token = ss.set_secret_scope(secrets)
    try:
        yield
    finally:
        ss.reset_secret_scope(token)


def _matrix(extra=None):
    from plugins.platforms.matrix.adapter import MatrixAdapter

    return MatrixAdapter(PlatformConfig(enabled=True, token="t", extra={"homeserver": "https://m.example",
                                                                       "user_id": "@bot:m.example", **(extra or {})}))


def _whatsapp():
    from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin

    host = WhatsAppBehaviorMixin()
    host._dm_allowlist_source, host._allow_from = "WHATSAPP_ALLOWED_USERS", set()
    return host


# (label, opt-in the SECONDARY scope grants, gate(scope) -> observed value, closed value, open value)
_GATES = [
    ("whatsapp.open_dm", {"GATEWAY_ALLOW_ALL_USERS": "true"},
     lambda: __import__("gateway.platforms.whatsapp_common", fromlist=["WhatsAppBehaviorMixin"]).WhatsAppBehaviorMixin._open_dm_opted_in(_whatsapp()),
     False, True),
    ("whatsapp.live_allow_from", {"WHATSAPP_ALLOWED_USERS": "+15559998888"},
     lambda: __import__("gateway.platforms.whatsapp_common", fromlist=["WhatsAppBehaviorMixin"]).WhatsAppBehaviorMixin._live_dm_allow_from(_whatsapp()),
     set(), {"+15559998888"}),
    ("matrix.authorized_user", {"MATRIX_ALLOWED_USERS": "@bot2-admin:example.org"},
     lambda: _matrix()._is_authorized_user("@bot2-admin:example.org"),
     False, True),
    ("matrix.ignored_patterns", {"MATRIX_IGNORE_USER_PATTERNS": r"^@bot2-spam:.*"},
     lambda: [p.pattern for p in _matrix()._ignored_user_patterns],
     [], [r"^@bot2-spam:.*"]),
]


@pytest.mark.parametrize("label,scope_opt_in,gate,closed,opened", _GATES, ids=[g[0] for g in _GATES])
def test_default_env_never_answers_a_scoped_gate(default_env, label, scope_opt_in, gate, closed, opened):
    """Secondary scope with NO opt-in: the default profile's permissive os.environ must not open it."""
    with _scope({}):
        assert gate() == closed


@pytest.mark.parametrize("label,scope_opt_in,gate,closed,opened", _GATES, ids=[g[0] for g in _GATES])
def test_scope_opt_in_opens_the_gate(default_env, monkeypatch, label, scope_opt_in, gate, closed, opened):
    """The secondary's own .env decides — even when the default profile's env is silent."""
    for name in _DEFAULT_ENV:
        monkeypatch.delenv(name, raising=False)
    with _scope(scope_opt_in):
        assert gate() == opened
