"""The ``inbox`` cron deliver token and its creation-time enforcement.

Home-server operator convention: every cron job gets its own dedicated,
job-named thread under the provisioned inbox channel — never the chat it
happened to be created from. Two halves:

* Resolution: ``inbox`` / ``inbox:<guild_id>`` resolves to the provisioned
  inbox channel (hermes_starts state first, the home_server shared inbox
  as fallback) carrying the same ``_thread_auto`` marker as a ``thread:``
  token, so the existing auto-create machinery opens the job-named thread
  on first delivery and persists the concrete target back onto the job.
  No provisioned inbox, or a guild mismatch, is unresolved (delivery
  error). The failure lane resolves to the plain inbox channel.
* Creation: a job whose effective deliver would be ``origin``, created
  from a Discord session in the inbox's guild, is rewritten to ``inbox``
  at create time (``cron.inbox_delivery_enforce``, default true). Jobs
  naming an explicit target are untouched.

All ids here are fixture values; every adapter is a fake — no network.
"""

import asyncio
import json
import logging
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import (
    _deliver_result,
    _parse_inbox_deliver_token,
    _preflight_check_delivery,
    _resolve_delivery_targets,
)
from gateway.config import Platform, PlatformConfig
from gateway.session_context import clear_session_vars, set_session_vars

# Fixture ids — deliberately unlike any real platform id.
GUILD = "155000000000000001"
INBOX_CHANNEL = "156000000000000002"
OTHER_GUILD = "157000000000000003"
OUTBOX_CHAT = "158000000000000004"
HS_INBOX_CHANNEL = "159000000000000005"


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """HERMES_HOME pointed at a temp dir; writes land in fixture state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def inbox_state(hermes_home):
    """A provisioned hermes_starts inbox in the fixture guild."""
    _write_json(
        hermes_home / "hermes_starts" / "state.json",
        {"guild_id": GUILD, "channel_id": INBOX_CHANNEL, "channel_name": "inbox"},
    )
    return hermes_home


@pytest.fixture
def temp_cron_home(tmp_path):
    from cron import jobs as cron_jobs

    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.ensure_dirs()
        yield tmp_path


def _job(deliver="inbox", origin=None):
    return {
        "id": "jinbox01",
        "name": "Nightly digest",
        "deliver": deliver,
        "origin": origin or {
            "platform": "discord", "chat_id": OUTBOX_CHAT, "scope_id": GUILD},
    }


# ---------------------------------------------------------------------------
# (a) Token parsing
# ---------------------------------------------------------------------------


class TestTokenParsing:
    def test_parser_shapes(self):
        assert _parse_inbox_deliver_token("inbox") == ""
        assert _parse_inbox_deliver_token("INBOX") == ""
        assert _parse_inbox_deliver_token(" Inbox ") == ""
        assert _parse_inbox_deliver_token("inbox:") == ""
        assert _parse_inbox_deliver_token("inbox:" + GUILD) == GUILD
        assert _parse_inbox_deliver_token("INBOX:" + GUILD) == GUILD
        assert _parse_inbox_deliver_token("origin") is None
        assert _parse_inbox_deliver_token("discord:123") is None
        assert _parse_inbox_deliver_token("thread:123") is None
        assert _parse_inbox_deliver_token("") is None
        assert _parse_inbox_deliver_token(None) is None


# ---------------------------------------------------------------------------
# (b) Provisioned-inbox state: hermes_starts preferred, home_server fallback
# ---------------------------------------------------------------------------


class TestProvisionedInboxState:
    def test_hermes_starts_state_wins_when_both_exist(self, hermes_home):
        _write_json(
            hermes_home / "hermes_starts" / "state.json",
            {"guild_id": GUILD, "channel_id": INBOX_CHANNEL, "channel_name": "inbox"})
        _write_json(
            hermes_home / "home_server" / "state.json",
            {"guild_id": GUILD, "channels": {"chat": {"inbox": HS_INBOX_CHANNEL}}})
        from plugins.hermes_starts import provisioned_inbox

        assert provisioned_inbox() == {"guild_id": GUILD, "channel_id": INBOX_CHANNEL}

    def test_home_server_state_used_when_hermes_starts_missing(self, hermes_home):
        _write_json(
            hermes_home / "home_server" / "state.json",
            {"guild_id": GUILD, "channels": {"chat": {"inbox": HS_INBOX_CHANNEL}}})
        from plugins.hermes_starts import provisioned_inbox

        assert provisioned_inbox() == {"guild_id": GUILD, "channel_id": HS_INBOX_CHANNEL}

    def test_home_server_state_used_when_hermes_starts_empty(self, hermes_home):
        # An unprovisioned (or corrupt -> empty) hermes_starts state adopts the
        # shared inbox instead.
        _write_json(hermes_home / "hermes_starts" / "state.json", {})
        _write_json(
            hermes_home / "home_server" / "state.json",
            {"guild_id": GUILD, "channels": {"chat": {"inbox": HS_INBOX_CHANNEL}}})
        from plugins.hermes_starts import provisioned_inbox

        assert provisioned_inbox() == {"guild_id": GUILD, "channel_id": HS_INBOX_CHANNEL}

    def test_no_state_anywhere_is_none(self, hermes_home):
        from plugins.hermes_starts import provisioned_inbox

        assert provisioned_inbox() is None

    def test_corrupt_state_never_raises(self, hermes_home):
        (hermes_home / "hermes_starts").mkdir(parents=True, exist_ok=True)
        (hermes_home / "hermes_starts" / "state.json").write_text("{not json", "utf-8")
        from plugins.hermes_starts import provisioned_inbox

        assert provisioned_inbox() is None


# ---------------------------------------------------------------------------
# (c) Resolution at fire time
# ---------------------------------------------------------------------------


class TestResolution:
    def test_bare_token_resolves_to_inbox_channel_with_thread_auto(self, inbox_state):
        targets = _resolve_delivery_targets(_job())
        assert len(targets) == 1
        target = targets[0]
        assert target["platform"] == "discord"
        assert target["chat_id"] == INBOX_CHANNEL
        assert target["thread_id"] is None
        assert target["_resolved_from"] == "explicit"
        assert target["_thread_auto"] is True
        assert target["_deliver_token"] == "inbox"

    def test_guild_scoped_token_matching_the_inbox_guild_resolves(self, inbox_state):
        targets = _resolve_delivery_targets(_job(deliver=f"inbox:{GUILD}"))
        assert len(targets) == 1
        assert targets[0]["chat_id"] == INBOX_CHANNEL
        assert targets[0]["_thread_auto"] is True
        assert targets[0]["_deliver_token"] == f"inbox:{GUILD}"

    def test_guild_scoped_token_mismatch_is_unresolved(self, inbox_state, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _resolve_delivery_targets(_job(deliver=f"inbox:{OTHER_GUILD}")) == []
        assert "names guild" in caplog.text

    def test_no_provisioned_inbox_is_unresolved(self, hermes_home, caplog):
        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            assert _resolve_delivery_targets(_job()) == []
        assert "no provisioned inbox" in caplog.text

    def test_home_server_fallback_channel_resolves(self, hermes_home):
        _write_json(
            hermes_home / "home_server" / "state.json",
            {"guild_id": GUILD, "channels": {"chat": {"inbox": HS_INBOX_CHANNEL}}})
        targets = _resolve_delivery_targets(_job())
        assert len(targets) == 1
        assert targets[0]["chat_id"] == HS_INBOX_CHANNEL

    def test_failure_lane_resolves_plain_channel_without_thread_auto(self, inbox_state):
        targets = _resolve_delivery_targets(_job(), for_failure=True)
        assert len(targets) == 1
        target = targets[0]
        assert target["platform"] == "discord"
        assert target["chat_id"] == INBOX_CHANNEL
        assert target["thread_id"] is None
        assert "_thread_auto" not in target
        assert "_deliver_token" not in target

    def test_explicit_failure_deliver_inbox_resolves_plain(self, inbox_state):
        job = _job(deliver="local", origin=None)
        job["failure_deliver"] = "inbox"
        targets = _resolve_delivery_targets(job, for_failure=True)
        assert len(targets) == 1
        assert targets[0]["chat_id"] == INBOX_CHANNEL
        assert "_thread_auto" not in targets[0]


# ---------------------------------------------------------------------------
# (d) First delivery opens the job-named thread under the inbox and persists
# ---------------------------------------------------------------------------


class _SendResult:
    def __init__(self, message_id):
        self.success = True
        self.message_id = message_id
        self.raw_response = {"ok": True}


class FakeThreadAdapter:
    """Live-adapter double; its only cron contract is create_handoff_thread."""

    def __init__(self, new_thread_id="9001"):
        self.new_thread_id = new_thread_id
        self.create_calls = []

    async def create_handoff_thread(self, parent_chat_id, name):
        self.create_calls.append((parent_chat_id, name))
        return self.new_thread_id


def _deliver(job, adapter, *, for_failure=False):
    """Drive ``_deliver_result`` once over the live-adapter lane (see the
    thread-token suite for the harness's rationale)."""
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


class TestFirstDeliveryAutoCreatesInboxThread:
    def test_creates_job_named_thread_under_inbox_and_persists(self, inbox_state):
        job = _job()
        adapter = FakeThreadAdapter(new_thread_id="9001")

        error, router_calls, update_job = _deliver(job, adapter)

        assert error is None
        # The thread is opened UNDER THE INBOX CHANNEL and named after the job.
        assert adapter.create_calls == [(INBOX_CHANNEL, "Nightly digest")]
        # The brief is routed into the NEW thread.
        assert router_calls[0]["target"].chat_id == INBOX_CHANNEL
        assert router_calls[0]["target"].thread_id == "9001"
        # The verbatim token is rewritten to the concrete target on the job.
        concrete = f"discord:{INBOX_CHANNEL}:9001"
        update_job.assert_called_once_with("jinbox01", {"deliver": concrete})
        assert job["deliver"] == concrete

    def test_second_delivery_reuses_the_persisted_target(self, inbox_state):
        job = _job()
        adapter = FakeThreadAdapter(new_thread_id="9001")

        _deliver(job, adapter)
        adapter.create_calls.clear()
        _, router_calls, update_job = _deliver(job, adapter)

        assert adapter.create_calls == []  # no second thread, ever
        assert router_calls[0]["target"].thread_id == "9001"
        update_job.assert_not_called()  # already concrete — nothing to write

    def test_failure_notice_delivers_flat_on_the_inbox_channel(self, inbox_state):
        job = _job()
        adapter = FakeThreadAdapter()

        error, router_calls, update_job = _deliver(job, adapter, for_failure=True)

        assert error is None
        assert adapter.create_calls == []  # a failure notice never mints threads
        assert router_calls[0]["target"].chat_id == INBOX_CHANNEL
        assert router_calls[0]["target"].thread_id is None
        update_job.assert_not_called()


# ---------------------------------------------------------------------------
# (e) Preflight
# ---------------------------------------------------------------------------


def _preflight(job, *, connected=(Platform.DISCORD,)):
    config = MagicMock()
    config.get_connected_platforms.return_value = list(connected)
    with patch("gateway.config.load_gateway_config", return_value=config):
        return _preflight_check_delivery(job)


class TestPreflightDelivery:
    def test_inbox_token_with_state_passes(self, inbox_state):
        assert _preflight(_job()) is None

    def test_guild_scoped_token_with_state_passes(self, inbox_state):
        assert _preflight(_job(deliver=f"inbox:{GUILD}")) is None

    def test_no_provisioned_inbox_blocks_naming_the_token(self, hermes_home):
        reason = _preflight(_job())
        assert reason is not None
        assert "'inbox'" in reason
        assert "provisioned inbox" in reason

    def test_guild_mismatch_blocks(self, inbox_state):
        reason = _preflight(_job(deliver=f"inbox:{OTHER_GUILD}"))
        assert reason is not None
        assert f"'inbox:{OTHER_GUILD}'" in reason


# ---------------------------------------------------------------------------
# (f) Creation-time enforcement
# ---------------------------------------------------------------------------


def _create(deliver=None):
    from tools.cronjob_tools import cronjob

    return json.loads(
        cronjob(
            action="create",
            schedule="every 4h",
            prompt="check the thing",
            deliver=deliver,
        )
    )


class TestCreationTimeEnforcement:
    def _session(self, platform="discord", scope=GUILD, chat=OUTBOX_CHAT):
        tokens = set_session_vars(platform=platform, chat_id=chat, scope_id=scope)
        return tokens

    def test_default_origin_deliver_rewritten_to_inbox(self, temp_cron_home, inbox_state):
        tokens = self._session()
        try:
            result = _create()
        finally:
            clear_session_vars(tokens)
        assert result["success"] is True
        assert result["deliver"] == "inbox"

    def test_stored_job_carries_the_inbox_token(self, temp_cron_home, inbox_state):
        from cron.jobs import get_job

        tokens = self._session()
        try:
            result = _create()
        finally:
            clear_session_vars(tokens)
        assert get_job(result["job_id"])["deliver"] == "inbox"

    def test_explicit_origin_deliver_also_rewritten(self, temp_cron_home, inbox_state):
        """``origin`` is not an explicit platform target — it roots the job in
        the creating chat, so the convention applies to it too."""
        tokens = self._session()
        try:
            result = _create(deliver="origin")
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == "inbox"

    def test_explicit_target_untouched(self, temp_cron_home, inbox_state):
        tokens = self._session()
        try:
            result = _create(deliver=f"discord:{OUTBOX_CHAT}")
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == f"discord:{OUTBOX_CHAT}"

    def test_explicit_local_untouched(self, temp_cron_home, inbox_state):
        tokens = self._session()
        try:
            result = _create(deliver="local")
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == "local"

    def test_thread_token_untouched(self, temp_cron_home, inbox_state):
        tokens = self._session()
        try:
            result = _create(deliver=f"thread:{OUTBOX_CHAT}")
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == f"thread:{OUTBOX_CHAT}"

    def test_config_opt_out_leaves_origin_in_place(self, temp_cron_home, inbox_state):
        tokens = self._session()
        try:
            with patch(
                "hermes_cli.config.load_config_readonly",
                return_value={"cron": {"inbox_delivery_enforce": False}},
            ):
                result = _create()
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == "origin"

    def test_non_discord_origin_untouched(self, temp_cron_home, inbox_state):
        tokens = self._session(platform="telegram", scope="TTEAM", chat="-100123")
        try:
            result = _create()
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == "origin"

    def test_non_matching_guild_untouched(self, temp_cron_home, inbox_state):
        tokens = self._session(scope=OTHER_GUILD)
        try:
            result = _create()
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == "origin"

    def test_missing_scope_id_untouched(self, temp_cron_home, inbox_state):
        tokens = self._session(scope="")
        try:
            result = _create()
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == "origin"

    def test_no_provisioned_inbox_untouched(self, temp_cron_home, hermes_home):
        tokens = self._session()
        try:
            result = _create()
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == "origin"

    def test_no_origin_session_untouched(self, temp_cron_home, inbox_state):
        # CLI/TUI create: no capturable origin, deliver defaults to local.
        result = _create()
        assert result["deliver"] == "local"

    def test_home_server_fallback_inbox_also_enforces(self, temp_cron_home, hermes_home):
        _write_json(
            hermes_home / "home_server" / "state.json",
            {"guild_id": GUILD, "channels": {"chat": {"inbox": HS_INBOX_CHANNEL}}})
        tokens = self._session()
        try:
            result = _create()
        finally:
            clear_session_vars(tokens)
        assert result["deliver"] == "inbox"


# ---------------------------------------------------------------------------
# (g) Review-gap pins: cron-context creates, OR-merge dedup, mixed unresolved
# ---------------------------------------------------------------------------


class TestInboxResolutionCombinations:
    def test_origin_and_inbox_tokens_dedup_to_one_target(self, hermes_home, inbox_state):
        """A job created FROM the inbox channel itself: ``origin,inbox`` both
        resolve to (discord, INBOX) — one merged target keeping origin
        provenance AND the create intent, in either token order."""
        job = _job(deliver="origin,inbox")
        job["origin"] = {"platform": "discord", "chat_id": INBOX_CHANNEL, "scope_id": GUILD}
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert targets[0]["_resolved_from"] == "origin"
        assert targets[0]["_thread_auto"] is True

    def test_inbox_and_origin_tokens_dedup_to_one_target(self, hermes_home, inbox_state):
        job = _job(deliver="inbox,origin")
        job["origin"] = {"platform": "discord", "chat_id": INBOX_CHANNEL, "scope_id": GUILD}
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert targets[0]["_thread_auto"] is True

    def test_mixed_list_with_unresolved_inbox_reports_the_token(self, hermes_home, inbox_state):
        """``inbox:<wrong guild>,discord:<inbox>``: the resolvable token still
        receives the output, and the unresolved token is NAMED in the
        unresolved list — a mixed list must never report clean success."""
        from cron.scheduler_delivery import _resolve_delivery_targets_detailed

        job = _job(deliver=f"inbox:{OTHER_GUILD},discord:{INBOX_CHANNEL}")
        targets, unresolved = _resolve_delivery_targets_detailed(job)
        assert [(t["platform"], t["chat_id"]) for t in targets] == [("discord", INBOX_CHANNEL)]
        assert unresolved == [f"inbox:{OTHER_GUILD}"]


class TestCronContextCreateXEnforcement:
    """A job created FROM a cron run: the creator's concrete target wins and
    a literal 'origin' never reaches the store — with the inbox rewrite hook
    in the path."""

    def _cron_session(self):
        from gateway.session_context import _VAR_MAP

        tokens = set_session_vars(platform="", chat_id="", cron_session="1")
        extra = [
            (_VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"],
             _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].set("discord")),
            (_VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"],
             _VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"].set(OUTBOX_CHAT)),
        ]
        return tokens, extra

    def test_cron_context_create_stores_concrete_target_not_origin(self, temp_cron_home, inbox_state):
        tokens, extra = self._cron_session()
        try:
            result = _create()
        finally:
            for var, token in reversed(extra):
                var.reset(token)
            clear_session_vars(tokens)
        assert result["success"] is True
        assert result["deliver"] == f"discord:{OUTBOX_CHAT}"

    def test_cron_context_without_creator_target_stays_local(self, temp_cron_home, inbox_state):
        tokens = set_session_vars(platform="", chat_id="", cron_session="1")
        try:
            result = _create()
        finally:
            clear_session_vars(tokens)
        assert result["success"] is True
        assert result["deliver"] == "local"
