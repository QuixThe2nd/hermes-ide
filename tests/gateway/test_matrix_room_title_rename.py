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

    # Child rooms carry the room id in chat_id; thread_id stays None.
    source = types.SimpleNamespace(
        platform=Platform.MATRIX,
        chat_id="!child:example.org",
        chat_type="dm",
        thread_id=None,
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


# ── source stamping through the real resolution path ──────────────────────────────────────


def _bare_context_adapter(is_child_room) -> MatrixAdapter:
    """A MatrixAdapter built via object.__new__ with just enough stubs for
    ``_resolve_message_context`` on a DM room."""
    from plugins.platforms.matrix.adapter import MatrixRoomIdentity

    adapter = object.__new__(MatrixAdapter)
    adapter.platform = Platform.MATRIX
    adapter.gateway_runner = None
    adapter.get_bot_display_name = lambda **kw: "Bot"
    identity = MatrixRoomIdentity(
        room_id="!child:example.org", room_name="Initial words", room_topic=None,
        canonical_alias=None, server_name="example.org", joined_member_count=2,
        is_direct_account_data=False, display_name="Initial words",
        has_explicit_name=True, chat_type="dm")

    async def _identity(room_id, *, force_refresh=False):
        return identity

    async def _dm(room_id):
        return True

    async def _display_name(room_id, sender):
        return "Tester"

    adapter._resolve_room_identity = _identity
    adapter._is_dm_room = _dm
    adapter._is_bot_mentioned = lambda *a, **k: False
    adapter._get_display_name = _display_name
    adapter._threads = types.SimpleNamespace(mark=lambda tid: None)
    adapter._dm_mention_threads = False
    adapter._dm_auto_thread = False
    adapter._background_read_receipt = lambda *a: None
    adapter._inbox_router = types.SimpleNamespace(is_child_room=is_child_room)
    return adapter


@pytest.mark.anyio
async def test_resolve_message_context_stamps_inbox_child_markers():
    adapter = _bare_context_adapter(lambda rid: rid == "!child:example.org")
    ctx = await adapter._resolve_message_context(
        "!child:example.org", "@tester:example.org", "$e1", "hi", {"body": "hi"}, {},
        initial_room_name="Initial words")
    assert ctx is not None
    source = ctx[5]
    assert source.auto_thread_created is True
    assert source.auto_thread_initial_name == "Initial words"
    assert source.thread_id is None  # child rooms are rooms, not threads


@pytest.mark.anyio
async def test_resolve_message_context_plain_dm_not_stamped():
    # A caller-supplied name can never mark a normal DM: is_child_room is the gate.
    adapter = _bare_context_adapter(lambda rid: False)
    ctx = await adapter._resolve_message_context(
        "!child:example.org", "@tester:example.org", "$e1", "hi", {"body": "hi"}, {},
        initial_room_name="Initial words")
    assert ctx is not None
    assert ctx[5].auto_thread_created is False
    assert not ctx[5].auto_thread_initial_name


@pytest.mark.anyio
async def test_resolve_message_context_child_without_name_not_stamped():
    adapter = _bare_context_adapter(lambda rid: True)
    ctx = await adapter._resolve_message_context(
        "!child:example.org", "@tester:example.org", "$e1", "hi", {"body": "hi"}, {})
    assert ctx is not None
    assert ctx[5].auto_thread_created is False
    assert not ctx[5].auto_thread_initial_name


# ── hub client rename proxy ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_hub_client_rename_room_proxy():
    from plugins.platforms.matrix.hub_client import MatrixHubClientAdapter

    adapter = object.__new__(MatrixHubClientAdapter)
    calls: list = []

    async def _rpc(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return True

    adapter._rpc = _rpc
    ok = await adapter.rename_room(
        "!child:example.org", "Semantic Title", only_if_current_name="Initial words")
    assert ok is True
    assert calls == [(
        "rename_room", ("!child:example.org", "Semantic Title"),
        {"only_if_current_name": "Initial words"})]


# ── InboxStore persistence of the initial room name ────────────────────────────────────────


def _old_schema_sql() -> str:
    return """
        CREATE TABLE matrix_inbox_routes (
            root_key TEXT PRIMARY KEY,
            inbox_room_id TEXT NOT NULL,
            root_event_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            encrypted INTEGER NOT NULL,
            parent_space_id TEXT,
            child_room_id TEXT,
            state TEXT NOT NULL,
            origin_link_event_id TEXT,
            acknowledgement_event_id TEXT,
            dispatch_accepted INTEGER NOT NULL DEFAULT 0,
            space_link_state TEXT NOT NULL DEFAULT 'pending',
            failure_notice_sent INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
    """


def test_inbox_store_persists_initial_room_name(tmp_path):
    from plugins.platforms.matrix.adapter_inbox import InboxRoute, _InboxStore

    db = tmp_path / "inbox.db"
    store = _InboxStore(db)
    store.begin(InboxRoute(
        root_key="rk", inbox_room_id="!inbox:example.org", root_event_id="$e1",
        sender="@tester:example.org", encrypted=False))
    store.mark_ready("rk", "!child:example.org", False, initial_room_name="Initial words")

    reopened = _InboxStore(db)
    routes = reopened.ready_routes()
    assert len(routes) == 1
    assert routes[0].child_room_id == "!child:example.org"
    assert routes[0].initial_room_name == "Initial words"


def test_inbox_store_migrates_old_schema(tmp_path):
    import sqlite3

    from plugins.platforms.matrix.adapter_inbox import _InboxStore

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(_old_schema_sql())
    conn.execute(
        """INSERT INTO matrix_inbox_routes
           (root_key, inbox_room_id, root_event_id, sender, encrypted, child_room_id,
            state, created_at, updated_at)
           VALUES ('rk', '!inbox:example.org', '$e1', '@tester:example.org', 0,
                   '!child:example.org', 'ready', 1.0, 1.0)""")
    conn.commit()
    conn.close()

    store = _InboxStore(db)
    routes = store.ready_routes()  # opening the store runs the defensive ALTER
    assert len(routes) == 1
    assert routes[0].child_room_id == "!child:example.org"
    assert routes[0].initial_room_name is None
