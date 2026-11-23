"""``discord.allowed_bots``: a per-bot allowlist narrowing bot-authored admission.

Two independent gates admit a bot-authored Discord message — the adapter's
``_discord_message_admission`` and the gateway's ``_principal_authorized``
bot block — so both must apply the list or one admits what the other denies.
The list only narrows: unset/empty is byte-for-byte the old behaviour (the
helper never runs), a listed bot still faces the ``allow_bots`` mode rules,
and an unevaluable list (malformed value, unavailable helper) fails closed
for bot authors with one warning while humans stay unaffected.

Parse/membership behaviour lives in ``internal/botallowlist`` (Go); the
shape coverage here consumes the shared table
(internal/botallowlist/testdata/admission_cases.json) through the real
binary. Negative-path tests also pass without a Go toolchain (fail closed).
"""

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import discord
import pytest

from gateway.platforms.helpers import MessageDeduplicator
from gateway.session import Platform, SessionSource

_REPO = Path(__file__).resolve().parents[2]
_MODULE_DIR = _REPO / "internal" / "botallowlist"
_TABLE = json.loads((_MODULE_DIR / "testdata" / "admission_cases.json").read_text())["cases"]

_LISTED = "424242424242424242"
_UNLISTED = "131313131313131313"


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch):
    for var in (
        "DISCORD_ALLOW_BOTS", "DISCORD_ALLOWED_BOTS", "DISCORD_BOTS_REQUIRE_INLINE_MENTION",
        "DISCORD_ALLOWED_USERS", "DISCORD_ALLOWED_ROLES", "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_bridge_state():
    """The bridge caches verdicts process-wide; each test starts cold."""
    module = importlib.import_module("gateway.bot_allowlist")
    module._verdict_cache.clear()
    module._malformed_lists.clear()
    module._warned_unavailable = False
    module._warned_malformed = False
    module._bridge_failed_at = None
    yield


@pytest.fixture
def adapter_mod():
    sys.modules.pop("plugins.platforms.discord.adapter", None)
    return importlib.import_module("plugins.platforms.discord.adapter")


def _bridge():
    return importlib.import_module("gateway.bot_allowlist")


@pytest.fixture(scope="session")
def go_botallowlist(tmp_path_factory):
    """Build cmd/botallowlist once per session; skip when no Go toolchain."""
    if shutil.which("go") is None:
        pytest.skip("Go toolchain unavailable")
    binary = tmp_path_factory.mktemp("botallowlist") / "botallowlist"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/botallowlist"],
        cwd=_MODULE_DIR, capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/botallowlist failed:\n{build.stderr}")
    return str(binary)


@pytest.fixture
def bridge(go_botallowlist, monkeypatch):
    """The real bridge pinned to the session-built Go binary."""
    module = _bridge()
    monkeypatch.setattr(module, "_bridge_binary", go_botallowlist)
    return module


def _adapter(adapter_mod, *, extra=None):
    """A DiscordAdapter shell with only the admission-gate state populated."""
    adapter = object.__new__(adapter_mod.DiscordAdapter)
    adapter.config = SimpleNamespace(extra=extra or {})
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=99, bot=True))
    adapter._dedup = MessageDeduplicator()
    adapter._is_allowed_user = Mock(return_value=True)
    adapter._text_batch_delay_seconds = 0.6
    adapter._text_batch_split_delay_seconds = 2.0
    adapter._bot_tag_debounce_until = {}
    adapter._gate_env_snapshot = {key: "" for key in adapter_mod._GATE_ENV_KEYS}
    return adapter


def _bot_message(adapter, *, author_id, content):
    mentions = [adapter._client.user] if "<@99>" in content else []
    return SimpleNamespace(
        id=123, author=SimpleNamespace(id=int(author_id), bot=True),
        channel=SimpleNamespace(id=7), content=content, mentions=mentions,
        type=discord.MessageType.default,
    )


def _admit_via_adapter(adapter_mod, extra, author_id, content="<@99> hi"):
    adapter = _adapter(adapter_mod, extra=extra)
    admitted, _ = adapter._discord_message_admission(
        _bot_message(adapter, author_id=author_id, content=content), claim=False)
    return admitted


def _runner(adapter_extra=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    if adapter_extra is not None:
        runner.adapters = {
            Platform.DISCORD: SimpleNamespace(config=SimpleNamespace(extra=adapter_extra)),
        }
    return runner


def _bot_source(user_id=_UNLISTED):
    return SessionSource(
        platform=Platform.DISCORD, chat_id="123", chat_type="channel",
        user_id=user_id, user_name="SomeBot", is_bot=True,
    )


# ── adapter admission gate ───────────────────────────────────────────────
# test_unlisted_bot_mention_denied_by_adapter takes no bridge fixture: on
# the unmodified tree the unknown extra key is ignored and the unlisted
# bot's mention is ADMITTED — the behavioural before/after signal.


def test_unlisted_bot_mention_denied_by_adapter(adapter_mod):
    extra = {"allow_bots": "mentions", "allowed_bots": [_LISTED]}
    assert _admit_via_adapter(adapter_mod, extra, _UNLISTED) is False


def test_listed_bot_mention_admitted_by_adapter(adapter_mod, bridge):
    extra = {"allow_bots": "mentions", "allowed_bots": [_LISTED]}
    assert _admit_via_adapter(adapter_mod, extra, _LISTED) is True


def test_listed_bot_denied_when_mode_none(adapter_mod, bridge):
    extra = {"allow_bots": "none", "allowed_bots": [_LISTED]}
    assert _admit_via_adapter(adapter_mod, extra, _LISTED) is False


def test_listed_bot_without_mention_denied_under_mentions(adapter_mod, bridge):
    extra = {"allow_bots": "mentions", "allowed_bots": [_LISTED]}
    assert _admit_via_adapter(adapter_mod, extra, _LISTED, content="no tag here") is False


def test_empty_list_shapes_narrow_nothing(adapter_mod, bridge):
    for empty in ([], "", "[]"):
        extra = {"allow_bots": "mentions", "allowed_bots": empty}
        assert _admit_via_adapter(adapter_mod, extra, _UNLISTED) is True


# ── gateway authorization gate ────────────────────────────────────────────


def test_unlisted_bot_denied_by_gateway_authz(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "mentions")
    runner = _runner(adapter_extra={"allowed_bots": [_LISTED]})
    assert runner._is_user_authorized(_bot_source(_UNLISTED)) is False


def test_listed_bot_admitted_by_gateway_authz(monkeypatch, bridge):
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "mentions")
    runner = _runner(adapter_extra={"allowed_bots": [_LISTED]})
    assert runner._is_user_authorized(_bot_source(_LISTED)) is True


def test_authz_reads_list_from_extra_without_env(bridge):
    """Hub case: the value lives only in PlatformConfig.extra, no env var."""
    runner = _runner(adapter_extra={"allow_bots": "all", "allowed_bots": [_LISTED]})
    assert runner._is_user_authorized(_bot_source(_UNLISTED)) is False
    assert runner._is_user_authorized(_bot_source(_LISTED)) is True


def test_unset_list_invokes_no_helper(adapter_mod, monkeypatch):
    """Invariant 1 at both gates: no helper run for an unset/empty list."""
    module = _bridge()
    monkeypatch.setattr(
        module, "go_binary",
        Mock(side_effect=AssertionError("helper invoked for an unset list")),
    )
    assert _admit_via_adapter(adapter_mod, {"allow_bots": "mentions"}, _UNLISTED) is True
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "all")
    assert _runner()._is_user_authorized(_bot_source(_UNLISTED)) is True


# ── fail-closed contract ──────────────────────────────────────────────────


def test_malformed_list_denies_bots_with_one_warning(adapter_mod, bridge, caplog):
    adapter = _adapter(
        adapter_mod, extra={"allow_bots": "all", "allowed_bots": {"not": "a list"}})
    with caplog.at_level("WARNING"):
        for author_id in (_LISTED, _UNLISTED):
            admitted, _ = adapter._discord_message_admission(
                _bot_message(adapter, author_id=author_id, content="<@99> hi"), claim=False)
            assert admitted is False
    warnings = [r for r in caplog.records if "allowed_bots" in r.getMessage()]
    assert len(warnings) == 1


def test_helper_unavailable_denies_bots(adapter_mod, monkeypatch):
    module = _bridge()
    monkeypatch.setattr(module, "go_binary", lambda: None)
    extra = {"allow_bots": "all", "allowed_bots": [_LISTED]}
    assert _admit_via_adapter(adapter_mod, extra, _LISTED) is False


def test_malformed_list_leaves_humans_unaffected(monkeypatch, bridge):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "555666777")
    runner = _runner(adapter_extra={"allowed_bots": {"not": "a list"}})
    human = SessionSource(
        platform=Platform.DISCORD, chat_id="123", chat_type="channel",
        user_id="555666777", user_name="SomeHuman", is_bot=False,
    )
    assert runner._is_user_authorized(human) is True


# ── shared admission table through the real binary ───────────────────────


def test_bridge_shared_admission_table(bridge):
    """Every row of the Go-shared table, through the real cmd/botallowlist.

    Bridge contract over the Go verdict: malformed → deny; a parsed count of
    zero narrows nothing → admit; otherwise the membership answer.
    """
    for case in _TABLE:
        expected = (
            False if case["malformed"]
            else True if case["count"] == 0
            else case["listed"]
        )
        assert bridge.admits(case["raw"], case["author_id"]) is expected, case["name"]


def test_bridge_caches_verdicts(bridge, monkeypatch):
    """A busy channel must not spawn a process per message."""
    calls = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(bridge.subprocess, "run", counting_run)
    for _ in range(5):
        assert bridge.admits([_LISTED], _LISTED) is True
        assert bridge.admits([_LISTED], _UNLISTED) is False
    assert len(calls) == 2  # one per distinct (list, author ID)


# ── YAML seeding ──────────────────────────────────────────────────────────


def test_yaml_allowed_bots_seeded_into_extra(adapter_mod):
    seeded = adapter_mod._apply_yaml_config({}, {"allowed_bots": [_LISTED, _UNLISTED]})
    assert seeded is not None
    assert seeded.get("allowed_bots") == [_LISTED, _UNLISTED]
    import os

    # The env bridge carries the comma-joined string form.
    assert os.environ.get("DISCORD_ALLOWED_BOTS") == f"{_LISTED},{_UNLISTED}"


def test_yaml_allowed_bots_seeded_from_platforms_extra(adapter_mod):
    seeded = adapter_mod._apply_yaml_config(
        {"platforms": {"discord": {"extra": {"allowed_bots": f"{_LISTED},{_UNLISTED}"}}}}, {})
    assert seeded is not None
    assert seeded.get("allowed_bots") == f"{_LISTED},{_UNLISTED}"
