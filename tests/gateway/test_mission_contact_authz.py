"""Mission-contact authorization for never-routed inbound DMs.

When an assistant mission is dispatched, pairing approval is written to the
serving profile's store. A never-routed contact has no ``source.profile``, so
the global pairing store misses that grant and the contact is default-denied
before mission routing can select the profile. This path honors an active
same-platform mission for DM-like chats after the pairing check.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

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


def _make_runner(extra=None):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.WHATSAPP: PlatformConfig(
                enabled=True, extra=dict(extra or {})
            )
        }
    )
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
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
        chat_type=kwargs.pop("chat_type", "dm"),
        user_id=user_id,
        user_name="Contact",
        is_bot=False,
        **kwargs,
    )


def _stub_missions(monkeypatch, missions, *, find_active_group_mission=None):
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
            if mission.get("status") not in (None, "active"):
                continue
            bound = {str(mission.get("chat_id") or "")} | {
                str(alias or "") for alias in (mission.get("aliases") or [])
            }
            if str(chat_id) in bound:
                return mission
        return None

    mod.find_active_mission_for_chat = find_active_mission_for_chat
    mod.find_active_group_mission = find_active_group_mission or (lambda _cid: None)
    monkeypatch.setitem(sys.modules, "plugins.missions", mod)
    return calls


# --------------------------------------------------------------------------
# core grant: active same-platform mission authorizes unrouted DM
# --------------------------------------------------------------------------


@pytest.mark.parametrize("chat_type", [None, "", "dm"])
def test_active_mission_authorizes_unrouted_dm(monkeypatch, chat_type):
    user_id = "61491234567@s.whatsapp.net"
    runner = _make_runner()
    _stub_missions(
        monkeypatch,
        [
            {
                "mission_id": "m1",
                "status": "active",
                "platform": "whatsapp",
                "chat_id": user_id,
            }
        ],
    )

    assert (
        runner._is_user_authorized(
            _make_source(user_id=user_id, chat_type=chat_type)
        )
        is True
    )


def test_phone_jid_lookup_finds_lid_mission_via_aliases(monkeypatch):
    """Authz passes the inbound JID to the helper; alias matching is the helper's job."""
    phone_jid = "61491234567@s.whatsapp.net"
    lid = "987654321@lid"
    runner = _make_runner()
    _stub_missions(
        monkeypatch,
        [
            {
                "mission_id": "m1",
                "status": "active",
                "platform": "whatsapp",
                "chat_id": lid,
                "aliases": [phone_jid],
            }
        ],
    )

    assert runner._is_user_authorized(_make_source(user_id=phone_jid)) is True


def test_mismatched_platform_does_not_authorize(monkeypatch):
    user_id = "61491234567@s.whatsapp.net"
    runner = _make_runner()
    _stub_missions(
        monkeypatch,
        [
            {
                "mission_id": "m1",
                "status": "active",
                "platform": "telegram",
                "chat_id": user_id,
            }
        ],
    )

    assert runner._is_user_authorized(_make_source(user_id=user_id)) is False


def test_inactive_mission_does_not_authorize(monkeypatch):
    user_id = "61491234567@s.whatsapp.net"
    runner = _make_runner()
    _stub_missions(
        monkeypatch,
        [
            {
                "mission_id": "m1",
                "status": "completed",
                "platform": "whatsapp",
                "chat_id": user_id,
            }
        ],
    )

    assert runner._is_user_authorized(_make_source(user_id=user_id)) is False


def test_absent_mission_does_not_authorize(monkeypatch):
    runner = _make_runner()
    _stub_missions(monkeypatch, [])

    assert (
        runner._is_user_authorized(
            _make_source(user_id="61491234567@s.whatsapp.net")
        )
        is False
    )


def test_lookup_exception_fails_closed(monkeypatch):
    runner = _make_runner()
    mod = types.ModuleType("plugins.missions")

    def _boom(_chat_id):
        raise RuntimeError("store unreadable")

    mod.find_active_mission_for_chat = _boom
    mod.find_active_group_mission = lambda _cid: None
    monkeypatch.setitem(sys.modules, "plugins.missions", mod)

    assert (
        runner._is_user_authorized(
            _make_source(user_id="61491234567@s.whatsapp.net")
        )
        is False
    )


def test_user_id_lookup_exception_aborts_entire_mission_grant(monkeypatch):
    """A user_id lookup error must fail closed even when chat_id has a mission.

    The loop consults user_id first, then a distinct chat_id. An exception on
    the FIRST lookup aborts the whole grant — the chat_id hit that follows
    must never authorize, otherwise a broken store turns into fail-open.
    """
    runner = _make_runner()
    mod = types.ModuleType("plugins.missions")
    calls = []

    def _boom(chat_id):
        calls.append(str(chat_id))
        if chat_id == "u@x":
            raise RuntimeError("store unreadable")
        return {
            "mission_id": "m1",
            "status": "active",
            "platform": "whatsapp",
            "chat_id": "c@x",
        }

    mod.find_active_mission_for_chat = _boom
    mod.find_active_group_mission = lambda _cid: None
    monkeypatch.setitem(sys.modules, "plugins.missions", mod)

    assert (
        runner._is_user_authorized(_make_source(user_id="u@x", chat_id="c@x"))
        is False
    )
    assert calls == ["u@x"]  # aborted before the chat_id lookup


@pytest.mark.parametrize("chat_type", ["group", "forum", "channel", "thread"])
def test_non_dm_chat_types_do_not_gain_mission_grant(monkeypatch, chat_type):
    user_id = "61491234567@s.whatsapp.net"
    runner = _make_runner()
    _stub_missions(
        monkeypatch,
        [
            {
                "mission_id": "m1",
                "status": "active",
                "platform": "whatsapp",
                "chat_id": user_id,
            }
        ],
    )

    assert (
        runner._is_user_authorized(
            _make_source(
                user_id=user_id,
                chat_id="120363123456789012@g.us",
                chat_type=chat_type,
            )
        )
        is False
    )


# --------------------------------------------------------------------------
# identifier coverage
# --------------------------------------------------------------------------


def test_chat_id_only_lookup_authorizes(monkeypatch):
    chat_id = "61491234567@s.whatsapp.net"
    runner = _make_runner()
    _stub_missions(
        monkeypatch,
        [
            {
                "mission_id": "m1",
                "status": "active",
                "platform": "whatsapp",
                "chat_id": chat_id,
            }
        ],
    )

    assert (
        runner._is_user_authorized(
            _make_source(user_id="", chat_id=chat_id)
        )
        is False
    )  # no user_id → early deny before mission grant

    assert (
        runner._is_user_authorized(
            _make_source(user_id="other@x", chat_id=chat_id)
        )
        is True
    )


def test_user_id_consulted_before_chat_id(monkeypatch):
    runner = _make_runner()
    calls = _stub_missions(
        monkeypatch,
        [
            {
                "mission_id": "m1",
                "status": "active",
                "platform": "whatsapp",
                "chat_id": "c@x",
            }
        ],
    )
    source = _make_source(user_id="u@x", chat_id="c@x")

    assert runner._is_user_authorized(source) is True
    assert calls == ["u@x", "c@x"]


def test_plugin_absence_fails_closed(monkeypatch):
    runner = _make_runner()
    monkeypatch.delitem(sys.modules, "plugins.missions", raising=False)

    assert (
        runner._is_user_authorized(
            _make_source(user_id="61491234567@s.whatsapp.net")
        )
        is False
    )


def test_missing_mission_platform_does_not_authorize(monkeypatch):
    user_id = "61491234567@s.whatsapp.net"
    runner = _make_runner()
    _stub_missions(
        monkeypatch,
        [
            {
                "mission_id": "m1",
                "status": "active",
                "chat_id": user_id,
            }
        ],
    )

    assert runner._is_user_authorized(_make_source(user_id=user_id)) is False


def test_pairing_approval_still_works_without_mission(monkeypatch):
    runner = _make_runner()
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: True)
    _stub_missions(monkeypatch, [])

    assert runner._is_user_authorized(_make_source()) is True


def test_group_mission_path_unaffected(monkeypatch):
    """Early find_active_group_mission grant must still work for groups."""
    group_id = "120363123456789012@g.us"
    runner = _make_runner()
    mod = types.ModuleType("plugins.missions")
    mod.find_active_mission_for_chat = MagicMock(return_value=None)
    mod.find_active_group_mission = MagicMock(return_value={"mission_id": "g1"})
    monkeypatch.setitem(sys.modules, "plugins.missions", mod)

    assert (
        runner._is_user_authorized(
            _make_source(
                user_id="61491234567@s.whatsapp.net",
                chat_id=group_id,
                chat_type="group",
            )
        )
        is True
    )
    mod.find_active_mission_for_chat.assert_not_called()
