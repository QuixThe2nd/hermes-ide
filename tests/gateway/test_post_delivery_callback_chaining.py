"""Tests for ``BasePlatformAdapter.register_post_delivery_callback`` chaining.

When two features want to run after the final response lands on the same
session (e.g. background-review release + temporary-progress cleanup), the
registration API chains them rather than clobbering. Per-callback
exceptions are swallowed so one bad callback can't sabotage the others.
Stale-generation registrations are rejected.

The chained wrapper is ``async`` so it transparently supports sync or async
callbacks — the outer invoker in ``_handle_message`` awaits awaitable
callbacks, and a sync wrapper would silently drop coroutine results from
async callbacks chained behind it.
"""
import asyncio
import inspect

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult


class _MinAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.fixture
def adapter():
    return _MinAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)


def _live_run(adapter, key: str = "s") -> asyncio.Event:
    """Install the session guard a live turn holds (what makes registration
    acceptable — the boundary consumer exists)."""
    event = asyncio.Event()
    adapter._active_sessions[key] = event
    return event


def _invoke(cb):
    """Invoke a popped callback, awaiting if it returns a coroutine.
    Single-registration callbacks are returned as the raw user callable
    (sync). Chained callbacks (two or more registrations on the same
    session) are wrapped in an async helper. Tests use this helper so
    they don't have to care which case they're exercising.
    """
    result = cb()
    if inspect.isawaitable(result):
        asyncio.run(result)


class TestPostDeliveryCallbackChaining:
    def test_single_callback_fires(self, adapter):
        _live_run(adapter)
        fired = []
        adapter.register_post_delivery_callback("s", lambda: fired.append("A"))
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["A"]

    def test_two_callbacks_chain_in_order(self, adapter):
        _live_run(adapter)
        fired = []
        adapter.register_post_delivery_callback("s", lambda: fired.append("A"))
        adapter.register_post_delivery_callback("s", lambda: fired.append("B"))
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["A", "B"]

    def test_three_callbacks_chain_in_order(self, adapter):
        """Chain composes over an already-chained callback."""
        _live_run(adapter)
        fired = []
        for label in ("A", "B", "C"):
            adapter.register_post_delivery_callback(
                "s", lambda x=label: fired.append(x)
            )
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["A", "B", "C"]


class TestPostDeliveryCallbackAsyncChaining:
    """When an async callback is chained, the wrapper must await it.

    Regression test for a bug where the sync ``_chained`` wrapper called
    async callbacks without awaiting, silently dropping the returned
    coroutine. This broke ``/goal`` continuations (Discord etc.) where
    the continuation injection is an async ``_deliver()`` coroutine.
    """

    def test_async_callback_in_chain_is_awaited(self, adapter):
        _live_run(adapter)
        fired = []

        async def async_cb():
            await asyncio.sleep(0)
            fired.append("async")

        adapter.register_post_delivery_callback("s", lambda: fired.append("sync"))
        adapter.register_post_delivery_callback("s", async_cb)
        cb = adapter.pop_post_delivery_callback("s")
        _invoke(cb)
        assert fired == ["sync", "async"]


class TestPostDeliveryBoundaryLifecycle:
    """Registration acceptance mirrors the delivery boundary's lifecycle.

    ``_fire_post_delivery_callback`` (the turn task's finally) is the
    registry's ONLY consumer. A registration that arrives when no run is
    active, or after the run's boundary already fired, would sit in the
    registry until an unrelated future turn — a stranded payload. Such
    registrations must be refused so the caller can deliver directly.
    """

    def test_registration_without_an_active_run_is_refused(self, adapter):
        fired = []
        assert adapter.register_post_delivery_callback("s", lambda: fired.append("A")) is False
        assert adapter._post_delivery_callbacks == {}
        assert adapter.pop_post_delivery_callback("s") is None
        assert fired == []

    def test_registration_during_an_active_run_is_accepted(self, adapter):
        adapter._active_sessions["s"] = asyncio.Event()
        assert adapter.register_post_delivery_callback("s", lambda: None) is True
        assert "s" in adapter._post_delivery_callbacks

    def test_registration_after_the_boundary_fired_is_refused(self, adapter):
        """The turn's boundary fired (response delivered, errored or
        interrupted turn): nothing will pop a new entry this run."""
        event = asyncio.Event()
        adapter._active_sessions["s"] = event
        fired = []
        assert adapter.register_post_delivery_callback("s", lambda: fired.append("A")) is True
        asyncio.run(adapter._fire_post_delivery_callback("s", event))
        assert fired == ["A"]  # the pre-boundary registration fired exactly once
        assert adapter.register_post_delivery_callback("s", lambda: fired.append("B")) is False
        assert adapter._post_delivery_callbacks == {}
        asyncio.run(adapter._fire_post_delivery_callback("s", event))
        assert fired == ["A"]  # refiring must not resurrect the refused payload

    def test_a_followup_run_reuses_the_guard_and_accepts_again(self, adapter):
        """Drain handoffs reuse the guard Event across runs (the drain turn
        re-reads it in _process_message_background); run 1's fired marker must
        not refuse run 2's registrations — each run resets it at entry."""
        from gateway.platforms.event import MessageEvent
        from gateway.session import SessionSource

        adapter.config.typing_indicator = False
        accepted: list = []
        fired: list = []

        async def noop_handler(event):
            return None

        async def registering_handler(event):
            accepted.append(
                adapter.register_post_delivery_callback("s", lambda: fired.append("B"))
            )
            return None

        event = asyncio.Event()
        adapter._active_sessions["s"] = event
        ev = MessageEvent(
            text="hi",
            source=SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1"),
        )

        async def _turn(handler):
            adapter.set_message_handler(handler)
            await adapter._process_message_background(ev, "s")

        asyncio.run(_turn(noop_handler))  # run 1: boundary fires, marker set
        assert adapter.register_post_delivery_callback("s", lambda: None) is False
        asyncio.run(_turn(registering_handler))  # run 2 reuses the guard Event
        assert accepted == [True]
        assert fired == ["B"]  # run 2's boundary fired its own registration
