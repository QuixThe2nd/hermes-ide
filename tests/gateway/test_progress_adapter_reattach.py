"""Mid-turn adapter reconnect: in-flight progress pump and stream consumer must follow the live transport."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.stream_consumer import GatewayStreamConsumer
from gateway.turn_context import TurnContext
from tools import clarify_gateway as cm


class _RecordingAdapter:
    """Minimal editable-progress adapter that records transport calls."""

    name = "test"
    MAX_MESSAGE_LENGTH = 100_000

    def __init__(self, label: str):
        self.label = label
        self.calls: list[tuple] = []

    async def send(self, chat_id=None, content=None, reply_to=None, metadata=None, **_kw):
        self.calls.append(("send", self.label, content))
        return SendResult(success=True, message_id=f"msg-{self.label}-{len(self.calls)}")

    async def edit_message(
        self, chat_id=None, message_id=None, content=None, metadata=None, **_kw
    ):
        self.calls.append(("edit", self.label, message_id, content))
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        self.calls.append(("typing", self.label, chat_id))


class _StubRunner:
    def __init__(self, adapter):
        self.current_adapter = adapter

    def _adapter_for_source(self, _source):
        return self.current_adapter

    def _delivery_adapter_for(self, _source):
        return self.current_adapter


class _StatusCapableAdapter:
    def __init__(self, token: object, *, supports_status_text: bool = True):
        self.token = token
        self.supports_status_text = supports_status_text


def _fixed_live_status_adapter_provider(runner, source, mode, lookup_attr):
    """Same closure shape as gateway/run.py and gateway/run_turn.py after the walrus fix."""

    def _live_status_adapter_provider():
        if mode == "off":
            return None
        a = getattr(runner, lookup_attr)(source)
        return a if getattr(a, "supports_status_text", False) else None

    return _live_status_adapter_provider


@pytest.mark.parametrize("lookup_attr", ["_adapter_for_source", "_delivery_adapter_for"])
def test_live_status_adapter_provider_resolves_live_adapter(lookup_attr):
    source = object()
    live_a = _StatusCapableAdapter("first")
    live_b = _StatusCapableAdapter("second")
    runner = _StubRunner(live_a)
    provider = _fixed_live_status_adapter_provider(runner, source, "full", lookup_attr)

    result = provider()
    assert result is not None
    assert result is live_a

    runner.current_adapter = live_b
    reattached = provider()
    assert reattached is not None
    assert reattached is live_b
    assert reattached is not live_a


@pytest.mark.parametrize("lookup_attr", ["_adapter_for_source", "_delivery_adapter_for"])
def test_live_status_adapter_provider_off_mode_returns_none(lookup_attr):
    source = object()
    runner = _StubRunner(_StatusCapableAdapter("ignored"))
    provider = _fixed_live_status_adapter_provider(runner, source, "off", lookup_attr)
    assert provider() is None


@pytest.mark.parametrize("lookup_attr", ["_adapter_for_source", "_delivery_adapter_for"])
def test_live_status_adapter_provider_without_capability_returns_none(lookup_attr):
    source = object()
    runner = _StubRunner(_StatusCapableAdapter("no-status", supports_status_text=False))
    provider = _fixed_live_status_adapter_provider(runner, source, "full", lookup_attr)
    assert provider() is None


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def _make_turn_context(progress_q: queue.Queue, still: list[bool]) -> TurnContext:
    source = MagicMock()
    source.chat_id = "chat-1"
    return TurnContext(
        source=source,
        _run_still_current=lambda: still[0],
        progress_queue=progress_q,
        progress_grouping="accumulate",
        progress_codebox=False,
        _progress_metadata=None,
        _progress_reply_to=None,
        _native_slack_task_cards=False,
        _cleanup_progress=False,
        agent_holder=[None],
    )


@pytest.mark.parametrize(
    "turn_runner_path",
    [
        pytest.param("gateway.run", id="live"),
        pytest.param("gateway.run_turn_runner", id="shadow"),
    ],
)
@pytest.mark.asyncio
async def test_progress_pump_re_resolves_adapter_after_mid_turn_swap(turn_runner_path):
    mod = importlib.import_module(turn_runner_path)
    TurnRunner = mod.TurnRunner

    progress_q: queue.Queue = queue.Queue()
    still = [True]
    ctx = _make_turn_context(progress_q, still)

    adapter_a = _RecordingAdapter("A")
    adapter_b = _RecordingAdapter("B")
    runner = _StubRunner(adapter_a)
    turn = TurnRunner(runner, ctx)

    task = asyncio.create_task(turn.send_progress_messages())
    try:
        progress_q.put("first tool line")
        await _wait_until(lambda: any(c[0] == "send" for c in adapter_a.calls))
        # Progress edits are throttled (1.5s); let the first bubble settle before the
        # post-swap row so the next queue item reaches the edit path in one iteration.
        await asyncio.sleep(1.6)

        calls_on_a_at_swap = len(adapter_a.calls)
        runner.current_adapter = adapter_b

        progress_q.put("second tool line")
        await _wait_until(
            lambda: any(
                c[0] in ("send", "edit") and c[1] == "B" for c in adapter_b.calls
            )
        )

        assert len(adapter_a.calls) == calls_on_a_at_swap, (
            "adapter A must receive no further transport calls after the swap"
        )
        post_swap_b = [
            c
            for c in adapter_b.calls
            if c[0] in ("send", "edit", "typing")
        ]
        assert post_swap_b, "adapter B must handle post-swap progress delivery"
        assert all(c[1] == "B" for c in post_swap_b)
    finally:
        still[0] = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_stream_consumer_adapter_provider_follows_live_adapter():
    adapter_a = MagicMock(name="adapter_a")
    adapter_b = MagicMock(name="adapter_b")
    slot = {"live": adapter_a}

    def provider():
        return slot["live"]

    consumer = GatewayStreamConsumer(
        adapter=adapter_a,
        chat_id="chat-1",
        adapter_provider=provider,
    )
    assert consumer.adapter is adapter_a

    slot["live"] = adapter_b
    assert consumer.adapter is adapter_b

    slot["live"] = None
    assert consumer.adapter is adapter_a


@pytest.mark.parametrize("prop_name,snapshot_field,provider_field", [
    ("_status_adapter", "_status_adapter_snapshot", "_status_adapter_provider"),
    ("_live_status_adapter", "_live_status_adapter_snapshot", "_live_status_adapter_provider"),
])
def test_turn_context_adapter_property_follows_provider(prop_name, snapshot_field, provider_field):
    adapter_a = object()
    adapter_b = object()
    slot = {"live": adapter_a}

    ctx = TurnContext(**{
        snapshot_field: adapter_a,
        provider_field: lambda: slot["live"],
    })
    assert getattr(ctx, prop_name) is adapter_a

    slot["live"] = adapter_b
    assert getattr(ctx, prop_name) is adapter_b

    slot["live"] = None
    assert getattr(ctx, prop_name) is adapter_a

    def _raise():
        raise RuntimeError("boom")

    setattr(ctx, provider_field, _raise)
    assert getattr(ctx, prop_name) is adapter_a


def test_turn_context_adapter_property_new_without_init():
    ctx = TurnContext.__new__(TurnContext)
    ctx._status_adapter = object()
    assert ctx._status_adapter is ctx._status_adapter_snapshot
    ctx._live_status_adapter = object()
    assert ctx._live_status_adapter is ctx._live_status_adapter_snapshot


class _ClarifySwapAdapter(BasePlatformAdapter):
    """Card send fails; adapter A swaps the provider slot before the text fallback reads ctx."""

    def __init__(self, label: str, *, on_card=None):
        self.label = label
        self.sent_text: list[str] = []
        self._on_card = on_card

    def pause_typing_for_chat(self, chat_id):
        return None

    def resume_typing_for_chat(self, chat_id):
        return None

    async def send_clarify(self, **kwargs):
        if self._on_card is not None:
            self._on_card()
        return SendResult(success=False, error="card rejected")

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent_text.append(content)
        return SendResult(success=True, message_id=f"t-{self.label}")

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {}


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


def test_clarify_text_fallback_re_resolves_status_adapter_after_mid_turn_swap(loop, monkeypatch):
    from gateway.run_turn_runner import TurnRunner

    slot = {"live": None}
    adapter_b = _ClarifySwapAdapter("B")
    adapter_a = _ClarifySwapAdapter("A", on_card=lambda: slot.update(live=adapter_b))
    slot["live"] = adapter_a

    ctx = TurnContext(
        _status_adapter_snapshot=adapter_a,
        _status_adapter_provider=lambda: slot["live"],
        _status_chat_id="42",
        _status_thread_metadata=None,
        session_key="sk-reattach",
        stream_consumer_holder=[None],
        _loop_for_step=loop,
        _run_still_current=lambda: True,
        source=SimpleNamespace(chat_id="42", platform="telegram"),
    )
    runner = TurnRunner(object(), ctx)
    runner._close_native_stream_boundary = lambda *a, **k: None
    monkeypatch.setattr(cm, "get_clarify_timeout", lambda: 5)

    class _Fut:
        def __init__(self, result):
            self._r = result

        def result(self, timeout=None):
            return self._r

    runner._schedule = lambda coro, _label: _Fut(asyncio.run(coro))

    _response, answered = runner._ask_clarify_question("Pick?", ["alpha", "beta"], False)
    assert answered is False
    assert adapter_a.sent_text == []
    assert len(adapter_b.sent_text) == 1
    assert "1. alpha" in adapter_b.sent_text[0]
