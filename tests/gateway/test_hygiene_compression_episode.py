"""Discord tool-style episode card for gateway session-hygiene compression.

Hygiene is the SECOND automatic compaction pass (gateway/run.py pre-agent
safety pass at 85% context). In-agent compression already posts one
``🗜️ context_compress · …`` message edited through its lifecycle
(agent/compression_status.py → the gateway episode rail); hygiene used to be
fully silent on Discord while a multi-minute summarization ran.

These tests pin the presentation-only contract: hygiene drives the SAME
episode machinery directly from the gateway under its OWN ``hygiene``-railed
registry key — separate state from the in-agent status rail on the same
(adapter, chat, session), so neither rail can drop or hand over the other's
lines (the detached hygiene agent keeps its deliberately stale platform and
no status_callback) — with a per-attempt token so a superseded attempt's
late edit cannot clobber its successor's episode. Card edges:

- start line the moment the summary executor is spawned;
- ONE non-terminal "still running in the background" edit at turn-hold
  expiry while the watermark-fenced worker keeps streaming;
- ✅ ONLY at the watermark-fenced adoption boundary (deferred path) or when
  the inline wait observed the commit;
- ⚠️ on timeout-cancel, fence-cancel, and did-not-commit outcomes — never a
  success for a no-op;
- a superseding attempt HANDS OVER the open bubble (edit, no second send)
  and the superseded attempt's late terminal is dropped;
- the deferred state is edited EXACTLY once per attempt, even if the
  turn-hold edge fires twice;
- a stale scheduled delivery (text no longer ep.last_text) never edits;
- with a hygiene card open, the agent rail stays independent: its raw abort
  warning still posts and its compression_tool start opens a SEPARATE
  bubble;
- nothing at all for non-episode-rail adapters (byte-identical legacy
  notices).
"""

import asyncio
import importlib
import sys
import threading
import time
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.compression_status import (
    COMPRESSION_ABORT_WARNING_PREFIX,
    COMPRESSION_TOOL_FAILURE_PREFIX,
    COMPRESSION_TOOL_START_PREFIX,
    COMPRESSION_TOOL_SUCCESS_PREFIX,
    compression_tool_deferred_line,
    compression_tool_start_line,
    compression_tool_success_line,
    emit_compression_tool_status,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource

SESSION_KEY = "agent:main:discord:dm:12345"
SESSION_ID = "sess-hyg-episode"


def _make_history(n_messages: int, content_size: int = 100) -> list:
    history = []
    content = "x" * content_size
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": content, "timestamp": f"t{i}"})
    return history


class DiscordEpisodeAdapter(BasePlatformAdapter):
    """Discord-shaped fake: edits bubbles, no send_or_update_status.

    This is the surface `_adapter_uses_compression_episode_rail` selects —
    platform discord, editable, without the Telegram/Slack per-status-key
    editing method.
    """

    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="fake-token"), Platform.DISCORD
        )
        self.sent = []
        self.edits = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def edit_message(
        self, chat_id, message_id, content, *, finalize=False, metadata=None
    ):
        self.edits.append(
            {"chat_id": chat_id, "message_id": message_id, "content": content}
        )
        return SendResult(success=True, message_id=message_id)

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class TelegramNoticeAdapter(BasePlatformAdapter):
    """Non-rail fake: every status is a fresh plain send, as on Telegram."""

    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM
        )
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id="x")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _write_episode_config(tmp_path, *, timeout=60, turn_hold=0.3):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "compression:\n"
        "  enabled: true\n"
        f"  hygiene_timeout_seconds: {timeout}\n"
        "  hygiene_total_ceiling_seconds: 600\n"
        f"  hygiene_max_turn_hold_seconds: {turn_hold}\n"
        "  hygiene_failure_cooldown_seconds: 120\n"
    )


def _build_runner(gateway_run, adapter, fake_db, platform):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=platform,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = _make_history(
        6, content_size=400
    )
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = SimpleNamespace(_db=fake_db)
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )
    return runner


def _make_event(platform):
    return MessageEvent(
        text="hello",
        source=SessionSource(
            platform=platform,
            chat_id="12345",
            chat_type="dm",
            user_id="12345",
        ),
        message_id="1",
    )


def _install_fakes(monkeypatch, gateway_run, tmp_path, agent_cls):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )


async def _drain_deferred(runner, timeout=10.0):
    tasks = getattr(runner, "_deferred_agent_cleanup_tasks", None) or set()
    if tasks:
        await asyncio.wait_for(
            asyncio.gather(*list(tasks), return_exceptions=True), timeout
        )


async def _wait_for_edit(adapter, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for edit in adapter.edits:
            if predicate(edit["content"]):
                return edit
        await asyncio.sleep(0.05)
    return None


async def _wait_for_send(adapter, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sent in adapter.sent:
            if predicate(sent["content"]):
                return sent
        await asyncio.sleep(0.05)
    return None


def _sent_contents(adapter):
    return [m["content"] for m in adapter.sent]


def _edit_contents(adapter):
    return [m["content"] for m in adapter.edits]


class _EpisodeAgentBase:
    """Shared fake hygiene agent: session-bound, in-place capable."""

    last_instance = None

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id", SESSION_ID)
        self._session_db = kwargs.get("session_db")
        self._last_compaction_in_place = False
        self.context_compressor = SimpleNamespace(
            bind_session_state=MagicMock(),
            _last_compress_aborted=False,
            _last_aux_model_failure_model=None,
        )
        self.shutdown_memory_provider = MagicMock()
        self.close = MagicMock()
        type(self).last_instance = self


class FastInPlaceAgent(_EpisodeAgentBase):
    """Commits in-place immediately — the inline (awaited) success path."""

    def _compress_context(self, messages, *_args, commit_fence=None, **_kwargs):
        if commit_fence is not None:
            commit_fence.mark_commit_watermark_fenced()
            commit_fence.touch_progress()
        self._session_db.archive_and_compact(
            self.session_id,
            [{"role": "assistant", "content": "summary"}],
            watermark=len(messages),
        )
        self._last_compaction_in_place = True
        return ([{"role": "assistant", "content": "summary"}], None)


class FencedStreamingAgent(_EpisodeAgentBase):
    """Watermark-fenced thinking model: streams past the turn-hold budget."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.worker_started = threading.Event()
        self.release_worker = threading.Event()
        self.committed = threading.Event()

    def _compress_context(self, messages, *_args, commit_fence=None, **_kwargs):
        if commit_fence is not None:
            commit_fence.mark_commit_watermark_fenced()
        self.worker_started.set()
        _spin_started = time.monotonic()
        while not self.release_worker.is_set():
            if time.monotonic() - _spin_started > 20:
                return (messages, None)
            if commit_fence is not None:
                commit_fence.touch_progress()
            time.sleep(0.01)
        if commit_fence is not None and not commit_fence.begin_commit():
            return (messages, None)
        try:
            self._session_db.archive_and_compact(
                self.session_id,
                [{"role": "assistant", "content": "summary"}],
                watermark=len(messages),
            )
            self._last_compaction_in_place = True
            self.committed.set()
            return ([{"role": "assistant", "content": "summary"}], None)
        finally:
            if commit_fence is not None:
                commit_fence.finish_commit()


class FencedNoCommitAgent(FencedStreamingAgent):
    """Fenced worker whose summary fails after the turn was released."""

    def _compress_context(self, messages, *_args, commit_fence=None, **_kwargs):
        if commit_fence is not None:
            commit_fence.mark_commit_watermark_fenced()
        self.worker_started.set()
        _spin_started = time.monotonic()
        while not self.release_worker.is_set():
            if time.monotonic() - _spin_started > 20:
                return (messages, None)
            if commit_fence is not None:
                commit_fence.touch_progress()
            time.sleep(0.01)
        # Summary failed — return unchanged, no commit.
        return (messages, None)


class SilentTimeoutAgent(_EpisodeAgentBase):
    """Never reports progress — the inactivity timeout cancels it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.worker_started = threading.Event()
        self.release_worker = threading.Event()

    def _compress_context(self, messages, *_args, commit_fence=None, **_kwargs):
        self.worker_started.set()
        # No touch_progress at all: silent worker.
        self.release_worker.wait(timeout=20)
        return (messages, None)


class MultiAttemptFencedStreamingAgent(FencedStreamingAgent):
    """Fenced streaming agent recording every instance (supersede tests).

    The shared base only keeps ``last_instance``; driving two attempts on
    one card needs BOTH handles (the superseded worker and its successor).
    """

    instances = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        type(self).instances.append(self)


@pytest.mark.asyncio
async def test_inline_commit_posts_start_card_then_success_edit(
    monkeypatch, tmp_path
):
    """Card sent on start; ✅ only when the inline wait observed the commit."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, FastInPlaceAgent)

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"

    starts = [
        c for c in _sent_contents(adapter) if c.startswith(COMPRESSION_TOOL_START_PREFIX)
    ]
    assert len(starts) == 1, f"exactly one start card, got: {_sent_contents(adapter)}"
    successes = [
        c
        for c in _edit_contents(adapter)
        if c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
    ]
    assert len(successes) == 1, f"one success edit, got: {_edit_contents(adapter)}"
    # Real before/after counts ride the success line.
    assert "6 → 1 messages" in successes[0]
    # Never a failure line for a committed attempt.
    assert not any(
        c.startswith(COMPRESSION_TOOL_FAILURE_PREFIX)
        for c in _sent_contents(adapter) + _edit_contents(adapter)
    )
    # One bubble: the success edit targets the start card's message.
    assert adapter.edits[0]["message_id"] == "m1"


@pytest.mark.asyncio
async def test_turn_hold_expiry_edits_deferred_then_adopts_success(
    monkeypatch, tmp_path
):
    """Turn-hold expiry → ONE non-terminal deferred edit; ✅ only at the
    watermark-fenced adoption boundary when the detached worker commits."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, FencedStreamingAgent)

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"

    starts = [
        c for c in _sent_contents(adapter) if c.startswith(COMPRESSION_TOOL_START_PREFIX)
    ]
    assert len(starts) == 1, f"exactly one start card, got: {_sent_contents(adapter)}"
    # The card was edited ONCE to the deferred state — not terminal, not
    # success — and the legacy plain deferral notice is gone on Discord.
    deferred_edit = await _wait_for_edit(
        adapter, lambda c: c == compression_tool_deferred_line()
    )
    assert deferred_edit is not None, f"deferred edit missing: {_edit_contents(adapter)}"
    assert not any("deferred" in c.lower() for c in _sent_contents(adapter)), (
        f"legacy deferral notice must fold into the card on Discord, got: "
        f"{_sent_contents(adapter)}"
    )
    # Deferred is not terminal: no ✅/⚠️ yet.
    assert not any(
        c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
        or c.startswith(COMPRESSION_TOOL_FAILURE_PREFIX)
        for c in _edit_contents(adapter)
    )

    # The detached worker finishes late and commits → adoption success edit.
    agent = FencedStreamingAgent.last_instance
    agent.release_worker.set()
    await asyncio.wait_for(asyncio.to_thread(agent.committed.wait, 5), timeout=6)
    success_edit = await _wait_for_edit(
        adapter, lambda c: c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
    )
    assert success_edit is not None, f"adoption edit missing: {_edit_contents(adapter)}"
    assert "6 → 1 messages" in success_edit["content"]

    await _drain_deferred(runner)
    # Still exactly one bubble, edited in place throughout.
    assert len(starts) == 1
    assert {e["message_id"] for e in adapter.edits} == {"m1"}


@pytest.mark.asyncio
async def test_turn_hold_deferred_attempt_that_never_commits_fails_the_card(
    monkeypatch, tmp_path
):
    """A deferred worker that ends WITHOUT committing closes the card as ⚠️ —
    never as success (summary failed / superseded / fence refused)."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, FencedNoCommitAgent)

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"
    assert await _wait_for_edit(
        adapter, lambda c: c == compression_tool_deferred_line()
    ), f"deferred edit missing: {_edit_contents(adapter)}"

    agent = FencedNoCommitAgent.last_instance
    agent.release_worker.set()
    await _drain_deferred(runner)
    failure_edit = await _wait_for_edit(
        adapter, lambda c: c.startswith(COMPRESSION_TOOL_FAILURE_PREFIX)
    )
    assert failure_edit is not None, f"failure edit missing: {_edit_contents(adapter)}"
    assert "stopped before finishing" in failure_edit["content"]
    assert not any(
        c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
        for c in _sent_contents(adapter) + _edit_contents(adapter)
    ), f"a non-committing attempt must never claim success: {_edit_contents(adapter)}"


@pytest.mark.asyncio
async def test_timeout_closes_card_as_failure_never_success(monkeypatch, tmp_path):
    """A silent worker times out → ⚠️ failure edit; the plain timeout warning
    folds into the card on Discord."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path, timeout=0.05, turn_hold=10)
    _install_fakes(monkeypatch, gateway_run, tmp_path, SilentTimeoutAgent)

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"
    failure_edit = await _wait_for_edit(
        adapter, lambda c: c.startswith(COMPRESSION_TOOL_FAILURE_PREFIX)
    )
    assert failure_edit is not None, f"failure edit missing: {_edit_contents(adapter)}"
    assert "failed" in failure_edit["content"]
    assert not any(
        c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
        for c in _sent_contents(adapter) + _edit_contents(adapter)
    )
    # The legacy plain timeout warning folded into the card on Discord.
    assert not any("timed out" in c.lower() for c in _sent_contents(adapter)), (
        f"legacy timeout warning must fold into the card on Discord, got: "
        f"{_sent_contents(adapter)}"
    )

    agent = SilentTimeoutAgent.last_instance
    agent.release_worker.set()
    await _drain_deferred(runner)


@pytest.mark.asyncio
async def test_non_episode_rail_adapter_gets_no_card(monkeypatch, tmp_path):
    """Telegram (no episode rail): no card, no lifecycle lines at all — the
    legacy plain notices stay byte-identical."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, FencedStreamingAgent)

    adapter = TelegramNoticeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.TELEGRAM)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.TELEGRAM)), timeout=15
    )
    assert result == "ok"

    tool_lines = [
        c
        for c in _sent_contents(adapter)
        if c.startswith(
            (
                COMPRESSION_TOOL_START_PREFIX,
                COMPRESSION_TOOL_SUCCESS_PREFIX,
                COMPRESSION_TOOL_FAILURE_PREFIX,
            )
        )
    ]
    assert tool_lines == [], f"non-rail adapter must see no card: {tool_lines}"
    # Legacy deferral notice still posts exactly as before.
    assert any(
        "deferred" in c.lower() or "still streaming" in c.lower()
        for c in _sent_contents(adapter)
    ), f"turn-hold deferral notice missing on Telegram: {_sent_contents(adapter)}"

    agent = FencedStreamingAgent.last_instance
    agent.release_worker.set()
    await asyncio.wait_for(asyncio.to_thread(agent.committed.wait, 5), timeout=6)
    await _drain_deferred(runner)
    # Adoption on a non-rail adapter stays silent (no new chatter).
    assert not any(
        c.startswith(
            (
                COMPRESSION_TOOL_START_PREFIX,
                COMPRESSION_TOOL_SUCCESS_PREFIX,
                COMPRESSION_TOOL_FAILURE_PREFIX,
            )
        )
        for c in _sent_contents(adapter)
    )


@pytest.mark.asyncio
async def test_second_attempt_hands_over_card_and_drops_superseded_terminal(
    monkeypatch, tmp_path
):
    """A second hygiene attempt while the card is open takes over the SAME
    bubble (an edit, never a second send) and the superseded attempt's late
    terminal is dropped — one card, one terminal, the newest attempt's."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    MultiAttemptFencedStreamingAgent.instances = []
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, MultiAttemptFencedStreamingAgent)

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    # Attempt 1: start card, then turn-hold expiry defers it (the worker
    # keeps streaming, so the card stays open and NON-terminal).
    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"
    assert await _wait_for_edit(
        adapter, lambda c: c == compression_tool_deferred_line()
    ), f"attempt 1 deferred edit missing: {_edit_contents(adapter)}"

    # Attempt 2 (next turn, same still-oversized transcript): must HAND OVER
    # the open bubble — an edit to m1, never a second send. (The hand-over
    # emission is awaited inline by the handler, so it has landed by return;
    # exact-content check because the deferred line shares the start prefix.)
    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"
    assert any(
        e["content"] == compression_tool_start_line() for e in adapter.edits
    ), f"attempt 2 hand-over start edit missing: {_edit_contents(adapter)}"
    start_sends = [
        c for c in _sent_contents(adapter) if c.startswith(COMPRESSION_TOOL_START_PREFIX)
    ]
    assert len(start_sends) == 1, (
        f"hand-over must edit the open card, not send a second bubble: "
        f"{_sent_contents(adapter)}"
    )
    assert {e["message_id"] for e in adapter.edits} == {"m1"}

    assert len(MultiAttemptFencedStreamingAgent.instances) == 2
    first, second = MultiAttemptFencedStreamingAgent.instances

    # The superseded attempt finishes late and COMMITS: its adoption terminal
    # carries the old attempt token and must be dropped by the registry.
    first.release_worker.set()
    await asyncio.wait_for(asyncio.to_thread(first.committed.wait, 5), timeout=6)
    await asyncio.sleep(0.2)  # let the dropped emission's slot come and go

    # The successor's commit is the ONE and ONLY terminal on the card.
    second.release_worker.set()
    await asyncio.wait_for(asyncio.to_thread(second.committed.wait, 5), timeout=6)
    success_edit = await _wait_for_edit(
        adapter, lambda c: c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
    )
    assert success_edit is not None, f"successor adoption edit missing: {_edit_contents(adapter)}"
    assert "6 → 1 messages" in success_edit["content"]
    await _drain_deferred(runner)
    assert (
        sum(
            1
            for c in _edit_contents(adapter)
            if c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
        )
        == 1
    ), f"superseded attempt's late terminal must be dropped: {_edit_contents(adapter)}"
    assert len(start_sends) == 1
    assert {e["message_id"] for e in adapter.edits} == {"m1"}


@pytest.mark.asyncio
async def test_stale_scheduled_update_is_skipped_not_edited():
    """The coro's stale-delivery guard: an update scheduled before a newer
    decision (text no longer ep.last_text) is skipped — no edit fires for
    it, exactly one delivery carries the current state."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    adapter = DiscordEpisodeAdapter()

    start = compression_tool_start_line()
    deferred = compression_tool_deferred_line()
    success = compression_tool_success_line(before_messages=6, after_messages=1)
    decide = gateway_run._compression_episode_decide
    deliver = gateway_run._send_or_update_compression_episode_coro
    rail = gateway_run._COMPRESSION_EPISODE_RAIL_HYGIENE

    action, content = decide(
        adapter,
        "12345",
        gateway_run.COMPRESSION_TOOL_STATUS_EVENT,
        start,
        session_key=SESSION_KEY,
        attempt_token="attempt-1",
        rail=rail,
    )
    assert (action, content) == ("deliver", start)
    await deliver(adapter, "12345", start, None, session_key=SESSION_KEY, rail=rail)
    assert _sent_contents(adapter) == [start]

    # Two updates decided back to back; only the NEWEST text may deliver.
    for line in (deferred, success):
        action, content = decide(
            adapter,
            "12345",
            gateway_run.COMPRESSION_TOOL_STATUS_EVENT,
            line,
            session_key=SESSION_KEY,
            attempt_token="attempt-1",
            rail=rail,
        )
        assert (action, content) == ("deliver", line)
    assert await deliver(
        adapter, "12345", success, None, session_key=SESSION_KEY, rail=rail
    )
    assert _edit_contents(adapter) == [success]

    # The deferred update, scheduled first, runs LAST: the registry has
    # moved on to the terminal — it must be skipped with no edit fired.
    assert (
        await deliver(adapter, "12345", deferred, None, session_key=SESSION_KEY, rail=rail)
        is None
    )
    assert _edit_contents(adapter) == [success], (
        f"stale scheduled update must not edit: {_edit_contents(adapter)}"
    )
    assert _sent_contents(adapter) == [start]


@pytest.mark.asyncio
async def test_agent_rail_independent_of_open_hygiene_card(monkeypatch, tmp_path):
    """With a hygiene card open (non-terminal), the agent rail keeps its own
    episode on the same chat: its raw abort warning still POSTS via the
    normal rail (not dropped by the hygiene card) and its compression_tool
    start opens a SEPARATE bubble — byte-identical to a chat with no hygiene
    episode at all."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, FencedStreamingAgent)

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"
    assert await _wait_for_edit(
        adapter, lambda c: c == compression_tool_deferred_line()
    ), f"hygiene card not open (non-terminal): {_edit_contents(adapter)}"
    hygiene_starts = [
        c for c in _sent_contents(adapter) if c.startswith(COMPRESSION_TOOL_START_PREFIX)
    ]
    assert len(hygiene_starts) == 1  # m1 — the open hygiene card

    # The real agent-rail consumer (TurnRunner._status_callback_sync) on a
    # ctx bound to the SAME adapter/chat/session as the hygiene card.
    turn_runner = gateway_run.TurnRunner(
        runner,
        SimpleNamespace(
            _status_adapter=adapter,
            _status_chat_id="12345",
            _run_still_current=lambda: True,
            source=SessionSource(
                platform=Platform.DISCORD,
                chat_id="12345",
                chat_type="dm",
                user_id="12345",
            ),
            session_key=SESSION_KEY,
            _status_thread_metadata=None,
            _loop_for_step=asyncio.get_running_loop(),
            _cleanup_progress=False,
            _cleanup_msg_ids=[],
            tool_progress_enabled=False,
            progress_queue=None,
            _native_slack_task_cards=False,
        ),
    )

    # 1) Raw abort warning while ONLY the hygiene card is open: must fall
    #    through to the normal rail and post as its own message. Before the
    #    rail split the shared non-terminal hygiene episode dropped it.
    warning = f"{COMPRESSION_ABORT_WARNING_PREFIX} summary provider timed out"
    turn_runner._status_callback_sync("warn", warning)
    assert await _wait_for_send(adapter, lambda c: c == warning), (
        f"agent-rail abort warning was dropped by the open hygiene card: "
        f"{_sent_contents(adapter)}"
    )

    # 2) Agent-rail compression_tool start through the REAL emit hop (token
    #    ContextVar included): opens its OWN bubble — no hand-over of the
    #    hygiene card, no edit to m1. The card's own start line has the same
    #    text, so wait for the SECOND start send to appear.
    fake_turn_agent = SimpleNamespace(
        platform="discord", status_callback=turn_runner._status_callback_sync
    )
    assert emit_compression_tool_status(
        fake_turn_agent, compression_tool_start_line(), attempt_token="agent-attempt-1"
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        start_sends = [
            c
            for c in _sent_contents(adapter)
            if c.startswith(COMPRESSION_TOOL_START_PREFIX)
        ]
        if len(start_sends) == 2:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(
            f"agent attempt must open a SEPARATE bubble, not take the card: "
            f"{_sent_contents(adapter)}"
        )
    assert {e["message_id"] for e in adapter.edits} == {"m1"}, (
        f"agent rail must never edit the hygiene card: {adapter.edits}"
    )

    # 3) Independence is symmetric: the hygiene card still owns its own
    #    lifecycle — its adoption terminal edits m1 and nothing else.
    agent = FencedStreamingAgent.last_instance
    agent.release_worker.set()
    await asyncio.wait_for(asyncio.to_thread(agent.committed.wait, 5), timeout=6)
    success_edit = await _wait_for_edit(
        adapter, lambda c: c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
    )
    assert success_edit is not None, f"hygiene adoption edit missing: {_edit_contents(adapter)}"
    assert success_edit["message_id"] == "m1"
    await _drain_deferred(runner)
    assert {e["message_id"] for e in adapter.edits} == {"m1"}


@pytest.mark.asyncio
async def test_deferred_edit_fires_exactly_once_on_double_fire(monkeypatch, tmp_path):
    """The deferred ("still compressing in the background") state is edited
    EXACTLY once per attempt even if the turn-hold edge fires twice — the
    registry already represents that exact non-terminal line on the card, so
    the re-fire must not re-edit (and must not fall back to a legacy send)."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, FencedStreamingAgent)

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    tokens = []
    real_update = runner._hygiene_compression_episode_update

    async def _capturing_update(**kwargs):
        tokens.append(kwargs.get("attempt_token"))
        return await real_update(**kwargs)

    runner._hygiene_compression_episode_update = _capturing_update

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"
    assert await _wait_for_edit(
        adapter, lambda c: c == compression_tool_deferred_line()
    ), f"deferred edit missing: {_edit_contents(adapter)}"
    assert _edit_contents(adapter).count(compression_tool_deferred_line()) == 1

    # The turn-hold edge fires AGAIN for the SAME attempt (same token): the
    # identical non-terminal line is already on the card — dropped, not
    # re-edited, and no legacy plain deferral notice appears either.
    attempt_token = next(t for t in tokens if t)
    assert await _capturing_update(
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="12345",
            chat_type="dm",
            user_id="12345",
        ),
        session_key=SESSION_KEY,
        metadata=None,
        line=compression_tool_deferred_line(),
        attempt_token=attempt_token,
    )
    await asyncio.sleep(0.1)
    assert _edit_contents(adapter).count(compression_tool_deferred_line()) == 1, (
        f"double-fired deferral must not re-edit the card: {_edit_contents(adapter)}"
    )
    assert not any(
        "deferred" in c.lower() or "still streaming" in c.lower()
        for c in _sent_contents(adapter)
    ), f"re-fired edge must not leak a legacy notice: {_sent_contents(adapter)}"

    # The episode is still healthy: the adoption terminal lands on the card.
    agent = FencedStreamingAgent.last_instance
    agent.release_worker.set()
    await asyncio.wait_for(asyncio.to_thread(agent.committed.wait, 5), timeout=6)
    assert await _wait_for_edit(
        adapter, lambda c: c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
    ), f"adoption edit missing after re-fire: {_edit_contents(adapter)}"
    await _drain_deferred(runner)
    assert {e["message_id"] for e in adapter.edits} == {"m1"}


# ---------------------------------------------------------------------------
# P2: compressor attribution in the success card
# ---------------------------------------------------------------------------


class RoutedInPlaceAgent(FastInPlaceAgent):
    """Commits in-place; its compressor recorded the ACTUAL aux route."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.context_compressor._last_compression_telemetry = {
            "aux_route_known": True,
            "aux_provider": "openrouter",
            "aux_model": "deepseek-chat-v3",
        }
        self.context_compressor._last_summary_fallback_used = False
        self.context_compressor._last_feasibility_skip = False


class LocalSummaryInPlaceAgent(RoutedInPlaceAgent):
    """Commits in-place via the deterministic LOCAL summary (provider failed)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.context_compressor._last_summary_fallback_used = True
        self.context_compressor._last_feasibility_skip = False


class FeasibilitySkipInPlaceAgent(RoutedInPlaceAgent):
    """Commits in-place via the local summary after a feasibility SKIP."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.context_compressor._last_summary_fallback_used = True
        self.context_compressor._last_feasibility_skip = True


class RoutedFencedStreamingAgent(FencedStreamingAgent):
    """Deferred adoption whose compressor recorded the ACTUAL aux route."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.context_compressor._last_compression_telemetry = {
            "aux_route_known": True,
            "aux_provider": "openrouter",
            "aux_model": "deepseek-chat-v3",
        }
        self.context_compressor._last_summary_fallback_used = False
        self.context_compressor._last_feasibility_skip = False


@pytest.mark.asyncio
async def test_inline_success_card_names_actual_compressor_route(
    monkeypatch, tmp_path
):
    """P2: the inline ✅ line reports the provider/model the compressor
    ACTUALLY selected (aux_route_known telemetry), mirroring the agent-side
    rail — not bare counts that leave attribution implicit."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, RoutedInPlaceAgent)

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"

    successes = [
        c
        for c in _edit_contents(adapter)
        if c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
    ]
    assert len(successes) == 1, f"one success edit, got: {_edit_contents(adapter)}"
    assert "openrouter/deepseek-chat-v3" in successes[0]
    assert "6 → 1 messages" in successes[0]
    # A provider-generated summary carries no local-summary label.
    assert "local deterministic summary" not in successes[0]


@pytest.mark.asyncio
async def test_inline_success_card_labels_local_summary_not_provider(
    monkeypatch, tmp_path
):
    """P2: when the engine inserted its deterministic LOCAL summary (provider
    summary unavailable), the ✅ line says so and the failed provider is
    NEVER credited with a summary it did not produce."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, LocalSummaryInPlaceAgent)

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"

    successes = [
        c
        for c in _edit_contents(adapter)
        if c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
    ]
    assert len(successes) == 1, f"one success edit, got: {_edit_contents(adapter)}"
    assert (
        "provider summary unavailable — used local deterministic summary"
        in successes[0]
    )
    assert "openrouter" not in successes[0], (
        f"failed provider must not be credited with a local summary: {successes[0]}"
    )


@pytest.mark.asyncio
async def test_inline_success_card_labels_feasibility_skip_local_summary(
    monkeypatch, tmp_path
):
    """P2: a feasibility-SKIP local summary gets the plain "local deterministic
    summary" label (not "unavailable"), and names no provider route."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(
        monkeypatch, gateway_run, tmp_path, FeasibilitySkipInPlaceAgent
    )

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"

    successes = [
        c
        for c in _edit_contents(adapter)
        if c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
    ]
    assert len(successes) == 1, f"one success edit, got: {_edit_contents(adapter)}"
    assert "· local deterministic summary" in successes[0]
    assert "unavailable" not in successes[0]
    assert "openrouter" not in successes[0]


@pytest.mark.asyncio
async def test_deferred_adoption_card_names_compressor_route(
    monkeypatch, tmp_path
):
    """P2: the deferred adoption ✅ (watermark-fenced commit boundary) also
    reports the actually selected provider/model from compressor telemetry."""
    gateway_run = importlib.import_module("gateway.run")
    gateway_run._reset_compression_episodes()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None
    _write_episode_config(tmp_path)
    _install_fakes(
        monkeypatch, gateway_run, tmp_path, RoutedFencedStreamingAgent
    )

    adapter = DiscordEpisodeAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db, Platform.DISCORD)

    result = await asyncio.wait_for(
        runner._handle_message(_make_event(Platform.DISCORD)), timeout=15
    )
    assert result == "ok"
    assert await _wait_for_edit(
        adapter, lambda c: c == compression_tool_deferred_line()
    ), f"deferred edit missing: {_edit_contents(adapter)}"

    agent = RoutedFencedStreamingAgent.last_instance
    agent.release_worker.set()
    await asyncio.wait_for(asyncio.to_thread(agent.committed.wait, 5), timeout=6)
    success_edit = await _wait_for_edit(
        adapter, lambda c: c.startswith(COMPRESSION_TOOL_SUCCESS_PREFIX)
    )
    assert success_edit is not None, f"adoption edit missing: {_edit_contents(adapter)}"
    assert "openrouter/deepseek-chat-v3" in success_edit["content"]
    assert "6 → 1 messages" in success_edit["content"]

    await _drain_deferred(runner)
    assert {e["message_id"] for e in adapter.edits} == {"m1"}
