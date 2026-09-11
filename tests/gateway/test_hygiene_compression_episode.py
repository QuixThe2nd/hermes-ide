"""Discord tool-style episode card for gateway session-hygiene compression.

Hygiene is the SECOND automatic compaction pass (gateway/run.py pre-agent
safety pass at 85% context). In-agent compression already posts one
``🗜️ context_compress · …`` message edited through its lifecycle
(agent/compression_status.py → the gateway episode rail); hygiene used to be
fully silent on Discord while a multi-minute summarization ran.

These tests pin the presentation-only contract: hygiene drives the SAME
per-(adapter, chat, session) episode state directly from the gateway (the
detached hygiene agent keeps its deliberately stale platform and no
status_callback), with a per-attempt token so a superseded attempt's late
edit cannot clobber its successor's episode. Card edges:

- start line the moment the summary executor is spawned;
- ONE non-terminal "still running in the background" edit at turn-hold
  expiry while the watermark-fenced worker keeps streaming;
- ✅ ONLY at the watermark-fenced adoption boundary (deferred path) or when
  the inline wait observed the commit;
- ⚠️ on timeout-cancel, fence-cancel, and did-not-commit outcomes — never a
  success for a no-op;
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
    COMPRESSION_TOOL_FAILURE_PREFIX,
    COMPRESSION_TOOL_START_PREFIX,
    COMPRESSION_TOOL_SUCCESS_PREFIX,
    compression_tool_deferred_line,
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
