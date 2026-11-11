"""Per-turn transient reconnect notice gate (2026-11-12 incident contract).

A WAN latency outage produced ~86 Codex TTFB watchdog kills in 2h; every kill
buffered a "⚠️ No first stream event ... Reconnecting." line and each replay
delivered one near-identical message to the chat per retry. The contract under
test: per user turn, AT MOST ONE transient reconnect notice is buffered for the
user (ttfb, idle and stale kill paths share the gate); later kills in the same
turn are logged and folded into activity only, and the gate re-arms next turn.
"""

from __future__ import annotations

import sys
import time
import types

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())


def _make_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    for name in ("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "HERMES_CODEX_TTFB_MAX_SECONDS",
                 "HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS", "HERMES_CODEX_TTFB_STRICT"):
        monkeypatch.delenv(name, raising=False)
    from run_agent import AIAgent

    agent = AIAgent(
        model="gpt-5.5",
        provider="openai-codex",
        api_key="sk-dummy",
        base_url="https://chatgpt.com/backend-api/codex",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    agent.api_mode = "codex_responses"
    monkeypatch.setattr(agent, "_emit_status", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_compute_non_stream_stale_timeout", lambda *a, **k: 60.0)
    return agent


def _reconnect_notices(agent) -> list:
    """Buffered status lines that look like a transient reconnect notice."""
    buf = getattr(agent, "_retry_status_buffer", None) or []
    return [m for _kind, m in buf if "econnecting" in str(m) or "no response from provider" in str(m).lower()]


def test_second_ttfb_kill_in_same_turn_buffers_no_second_notice(tmp_path, monkeypatch):
    """Two TTFB watchdog kills inside one user turn → exactly one buffered notice."""
    from agent import chat_completion_helpers as h

    agent = _make_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "0.3")

    closes: list = []
    dummy_client = types.SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(agent, "_abort_request_openai_client",
                        lambda c, reason=None: closes.append(reason))
    monkeypatch.setattr(agent, "_close_request_openai_client",
                        lambda c, reason=None: closes.append(reason))

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None):
        deadline = time.time() + 1
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    try:
        for _ in range(2):  # two retries of the same turn's API call
            with pytest.raises(TimeoutError):
                h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
    finally:
        stop["flag"] = True

    assert closes.count("codex_ttfb_kill") == 2, "fixture must produce two kills"
    notices = _reconnect_notices(agent)
    assert len(notices) == 1, f"expected exactly one buffered reconnect notice, got {notices}"


def test_gate_rearms_on_the_next_turn(tmp_path, monkeypatch):
    """The gate is per-turn: after the turn prolog resets it, a fresh notice buffers."""
    from agent import chat_completion_helpers as h

    agent = _make_agent(tmp_path, monkeypatch)
    agent._buffer_transient_reconnect_notice("⚠️ notice one. Reconnecting.")
    agent._buffer_transient_reconnect_notice("⚠️ notice two. Reconnecting.")
    # What the per-turn prolog does at the top of every user turn:
    agent._transient_reconnect_notice_sent = False
    agent._buffer_transient_reconnect_notice("⚠️ notice three. Reconnecting.")

    notices = _reconnect_notices(agent)
    assert len(notices) == 2, notices
    assert "notice one" in str(notices[0])
    assert "notice three" in str(notices[1])


def test_suppressed_notice_is_folded_into_activity_only(tmp_path, monkeypatch):
    """A gated-out notice must not vanish silently: it lands in the activity feed."""
    agent = _make_agent(tmp_path, monkeypatch)
    activities: list = []
    monkeypatch.setattr(agent, "_touch_activity", lambda label, **kw: activities.append(label))

    agent._buffer_transient_reconnect_notice("⚠️ first. Reconnecting.")
    agent._buffer_transient_reconnect_notice("⚠️ second. Reconnecting.")

    assert len(_reconnect_notices(agent)) == 1
    assert activities, "the suppressed notice must still touch activity"


def test_stale_kill_notice_shares_the_gate(tmp_path, monkeypatch):
    """The stale (no-response) kill path routes through the same per-turn gate."""
    from agent import chat_completion_helpers as h

    agent = _make_agent(tmp_path, monkeypatch)
    h._report_stale_nonstream_kill(agent, {"model": "gpt-5.5", "input": "hi"}, 130.0, 120.0)
    h._report_stale_nonstream_kill(agent, {"model": "gpt-5.5", "input": "hi"}, 130.0, 120.0)

    stale_notices = [m for _kind, m in (getattr(agent, "_retry_status_buffer", None) or [])
                     if "no response from provider" in str(m).lower()]
    assert len(stale_notices) == 1, stale_notices


def test_streaming_stale_kill_notice_shares_the_gate(tmp_path, monkeypatch):
    """The streaming stale kill (SSE pings but no chunks) buffers through the
    same per-turn gate — two kills, one buffered notice."""
    from agent import chat_completion_helpers as h

    agent = _make_agent(tmp_path, monkeypatch)
    stub = types.SimpleNamespace(
        agent=agent,
        api_kwargs={"model": "gpt-5.5"},
        _stream_stale_timeout=120.0,
        _cancel_current_stream_attempt=lambda reason: None,
        clients=types.SimpleNamespace(close_once=lambda reason: None),
        _attempt_stream_response=None,
        _shutdown_stale_attempt_socket=lambda response: None,
        last_chunk_time={"t": 0.0},
    )
    h._StreamingCall._kill_stale_stream(stub, 300.0)
    h._StreamingCall._kill_stale_stream(stub, 360.0)

    stale_notices = [m for _kind, m in (getattr(agent, "_retry_status_buffer", None) or [])
                     if "no response from provider" in str(m).lower()]
    assert len(stale_notices) == 1, stale_notices


def test_stream_drop_retry_notices_share_the_gate(tmp_path, monkeypatch):
    """``emit_stream_drop`` fires once per stream retry; per user turn only the
    first may be buffered for the user."""
    from agent.stream_diag import emit_stream_drop

    agent = _make_agent(tmp_path, monkeypatch)
    for attempt in (1, 2, 3):
        emit_stream_drop(
            agent, error=ConnectionError("peer closed"), attempt=attempt,
            max_attempts=4, mid_tool_call=False,
        )

    drop_notices = [m for _kind, m in (getattr(agent, "_retry_status_buffer", None) or [])
                    if "reconnecting" in str(m).lower()]
    assert len(drop_notices) == 1, drop_notices
