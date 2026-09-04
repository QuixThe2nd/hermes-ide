"""Auto-created delivery threads for the ``thread:`` cron deliver token.

A job whose ``deliver`` lane carries ``thread:<parent_chat_id>`` (platform
derived by matching the id against configured home channels) or
``thread:<platform>:<parent_chat_id>`` gets a FRESH platform thread on first
delivery, opened through the shipped ``adapter.create_handoff_thread`` surface
and named after the job. The concrete ``platform:parent:new_thread_id`` target
is persisted back onto the job the moment the thread exists, so no later run —
and no restart-safe worker replay, which cannot create threads — can ever mint
a second one. When creation is impossible (no live adapter, platform without
threads, permissions) the run still delivers, flat on the parent chat, and the
job is left untouched.

The failure lane (``failure_deliver``, or ``deliver`` when no override
exists) never auto-creates: an unresolved ``thread:`` token resolves to the
plain parent chat there. All chat ids here are fixture values, not real
channels; every adapter is a fake — no network.
"""

import asyncio
import logging
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import (
    _deliver_result,
    _parse_thread_deliver_token,
    _resolve_delivery_targets,
    _thread_autocreate_name,
)
from gateway.config import Platform, PlatformConfig

# Fixture home channels — deliberately unlike any real platform id.
DISCORD_HOME = "1549999999999999999"
SLACK_HOME = "C0TESTTEST"


@pytest.fixture(autouse=True)
def _home_channels(monkeypatch):
    """Two configured home channels so bare-id derivation is unambiguous."""
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", DISCORD_HOME)
    monkeypatch.setenv("SLACK_HOME_CHANNEL", SLACK_HOME)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)


def _job(deliver=None, name="Nightly digest", failure_deliver=None):
    job = {
        "id": "jthread01",
        "name": name,
        "deliver": deliver or f"thread:{DISCORD_HOME}",
        "origin": None,
    }
    if failure_deliver is not None:
        job["failure_deliver"] = failure_deliver
    return job


# ---------------------------------------------------------------------------
# (a) Token parsing / expansion
# ---------------------------------------------------------------------------


class TestTokenParsing:
    def test_bare_id_derives_platform_from_home_channel(self):
        targets = _resolve_delivery_targets(_job())
        assert len(targets) == 1
        target = targets[0]
        assert target["platform"] == "discord"
        assert target["chat_id"] == DISCORD_HOME
        assert target["thread_id"] is None
        assert target["_thread_auto"] is True

    def test_explicit_platform_form_names_the_platform(self):
        targets = _resolve_delivery_targets(_job(deliver=f"thread:slack:{SLACK_HOME}"))
        assert len(targets) == 1
        assert targets[0]["platform"] == "slack"
        assert targets[0]["chat_id"] == SLACK_HOME
        assert targets[0]["_thread_auto"] is True

    def test_combined_origin_and_thread_token_dedups_to_one_target(self):
        """``origin,thread:<id>`` is valid; only the thread: token's target
        auto-creates — and when both resolve to the same chat, the merged
        target keeps BOTH the origin provenance and the create intent."""
        job = _job(
            deliver=f"origin,thread:{DISCORD_HOME}",
        )
        job["origin"] = {"platform": "discord", "chat_id": DISCORD_HOME}
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert targets[0]["_resolved_from"] == "origin"
        assert targets[0]["_thread_auto"] is True

    def test_token_order_does_not_strip_the_create_intent(self):
        job = _job(deliver=f"thread:{DISCORD_HOME},origin")
        job["origin"] = {"platform": "discord", "chat_id": DISCORD_HOME}
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert targets[0]["_thread_auto"] is True

    def test_pre_resolved_thread_token_does_not_auto_create(self):
        """A token that already names a concrete thread is concrete itself —
        nothing to create."""
        targets = _resolve_delivery_targets(
            _job(deliver=f"thread:discord:{DISCORD_HOME}:777")
        )
        assert len(targets) == 1
        assert targets[0]["thread_id"] == "777"
        assert "_thread_auto" not in targets[0]

    def test_malformed_token_without_parent_resolves_to_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _resolve_delivery_targets(_job(deliver="thread:")) == []
        assert "missing its parent chat id" in caplog.text

    def test_unknown_parent_chat_id_resolves_to_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _resolve_delivery_targets(_job(deliver="thread:424242")) == []
        assert "matches no configured home channel" in caplog.text

    def test_parser_shapes(self):
        assert _parse_thread_deliver_token("thread:123") == (None, "123")
        assert _parse_thread_deliver_token("thread:discord:123") == ("discord", "123")
        assert _parse_thread_deliver_token("THREAD:123") == (None, "123")
        assert _parse_thread_deliver_token("thread:") == ("", "")
        assert _parse_thread_deliver_token("origin") is None
        assert _parse_thread_deliver_token("discord:123") is None

    def test_thread_name_sanitization(self):
        assert _thread_autocreate_name({"name": "Daily  \n digest"}, "discord") == (
            "Daily digest"
        )
        # Discord's thread-name cap, applied before the adapter ever sees it.
        assert _thread_autocreate_name({"name": "N" * 150}, "discord") == "N" * 100
        # A name that sanitizes to empty falls back to the job id.
        assert _thread_autocreate_name({"id": "j9", "name": "  "}, "discord") == "j9"


# ---------------------------------------------------------------------------
# Live-lane delivery harness
# ---------------------------------------------------------------------------


class _SendResult:
    def __init__(self, message_id):
        self.success = True
        self.message_id = message_id
        self.raw_response = {"ok": True}


class FakeThreadAdapter:
    """Live-adapter double; its only cron contract is create_handoff_thread."""

    def __init__(self, new_thread_id="9001", raise_on_create=False):
        self.new_thread_id = new_thread_id
        self.raise_on_create = raise_on_create
        self.create_calls = []

    async def create_handoff_thread(self, parent_chat_id, name):
        self.create_calls.append((parent_chat_id, name))
        if self.raise_on_create:
            raise RuntimeError("no permission to create threads")
        return self.new_thread_id


def _deliver(job, adapter, *, for_failure=False):
    """Drive ``_deliver_result`` once over the live-adapter lane.

    Returns ``(error, router_calls, update_job_mock)`` — the DeliveryRouter is
    stubbed (recording every routed send), the standalone sender and job
    persistence are patched, and ``create_handoff_thread`` coroutines scheduled
    onto the gateway loop are executed inline.
    """
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as e:  # noqa: BLE001
            future.set_exception(e)
        return future

    router_calls = []
    router = MagicMock()

    async def _deliver_to_platform(target, text, metadata):
        router_calls.append({"target": target, "text": text, "metadata": metadata})
        return _SendResult(message_id=4321)

    router._deliver_to_platform = _deliver_to_platform

    config = MagicMock()
    config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda p: None

    async def _unused_standalone(*args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("standalone sender must not run on the live lane")

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config",
               return_value={"cron": {"wrap_response": False}}), \
         patch("cron.scheduler._record_delivery_verification"), \
         patch("gateway.delivery.DeliveryRouter", return_value=router), \
         patch("tools.send_message_tool._send_to_platform", _unused_standalone), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro), \
         patch("cron.jobs.update_job") as update_job:
        error = _deliver_result(
            job,
            "Nightly report.",
            adapters={Platform.DISCORD: adapter},
            loop=loop,
            for_failure=for_failure,
        )
    return error, router_calls, update_job


# ---------------------------------------------------------------------------
# (b) + (e) First delivery creates the thread and persists the concrete token
# ---------------------------------------------------------------------------


class TestFirstDeliveryAutoCreatesThread:
    def test_creates_thread_delivers_into_it_and_persists(self):
        job = _job()
        adapter = FakeThreadAdapter(new_thread_id="9001")

        error, router_calls, update_job = _deliver(job, adapter)

        assert error is None
        # One create call: the job's name on the parent chat.
        assert adapter.create_calls == [(DISCORD_HOME, "Nightly digest")]
        # The brief is routed into the NEW thread, not the parent chat.
        assert router_calls[0]["target"].thread_id == "9001"
        # The concrete token replaces the thread: token on the job.
        concrete = f"discord:{DISCORD_HOME}:9001"
        update_job.assert_called_once_with("jthread01", {"deliver": concrete})
        assert job["deliver"] == concrete

    def test_persistence_replaces_only_the_thread_token(self):
        job = _job(deliver=f"origin,thread:{DISCORD_HOME}")
        job["origin"] = {"platform": "discord", "chat_id": DISCORD_HOME}

        error, _, update_job = _deliver(job, FakeThreadAdapter())

        assert error is None
        update_job.assert_called_once_with(
            "jthread01", {"deliver": f"origin,discord:{DISCORD_HOME}:9001"}
        )

    def test_thread_name_sanitized_before_reaching_the_adapter(self):
        job = _job(name="N" * 150)
        adapter = FakeThreadAdapter()

        _deliver(job, adapter)

        assert adapter.create_calls == [(DISCORD_HOME, "N" * 100)]

    def test_nameless_job_names_the_thread_after_the_job_id(self):
        job = _job(name="")
        adapter = FakeThreadAdapter()

        _deliver(job, adapter)

        assert adapter.create_calls == [(DISCORD_HOME, "jthread01")]

    def test_second_delivery_reuses_the_persisted_target(self):
        """Idempotence: after first-run persistence the concrete target is
        used and NO second create call is issued."""
        job = _job()
        adapter = FakeThreadAdapter(new_thread_id="9001")

        first_error, first_calls, _ = _deliver(job, adapter)
        assert first_error is None
        assert len(adapter.create_calls) == 1
        assert first_calls[0]["target"].thread_id == "9001"

        adapter.create_calls.clear()
        second_error, second_calls, update_job = _deliver(job, adapter)

        assert second_error is None
        assert adapter.create_calls == []  # no second thread, ever
        assert second_calls[0]["target"].thread_id == "9001"  # same thread
        assert second_calls[0]["target"].chat_id == DISCORD_HOME
        update_job.assert_not_called()  # already concrete — nothing to write


# ---------------------------------------------------------------------------
# (c) Fallback: creation fails or is impossible → parent chat, job untouched
# ---------------------------------------------------------------------------


class TestCreateFallingBackToParentChat:
    def test_none_create_result_delivers_flat_and_leaves_the_job_unchanged(self, caplog):
        job = _job()
        adapter = FakeThreadAdapter(new_thread_id=None)

        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            error, router_calls, update_job = _deliver(job, adapter)

        assert error is None  # the run must not fail
        assert adapter.create_calls  # creation was attempted
        assert router_calls[0]["target"].chat_id == DISCORD_HOME
        assert router_calls[0]["target"].thread_id is None  # flat on the parent
        update_job.assert_not_called()  # nothing persisted
        assert job["deliver"] == f"thread:{DISCORD_HOME}"
        assert "could not create a thread" in caplog.text

    def test_raising_create_is_contained_the_same_way(self, caplog):
        job = _job()
        adapter = FakeThreadAdapter(raise_on_create=True)

        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            error, router_calls, update_job = _deliver(job, adapter)

        assert error is None
        assert router_calls[0]["target"].thread_id is None
        update_job.assert_not_called()

    def test_no_live_adapter_warns_and_delivers_to_the_parent(self, caplog):
        """Standalone lane (`hermes cron run` without the gateway): no live
        adapter means no create attempt — warn, deliver flat, persist nothing."""
        job = _job()
        standalone_calls = []

        async def fake_standalone(platform, pconfig, chat_id, text, **kwargs):
            standalone_calls.append({"chat_id": chat_id, "kwargs": kwargs})
            return {}

        config = MagicMock()
        config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True)}
        config.get_home_channel = lambda p: None

        with caplog.at_level(logging.WARNING, logger="cron.scheduler"), \
             patch("gateway.config.load_gateway_config", return_value=config), \
             patch("cron.scheduler.load_config",
                   return_value={"cron": {"wrap_response": False}}), \
             patch("cron.scheduler._record_delivery_verification"), \
             patch("tools.send_message_tool._send_to_platform", fake_standalone), \
             patch("cron.jobs.update_job") as update_job:
            error = _deliver_result(job, "Nightly report.", adapters=None, loop=None)

        assert error is None
        assert len(standalone_calls) == 1
        assert standalone_calls[0]["chat_id"] == DISCORD_HOME
        assert standalone_calls[0]["kwargs"].get("thread_id") is None
        update_job.assert_not_called()
        assert job["deliver"] == f"thread:{DISCORD_HOME}"
        assert "no live gateway adapter" in caplog.text


# ---------------------------------------------------------------------------
# (d) The failure lane never auto-creates
# ---------------------------------------------------------------------------


class TestFailureLaneNeverAutoCreates:
    def test_failure_notice_resolves_the_token_to_the_parent_chat(self):
        """failure_deliver unset: the failure lane reads `deliver`, resolves
        the thread: token flat, and issues no create call."""
        job = _job()
        adapter = FakeThreadAdapter()

        error, router_calls, update_job = _deliver(job, adapter, for_failure=True)

        assert error is None
        assert adapter.create_calls == []
        assert router_calls[0]["target"].chat_id == DISCORD_HOME
        assert router_calls[0]["target"].thread_id is None
        update_job.assert_not_called()

    def test_explicit_failure_deliver_thread_token_resolves_flat(self):
        job = _job(failure_deliver=f"thread:{DISCORD_HOME}")
        adapter = FakeThreadAdapter()

        error, router_calls, update_job = _deliver(job, adapter, for_failure=True)

        assert error is None
        assert adapter.create_calls == []
        assert router_calls[0]["target"].thread_id is None
        update_job.assert_not_called()

    def test_failure_lane_resolution_carries_no_create_marker(self):
        job = _job()
        targets = _resolve_delivery_targets(job, for_failure=True)
        assert len(targets) == 1
        assert targets[0]["platform"] == "discord"
        assert targets[0]["chat_id"] == DISCORD_HOME
        assert targets[0]["thread_id"] is None
        assert "_thread_auto" not in targets[0]
