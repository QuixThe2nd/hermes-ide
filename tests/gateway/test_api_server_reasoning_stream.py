"""Reasoning on the OpenAI-compatible SSE writers (#99552).

The agent's structured ``reasoning_callback`` reaches ``/v1/chat/completions`` as
``delta.reasoning_content`` and ``/v1/responses`` as the reasoning-summary event family,
kept distinct from answer text with monotonic ``sequence_number``.
"""

import asyncio
import json
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, ThreadSafeAsyncQueue


def _frames(payloads):
    out = []
    for raw in payloads:
        text = raw.decode() if isinstance(raw, bytes) else raw
        event = None
        for line in text.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and line != "data: [DONE]":
                out.append((event, json.loads(line[6:])))
    return out


def _fake_writer_env():
    request = MagicMock()
    request.headers = {}
    written: list = []

    class _FakeStreamResponse:
        async def prepare(self, req):
            pass

        async def write(self, payload):
            written.append(payload)

    return request, written, _FakeStreamResponse()


@pytest.fixture
def adapter():
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


@pytest.mark.asyncio
async def test_chat_completions_stream_forwards_reasoning_as_reasoning_content(adapter):
    """Reasoning deltas ride ``delta.reasoning_content``; answer text stays in ``delta.content``."""
    import gateway.platforms.api_server as api_mod
    request, written, fake_response = _fake_writer_env()
    stream_q = ThreadSafeAsyncQueue()

    async def _agent():
        stream_q.put_nowait(("__reasoning__", "thinking..."))
        stream_q.put_nowait("answer")
        return {"final_response": "answer", "completed": True}, None

    agent_task = asyncio.ensure_future(_agent())
    agent_task.add_done_callback(lambda _f: stream_q.put_nowait(None))
    with patch.object(api_mod.web, "StreamResponse", return_value=fake_response):
        await adapter._write_sse_chat_completion(
            request, "chatcmpl-x", "hermes-agent", int(time.time()), stream_q, agent_task, [None])
    deltas = [d["choices"][0]["delta"] for _e, d in _frames(written)]
    assert [d.get("reasoning_content") for d in deltas if d.get("reasoning_content")] == ["thinking..."]
    assert "".join(d.get("content") or "" for d in deltas) == "answer"
    assert not any("thinking" in (d.get("content") or "") for d in deltas)


@pytest.mark.asyncio
async def test_responses_stream_emits_reasoning_summary_events_before_message(adapter):
    """A thinking burst becomes one ``reasoning`` output item (summary_part/text added→delta→done)
    closed before the message item opens; it is echoed in ``response.completed`` and every
    event's ``sequence_number`` stays strictly increasing."""
    import gateway.platforms.api_server as api_mod
    request, written, fake_response = _fake_writer_env()
    stream_q = ThreadSafeAsyncQueue()

    async def _agent():
        stream_q.put_nowait(("__reasoning__", "step one "))
        stream_q.put_nowait(("__reasoning__", "step two"))
        stream_q.put_nowait("final text")
        return {"final_response": "final text", "completed": True}, None

    agent_task = asyncio.ensure_future(_agent())
    agent_task.add_done_callback(lambda _f: stream_q.put_nowait(None))
    with patch.object(api_mod.web, "StreamResponse", return_value=fake_response):
        await adapter._write_sse_responses(
            request=request, response_id=f"resp_{uuid.uuid4().hex[:28]}", model="hermes-agent",
            created_at=int(time.time()), stream_q=stream_q, agent_task=agent_task, agent_ref=[None],
            conversation_history=[], user_message="q", instructions=None, conversation=None,
            store=False, session_id=None)
    frames = _frames(written)
    events = [e for e, _d in frames]
    assert events[:8] == [
        "response.created", "response.output_item.added", "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta", "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done", "response.reasoning_summary_part.done",
        "response.output_item.done"]
    assert events.index("response.output_item.done") < events.index("response.output_text.delta")
    reasoning_done = next(d for e, d in frames if e == "response.reasoning_summary_text.done")
    assert reasoning_done["text"] == "step one step two"
    completed = next(d for e, d in frames if e == "response.completed")
    assert [o["type"] for o in completed["response"]["output"]] == ["reasoning", "message"]
    assert completed["response"]["output"][0]["summary"] == [
        {"type": "summary_text", "text": "step one step two"}]
    assert "step one" not in completed["response"]["output"][1]["content"][0]["text"]
    seqs = [d["sequence_number"] for _e, d in frames]
    assert seqs == list(range(len(seqs)))
