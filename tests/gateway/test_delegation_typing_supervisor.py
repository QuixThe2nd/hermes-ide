"""Async-delegation typing supervisor on BasePlatformAdapter."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import tools.async_delegation as ad
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource, build_session_key


class _StubAdapter(BasePlatformAdapter):
    _delegation_typing_enabled = True

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _BaseOffAdapter(_StubAdapter):
    _delegation_typing_enabled = False


_tracked_adapters: list[_StubAdapter] = []


@pytest.fixture
def make_adapter():
    def _factory(*, typing_indicator: bool = True, enabled: bool = True) -> _StubAdapter:
        cls = _StubAdapter if enabled else _BaseOffAdapter
        adapter = cls(
            PlatformConfig(enabled=True, token="t", typing_indicator=typing_indicator),
            Platform.DISCORD,
        )
        adapter._delegation_typing_poll_interval = 0.05
        adapter.send_typing = AsyncMock(return_value=None)
        adapter.stop_typing = AsyncMock(return_value=None)
        adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m1"))
        _tracked_adapters.append(adapter)
        return adapter

    return _factory


@pytest_asyncio.fixture(autouse=True)
async def _adapter_teardown():
    ad._reset_for_tests()
    _tracked_adapters.clear()
    yield
    for adapter in list(_tracked_adapters):
        await adapter.cancel_background_tasks()
        await asyncio.sleep(0)
        assert not adapter._delegation_typing_tasks, (
            "delegation typing supervisor leaked after test teardown"
        )
        pending = [t for t in adapter._background_tasks if not t.done()]
        assert not pending, (
            f"background tasks still pending after test teardown: {pending!r}"
        )
    ad._reset_for_tests()
    _tracked_adapters.clear()


def _sk(chat_id="C123"):
    return build_session_key(
        SessionSource(platform=Platform.DISCORD, chat_id=chat_id, chat_type="dm")
    )


def _seed_live(session_key: str, delegation_id: str = "d1") -> None:
    with ad._records_lock:
        ad._records[delegation_id] = {
            "delegation_id": delegation_id,
            "status": "running",
            "session_key": session_key,
            "parent_session_id": "",
            "interrupt_fn": MagicMock(),
        }


def _mark_complete(delegation_id: str = "d1") -> None:
    with ad._records_lock:
        ad._records[delegation_id]["status"] = "completed"


@pytest.mark.asyncio
async def test_live_delegation_after_turn_end_keeps_typing_without_session_lock(make_adapter):
    adapter = make_adapter()
    session_key = _sk()
    _seed_live(session_key)

    await adapter._ensure_delegation_typing(session_key, "C123")
    await asyncio.sleep(0.12)

    assert session_key in adapter._delegation_typing_tasks
    assert adapter.send_typing.await_count >= 1
    assert session_key not in adapter._active_sessions


@pytest.mark.asyncio
async def test_no_live_delegation_does_not_create_supervisor(make_adapter):
    adapter = make_adapter()
    session_key = _sk()

    await adapter._ensure_delegation_typing(session_key, "C123")
    await asyncio.sleep(0.08)

    assert session_key not in adapter._delegation_typing_tasks
    adapter.send_typing.assert_not_called()


@pytest.mark.asyncio
async def test_last_completion_stops_supervisor_and_platform_typing(make_adapter):
    adapter = make_adapter()
    session_key = _sk()
    _seed_live(session_key)

    await adapter._ensure_delegation_typing(session_key, "C123")
    task = adapter._delegation_typing_tasks[session_key]
    _mark_complete()

    await asyncio.wait_for(task, timeout=1.0)

    assert session_key not in adapter._delegation_typing_tasks
    adapter.stop_typing.assert_awaited()


@pytest.mark.asyncio
async def test_session_isolation(make_adapter):
    adapter = make_adapter()
    sk_a = _sk("A")
    sk_b = _sk("B")
    _seed_live(sk_a, "d-a")

    await adapter._ensure_delegation_typing(sk_a, "A")
    await adapter._ensure_delegation_typing(sk_b, "B")
    await asyncio.sleep(0.12)

    assert sk_a in adapter._delegation_typing_tasks
    assert sk_b not in adapter._delegation_typing_tasks
    assert adapter.send_typing.await_count >= 1


@pytest.mark.asyncio
async def test_ensure_is_idempotent(make_adapter):
    adapter = make_adapter()
    session_key = _sk()
    _seed_live(session_key)

    await adapter._ensure_delegation_typing(session_key, "C123")
    first = adapter._delegation_typing_tasks[session_key]
    await adapter._ensure_delegation_typing(session_key, "C123")

    assert adapter._delegation_typing_tasks[session_key] is first


@pytest.mark.asyncio
async def test_follow_up_turn_handoff_restarts_single_supervisor(make_adapter):
    adapter = make_adapter()
    session_key = _sk()
    _seed_live(session_key)
    adapter._message_handler = AsyncMock(return_value="ok")

    event = MessageEvent(
        text="hi",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="C123", chat_type="dm"),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    bg_task = asyncio.create_task(
        adapter._process_message_background(event, session_key)
    )
    adapter._session_tasks[session_key] = bg_task
    await bg_task

    live_tasks = [
        t for t in adapter._delegation_typing_tasks.values() if not t.done()
    ]
    assert len(live_tasks) == 1
    assert adapter.send_typing.await_count >= 1


@pytest.mark.asyncio
async def test_in_band_drain_parent_does_not_start_supervisor_during_follow_up(make_adapter):
    adapter = make_adapter()
    session_key = _sk()
    _seed_live(session_key)

    follow_up = MessageEvent(
        text="follow-up",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="C123", chat_type="dm"),
    )
    drain_entered = asyncio.Event()
    release_drain = asyncio.Event()

    async def handler(event):
        if event.text == "hi":
            adapter._pending_messages[session_key] = follow_up
            return "ok"
        drain_entered.set()
        await release_drain.wait()
        return "ok"

    adapter._message_handler = handler
    adapter._active_sessions[session_key] = asyncio.Event()

    parent_event = MessageEvent(
        text="hi",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.DISCORD, chat_id="C123", chat_type="dm"),
    )
    parent_task = asyncio.create_task(
        adapter._process_message_background(parent_event, session_key)
    )
    adapter._session_tasks[session_key] = parent_task
    await drain_entered.wait()
    assert session_key not in adapter._delegation_typing_tasks
    release_drain.set()
    drain_task = adapter._session_tasks.get(session_key)
    assert drain_task is not None
    await asyncio.gather(parent_task, drain_task)

    live_tasks = [
        t for t in adapter._delegation_typing_tasks.values() if not t.done()
    ]
    assert len(live_tasks) == 1


@pytest.mark.asyncio
async def test_stop_delegation_typing_propagates_caller_cancelled(make_adapter):
    adapter = make_adapter()
    session_key = _sk()
    _seed_live(session_key)

    await adapter._ensure_delegation_typing(session_key, "C123")

    stop_task = asyncio.create_task(
        adapter._stop_delegation_typing(session_key, "C123")
    )
    await asyncio.sleep(0)
    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task


@pytest.mark.asyncio
async def test_expected_cancelled_task_does_not_ensure_supervisor(make_adapter):
    adapter = make_adapter()
    session_key = _sk()
    _seed_live(session_key)
    adapter._session_tasks[session_key] = asyncio.current_task()
    adapter._expected_cancelled_tasks.add(asyncio.current_task())

    await adapter._ensure_delegation_typing(session_key, "C123")

    assert session_key not in adapter._delegation_typing_tasks
    adapter.send_typing.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_background_tasks_cleans_supervisor(make_adapter):
    adapter = make_adapter()
    session_key = _sk()
    _seed_live(session_key)

    await adapter._ensure_delegation_typing(session_key, "C123")
    supervisor = adapter._delegation_typing_tasks[session_key]
    assert supervisor in adapter._background_tasks

    await asyncio.wait_for(adapter.cancel_background_tasks(), timeout=5.0)

    assert supervisor.done()
    assert session_key not in adapter._delegation_typing_tasks
    adapter.stop_typing.assert_awaited()


@pytest.mark.asyncio
async def test_default_off_base_adapter_never_starts_supervisor(make_adapter):
    adapter = make_adapter(enabled=False)
    session_key = _sk()
    _seed_live(session_key)

    await adapter._ensure_delegation_typing(session_key, "C123")
    await asyncio.sleep(0.08)

    assert session_key not in adapter._delegation_typing_tasks
    adapter.send_typing.assert_not_called()


@pytest.mark.asyncio
async def test_typing_indicator_disabled_suppresses_supervisor(make_adapter):
    adapter = make_adapter(typing_indicator=False)
    session_key = _sk()
    _seed_live(session_key)

    await adapter._ensure_delegation_typing(session_key, "C123")
    await asyncio.sleep(0.08)

    assert session_key not in adapter._delegation_typing_tasks
    adapter.send_typing.assert_not_called()


@pytest.mark.asyncio
async def test_stop_delegation_typing_cleans_platform_typing(make_adapter):
    adapter = make_adapter()
    session_key = _sk()
    _seed_live(session_key)

    await adapter._ensure_delegation_typing(session_key, "C123")
    await adapter._stop_delegation_typing(session_key, "C123")

    assert session_key not in adapter._delegation_typing_tasks
    adapter.stop_typing.assert_awaited()
