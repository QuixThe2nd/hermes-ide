"""One gateway generation for the restart-array E2E harness.

Runs the REAL restart and routing decision code — ``queue_user_restart``,
``request_restart`` (spawn → release → drain), ``claim_authority_when_lock_frees``
and the production ``DiscordHubClientAdapter`` hub protocol — against a real
``DiscordHubServer`` socket owned by the test process.

Faked, per the harness contract: the agent turn bodies (an asyncio hold per
session plus a ``_running_agents`` entry) and the hub-side Discord transport
(``DiscordAdapter.connect`` is patched in the parent; no network). Successors
are launched by the parent's fake ``spawn_gateway_successor`` with the
production env markers, so overlap identity, hub attestation and the
generation registry are all real.

Protocol: newline-delimited JSON on stdin/stdout.  The child emits
``{"event": ...}`` records (``boot``, ``attached``, ``claimed``, ``received``,
``restart_result``, ``probe``, ``fatal``) and accepts commands ``end_turn``,
``queue_restart`` and ``probe``.  Stdin EOF exits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


STAND_IN_TOKEN = "restart-array-stand-in-token"


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--home", required=True)
    args = parser.parse_args()

    os.environ["HERMES_HOME"] = args.home
    emit({"event": "boot", "pid": os.getpid()})

    from gateway import run as gateway_run
    from gateway.config import Platform, PlatformConfig
    from gateway.overlap_restart import (
        OVERLAP_PREDECESSOR_PID_ENV, OVERLAP_SUCCESSOR_ENV,
        claim_authority_when_lock_frees, remember_pending_predecessor,
    )
    from gateway.restart import queue_user_restart
    from gateway.run import GatewayRunner
    from gateway.status import (
        acquire_gateway_runtime_lock, clear_runtime_status_released,
        write_pid_file,
    )
    from gateway.session import SessionSource
    from plugins.platforms.discord.hub_client import DiscordHubClientAdapter
    from tests.gateway.restart_test_helpers import make_restart_source, make_restart_runner

    gateway_run._hermes_home = Path(args.home)
    runner, _legacy_adapter = make_restart_runner()
    for _name in (
        "_handle_forwarded_restart", "_intake_adapter_for", "_adapter_for_source",
        "_admit_bot_message_for_source", "_peek_session_state",
        "_resolve_busy_steer_or_redirect", "_reply_anchor_for_event",
        "_thread_metadata_for_source", "_busy_drain_admission",
    ):
        _method = getattr(GatewayRunner, _name, None)
        if _method is not None:
            setattr(runner, _name, _method.__get__(runner, GatewayRunner))
    runner._source_from_restart_notify_payload = (
        GatewayRunner._source_from_restart_notify_payload)
    # Deterministic busy-input behavior for the harness: queue follow-ups.
    runner._busy_input_mode = "queue"

    adapter = DiscordHubClientAdapter(
        PlatformConfig(enabled=True, token=STAND_IN_TOKEN))
    # Fail fast instead of hanging for the 300s default attach timeout.
    adapter.overlap_successor_attach_timeout = 30.0
    adapter._socket_path = Path(args.socket)
    adapter.gateway_runner = runner

    held_turns: dict[str, asyncio.Event] = {}

    async def _record_event(event) -> None:
        session_key = adapter._event_session_key(event)
        restart_resume = getattr(event, "_hub_generation_restart", None)
        emit({
            "event": "received", "session_key": session_key,
            "generation_changed": bool(restart_resume),
        })
        event._gateway_accepted = True
        # A live turn: holds the session busy (adapter guard is already
        # installed by _start_session_processing) and blocks the restart
        # drain until the harness releases it.
        runner._running_agents[session_key] = SimpleNamespace(
            interrupt_event=None)
        held = held_turns[session_key] = asyncio.Event()
        try:
            await held.wait()
        finally:
            held_turns.pop(session_key, None)
            runner._running_agents.pop(session_key, None)

    adapter.set_message_handler(_record_event)
    adapter.set_restart_request_handler(runner._handle_forwarded_restart)

    runner.adapters = {Platform.DISCORD: adapter}

    successor_mode = os.environ.get(OVERLAP_SUCCESSOR_ENV) == "1"
    predecessor_pid = 0
    claim_task = None
    if successor_mode:
        try:
            predecessor_pid = int(os.environ.get(OVERLAP_PREDECESSOR_PID_ENV, ""))
        except ValueError:
            predecessor_pid = 0
        runner._overlap_successor_pending = True
        runner._overlap_predecessor_pid = predecessor_pid
        # Production boot remembers the predecessor before the claim task can
        # pop the launch markers (gateway.run pops them once the runner owns
        # the one-shot state); overlap_attachment() then falls back to the
        # module global. Mirror that or the hub attach loses successor proof.
        remember_pending_predecessor(predecessor_pid)

        async def _on_authoritative() -> None:
            clear_runtime_status_released()
            emit({"event": "claimed", "pid": os.getpid(),
                  "predecessor": predecessor_pid})
            try:
                await runner._send_restart_notification()
            except Exception:
                pass

        claim_task = asyncio.create_task(
            claim_authority_when_lock_frees(
                runner, predecessor_pid, _on_authoritative),
        )
    else:
        # Original generation: take the singleton artifacts exactly like a
        # plain gateway boot (lock + PID file under the shared home).
        acquire_gateway_runtime_lock()
        write_pid_file()

    connected = await adapter.connect()
    if not connected:
        emit({"event": "fatal", "error": "hub attach failed"})
        return
    emit({"event": "attached", "pid": os.getpid(),
          "generation_id": adapter._generation_id,
          "successor_mode": successor_mode})

    async def _probe() -> None:
        emit({
            "event": "probe", "pid": os.getpid(),
            "draining": bool(runner._draining),
            "restart_requested": bool(runner._restart_requested),
            "successor_pid": getattr(runner, "_overlap_restart_successor_pid", None),
            "active_sessions": sorted(
                str(key) for key in getattr(adapter, "_active_sessions", {})),
            "pending": sorted(
                str(key) for key in (runner._pending_messages or {})),
            "running": sorted(
                str(key) for key in (runner._running_agents or {})),
        })

    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except ValueError:
            emit({"event": "fatal", "error": f"bad command: {line!r}"})
            continue
        kind = command.get("command")
        try:
            if kind == "end_turn":
                held = held_turns.get(str(command.get("session_key")))
                if held is not None:
                    held.set()
                await asyncio.sleep(0)  # let the handler's finally run
            elif kind == "queue_restart":
                source = make_restart_source(
                    platform=Platform.DISCORD,
                    chat_id=str(command.get("chat_id") or "restart-requester"))
                result = await queue_user_restart(
                    runner, source, str(command.get("message_id") or "m-1"))
                emit({"event": "restart_result", "pid": os.getpid(),
                      "result": result})
            elif kind == "probe":
                await _probe()
            else:
                emit({"event": "fatal", "error": f"unknown command {kind!r}"})
        except Exception:
            emit({"event": "fatal", "error": traceback.format_exc()})

    if claim_task is not None:
        claim_task.cancel()
        await asyncio.gather(claim_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        emit({"event": "fatal", "error": traceback.format_exc()})
