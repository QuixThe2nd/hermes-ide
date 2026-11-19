"""Kind-labelled context-injection cards.

Every injected segment is tagged with a kind by its producer; the
``context.injected`` payload carries them as ``kinds`` and the gateway renders
one sentence-cased heading line per kind instead of the generic
"Injecting context" for every unclassifiable byte. ``kinds`` is additive
metadata next to the legacy ``sources``: a payload without it renders exactly
as before, and the model-facing bytes never change (labels are display-only).
"""

from __future__ import annotations

import json
import queue
import types
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.run import _format_context_injection_progress
from gateway.run_inbound import discord_triggering_note
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# Formatter contract (gateway.run._format_context_injection_progress)
# ---------------------------------------------------------------------------

_KIND_LABELS = [
    ("message_id", "📎 Injecting message id"),
    ("restart", "🔁 Injecting restart notice"),
    ("voice", "🎙️ Injecting voice status"),
    ("session_reset", "🆕 Injecting session reset"),
    ("intro", "👋 Injecting intro"),
    ("surface_switch", "🔀 Injecting surface switch"),
    ("plugin", "🧩 Injecting plugin context"),
]


def _fmt(**overrides):
    kwargs = dict(
        content="fact one\nfact two",
        injected_chars=18,
        sources=["gateway"],
        message_limit=4000,
        supports_code_blocks=True,
        kinds=["restart"],
    )
    kwargs.update(overrides)
    return _format_context_injection_progress(**kwargs)


def _header(messages):
    """First card's heading block (everything above the opening fence)."""
    return messages[0].split("\n```\n", 1)[0]


@pytest.mark.parametrize("kind,label", _KIND_LABELS)
def test_known_kind_renders_its_own_heading(kind, label):
    messages = _fmt(kinds=[kind])
    assert messages
    assert _header(messages).startswith(label)


def test_unknown_kind_renders_generic_heading():
    assert _header(_fmt(kinds=["mystery"])).startswith("🧠 Injecting context")
    assert _header(_fmt(kinds=[None])).startswith("🧠 Injecting context")


def test_mixed_kinds_render_one_heading_line_each_in_payload_order():
    messages = _fmt(kinds=["plugin", "surface_switch"])
    assert _header(messages) == "🧩 Injecting plugin context\n🔀 Injecting surface switch"


def test_duplicate_kinds_render_one_line():
    messages = _fmt(kinds=["restart", "restart"])
    assert _header(messages) == "🔁 Injecting restart notice"


def test_memory_kind_keeps_plain_and_counted_headings():
    assert _header(_fmt(kinds=["memory"], sources=["memory"], recalled_count=3)).startswith(
        "🧠 Injecting 3 memories"
    )
    assert _header(_fmt(kinds=["memory"], sources=["memory"], recalled_count=1)).startswith(
        "🧠 Injecting 1 memory"
    )
    # Degenerate counts fall back to the plain label — never a mislabel.
    assert _header(_fmt(kinds=["memory"], sources=["memory"])).startswith("🧠 Injecting memory")
    assert _header(_fmt(kinds=["memory"], sources=["memory"], recalled_count=0)).startswith(
        "🧠 Injecting memory"
    )
    assert _header(_fmt(kinds=["memory"], sources=["memory"], recalled_count=True)).startswith(
        "🧠 Injecting memory"
    )


def test_legacy_payload_without_kinds_renders_unchanged():
    # sources-only payload (older producers): generic stays the heading for
    # every unclassified byte, and memory-sourced legacy payloads keep the
    # memory heading.
    assert _header(_fmt(kinds=None)).startswith("🧠 Injecting context")
    assert _header(_fmt(kinds=None, sources=["memory"])).startswith("🧠 Injecting memory")
    assert _fmt(kinds=["plugin"], sources=["plugin"], include_content=False) == [
        "🧩 Injecting plugin context"
    ]


def test_discord_note_matches_anchored_message_id_heading():
    note = discord_triggering_note("9001")
    content = f"{note}\n\n<memory-context>fact</memory-context>"
    messages = _fmt(
        content=content, kinds=["memory"], sources=["gateway", "memory"],
        triggering_message_id="9001",
    )
    assert _header(messages) == "📎 Injecting message id\n🧠 Injecting memory"


def test_discord_note_with_mismatched_id_never_renders_message_id_heading():
    note = discord_triggering_note("9001")
    content = f"{note}\n\n<memory-context>fact</memory-context>"
    messages = _fmt(
        content=content, kinds=["memory"], sources=["gateway", "memory"],
        triggering_message_id="1111",
    )
    assert all("📎" not in message for message in messages)
    assert _header(messages).startswith("🧠 Injecting memory")


def test_timestamp_line_precedes_message_id_and_memory_headings():
    note = discord_triggering_note("9001")
    content = f"[Tue 2026-04-28 13:40:53 CEST] {note}\n\n<memory-context>fact</memory-context>"
    messages = _fmt(
        content=content, kinds=["memory"], sources=["gateway", "memory"],
        triggering_message_id="9001", include_content=False,
    )
    assert messages[0].endswith("Injecting time")
    assert messages[1] == "📎 Injecting message id\n🧠 Injecting memory"


def test_multiline_headings_respect_tiny_caps():
    messages = _fmt(kinds=["plugin", "surface_switch", "restart"], message_limit=50)
    assert len(messages) > 1
    assert all(len(message) <= 50 for message in messages)


# ---------------------------------------------------------------------------
# Runner seam (TurnRunner._progress_context_injection)
# ---------------------------------------------------------------------------


class _Adapter:
    supports_code_blocks = True
    MAX_MESSAGE_LENGTH = 4000


def _queue_ctx(**ctx_overrides):
    from gateway.turn_context import TurnContext

    progress_queue = queue.Queue()
    ctx = TurnContext(
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="c1", user_id="u1", chat_type="dm",
        ),
        _run_still_current=lambda: True,
        progress_mode="all",
        tool_progress_enabled=True,
        progress_queue=progress_queue,
        **ctx_overrides,
    )
    return ctx, progress_queue


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_runner_renders_payload_kinds_through_the_real_seam():
    from gateway.run_turn_runner import TurnRunner

    class _Stub:
        def _delivery_adapter_for(self, source):
            return _Adapter()

    ctx, progress_queue = _queue_ctx(
        inbound_message_id="9001",
        user_config={"display": {"context_injection_content": False}},
    )
    note = discord_triggering_note("9001")
    TurnRunner(_Stub(), ctx).progress_callback(
        "context.injected", "context", None,
        {
            "content": f"{note}\n\nctx",
            "injected_chars": 9,
            "sources": ["gateway"],
            "kinds": ["session_reset"],
        },
    )
    assert _drain(progress_queue) == ["📎 Injecting message id\n🆕 Injecting session reset"]


def test_runner_keeps_generic_heading_for_legacy_payload():
    from gateway.run_turn_runner import TurnRunner

    class _Stub:
        def _delivery_adapter_for(self, source):
            return _Adapter()

    ctx, progress_queue = _queue_ctx(
        user_config={"display": {"context_injection_content": False}},
    )
    TurnRunner(_Stub(), ctx).progress_callback(
        "context.injected", "context", None,
        {"content": "some note", "injected_chars": 9, "sources": ["gateway"]},
    )
    assert _drain(progress_queue) == ["🧠 Injecting context"]


# ---------------------------------------------------------------------------
# Turn wiring (TurnRunner._wire_turn_agent_callbacks)
# ---------------------------------------------------------------------------


def _wire(consumed_notes, runtime_note="", transition_note=None):
    """Run `_wire_turn_agent_callbacks` over minimal fakes; return the agent."""
    from gateway.run_turn_runner import TurnRunner

    agent = types.SimpleNamespace()
    ctx = types.SimpleNamespace(
        progress_callback=None,
        native_tool_start_callback=None,
        _native_slack_task_cards=False,
        native_tool_complete_callback=None,
        _step_callback_sync=None,
        _hooks_ref=types.SimpleNamespace(loaded_hooks=[]),
        _status_callback_sync=None,
        _event_callback_sync=None,
        _status_adapter=None,
        session_key="agent:main:discord:dm:c1",
        user_config={},
        source=types.SimpleNamespace(platform="discord"),
        mute_notification_reply=False,
        _thinking_enabled=False,
        agent_holder=[None],
        tools_holder=[None],
        process_task_id=None,
        process_baseline=None,
        run_generation=0,
    )
    holder = types.SimpleNamespace(
        _ctx=ctx,
        _runner=types.SimpleNamespace(
            _service_tier=None,
            _consume_pending_turn_sidecar_notes=lambda key: list(consumed_notes),
            session_store=None,
            session_id=None,
        ),
        _make_bg_review_callbacks=lambda: (lambda message: None, lambda: None),
        _merge_turn_request_overrides=TurnRunner._merge_turn_request_overrides,
        _clarify_callback_sync=lambda *a, **k: None,
        _notice_callback_sync=lambda *a, **k: None,
        _attach_session_title_callback=lambda agent, ctx: None,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "gateway.serving_transition.session_turn_notes",
            lambda store, session_id, runner: (runtime_note, transition_note),
        )
        TurnRunner._wire_turn_agent_callbacks(holder, agent, {}, None, None, None, False)
    return agent


def test_turn_wiring_joins_tagged_and_legacy_notes_with_kinds():
    from gateway.context_injection_labels import tag_turn_note

    agent = _wire(
        consumed_notes=[tag_turn_note("session_reset", "reset note"), "legacy plain note"],
    )
    assert agent._gateway_turn_context_notes == "reset note\n\nlegacy plain note"
    assert agent._gateway_turn_note_kinds == ["session_reset", None]


def test_turn_wiring_accepts_session_state_restored_tagged_entries():
    # The staged notes persist through session state; a JSON round-trip hands
    # the tagged tuples back as plain lists, so both shapes must classify.
    agent = _wire(consumed_notes=[["session_reset", "reset note"], "plain note"])
    assert agent._gateway_turn_context_notes == "reset note\n\nplain note"
    assert agent._gateway_turn_note_kinds == ["session_reset", None]


def test_turn_wiring_tags_transition_note_as_restart_not_the_runtime_note():
    # The "conversation moved processes" transition note (#829) is a restart
    # notice; the per-turn serving-identity runtime note names the serving
    # process on every turn and is NOT a restart notice — it stays
    # unclassified rather than guessing a label.
    agent = _wire(consumed_notes=[], runtime_note="runtime note", transition_note="moved note")
    assert agent._gateway_turn_context_notes == "runtime note\n\nmoved note"
    assert agent._gateway_turn_note_kinds == [None, "restart"]


def test_turn_wiring_assigns_empty_kinds_when_no_notes_staged():
    agent = _wire(consumed_notes=[])
    assert agent._gateway_turn_context_notes == ""
    assert agent._gateway_turn_note_kinds == []


# ---------------------------------------------------------------------------
# Shared contract table (internal/injectionlabels/testdata/labels.json)
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
_LABELS_TABLE = _REPO / "internal" / "injectionlabels" / "testdata" / "labels.json"


def test_headings_match_the_shared_contract_table():
    """The Python formatter and the Go twin (internal/injectionlabels) read
    the SAME table: every case's heading lines must match it byte-for-byte,
    and the table's generic/kind labels must be the module's own."""
    from gateway.context_injection_labels import (
        CONTEXT_INJECTION_GENERIC_LABEL,
        CONTEXT_INJECTION_KIND_LABELS,
        context_injection_heading_lines,
    )

    table = json.loads(_LABELS_TABLE.read_text(encoding="utf-8"))
    assert CONTEXT_INJECTION_GENERIC_LABEL == table["generic_label"]
    assert CONTEXT_INJECTION_KIND_LABELS == table["kind_labels"]
    memory = table["memory"]
    assert context_injection_heading_lines(None, memory_sourced=True) == [memory["plain"]]
    assert context_injection_heading_lines(
        ["memory"], memory_sourced=True, recalled_count=1
    ) == [memory["one"]]
    assert context_injection_heading_lines(
        ["memory"], memory_sourced=True, recalled_count=3
    ) == [memory["many"]]
    for case in table["cases"]:
        lines = context_injection_heading_lines(
            case.get("kinds"),
            memory_sourced=case["memory_sourced"],
            recalled_count=case.get("recalled_count"),
        )
        assert lines == case["expect_lines"], case["name"]
