"""Mission-only WhatsApp DMs (``mission_only_dms``) authorization gate.

Opt-in, default off. With ``platforms.<whatsapp|whatsapp_cloud>.extra.
mission_only_dms: true`` a WhatsApp sender is authorized only while an
assistant-mission is bound to their chat — the allowlist, the pairing
store, and allow-all flags cannot override the denial. Absent or ``false``
keeps the exact current allowlist policy.
"""

import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "WHATSAPP_ALLOWED_USERS",
        "WHATSAPP_CLOUD_ALLOWED_USERS",
        "WHATSAPP_ALLOW_ALL_USERS",
        "WHATSAPP_CLOUD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_runner(extra, platform=Platform.WHATSAPP):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True, extra=dict(extra))}
    )
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.pairing_store = None
    runner.pairing_stores = {}
    return runner


def _make_source(
    user_id="61491234567@s.whatsapp.net",
    platform=Platform.WHATSAPP,
    **kwargs,
):
    kwargs.setdefault("chat_id", user_id)
    return SessionSource(
        platform=platform,
        chat_type="dm",
        user_id=user_id,
        user_name="Contact",
        is_bot=False,
        **kwargs,
    )


def _stub_missions(monkeypatch, missions):
    """Install a fake ``plugins.missions`` module; return the lookup call log.

    A mission binds the identifiers in its ``chat_id`` plus ``aliases``;
    matching here is verbatim — the unit under test is which identifiers the
    authz gate consults, not the mission store's own canonicalization.
    """
    calls = []
    mod = types.ModuleType("plugins.missions")

    def find_active_mission_for_chat(chat_id):
        calls.append(str(chat_id))
        for mission in missions:
            bound = {str(mission.get("chat_id") or "")} | {
                str(alias or "") for alias in (mission.get("aliases") or [])
            }
            if str(chat_id) in bound:
                return mission
        return None

    mod.find_active_mission_for_chat = find_active_mission_for_chat
    monkeypatch.setitem(sys.modules, "plugins.missions", mod)
    return calls


# --------------------------------------------------------------------------
# flag on: allowlist / pairing / allow-all cannot override the denial
# --------------------------------------------------------------------------

def test_mission_only_denies_allowlisted_sender_without_mission(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "61491234567@s.whatsapp.net")
    runner = _make_runner({"mission_only_dms": True})
    _stub_missions(monkeypatch, [])

    assert runner._is_user_authorized(_make_source()) is False


def test_mission_only_allows_allowlisted_sender_with_mission(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "61491234567@s.whatsapp.net")
    runner = _make_runner({"mission_only_dms": True})
    _stub_missions(
        monkeypatch,
        [{"mission_id": "m1", "status": "active", "chat_id": "61491234567@s.whatsapp.net"}],
    )

    assert runner._is_user_authorized(_make_source()) is True


def test_mission_only_overrides_allow_all(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")
    runner = _make_runner({"mission_only_dms": True})
    _stub_missions(monkeypatch, [])

    assert runner._is_user_authorized(_make_source()) is False


def test_mission_only_overrides_pairing_approval(monkeypatch):
    runner = _make_runner({"mission_only_dms": True})
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: True)
    _stub_missions(monkeypatch, [])

    assert runner._is_user_authorized(_make_source()) is False


def test_mission_bound_sender_authorized_via_pairing(monkeypatch):
    """Real mission flow: the plugin approves the contact on the serving
    profile's pairing store, so a mission-bound contact without an allowlist
    entry stays authorized while the mission runs."""
    runner = _make_runner({"mission_only_dms": True})
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: True)
    _stub_missions(
        monkeypatch,
        [{"mission_id": "m1", "status": "active", "chat_id": "61491234567@s.whatsapp.net"}],
    )

    assert runner._is_user_authorized(_make_source()) is True


def test_mission_only_fails_closed_without_missions_plugin(monkeypatch):
    """Plugin absent + flag on → treated as no active mission → deny."""
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "61491234567@s.whatsapp.net")
    runner = _make_runner({"mission_only_dms": True})
    monkeypatch.delitem(sys.modules, "plugins.missions", raising=False)

    assert runner._is_user_authorized(_make_source()) is False


# --------------------------------------------------------------------------
# identifier coverage
# --------------------------------------------------------------------------

def test_mission_bound_to_alt_identifier_authorizes_sender(monkeypatch):
    """A mission keyed by the LID form in user_id_alt must still match."""
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "61491234567:47@s.whatsapp.net")
    runner = _make_runner({"mission_only_dms": True})
    _stub_missions(
        monkeypatch,
        [{"mission_id": "m1", "status": "active", "chat_id": "987654321@lid"}],
    )

    assert (
        runner._is_user_authorized(
            _make_source(user_id="61491234567:47@s.whatsapp.net", user_id_alt="987654321@lid")
        )
        is True
    )


def test_gate_consults_all_source_identifiers(monkeypatch):
    runner = _make_runner({"mission_only_dms": True})
    calls = _stub_missions(monkeypatch, [])
    source = _make_source(
        user_id="u@x",
        chat_id="c@x",
        chat_id_alt="calt",
        user_id_alt="ualt",
    )

    runner._is_user_authorized(source)
    assert set(calls) == {"c@x", "u@x", "calt", "ualt"}


# --------------------------------------------------------------------------
# flag off: current allowlist behavior unchanged
# --------------------------------------------------------------------------

def test_flag_absent_keeps_allowlist_authorization(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "61491234567@s.whatsapp.net")
    runner = _make_runner({})
    _stub_missions(monkeypatch, [])

    assert runner._is_user_authorized(_make_source()) is True


def test_flag_false_keeps_allowlist_authorization(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "61491234567@s.whatsapp.net")
    runner = _make_runner({"mission_only_dms": False})
    _stub_missions(monkeypatch, [])

    assert runner._is_user_authorized(_make_source()) is True


def test_flag_absent_ignores_mission_state(monkeypatch):
    """Without the flag a mission must not change (nor be needed for) authz."""
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "61491234567@s.whatsapp.net")
    runner = _make_runner({})
    calls = _stub_missions(
        monkeypatch,
        [{"mission_id": "m1", "status": "active", "chat_id": "61491234567@s.whatsapp.net"}],
    )

    assert runner._is_user_authorized(_make_source()) is True
    assert calls == []  # missions never consulted when the flag is off


# --------------------------------------------------------------------------
# whatsapp_cloud variant + platform scoping
# --------------------------------------------------------------------------

def test_whatsapp_cloud_mission_only_dms(monkeypatch):
    monkeypatch.setenv("WHATSAPP_CLOUD_ALLOWED_USERS", "61491234567")
    runner = _make_runner({"mission_only_dms": True}, platform=Platform.WHATSAPP_CLOUD)
    source = _make_source(user_id="61491234567", platform=Platform.WHATSAPP_CLOUD)

    assert runner._is_user_authorized(source) is False

    _stub_missions(
        monkeypatch,
        [{"mission_id": "m1", "status": "active", "chat_id": "61491234567"}],
    )
    assert runner._is_user_authorized(source) is True


def test_whatsapp_flag_does_not_gate_whatsapp_cloud(monkeypatch):
    """The whatsapp flag must not deny whatsapp_cloud traffic."""
    monkeypatch.setenv("WHATSAPP_CLOUD_ALLOWED_USERS", "61491234567")
    runner = _make_runner({"mission_only_dms": True})  # config on whatsapp only
    _stub_missions(monkeypatch, [])

    assert (
        runner._is_user_authorized(
            _make_source(user_id="61491234567", platform=Platform.WHATSAPP_CLOUD)
        )
        is True
    )


# --------------------------------------------------------------------------
# profile scoping: the default profile's flag must not gate a secondary one
# --------------------------------------------------------------------------

def _make_multiplex_runner(default_extra, assistant_extra):
    """A runner whose default profile owns ``config`` and assistant owns an adapter."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.WHATSAPP: PlatformConfig(enabled=True, extra=dict(default_extra))
        }
    )
    runner.adapters = {}
    runner._profile_adapters = {
        "assistant": {
            Platform.WHATSAPP: SimpleNamespace(
                config=SimpleNamespace(extra=dict(assistant_extra))
            )
        }
    }
    runner.pairing_store = None
    runner.pairing_stores = {}
    return runner


def test_default_profile_flag_does_not_gate_secondary_profile(monkeypatch):
    """Only the config the profile actually owns may turn the gate on.

    The assistant profile's own config leaves ``mission_only_dms`` unset, so
    its DMs must keep the allow-all policy even though the DEFAULT profile
    configured the flag — ``self.config`` describes the default profile, not
    this one (same cross-profile leak class as #72348).
    """
    monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")
    runner = _make_multiplex_runner({"mission_only_dms": True}, {})
    _stub_missions(monkeypatch, [])

    assert (
        runner._is_user_authorized(
            _make_source(user_id="61491234567@s.whatsapp.net", profile="assistant")
        )
        is True
    )


def test_secondary_profile_flag_gates_only_that_profile(monkeypatch):
    """A secondary profile opting in is gated; the default profile is not."""
    monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")
    runner = _make_multiplex_runner({}, {"mission_only_dms": True})
    _stub_missions(monkeypatch, [])

    assert (
        runner._is_user_authorized(
            _make_source(user_id="61491234567@s.whatsapp.net", profile="assistant")
        )
        is False
    )
    assert runner._is_user_authorized(_make_source()) is True
