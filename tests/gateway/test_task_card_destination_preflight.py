"""Destination eligibility must precede transport failure and its text fallback."""

import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.run_turn_runner import TurnRunner


class CardTransport:
    def __init__(self, error="relay outbound timed out"):
        self.error = error
        self.frames = []

    async def send_outbound(self, payload, platform=None):
        self.frames.append(payload)
        if payload["op"] == "task_card":
            return {"success": False, "error": self.error, "ambiguous": True}
        return {"success": True, "message_id": "900.1"}


def make_adapter(monkeypatch, *, error="relay outbound timed out"):
    descriptor = CapabilityDescriptor(
        contract_version=CONTRACT_VERSION, platform="slack", label="Relay",
        max_message_length=39000, supports_draft_streaming=True, supports_edit=True,
        supports_threads=True, markdown_dialect="slack", len_unit="chars",
        emoji="", platform_hint="", pii_safe=False,
        supported_ops=("send", "edit", "task_card", "task_card_stop"),
    )
    transport = CardTransport(error)
    adapter = RelayAdapter(PlatformConfig(), descriptor, transport=transport)
    adapter._platform_by_chat["D1"] = "slack"
    adapter._chat_type_by_chat["D1"] = "dm"
    return adapter, transport


async def run_progress(adapter, metadata, reply_to):
    events = queue.Queue()
    for event_type in ("tool.started", "tool.completed", "tool.completed"):
        events.put({"type": event_type, "tool_call_id": "call-1", "tool_name": "terminal"})
    ctx = SimpleNamespace(
        source=SimpleNamespace(chat_id="D1"), _progress_reply_to=reply_to,
        _progress_metadata=metadata, _cleanup_progress=False, progress_queue=events,
        tool_progress_enabled=False,  # relay tier default: no text lane was asked for
        _run_still_current=lambda: not events.empty(), agent_holder=[None],
        deliver_visible=lambda deliver: deliver(),
    )
    await TurnRunner(None, ctx)._send_native_task_card_progress(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata,reply_to,eligible", [
    ({}, None, False),
    ({"message_id": "100.2", "reply_to_message_id": "100.2"}, None, False),
    ({"thread_id": "100.1"}, "100.2", True),
    ({"thread_ts": "100.1"}, None, True),
    ({}, "100.1", True),
])
async def test_destination_preflight_survives_transport_recovery(
    monkeypatch, metadata, reply_to, eligible,
):
    adapter, io = make_adapter(monkeypatch)
    original_metadata = dict(metadata)
    stop = AsyncMock(wraps=adapter.stop_native_task_card_progress)
    monkeypatch.setattr(adapter, "stop_native_task_card_progress", stop)

    await run_progress(adapter, metadata, reply_to)

    stop.assert_awaited_once_with("D1", reply_to=reply_to, metadata=metadata)
    assert metadata == original_metadata
    ops = [frame["op"] for frame in io.frames]
    assert ops == (["task_card", "send", "edit", "edit", "task_card_stop"]
                   if eligible else ["task_card_stop"])
    if eligible:
        card = io.frames[0]
        anchor = card["metadata"].get("thread_id") or card["metadata"].get("thread_ts")
        assert anchor == (metadata.get("thread_id") or metadata.get("thread_ts") or reply_to)

    # Suppression belongs to this turn, not the adapter: the same chat can host
    # a later real thread, which still gets native publication and finalization.
    if not eligible:
        io.frames.clear()
        await run_progress(adapter, {"thread_id": "200.1"}, "200.2")
        assert [frame["op"] for frame in io.frames] == [
            "task_card", "send", "edit", "edit", "task_card_stop",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("error,suppressed", [
    ("slack task_card requires a thread anchor", True),
    ("slack task_card requires a thread anchor (Slack streams are thread replies)", True),
    ("thread anchor lookup timed out", False),
    ("thread target temporarily unavailable", False),
])
async def test_only_explicit_destination_refusals_suppress_threaded_fallback(
    monkeypatch, error, suppressed,
):
    adapter, io = make_adapter(monkeypatch, error=error)
    await run_progress(adapter, {"thread_id": "100.1"}, "100.2")
    assert [frame["op"] for frame in io.frames] == (
        ["task_card", "task_card_stop"] if suppressed
        else ["task_card", "send", "edit", "edit", "task_card_stop"]
    )
