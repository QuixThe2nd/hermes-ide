"""Back-to-back overlap restarts ride one-shot markers across generations."""

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.overlap_restart import claim_when_predecessor_exits
from gateway.restart import queue_user_restart
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.fixture(autouse=True)
def isolated_chain_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        "gateway.restart_preflight.warn_before_user_restart",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("gateway.restart.user_restart_via_service", lambda: False)
    return tmp_path


def _draining_runner(*, successor: bool = True, restart_requested: bool = True):
    runner, _adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = restart_requested
    runner._restart_task_started = restart_requested
    runner._overlap_restart_successor_started = successor
    runner._overlap_restart_successor_pid = 202 if successor else None
    runner.request_restart = MagicMock(return_value=False)
    return runner


def _successor_runner():
    return SimpleNamespace(
        _overlap_successor_pending=True,
        _shutdown_event=asyncio.Event(),
        stop=AsyncMock(),
        request_restart=MagicMock(return_value=True),
    )


def _allow_claim(monkeypatch):
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    monkeypatch.setattr("gateway.run._start_gateway_claim_pid_file", lambda *_: True)


@pytest.mark.asyncio
async def test_draining_restart_with_pending_successor_is_chained(isolated_chain_home):
    runner = _draining_runner()
    source = make_restart_source(chat_id="chat-7", user_id="user-9")

    status = await queue_user_restart(runner, source, "message-11")

    assert status == {
        "status": "chained",
        "active_agents": 0,
        "via_service": False,
    }
    runner.request_restart.assert_not_called()
    marker = isolated_chain_home / ".restart_chain.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["requested_at"] == pytest.approx(time.time(), abs=5)
    assert payload["via_service"] is False
    assert payload["source"] == {
        "kind": "user",
        "platform": "discord",
        "chat_id": "chat-7",
        "chat_type": "dm",
        "user_id": "user-9",
        "message_id": "message-11",
    }


@pytest.mark.asyncio
async def test_requests_while_draining_coalesce_into_one_marker(isolated_chain_home):
    runner = _draining_runner()
    statuses = []
    for index in range(12):
        statuses.append(await queue_user_restart(
            runner,
            make_restart_source(chat_id=f"chat-{index}", user_id=f"user-{index}"),
            f"message-{index}",
        ))

    assert {status["status"] for status in statuses} == {"chained"}
    marker = isolated_chain_home / ".restart_chain.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["source"]["chat_id"] == "chat-0"
    assert payload["source"]["message_id"] == "message-0"
    assert list(isolated_chain_home.glob(".restart_chain.json*")) == [marker]
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_draining_without_successor_keeps_shutdown_status(isolated_chain_home):
    runner = _draining_runner(successor=False, restart_requested=False)

    status = await queue_user_restart(runner, make_restart_source(), "message-1")

    assert status["status"] == "shutdown_in_progress"
    assert not (isolated_chain_home / ".restart_chain.json").exists()
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_failed_marker_write_never_claims_a_chain(isolated_chain_home, monkeypatch):
    runner = _draining_runner()
    monkeypatch.setattr(
        "gateway.restart_chain.atomic_json_write",
        MagicMock(side_effect=OSError("disk full")),
    )

    status = await queue_user_restart(runner, make_restart_source(), "message-1")

    assert status["status"] == "already_in_progress"
    assert not (isolated_chain_home / ".restart_chain.json").exists()


@pytest.mark.asyncio
async def test_direct_restart_request_records_signal_style_chain(isolated_chain_home):
    runner = _draining_runner()
    # Use the real single-flight guard, as SIGUSR1 does, rather than the queue mock.
    from gateway.run import GatewayRunner
    runner.request_restart = GatewayRunner.request_restart.__get__(runner, GatewayRunner)

    assert runner.request_restart(detached=False, via_service=True) is False

    payload = json.loads(
        (isolated_chain_home / ".restart_chain.json").read_text(encoding="utf-8")
    )
    assert payload["source"] == {"kind": "request_restart"}
    assert payload["via_service"] is True


@pytest.mark.asyncio
async def test_successor_claim_consumes_marker_and_requests_overlap_restart(
    isolated_chain_home, monkeypatch
):
    marker = isolated_chain_home / ".restart_chain.json"
    marker.write_text(json.dumps({
        "requested_at": time.time(),
        "source": {"kind": "user"},
        "via_service": True,
    }), encoding="utf-8")
    runner = _successor_runner()
    on_authoritative = AsyncMock()
    _allow_claim(monkeypatch)

    assert await claim_when_predecessor_exits(
        runner, 101, on_authoritative, poll_seconds=0,
    ) is True

    on_authoritative.assert_awaited_once_with()
    runner.request_restart.assert_called_once_with(overlap=True, via_service=True)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_consume_before_act_prevents_crash_retrigger(isolated_chain_home, monkeypatch):
    marker = isolated_chain_home / ".restart_chain.json"
    marker.write_text(json.dumps({
        "requested_at": time.time(),
        "source": {"kind": "user"},
        "via_service": False,
    }), encoding="utf-8")
    runner = _successor_runner()
    _allow_claim(monkeypatch)
    observed_absent = []

    def crash_after_consume(**_kwargs):
        observed_absent.append(not marker.exists())
        raise RuntimeError("simulated crash after marker consumption")

    runner.request_restart = MagicMock(side_effect=crash_after_consume)

    assert await claim_when_predecessor_exits(
        runner, 101, AsyncMock(), poll_seconds=0,
    ) is True
    assert observed_absent == [True]

    # A later claim/boot cannot replay a marker already consumed before the crash.
    assert await claim_when_predecessor_exits(
        runner, 101, AsyncMock(), poll_seconds=0,
    ) is True
    runner.request_restart.assert_called_once_with(overlap=True, via_service=False)


@pytest.mark.asyncio
async def test_three_generations_chain_exactly_once_and_exit_cleanly(
    isolated_chain_home, monkeypatch
):
    _allow_claim(monkeypatch)
    generation_a = _draining_runner()
    generation_b = _successor_runner()
    generation_c = _successor_runner()
    exited = []

    def clean_exit(name):
        exited.append(name)

    generation_a.stop = AsyncMock(side_effect=lambda: clean_exit("A"))
    generation_b.stop = AsyncMock(side_effect=lambda: clean_exit("B"))
    generation_c.stop = AsyncMock(side_effect=lambda: clean_exit("C"))

    chained = await queue_user_restart(generation_a, make_restart_source(), "message-A")
    assert chained["status"] == "chained"
    await generation_a.stop()

    def spawn_generation_c(**_kwargs):
        generation_b._overlap_restart_successor_started = True
        generation_b._overlap_restart_successor_pid = 303
        return True

    generation_b.request_restart = MagicMock(side_effect=spawn_generation_c)
    assert await claim_when_predecessor_exits(
        generation_b, 101, AsyncMock(), poll_seconds=0,
    ) is True
    await generation_b.stop()

    # C becomes authoritative with the marker already consumed: no fourth generation.
    assert await claim_when_predecessor_exits(
        generation_c, 202, AsyncMock(), poll_seconds=0,
    ) is True
    await generation_c.stop()

    generation_b.request_restart.assert_called_once_with(overlap=True, via_service=False)
    generation_c.request_restart.assert_not_called()
    assert not (isolated_chain_home / ".restart_chain.json").exists()
    assert exited == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_control_socket_ack_reports_chained_as_accepted(monkeypatch):
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False))
    queued = AsyncMock(return_value={
        "status": "chained", "active_agents": 0, "via_service": False,
    })
    monkeypatch.setattr("gateway.restart.queue_user_restart", queued)
    server = await gateway_run._start_gateway_start_control_socket(runner)
    assert server is not None
    try:
        response = await asyncio.get_running_loop().run_in_executor(
            None,
            server.handle_request_line,
            json.dumps({"verb": "restart", "id": 1}).encode(),
        )
    finally:
        await server.stop()

    result = json.loads(response)["result"]
    assert result["queued"] is True
    assert result["status"] == "chained"


# ── stale markers: a direct authority claim settles chain leftovers ───────


def _write_chain_marker(home, payload):
    (home / ".restart_chain.json").write_text(json.dumps(payload), encoding="utf-8")


def _read_chain_marker(home):
    return json.loads((home / ".restart_chain.json").read_text(encoding="utf-8"))


def test_recorded_marker_names_the_generation_it_belongs_to(isolated_chain_home):
    """The marker must carry its own generation's identity (predecessor and
    pending successor, pid + start fingerprint): without it, neither the
    startup sweep nor status can tell a live chain from an orphaned one."""
    from gateway.status import get_process_start_time

    runner = _draining_runner()
    runner._overlap_restart_successor_pid = 202
    runner._overlap_restart_successor_start_time = 987654

    from gateway.restart_chain import record_chained_restart

    assert record_chained_restart(runner) is True
    payload = _read_chain_marker(isolated_chain_home)
    assert payload["successor_pid"] == 202
    assert payload["successor_start_time"] == 987654
    assert payload["predecessor"]["pid"] == os.getpid()
    assert payload["predecessor"]["start_time"] == get_process_start_time(os.getpid())


@pytest.mark.asyncio
async def test_direct_claim_honors_a_marker_naming_this_process(isolated_chain_home):
    """A successor whose predecessor died before the deferred claim still
    owes the user the chained restart when the marker provably names it."""
    from gateway.status import get_process_start_time

    _write_chain_marker(isolated_chain_home, {
        "requested_at": time.time() - 60, "source": {"kind": "user"},
        "successor_pid": os.getpid(),
        "successor_start_time": get_process_start_time(os.getpid()),
    })
    runner = _successor_runner()

    from gateway.restart_chain import settle_chained_restart_after_direct_claim

    await settle_chained_restart_after_direct_claim(
        runner, launched_as_overlap_successor=False)
    runner.request_restart.assert_called_once_with(overlap=True, via_service=False)
    assert not (isolated_chain_home / ".restart_chain.json").exists()


@pytest.mark.asyncio
async def test_direct_claim_clears_an_orphaned_marker(isolated_chain_home):
    """Live incident: the marker's generation is gone (its successor never
    claimed); the newly authoritative process is not that successor, so the
    marker must be consumed before a future successor acts on it spuriously."""
    _write_chain_marker(isolated_chain_home, {
        "requested_at": time.time() - 7200, "source": {"kind": "user", "platform": "discord"},
        "predecessor": {"pid": 709505, "start_time": 111111},
        "successor_pid": 1531038, "successor_start_time": 222222,
    })
    runner = _successor_runner()

    from gateway.restart_chain import settle_chained_restart_after_direct_claim

    await settle_chained_restart_after_direct_claim(
        runner, launched_as_overlap_successor=False)
    runner.request_restart.assert_not_called()
    assert not (isolated_chain_home / ".restart_chain.json").exists()


@pytest.mark.asyncio
async def test_direct_claim_clears_a_legacy_marker_on_a_normal_start(isolated_chain_home):
    """A marker written by pre-fingerprint code names no generation. A
    normally started gateway (not the drainer's spawned successor) can never
    be its target: clear it — the incident showed leaving it means forever."""
    _write_chain_marker(isolated_chain_home, {
        "requested_at": time.time() - 7200, "source": {"kind": "user"},
    })
    runner = _successor_runner()

    from gateway.restart_chain import settle_chained_restart_after_direct_claim

    await settle_chained_restart_after_direct_claim(
        runner, launched_as_overlap_successor=False)
    runner.request_restart.assert_not_called()
    assert not (isolated_chain_home / ".restart_chain.json").exists()


@pytest.mark.asyncio
async def test_direct_claim_honors_a_legacy_marker_when_spawned_as_successor(
        isolated_chain_home):
    """The OVERLAP_SUCCESSOR launch env proves the drainer spawned THIS
    process, so a legacy marker it left behind is this process's chain."""
    _write_chain_marker(isolated_chain_home, {
        "requested_at": time.time() - 5, "source": {"kind": "user"},
    })
    runner = _successor_runner()

    from gateway.restart_chain import settle_chained_restart_after_direct_claim

    await settle_chained_restart_after_direct_claim(
        runner, launched_as_overlap_successor=True)
    runner.request_restart.assert_called_once_with(overlap=True, via_service=False)
    assert not (isolated_chain_home / ".restart_chain.json").exists()


@pytest.mark.asyncio
async def test_direct_claim_settle_is_a_noop_without_a_marker(isolated_chain_home):
    runner = _successor_runner()

    from gateway.restart_chain import settle_chained_restart_after_direct_claim

    await settle_chained_restart_after_direct_claim(
        runner, launched_as_overlap_successor=False)
    runner.request_restart.assert_not_called()


@pytest.mark.asyncio
async def test_direct_claim_sweep_survives_a_broken_marker_store(
        isolated_chain_home, monkeypatch):
    """Persistence trouble during the sweep must never break the startup."""
    _write_chain_marker(isolated_chain_home, {
        "requested_at": time.time() - 5, "source": {"kind": "user"},
        "predecessor": {"pid": 709505, "start_time": 111111},
        "successor_pid": 1531038, "successor_start_time": 222222,
    })
    runner = _successor_runner()
    monkeypatch.setattr(
        "gateway.restart_chain.consume_chained_restart",
        lambda: (_ for _ in ()).throw(OSError("disk")))

    from gateway.restart_chain import settle_chained_restart_after_direct_claim

    await settle_chained_restart_after_direct_claim(
        runner, launched_as_overlap_successor=False)
    runner.request_restart.assert_not_called()
