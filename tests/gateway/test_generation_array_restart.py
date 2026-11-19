"""Generation-array restart E2E: real decisions, real hub, fake transports.

Drives the REAL restart pipeline across overlapping gateway generations —
``queue_user_restart`` → ``request_restart`` → ``spawn_successor`` →
``release_authority`` → ``claim_authority_when_lock_frees`` — plus the real
``DiscordHubServer`` routing decisions, with only the agent turn bodies and
the hub-side Discord transport faked.  Successors are real OS processes
launched with the production env markers, so hub attestation, the successor
registry and PID-fingerprint liveness are all genuine.

Covers the array contract end to end:
* a restart on a serving generation with live turns spawns the next
  generation immediately (before any drain completes) and never reports a
  "chained" status;
* new sessions route to the newest generation while busy sessions stay with
  the generation running their turn;
* predecessor exit order (old→new and new→old) never disturbs the newest
  generation's service or authority, and never forks the lineage;
* a restart landing on an older draining generation is forwarded to the
  newest one, which spawns its own successor;
* singleton authority (runtime PID record) moves to the newest generation
  at drain start, while predecessors are still alive.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import pytest_asyncio

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

REPO_ROOT = Path(__file__).resolve().parents[2]
STAND_IN = REPO_ROOT / "tests" / "gateway" / "restart_array_stand_in.py"

_POLL = 0.05


class _Child:
    """One stand-in generation process plus its stdout event stream."""

    def __init__(self, proc: subprocess.Popen) -> None:
        assert proc.stdin is not None and proc.stdout is not None
        self.proc = proc
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._drain_stdout, daemon=True)
        self._reader.start()

    def _drain_stdout(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                self.events.append(record)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    async def wait_for(self, event: str, *, timeout: float = 40.0,
                       since: int = 0) -> dict:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            with self._lock:
                for record in self.events[since:]:
                    if record.get("event") == event:
                        return record
            if loop.time() > deadline:
                with self._lock:
                    recent = self.events[-5:]
                raise AssertionError(
                    f"timed out waiting for {event!r} from pid {self.pid}; "
                    f"recent events: {recent}")
            await asyncio.sleep(_POLL)

    async def probe(self) -> dict:
        with self._lock:
            since = len(self.events)
        self._send({"command": "probe"})
        return await self.wait_for("probe", since=since)

    async def queue_restart(self, *, chat_id: str = "restart-requester") -> dict:
        with self._lock:
            since = len(self.events)
        self._send({"command": "queue_restart", "chat_id": chat_id})
        return await self.wait_for("restart_result", since=since)

    async def end_turn(self, session_key: str) -> None:
        self._send({"command": "end_turn", "session_key": session_key})

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


class ArrayHarness:
    """A real hub plus a tree of stand-in generations under one home."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        from plugins.platforms.discord import hub as hub_mod
        import plugins.platforms.discord.adapter as discord_adapter_mod
        from plugins.platforms.discord import overlap_spawn as overlap_spawn_mod
        from gateway.platforms.base import SendResult

        self.home = tmp_path / "home"
        self.home.mkdir()
        self.socket_path = tmp_path / "hub" / "hub.sock"
        self.tmp_path = tmp_path
        self.children: list[_Child] = []
        self._errfiles: list = []

        # Fake transport only: the hub-side Discord adapter "connects" and
        # "sends" without a network. Everything downstream — routing,
        # registry, controls, spawn dispatch — is production code.
        async def _fake_connect(self, *, is_reconnect: bool = False) -> bool:
            self._running = True
            return True

        async def _fake_send(self, chat_id, content, reply_to=None,
                             metadata=None):
            return SendResult(success=True, message_id="fake")

        async def _fake_send_typing(self, chat_id, metadata=None):
            return None

        monkeypatch.setattr(
            discord_adapter_mod.DiscordAdapter, "connect", _fake_connect)
        monkeypatch.setattr(
            discord_adapter_mod.DiscordAdapter, "send", _fake_send)
        monkeypatch.setattr(
            discord_adapter_mod.DiscordAdapter, "send_typing", _fake_send_typing)

        # Fake spawn mechanics, real spawn decision: the hub still records
        # and fingerprints the child exactly like a production successor.
        harness = self

        def _fake_spawn_gateway(*, home: str, predecessor_pid: int,
                                preserve_supervisor: bool = False):
            from gateway.overlap_restart import (
                OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV)
            from tools.environments.local import served_profile_child_env

            env = served_profile_child_env(target_home=home, inherit_credentials=True)
            env["PYTHONPATH"] = str(REPO_ROOT)
            env["HERMES_HOME"] = home
            for marker in (
                "INVOCATION_ID", "NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID",
                "XPC_SERVICE_NAME", "_HERMES_GATEWAY", "HERMES_GATEWAY",
                "HERMES_GATEWAY_MODE",
            ):
                env.pop(marker, None)
            env[OVERLAP_SUCCESSOR_ENV] = "1"
            env[OVERLAP_PREDECESSOR_PID_ENV] = str(predecessor_pid)
            errfile = open(
                harness.tmp_path / f"child-of-{predecessor_pid}-stderr.log", "ab")
            harness._errfiles.append(errfile)
            proc = subprocess.Popen(
                [sys.executable, str(STAND_IN),
                 "--socket", str(harness.socket_path), "--home", home],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errfile,
                env=env, cwd=str(REPO_ROOT), text=True, bufsize=1,
            )
            harness.children.append(_Child(proc))
            return proc

        monkeypatch.setattr(
            overlap_spawn_mod, "spawn_gateway_successor", _fake_spawn_gateway)

        self.hub = hub_mod.DiscordHubServer(self.socket_path)
        self.hub_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.hub.start()
        self.hub_task = asyncio.create_task(self.hub.serve_forever())

    def spawn_root(self) -> _Child:
        """Launch the original generation directly (not a hub successor)."""
        env = dict(os.environ)
        env["HERMES_HOME"] = str(self.home)
        env["PYTHONPATH"] = str(REPO_ROOT) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        for marker in ("_HERMES_GATEWAY", "HERMES_GATEWAY", "HERMES_GATEWAY_MODE"):
            env.pop(marker, None)
        errfile = open(self.tmp_path / "child-root-stderr.log", "ab")
        self._errfiles.append(errfile)
        proc = subprocess.Popen(
            [sys.executable, str(STAND_IN),
             "--socket", str(self.socket_path), "--home", str(self.home)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errfile,
            env=env, cwd=str(REPO_ROOT), text=True, bufsize=1,
        )
        child = _Child(proc)
        self.children.append(child)
        return child

    async def wait_child(self, count: int, *, timeout: float = 60.0) -> _Child:
        """Wait until the tree holds *count* children; return the newest."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while len(self.children) < count:
            if loop.time() > deadline:
                raise AssertionError(
                    f"only {len(self.children)} children exist, wanted {count}")
            await asyncio.sleep(_POLL)
        return self.children[count - 1]

    async def inject(self, chat_id: str, text: str = "hello") -> bool:
        """Send one inbound event through the hub's real routing path."""
        adapters = list(self.hub._adapters.values())
        assert adapters, "hub has no attached transport adapter"
        event = MessageEvent(
            text=text,
            source=SessionSource(
                platform=Platform.DISCORD, chat_id=chat_id, chat_type="dm"),
            message_id=f"msg-{chat_id}-{text}",
        )
        await adapters[0].handle_message(event)
        return bool(event._gateway_accepted)

    async def deliver_to_owner(self, chat_id: str, *, timeout: float = 30.0,
                               text: str = "hello") -> tuple[_Child, str]:
        """Inject one event; return (receiving generation, session key)."""
        with_locks = [child._lock for child in self.children]
        marks = []
        for child, lock in zip(self.children, with_locks):
            with lock:
                marks.append((child, len(child.events)))
        accepted = await self.inject(chat_id, text=text)
        assert accepted, f"hub retained the event for {chat_id!r}"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            for child, since in marks:
                with child._lock:
                    fresh = [record for record in child.events[since:]
                             if record.get("event") == "received"]
                for record in fresh:
                    return child, str(record["session_key"])
            if loop.time() > deadline:
                raise AssertionError(f"no generation reported receiving {chat_id!r}")
            await asyncio.sleep(_POLL)

    async def stop(self) -> None:
        for child in self.children:
            child.kill()
        for errfile in self._errfiles:
            errfile.close()
        if self.hub_task is not None:
            self.hub_task.cancel()
        try:
            await self.hub.stop()
        except Exception:
            pass


@pytest_asyncio.fixture
async def harness(tmp_path, monkeypatch):
    h = ArrayHarness(tmp_path, monkeypatch)
    await h.start()
    try:
        yield h
    finally:
        await h.stop()


def _authority_pid(home: Path) -> int | None:
    """PID the singleton artifacts name (pid record and runtime status agree).

    ``get_running_pid`` additionally demands a gateway-shaped live argv, which
    a stand-in child never has; the repo's overlap tests assert the records
    for the same reason (see test_overlap_claim_authority).
    """
    from gateway import status as gateway_status

    saved = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        pid = gateway_status._pid_from_record(gateway_status._read_pid_record())
        state = gateway_status._read_json_file(
            gateway_status._get_runtime_status_path()) or {}
        state_pid = state.get("pid")
        assert state_pid in (None, pid), (state_pid, pid)
        return pid
    finally:
        if saved is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = saved


async def _wait_probe(child: _Child, predicate, *, timeout: float = 30.0) -> dict:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    probe = await child.probe()
    while not predicate(probe):
        if loop.time() > deadline:
            raise AssertionError(f"probe never satisfied for pid {child.pid}: {probe}")
        await asyncio.sleep(0.2)
        probe = await child.probe()
    return probe


async def _build_array(harness: ArrayHarness) -> tuple[_Child, _Child, _Child, str, str]:
    """v1 serving a live turn → restart → v2; v2 serving a live turn → restart → v3.

    Returns (v1, v2, v3, key of v1's held turn, key of v2's held turn).
    """
    v1 = harness.spawn_root()
    await v1.wait_for("attached")
    child, key_a = await harness.deliver_to_owner("turn-a")
    assert child is v1, "the original generation must take the first turn"

    result = await v1.queue_restart()
    assert result["result"]["status"] == "restarting"
    v2 = await harness.wait_child(2)
    await v2.wait_for("attached")
    await v2.wait_for("claimed")
    probe_v1 = await _wait_probe(v1, lambda p: p["successor_pid"] == v2.pid)
    assert probe_v1["draining"] is True
    assert key_a in probe_v1["running"]

    child, key_b = await harness.deliver_to_owner("turn-b")
    assert child is v2, "a new session must route to the newest generation"

    result = await v2.queue_restart()
    assert result["result"]["status"] == "restarting"
    v3 = await harness.wait_child(3)
    await v3.wait_for("attached")
    await v3.wait_for("claimed")
    probe_v2 = await _wait_probe(v2, lambda p: p["successor_pid"] == v3.pid)
    assert probe_v2["draining"] is True
    return v1, v2, v3, key_a, key_b


@pytest.mark.spawns_gateway_lookalike
@pytest.mark.asyncio
async def test_restart_spawns_next_generation_and_routes_by_ownership(harness):
    """(a)+(b)+(e): immediate successor, ownership routing, authority moves."""
    v1, v2, v3, key_a, key_b = await _build_array(harness)

    # New sessions go to the newest generation.
    child, _ = await harness.deliver_to_owner("fresh-session")
    assert child is v3

    # Busy sessions stay with the generation running their turn: the
    # follow-up is admitted on v1 (never handed to v3), and symmetrically
    # on v2.
    accepted = await harness.inject("turn-a", text="follow-up-a")
    assert accepted
    await asyncio.sleep(1.0)
    probe_v1 = await v1.probe()
    assert (key_a in probe_v1["pending"]) or (key_a in probe_v1["running"])
    accepted = await harness.inject("turn-b", text="follow-up-b")
    assert accepted
    await asyncio.sleep(1.0)
    probe_v2 = await v2.probe()
    assert (key_b in probe_v2["pending"]) or (key_b in probe_v2["running"])

    # Singleton authority moved to v3 while v1 and v2 are still alive.
    assert v1.proc.poll() is None and v2.proc.poll() is None
    assert _authority_pid(harness.home) == v3.pid

    # Registry relationship: every spawned successor names a live predecessor.
    rows = harness.hub._successors.snapshot()
    predecessors = {row["predecessor_pid"] for row in rows}
    assert {v1.pid, v2.pid} <= predecessors

    # Cleanup: release v3's held turn so the fixture's children can exit.
    await v3.end_turn(_last_key(v3))


def _last_key(child: _Child) -> str:
    with child._lock:
        received = [r for r in child.events if r.get("event") == "received"]
    assert received, "no received events"
    return str(received[-1]["session_key"])


@pytest.mark.spawns_gateway_lookalike
@pytest.mark.asyncio
async def test_predecessor_exits_oldest_first_newest_undisturbed(harness):
    """(c) v1 exits, then v2 exits: v3 keeps serving and stays authoritative."""
    v1, v2, v3, _key_a, _key_b = await _build_array(harness)

    v1.kill()
    await asyncio.sleep(1.0)
    child, _ = await harness.deliver_to_owner("after-v1")
    assert child is v3
    assert _authority_pid(harness.home) == v3.pid

    v2.kill()
    await asyncio.sleep(1.0)
    child, _ = await harness.deliver_to_owner("after-both")
    assert child is v3
    assert _authority_pid(harness.home) == v3.pid


@pytest.mark.spawns_gateway_lookalike
@pytest.mark.asyncio
async def test_predecessor_exits_newest_draining_first_no_lineage_fork(harness):
    """(c) v2 exits while v1 still drains: no respawn may fork past v3.

    v1's successor watcher sees v2 die. A newer generation (v3) already
    continues the line, so respawning would create a parallel newest
    generation — the exact duplicate the array exists to prevent.
    """
    v1, v2, v3, key_a, _key_b = await _build_array(harness)

    v2.kill()
    # Outlive the 2s successor-watch poll plus slack.
    await asyncio.sleep(4.0)

    assert len(harness.children) == 3, "a replacement successor forked the lineage"
    child, _ = await harness.deliver_to_owner("after-v2-death")
    assert child is v3
    assert _authority_pid(harness.home) == v3.pid

    # v1 keeps finishing its own turn to the end.
    accepted = await harness.inject("turn-a", text="still-mine")
    assert accepted
    await asyncio.sleep(0.5)
    probe_v1 = await v1.probe()
    assert (key_a in probe_v1["pending"]) or (key_a in probe_v1["running"])

    v1.kill()
    await asyncio.sleep(1.0)
    child, _ = await harness.deliver_to_owner("final")
    assert child is v3


@pytest.mark.spawns_gateway_lookalike
@pytest.mark.asyncio
async def test_restart_on_draining_generation_forwards_to_newest(harness):
    """(d) A restart landing on draining v1 is taken by the newest generation."""
    v1, v2, v3, _key_a, _key_b = await _build_array(harness)

    result = await v1.queue_restart(chat_id="late-restart")
    assert result["result"]["status"] == "forwarded"
    assert result["result"]["forwarded_to"] == v3.pid

    # The newest generation spawned its own successor — one line, no fork.
    # (v3 has no active turn here, so by probe time it may already have
    # finished draining; the successor latch is the durable fact.)
    v4 = await harness.wait_child(4)
    await v4.wait_for("attached")
    probe_v1 = await v1.probe()
    assert probe_v1["successor_pid"] == v2.pid, "v1 must not spawn its own successor"
    probe_v3 = await _wait_probe(v3, lambda p: p["successor_pid"] == v4.pid)
    assert _authority_pid(harness.home) == v4.pid
