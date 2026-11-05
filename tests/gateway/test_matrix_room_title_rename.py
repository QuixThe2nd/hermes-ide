"""Matrix inbox-room auto-title lane.

Inbox child rooms are created with a static "New conversation"-style name. The
gateway's session titler should rename them to the LLM-generated session title
exactly like Discord auto-threads: llm titles only (a rate-limited state event
per rename), and only when the room still carries its initial name (a human
rename must never be clobbered).
"""

from __future__ import annotations

import inspect
import types

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.matrix.adapter import MatrixAdapter
from plugins.platforms.matrix.adapter_inbox import _room_title


def test_room_title_derived_from_body():
    title = _room_title(
        "m.text",
        "hello   world this is a long body that keeps going and going "
        "beyond the limit for sure yes really",
    )
    assert title == "hello world this is a long body that keeps going"[:48]
    assert len(title) == 48


def test_room_title_media_fallback():
    assert _room_title("m.image") == "Image conversation"
    assert _room_title("m.text") == "New conversation"


def _attach_matrix():
    renames: list = []
    source = types.SimpleNamespace(platform=Platform.MATRIX, chat_id="!room:example.org")

    runner = types.SimpleNamespace(
        _is_telegram_topic_lane=lambda src: False,
        _is_matrix_inbox_room_lane=lambda src: True,
        _is_discord_auto_thread_lane=lambda src: False,
        _is_relay_discord_channel_lane=lambda src: False,
        _schedule_telegram_topic_title_rename=lambda src, sid, title: None,
        _schedule_discord_semantic_thread_rename=lambda src, sid, title: None,
        _schedule_matrix_room_title_rename=lambda src, sid, title: renames.append(title),
    )
    holder = types.SimpleNamespace(
        _runner=runner,
        _attach_session_title_callback=TurnRunner._attach_session_title_callback,
    )
    agent = types.SimpleNamespace(session_id="sess-1")
    holder._attach_session_title_callback(
        holder, agent, types.SimpleNamespace(source=source)
    )
    return agent._on_session_title, renames


def test_attach_matrix_lane_llm_only():
    callback, renames = _attach_matrix()

    callback("derived slice of the user words", "derived")
    assert renames == []

    callback("LLM Session Title", "llm")
    assert renames == ["LLM Session Title"]


class _StubClient:
    def __init__(self, current_name):
        self.current_name = current_name
        self.state_events: list[tuple[str, object, dict]] = []

    async def get_state_event(self, room_id, event_type):
        return {"name": self.current_name} if self.current_name else {}

    async def send_state_event(self, room_id, event_type, content, **kwargs):
        self.state_events.append((str(room_id), event_type, dict(content)))
        return "$evt"


@pytest.fixture
def matrix_adapter(monkeypatch):
    adapter = object.__new__(MatrixAdapter)
    return adapter


@pytest.mark.anyio
async def test_rename_room_guard(matrix_adapter):
    client = _StubClient("Initial")
    matrix_adapter._client = client
    ok = await matrix_adapter.rename_room(
        "!child:example.org", "Semantic Title", only_if_current_name="Initial")
    assert ok is True
    assert len(client.state_events) == 1
    room_id, event_type, content = client.state_events[0]
    assert room_id == "!child:example.org"
    assert content == {"name": "Semantic Title"}

    # Human renamed the room in between: guard declines, no state event.
    client2 = _StubClient("Human Renamed")
    matrix_adapter._client = client2
    ok = await matrix_adapter.rename_room(
        "!child:example.org", "Semantic Title", only_if_current_name="Initial")
    assert ok is False
    assert client2.state_events == []


@pytest.mark.anyio
async def test_rename_room_idempotent_and_invalid(matrix_adapter):
    client = _StubClient("Same Name")
    matrix_adapter._client = client
    assert await matrix_adapter.rename_room("!child:example.org", "Same Name") is True
    assert client.state_events == []  # no API call when unchanged

    matrix_adapter._client = None
    assert await matrix_adapter.rename_room("!child:example.org", "x") is False
    matrix_adapter._client = _StubClient(None)
    assert await matrix_adapter.rename_room("!child:example.org", "   ") is False


@pytest.mark.anyio
async def test_matrix_lane_rename_passes_initial_name_guard():
    """The Matrix rename lane must honor the strict adapter contract."""
    calls: list[tuple[str, str, str | None]] = []

    class StrictMatrixAdapter:
        async def rename_room(
            self, room_id: str, name: str, *, only_if_current_name: str | None = None,
        ) -> bool:
            calls.append((room_id, name, only_if_current_name))
            return True

    class MatrixRenameRunner:
        _is_matrix_inbox_room_lane = GatewayRunner._is_matrix_inbox_room_lane
        _sanitize_matrix_room_title = GatewayRunner._sanitize_matrix_room_title
        _rename_matrix_room_for_session_title = (
            GatewayRunner._rename_matrix_room_for_session_title
        )

        def __init__(self, adapter):
            self.adapters = {Platform.MATRIX: adapter}

        def _delivery_adapter_for(self, source):
            return self.adapters[source.platform]

    source = types.SimpleNamespace(
        platform=Platform.MATRIX,
        chat_id="!room:example.org",
        chat_type="dm",
        thread_id="!child:example.org",
        auto_thread_created=True,
        auto_thread_initial_name="Initial words",
    )

    runner = MatrixRenameRunner(StrictMatrixAdapter())
    await runner._rename_matrix_room_for_session_title(
        source, "session-1", "Semantic Session Title")

    assert calls == [("!child:example.org", "Semantic Session Title", "Initial words")]


def test_rename_room_hub_rpc_reachable():
    sig = inspect.signature(MatrixAdapter.rename_room)
    assert "only_if_current_name" in sig.parameters
    assert sig.parameters["only_if_current_name"].default is None
    assert not MatrixAdapter.rename_room.__name__.startswith("_")
