"""Contract tests for overlap authority handoff at drain start.

The behavioral core of the generation-array restart design (see
gateway/overlap_restart.py): a draining predecessor releases the runtime
lock, PID file, runtime-status record, control socket and singleton
listeners at drain START, and the newest generation claims authority while
every older generation is still alive and finishing in-flight sessions.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway import status as gateway_status
from gateway.overlap_restart import (
    claim_authority_when_lock_frees,
    release_authority,
    start_deferred_adapters,
)
from gateway import overlap_restart

_REPO_ROOT = Path(__file__).resolve().parents[2]

_LOCK_HOLDER_SCRIPT = """
import os, time
from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock, write_pid_file
write_pid_file()
acquire_gateway_runtime_lock()
with open({ready!r}, "w") as f:
    f.write("1")
while not os.path.exists({release!r}):
    time.sleep(0.02)
release_gateway_runtime_lock()
time.sleep(120)
"""


@pytest.fixture(autouse=True)
def isolated_restart_home(tmp_path, monkeypatch):
    """Keep lock/PID/status writes in the per-test home, not the session default."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_OVERLAP_SUCCESSOR", "1")
    gateway_status.clear_runtime_status_released()
    monkeypatch.delenv("HERMES_OVERLAP_PREDECESSOR_PID", raising=False)
    try:
        gateway_status.release_gateway_runtime_lock()
    except Exception:
        pass
    gateway_status.remove_pid_file()


@pytest.fixture
def live_predecessor():
    """A REAL live process standing in for the draining predecessor (no lock)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "hermes-gateway"]
    )
    try:
        yield proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def locked_predecessor(tmp_path):
    """A live predecessor that HOLDS the real runtime lock until released.

    Its argv carries a ``hermes-gateway`` token (an inert extra ``python -c``
    argument) so gateway.status's live-record validation reads it as the
    gateway it stands in for — a bare ``python -c`` child looks like a
    foreign process and its identity files get cleaned up instead of
    blocking the claim.
    """
    ready = tmp_path / "predecessor-ready"
    release = tmp_path / "predecessor-release"
    proc = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER_SCRIPT.format(
            ready=str(ready), release=str(release)),
         "hermes-gateway"],
        cwd=str(_REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
    )
    try:
        for _ in range(200):
            if ready.exists() or proc.poll() is not None:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("predecessor never reported ready")
        if proc.poll() is not None:
            raise RuntimeError(f"predecessor died: returncode {proc.returncode}")
        yield SimpleNamespace(pid=proc.pid, release=lambda: release.touch())
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _handoff_record(predecessor_pid: int, successor_pid: int | None) -> None:
    """Write the runtime-status record a released predecessor leaves behind."""
    record = {
        "kind": "gateway", "pid": int(predecessor_pid),
        "gateway_start_time": 4242, "gateway_state": "handoff",
        "hermes_home": str(Path(os.environ["HERMES_HOME"])),
        "updated_at": "2026-11-19T00:00:00Z",
    }
    if successor_pid:
        record["authority_pid"] = int(successor_pid)
    gateway_status._write_json_file(gateway_status._get_runtime_status_path(), record)


def _make_claim_runner(predecessor_pid: int):
    return SimpleNamespace(
        _overlap_successor_pending=True,
        _overlap_predecessor_pid=predecessor_pid,
        _draining=False,
        _shutdown_event=asyncio.Event(),
        adapters={},
        _profile_adapters={},
        _overlap_deferred_primary_adapters=[],
        _overlap_deferred_secondary_adapters=False,
        stop=lambda: None,
    )


@pytest.mark.asyncio
@pytest.mark.spawns_gateway_lookalike
async def test_successor_claims_while_predecessor_alive_after_release(live_predecessor):
    """Claim happens at authority RELEASE, not predecessor exit.

    The predecessor stays alive for the whole claim (it is draining its own
    in-flight sessions): the released lock, removed PID file and handoff
    runtime-status record are what unblock the successor.
    """
    _handoff_record(live_predecessor, os.getpid())
    runner = _make_claim_runner(live_predecessor)
    on_authoritative = AsyncMock()

    claimed = await asyncio.wait_for(
        claim_authority_when_lock_frees(runner, live_predecessor, on_authoritative),
        timeout=5.0,
    )

    assert claimed is True
    on_authoritative.assert_awaited_once()
    assert runner._overlap_successor_pending is False
    assert gateway_status._pid_exists(live_predecessor)
    assert gateway_status._pid_from_record(
        gateway_status._read_pid_record()) == os.getpid()


@pytest.mark.asyncio
@pytest.mark.spawns_gateway_lookalike
async def test_successor_waits_until_predecessor_releases_lock_while_alive(locked_predecessor):
    """While the predecessor still holds the lock/PID file, no claim; release mid-wait unblocks.

    The predecessor is a real process holding the real flock. The claim
    starts only after it releases (sentinel) — with the process still alive.
    """
    _handoff_record(locked_predecessor.pid, os.getpid())
    runner = _make_claim_runner(locked_predecessor.pid)
    on_authoritative = AsyncMock()

    task = asyncio.create_task(
        claim_authority_when_lock_frees(runner, locked_predecessor.pid, on_authoritative)
    )
    await asyncio.sleep(0.7)
    assert not task.done(), "claim must wait while the predecessor physically holds the lock"

    locked_predecessor.release()
    claimed = await asyncio.wait_for(task, timeout=5.0)

    assert claimed is True
    on_authoritative.assert_awaited_once()
    assert gateway_status._pid_exists(locked_predecessor.pid), \
        "predecessor is still draining"


@pytest.mark.asyncio
@pytest.mark.spawns_gateway_lookalike
async def test_successor_waits_while_predecessor_still_holds_artifacts(live_predecessor):
    """A released lock is not enough: a runtime-status record still naming the
    live predecessor as the RUNNING gateway blocks the claim until the
    handoff stamp lands."""
    home = Path(os.environ["HERMES_HOME"])
    gateway_status._write_json_file(gateway_status._get_runtime_status_path(), {
        "kind": "gateway", "pid": int(live_predecessor),
        "gateway_start_time": 4242, "gateway_state": "running",
        "hermes_home": str(home), "updated_at": "2026-11-19T00:00:00Z",
    })
    runner = _make_claim_runner(live_predecessor)
    on_authoritative = AsyncMock()

    task = asyncio.create_task(
        claim_authority_when_lock_frees(runner, live_predecessor, on_authoritative)
    )
    await asyncio.sleep(0.7)
    assert not task.done(), "claim must wait while the runtime-status record still names the predecessor"

    _handoff_record(live_predecessor, os.getpid())
    claimed = await asyncio.wait_for(task, timeout=5.0)

    assert claimed is True
    on_authoritative.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.spawns_gateway_lookalike
async def test_superseded_successor_never_claims(live_predecessor):
    """A generation whose own successor is already live must not claim authority."""
    from gateway.generations import Generation, GenerationArray

    me = os.getpid()
    array = GenerationArray([
        Generation(pid=live_predecessor, spawn_seq=1),
        Generation(pid=me, predecessor=live_predecessor, spawn_seq=2),
        Generation(pid=1000, predecessor=me, spawn_seq=3),
    ])
    _handoff_record(live_predecessor, None)
    runner = _make_claim_runner(live_predecessor)
    on_authoritative = AsyncMock()

    async def fixed_array(_runner):
        return array

    mp = pytest.MonkeyPatch()
    mp.setattr(overlap_restart, "generation_array_for_runner", fixed_array)
    try:
        task = asyncio.create_task(
            claim_authority_when_lock_frees(runner, live_predecessor, on_authoritative)
        )
        await asyncio.sleep(0.5)
        assert not task.done()
        runner._shutdown_event.set()
        assert await asyncio.wait_for(task, timeout=5.0) is False
    finally:
        mp.undo()
    on_authoritative.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.spawns_gateway_lookalike
async def test_shutdown_request_ends_claim_wait(locked_predecessor):
    """SIGTERM-style shutdown while waiting: claim loop exits False, no claim."""
    _handoff_record(locked_predecessor.pid, os.getpid())
    runner = _make_claim_runner(locked_predecessor.pid)
    on_authoritative = AsyncMock()

    task = asyncio.create_task(
        claim_authority_when_lock_frees(runner, locked_predecessor.pid, on_authoritative)
    )
    await asyncio.sleep(0.4)
    assert not task.done()
    runner._shutdown_event.set()

    assert await asyncio.wait_for(task, timeout=5.0) is False
    on_authoritative.assert_not_awaited()


@pytest.mark.spawns_gateway_lookalike
def test_release_authority_moves_every_artifact(live_predecessor):
    """release_authority drops lock+PID, stamps handoff, stops intake+control socket."""
    assert gateway_status.acquire_gateway_runtime_lock() is True
    gateway_status.write_pid_file()
    gateway_status.write_runtime_status(gateway_state="running")
    stopped_intake = []
    discord_adapter = SimpleNamespace(
        platform="discord",
        stop_intake=lambda: stopped_intake.append(1),
    )
    _control_server = MagicMock()
    runner = SimpleNamespace(
        _overlap_authority_released=False,
        _overlap_restart_successor_pid=live_predecessor,
        _control_server=_control_server,
        adapters={"discord": discord_adapter},
        _profile_adapters={},
    )

    release_authority(runner)

    assert gateway_status.owns_gateway_runtime_lock() is False
    assert gateway_status.get_running_pid() is None
    assert (gateway_status.read_runtime_status() or {}).get(
        "gateway_state") == "handoff"
    assert stopped_intake == [1]
    _control_server.stop.assert_called_once()
    assert runner._control_server is None


@pytest.mark.spawns_gateway_lookalike
def test_get_running_pid_ignores_handoff_record(live_predecessor):
    """A live-but-released predecessor must not read as THE running gateway."""
    gateway_status._write_json_file(gateway_status._get_runtime_status_path(), {
        "kind": "gateway", "pid": int(live_predecessor),
        "gateway_start_time": 4242, "gateway_state": "handoff",
        "hermes_home": str(Path(os.environ["HERMES_HOME"])),
        "updated_at": "2026-11-19T00:00:00Z",
    })
    assert gateway_status._pid_exists(live_predecessor)
    assert gateway_status.get_running_pid() is None


def _fake_adapter(singleton: bool = False):
    return SimpleNamespace(owns_singleton_listener=singleton, connected=False)


def _deferred_runner(listeners, shared, *, may_shared: bool = False):
    """Runner whose deferred queues hold ``(platform, cfg, adapter)`` entries."""
    runner = SimpleNamespace(
        _overlap_deferred_primary_adapters=[*listeners, *shared],
        _overlap_deferred_secondary_adapters=False,
        _shutdown_event=asyncio.Event(),
    )

    async def connect_pending(pending):
        for entry in pending:
            entry[2].connected = True

    runner._start_connect_pending = AsyncMock(side_effect=connect_pending)
    runner._start_secondary_profile_adapters = AsyncMock()
    runner._start_aggregate_connect_results = MagicMock(return_value=None)

    array = MagicMock()
    array.may_start_shared_adapters = lambda me: may_shared

    async def fixed_array(_runner):
        return array

    mp = pytest.MonkeyPatch()
    mp.setattr(overlap_restart, "generation_array_for_runner", fixed_array)
    return runner, mp


@pytest.mark.asyncio
async def test_claim_starts_singleton_listeners_while_ancestors_drain():
    """Ports must move to the new generation at claim even while ancestors live."""
    listener = _fake_adapter(singleton=True)
    shared = _fake_adapter(singleton=False)
    runner, mp = _deferred_runner(
        [("solo", {"platform": "solo"}, listener)],
        [("shared", {"platform": "shared"}, shared)],
        may_shared=False,
    )
    await start_deferred_adapters(runner)
    mp.undo()

    assert listener.connected is True, "singleton listener starts at claim"
    assert shared.connected is False, "shared-credential adapter still held"
    assert runner._overlap_deferred_primary_adapters == [
        ("shared", {"platform": "shared"}, shared)]
    assert runner._overlap_shared_adapter_watch_task is not None
    runner._overlap_shared_adapter_watch_task.cancel()


@pytest.mark.asyncio
async def test_claim_starts_everything_when_ancestors_gone():
    """No live ancestors: singleton listeners AND shared adapters start at claim."""
    listener = _fake_adapter(singleton=True)
    shared = _fake_adapter(singleton=False)
    runner, mp = _deferred_runner(
        [("solo", {"platform": "solo"}, listener)],
        [("shared", {"platform": "shared"}, shared)],
        may_shared=True,
    )
    await start_deferred_adapters(runner)
    mp.undo()

    assert listener.connected is True
    assert shared.connected is True
    assert runner._overlap_deferred_primary_adapters == []
    assert getattr(runner, "_overlap_shared_adapter_watch_task", None) is None


@pytest.mark.asyncio
async def test_watch_task_starts_held_adapters_when_ancestors_exit():
    """The background watcher finishes the held adapters once the array clears."""
    listener = _fake_adapter(singleton=True)
    shared = _fake_adapter(singleton=False)
    runner, mp = _deferred_runner(
        [("solo", {"platform": "solo"}, listener)],
        [("shared", {"platform": "shared"}, shared)],
        may_shared=False,
    )
    await start_deferred_adapters(runner)
    watch = runner._overlap_shared_adapter_watch_task
    assert watch is not None
    await asyncio.sleep(0.1)
    assert shared.connected is False

    cleared = MagicMock()
    cleared.may_start_shared_adapters = lambda me: True

    async def cleared_array(_runner):
        return cleared

    mp.setattr(overlap_restart, "generation_array_for_runner", cleared_array)
    await asyncio.wait_for(watch, timeout=5.0)

    assert shared.connected is True
    assert getattr(runner, "_overlap_shared_adapter_watch_task", None) is None
    mp.undo()


@pytest.mark.asyncio
async def test_watch_task_also_finishes_deferred_secondary_profiles():
    """A deferred secondary-profile batch rides the same watcher."""
    listener = _fake_adapter(singleton=True)
    runner, mp = _deferred_runner(
        [("solo", {"platform": "solo"}, listener)],
        [],
        may_shared=False,
    )
    runner._overlap_deferred_secondary_adapters = True
    await start_deferred_adapters(runner)
    watch = runner._overlap_shared_adapter_watch_task
    assert watch is not None
    runner._start_secondary_profile_adapters.assert_not_awaited()

    cleared = MagicMock()
    cleared.may_start_shared_adapters = lambda me: True

    async def cleared_array(_runner):
        return cleared

    mp.setattr(overlap_restart, "generation_array_for_runner", cleared_array)
    await asyncio.wait_for(watch, timeout=5.0)

    runner._start_secondary_profile_adapters.assert_awaited_once()
    mp.undo()


def test_singleton_listener_flags():
    """The three port-owning adapters declare the singleton-listener contract."""
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter

    assert APIServerAdapter.owns_singleton_listener is True
    assert WebhookAdapter.owns_singleton_listener is True
    assert WhatsAppCloudAdapter.owns_singleton_listener is True


@pytest.mark.asyncio
async def test_webhook_stop_intake_releases_site():
    """stop_intake stops the listening site but keeps the runner + outbound."""
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter

    adapter = WebhookAdapter(PlatformConfig(
        enabled=True, token="***",
        extra={"host": "127.0.0.1", "routes": {"r": {"secret": "INSECURE_NO_AUTH"}}},
    ))
    adapter._runner = MagicMock()
    site = MagicMock()
    site.stop = AsyncMock()
    adapter._site = site

    await adapter.stop_intake()

    site.stop.assert_awaited_once()
    assert adapter._site is None
    assert adapter._runner is not None


@pytest.mark.asyncio
async def test_whatsapp_stop_intake_releases_site():
    """stop_intake stops the owned listener; shared-listener mode has no site."""
    from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter

    adapter = object.__new__(WhatsAppCloudAdapter)
    site = MagicMock()
    site.stop = AsyncMock()
    adapter._ingress_site = site

    await adapter.stop_intake()

    site.stop.assert_awaited_once()
    assert adapter._ingress_site is None


@pytest.mark.asyncio
async def test_api_server_stop_intake_releases_site():
    """stop_intake stops the API server's bound listener."""
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    adapter = APIServerAdapter(PlatformConfig(
        enabled=True, token="***", extra={"port": "0"},
    ))
    site = MagicMock()
    site.stop = AsyncMock()
    adapter._site = site

    await adapter.stop_intake()

    site.stop.assert_awaited_once()
    assert adapter._site is None
