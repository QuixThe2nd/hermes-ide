"""Gateway coverage for the counted recall display heading.

* The counted injection heading — ``_format_context_injection_progress``
  renders ``🧠 Recalled N memories`` (singular for 1) for a memory-sourced
  injection carrying a positive integer ``recalled_count``; every other shape
  keeps the existing headings.

End-to-end: the ``context.injected`` events flow through the real
``TurnRunner.progress_callback`` into a recording progress queue, and the
status texts flow through the real ``_status_callback_sync`` with a recording
scheduler — no adapter internals involved.
"""

from __future__ import annotations

import queue as queue_mod
from types import SimpleNamespace

import pytest

from gateway.turn_context import TurnContext


def _make_runner(ctx, adapter=None):
    from gateway.run import TurnRunner

    class _StubGatewayRunner:
        def _adapter_for_source(self, source):
            return adapter

    return TurnRunner(_StubGatewayRunner(), ctx)


# ---------------------------------------------------------------------------
# Counted injection heading — _format_context_injection_progress
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "recalled_count,expected",
    [
        (1, "🧠 Recalled 1 memory"),
        (3, "🧠 Recalled 3 memories"),
        (2, "🧠 Recalled 2 memories"),
    ],
)
def test_counted_heading_for_positive_counts(recalled_count, expected):
    from gateway.run import _format_context_injection_progress

    assert _format_context_injection_progress(
        content="recalled bytes",
        injected_chars=14,
        sources=["memory"],
        message_limit=4000,
        supports_code_blocks=True,
        include_content=False,
        recalled_count=recalled_count,
    ) == [expected]


@pytest.mark.parametrize("recalled_count", [None, 0, -1, "3", 3.0, True])
def test_non_positive_or_non_int_counts_keep_injecting_memory_heading(recalled_count):
    from gateway.run import _format_context_injection_progress

    assert _format_context_injection_progress(
        content="recalled bytes",
        injected_chars=14,
        sources=["memory"],
        message_limit=4000,
        supports_code_blocks=True,
        include_content=False,
        recalled_count=recalled_count,
    ) == ["🧠 Injecting Memory"]


def test_count_ignored_for_non_memory_sources():
    from gateway.run import _format_context_injection_progress

    assert _format_context_injection_progress(
        content="gateway note",
        injected_chars=12,
        sources=["gateway"],
        message_limit=4000,
        supports_code_blocks=True,
        include_content=False,
        recalled_count=3,
    ) == ["🧠 Context Injection"]


def test_default_heading_unchanged_without_count():
    from gateway.run import _format_context_injection_progress

    assert _format_context_injection_progress(
        content="recalled bytes",
        injected_chars=14,
        sources=["memory"],
        message_limit=4000,
        supports_code_blocks=True,
        include_content=False,
    ) == ["🧠 Injecting Memory"]


def test_counted_heading_reused_on_split_chunks():
    from gateway.run import _format_context_injection_progress

    messages = _format_context_injection_progress(
        content="x" * 500,
        injected_chars=500,
        sources=["memory"],
        message_limit=128,
        supports_code_blocks=True,
        recalled_count=7,
    )
    assert len(messages) > 1
    assert messages[0].startswith("🧠 Recalled 7 memories\n")
    assert all(message.startswith("🧠 Recalled 7 memories\n") for message in messages)
    assert all(len(message) <= 128 for message in messages)


def test_counted_heading_still_redacts_and_neutralises():
    from gateway.run import _format_context_injection_progress

    messages = _format_context_injection_progress(
        content="token=sk-live-abc123secret ping @someone",
        injected_chars=36,
        sources=["memory"],
        message_limit=4000,
        supports_code_blocks=True,
        recalled_count=1,
    )
    assert len(messages) == 1
    assert messages[0].startswith("🧠 Recalled 1 memory\n")
    body = messages[0]
    assert "sk-live-abc123secret" not in body
    assert "@​someone" in body


# ---------------------------------------------------------------------------
# Counted heading end-to-end through the real progress_callback
# ---------------------------------------------------------------------------

class _CardAdapter:
    MAX_MESSAGE_LENGTH = 2000
    supports_code_blocks = True


def _progress_ctx(**overrides):
    ctx = TurnContext(
        source=SimpleNamespace(chat_id="c1"),
        _run_still_current=lambda: True,
        progress_mode="all",
        tool_progress_enabled=True,
        progress_queue=queue_mod.Queue(),
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def test_progress_callback_renders_counted_heading_end_to_end():
    ctx = _progress_ctx()
    runner = _make_runner(ctx, _CardAdapter())
    runner.progress_callback(
        "context.injected",
        "context",
        None,
        {"content": "RECALLED", "injected_chars": 8, "sources": ["memory"],
         "recalled_count": 3},
    )
    messages = []
    while not ctx.progress_queue.empty():
        messages.append(ctx.progress_queue.get_nowait())
    assert messages and messages[0].startswith("🧠 Recalled 3 memories\n")


def test_progress_callback_keeps_uncounted_heading_without_count():
    ctx = _progress_ctx()
    runner = _make_runner(ctx, _CardAdapter())
    runner.progress_callback(
        "context.injected",
        "context",
        None,
        {"content": "RECALLED", "injected_chars": 8, "sources": ["memory"]},
    )
    messages = []
    while not ctx.progress_queue.empty():
        messages.append(ctx.progress_queue.get_nowait())
    assert messages and messages[0].startswith("🧠 Injecting Memory\n")

