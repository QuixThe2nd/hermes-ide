"""An interrupt abort must reach the in-flight stream's socket (#98974).

Report #98974: ``/stop`` / ``/reset`` logged ``OpenAI client aborted
(stream_interrupt_abort, ..., tcp_force_closed=0, deferred_close=stranger_thread)
— no sockets found`` and the self-hosted serve kept generating for minutes.
Two shapes miss the socket: (a) the pool sweep skips the connection checked
out for the in-flight body read — the stale kill already shuts down the
attempt's own socket, the interrupt path did not; (b) the abort fires during
``create()``'s connect/TLS window, before any socket exists, so nothing stops
the request once headers arrive. Both stay shutdown-only (never a cross-thread
``close()``, #30858).
"""
import socket as _socket
from types import SimpleNamespace

import run_agent
from agent import chat_completion_helpers as helpers


def _agent():
    return run_agent.AIAgent(
        api_key="test-key", base_url="http://127.0.0.1:1/v1", model="m", provider="custom",
        quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=[], max_iterations=1,
    )


def _call(agent):
    call = helpers._StreamingCall(agent, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, None)
    call._stream_stale_timeout = 5.0
    return call


def _response_over(reader):
    """Live httpx 0.28 wrapper shape down to the socket; ``close`` must never run."""
    def _no_close():
        raise AssertionError("stranger thread must never close the response")
    h11_conn = SimpleNamespace(_network_stream=SimpleNamespace(_sock=reader),
                               _stream=None, _connection=None, _httpcore_stream=None)
    pool_stream = SimpleNamespace(_stream=SimpleNamespace(_connection=h11_conn), _connection=None)
    return SimpleNamespace(close=_no_close,
                           stream=SimpleNamespace(_stream=SimpleNamespace(_httpcore_stream=pool_stream)))


def _assert_shut_down(reader, writer):
    writer.settimeout(5)
    assert writer.recv(1) == b"", "the in-flight stream's socket was not shut down"


def test_interrupt_abort_shuts_down_the_attempts_own_socket():
    reader, writer = _socket.socketpair()
    try:
        call = _call(_agent())
        call._attempt_stream_response = _response_over(reader)
        call.worker = None
        call._monitor_interrupted = {"yes": False}  # set by the poll loop in production
        call._abort_for_interrupt(stale_elapsed=1.0)
        assert call._request_cancelled["value"] is True
        _assert_shut_down(reader, writer)
    finally:
        reader.close()
        writer.close()


def test_stream_created_after_cancel_is_re_aborted():
    reader, writer = _socket.socketpair()
    try:
        call = _call(_agent())
        call._request_cancelled["value"] = True  # abort fired while create() was still connecting
        raw_stream = SimpleNamespace(response=_response_over(reader))
        call._chat_stream_created(raw_stream)
        _assert_shut_down(reader, writer)
    finally:
        reader.close()
        writer.close()
