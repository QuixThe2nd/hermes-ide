import asyncio
import shutil
import subprocess
import threading
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.restart as gateway_restart
import gateway.run as gateway_run
from agent.i18n import t
from gateway.platforms.base import MessageEvent, MessageType
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT,
    pid_alive_fail_closed,
    run_detached_restart_watcher,
    spawn_replacement_gateway,
    wait_for_pid_exit,
)
from gateway.session import SessionEntry, build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.asyncio
async def test_restart_command_while_busy_requests_drain_without_interrupt(monkeypatch):
    # Ensure INVOCATION_ID is NOT set — systemd sets this in service mode,
    # which changes the restart call signature.
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_EXTERNAL_SUPERVISOR", raising=False)
    # Hermeticity: neutralize the real container probe (see
    # test_restart_service_detection.py) — /.dockerenv on a containerized CI
    # runner would otherwise route via_service=True under this test.
    monkeypatch.setattr(
        "gateway.restart.is_container_restart_context", lambda: False
    )
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
    )
    session_key = build_session_key(event.source)
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent

    result = await runner._handle_message(event)

    expected = t("gateway.draining", count=1)
    assert result == expected
    # Guard against the silent-degradation regression in #22266: if the i18n
    # catalog cannot be resolved (e.g. xdist workers losing the locales path)
    # then ``t("gateway.draining", count=1)`` returns the bare key
    # ``"gateway.draining"`` instead of the formatted English string, and both
    # sides of the equality above would still match. Assert on the catalog
    # output explicitly so a broken locale resolution fails loudly here.
    assert expected != "gateway.draining"
    assert "Asking" in expected and "1" in expected
    running_agent.interrupt.assert_not_called()
    runner.request_restart.assert_called_once_with(detached=True, via_service=False)


def test_load_busy_text_mode_follows_input_mode_and_honors_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_TEXT_MODE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)

    # No knobs set → follows busy_input_mode, which defaults to interrupt.
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "interrupt"

    # busy_input_mode=queue propagates to text handling (single source of truth).
    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: queue\n", encoding="utf-8"
    )
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "queue"

    # Legacy explicit busy_text_mode still wins for backward compat.
    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: interrupt\n  busy_text_mode: queue\n",
        encoding="utf-8",
    )
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "queue"

    # Legacy env override wins too.
    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: interrupt\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_TEXT_MODE", "queue")
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "queue"

    # Bogus legacy value is ignored → falls through to busy_input_mode (interrupt).
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_TEXT_MODE", "bogus")
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "interrupt"


def test_load_signal_interrupt_grace_timeout_from_typed_config(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    assert (
        gateway_run.GatewayRunner._load_signal_interrupt_grace_timeout()
        == DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT
    )

    (tmp_path / "config.yaml").write_text(
        "gateway:\n  signal_interrupt_grace_timeout: 0.25\n",
        encoding="utf-8",
    )
    assert gateway_run.GatewayRunner._load_signal_interrupt_grace_timeout() == 0.25

    (tmp_path / "config.yaml").write_text(
        "gateway:\n  signal_interrupt_grace_timeout: 0\n",
        encoding="utf-8",
    )
    assert gateway_run.GatewayRunner._load_signal_interrupt_grace_timeout() == 0.0

    (tmp_path / "config.yaml").write_text(
        "gateway:\n  signal_interrupt_grace_timeout: .inf\n",
        encoding="utf-8",
    )
    assert (
        gateway_run.GatewayRunner._load_signal_interrupt_grace_timeout()
        == DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT
    )

    (tmp_path / "config.yaml").write_text(
        "gateway:\n  signal_interrupt_grace_timeout: invalid\n",
        encoding="utf-8",
    )
    assert (
        gateway_run.GatewayRunner._load_signal_interrupt_grace_timeout()
        == DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT
    )
    assert "Invalid signal_interrupt_grace_timeout" in caplog.text


@pytest.mark.asyncio
async def test_request_restart_is_idempotent():
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._launch_detached_restart_watcher = AsyncMock()

    # _run_restart is held on self._restart_task and is intentionally NOT in
    # _background_tasks, so _stop_impl's cancel loop can't abort it mid-await
    # (see #12875).
    assert runner.request_restart(detached=True, via_service=False) is True
    assert runner._restart_task is not None
    assert runner._restart_task not in runner._background_tasks
    assert runner.request_restart(detached=True, via_service=False) is False
    # In-band restart marks draining immediately so new turns are refused
    # while any after-turn wait runs (#77184).
    assert runner._draining is True

    await runner._restart_task

    # stop() is the SOLE owner of the standalone watcher: the drain loop
    # itself never launches one — nothing is awaited between the final
    # active-count boundary check and stop().
    runner._launch_detached_restart_watcher.assert_not_awaited()
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=True, service_restart=False
    )


@pytest.mark.asyncio
async def test_request_restart_defers_stop_until_active_turn_finishes():
    """Regression for #77184: requesting turn must not enter the drain set."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._launch_detached_restart_watcher = AsyncMock()
    runner._restart_after_turn_timeout = 5.0
    session_key = "agent:main:telegram:dm:123"
    runner._running_agents[session_key] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True
    assert runner._draining is True

    # While the requesting turn is still active, stop() must not run.
    await asyncio.sleep(0.25)
    runner.stop.assert_not_awaited()
    assert session_key in runner._running_agents

    # Turn finishes → restart proceeds immediately (drain set empty).
    del runner._running_agents[session_key]
    await runner._restart_task

    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )
    # The standalone watcher is only for the non-service path — and only
    # stop() may launch it, never the drain loop.
    runner._launch_detached_restart_watcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_restart_after_turn_timeout_zero_still_waits_for_active_work():
    """restart_after_turn_timeout=0 is legacy and NON-authoritative: it must
    not advance a user-requested restart into stop() while work is active
    (#77184 — the wait is unbounded for every value)."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 0.0
    key = "agent:main:telegram:dm:1"
    runner._running_agents[key] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True
    # Well past what a 0s "cap" would ever authorise: still no stop().
    await asyncio.sleep(0.3)
    runner.stop.assert_not_awaited()
    assert key in runner._running_agents

    # Work finishes → stop() runs exactly once.
    del runner._running_agents[key]
    await runner._restart_task
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )


@pytest.mark.asyncio
async def test_request_restart_cap_elapsed_never_calls_stop_with_active_work():
    """A tiny legacy cap elapsing must NOT authorise stop() over live work."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 0.2
    key = "agent:main:telegram:dm:1"
    runner._running_agents[key] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True

    # 1.5s = ~7x the configured 0.2s cap: the elapsed time never becomes
    # authority to force; the restart keeps waiting.
    await asyncio.sleep(1.5)
    runner.stop.assert_not_awaited()
    assert key in runner._running_agents

    del runner._running_agents[key]
    await runner._restart_task
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )


@pytest.mark.asyncio
async def test_request_restart_wait_cancelled_never_calls_stop():
    """Cancellation of the restart wait must not fall through into stop()."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._running_agents["agent:main:telegram:dm:1"] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True
    await asyncio.sleep(0.15)  # the wait is now inside its poll loop
    runner._restart_task.cancel()
    await asyncio.gather(runner._restart_task, return_exceptions=True)

    runner.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_restart_wait_exception_never_calls_stop(monkeypatch):
    """A failing wait retries; it must never fall through into stop()."""
    monkeypatch.setattr(gateway_run, "_RESTART_WAIT_RETRY_SLEEP_S", 0.01)
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._running_agents["agent:main:telegram:dm:1"] = MagicMock()

    real_wait = runner._await_active_work_before_restart

    async def exploding_wait():
        raise RuntimeError("wait exploded")

    runner._await_active_work_before_restart = exploding_wait
    assert runner.request_restart(detached=False, via_service=True) is True
    # Several retry cycles: no stop(), and the task is still waiting.
    await asyncio.sleep(0.1)
    runner.stop.assert_not_awaited()
    assert not runner._restart_task.done()

    # Recovery: a working wait plus drained work completes the restart.
    runner._await_active_work_before_restart = real_wait
    runner._running_agents.clear()
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_boundary_guard_holds_stop_over_work_returning_mid_wait():
    """The pre-stop() boundary check keeps holding when work reappears.

    The wait itself polls the real count, but the guard in ``_run_restart``
    is the structural backstop (#77184): if a wait call returns while the
    real count is again above zero, stop() must not run — the loop keeps
    waiting instead.
    """
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()

    async def wait_that_claims_drain_over_live_work():
        # Returns "drained" immediately while an agent is still registered —
        # simulating a future timeout branch reporting success over live
        # work. The boundary guard must not believe it.
        return True

    runner._await_active_work_before_restart = wait_that_claims_drain_over_live_work
    runner._running_agents["agent:main:telegram:dm:1"] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True
    await asyncio.sleep(0.2)
    runner.stop.assert_not_awaited()
    assert runner._running_agents

    # Work truly clears → stop() runs exactly once.
    runner._running_agents.clear()
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )


@pytest.mark.asyncio
async def test_stop_waits_through_late_work_injected_at_every_pre_stop_await(
    monkeypatch,
):
    """The fresh-review probe: work appearing during ANY await that runs
    before stop() — inside the drain wait, during the settle sleep — must
    send the restart back to the no-deadline wait. stop() runs exactly once,
    only once the real count is zero, and NOTHING is launched by the drain
    loop itself: stop() owns the standalone watcher, and no awaited helper
    sits between the final boundary check and stop().
    """
    runner, _adapter = make_restart_runner()
    counts_at_stop: list[int] = []

    async def _stop(**_kwargs):
        counts_at_stop.append(runner._active_work_count())

    runner.stop = AsyncMock(side_effect=_stop)

    real_wait = runner._await_active_work_before_restart
    wait_calls = {"n": 0}

    async def wait_injecting_once_then_real():
        wait_calls["n"] += 1
        if wait_calls["n"] == 1:
            # A unit appears while the wait's own awaits run.
            runner._running_agents["late-during-wait"] = MagicMock()
            return True
        return await real_wait()

    runner._await_active_work_before_restart = wait_injecting_once_then_real

    # The drain loop must launch NO watcher of its own: stop() is the sole
    # owner. Recording the call (rather than leaving the bound method) keeps
    # this asserting the architecture, not just the absence of a crash.
    runner._launch_detached_restart_watcher = AsyncMock()

    real_sleep = asyncio.sleep
    sleep_injections = {"left": 1}

    async def sleep_injecting_once(delay, *args, **kwargs):
        # A unit appears during the pre-boundary settle sleep (0.05s only,
        # so the drain wait's own 0.1s polls are untouched).
        if float(delay) == 0.05 and sleep_injections["left"] > 0:
            sleep_injections["left"] -= 1
            runner._running_agents["late-at-sleep"] = MagicMock()
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", sleep_injecting_once)

    assert runner.request_restart(detached=True, via_service=False) is True

    # Every injected unit is live and stop() has not run.
    await asyncio.sleep(0.3)
    runner.stop.assert_not_awaited()
    assert sorted(runner._running_agents) == [
        "late-at-sleep",
        "late-during-wait",
    ]
    # The drain loop never launched a watcher despite restarting its pass.
    runner._launch_detached_restart_watcher.assert_not_awaited()

    # The final item clearing is what unlocks exactly one stop() at zero.
    runner._running_agents.clear()
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=True, service_restart=False
    )
    assert counts_at_stop == [0]


@pytest.mark.asyncio
async def test_request_restart_create_task_failure_leaves_gateway_retryable(
    monkeypatch,
):
    """If ``create_task`` itself fails (a closing loop), request_restart
    must undo its flags — no task, no restart, admission restored, so a
    retry works instead of answering ``already_in_progress`` forever."""

    def _failing_create_task(coro):
        coro.close()
        raise RuntimeError("loop is closed")

    monkeypatch.setattr(gateway_run.asyncio, "create_task", _failing_create_task)

    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()

    with pytest.raises(RuntimeError, match="loop is closed"):
        runner.request_restart(detached=False, via_service=True)

    assert runner._restart_task_started is False
    assert runner._restart_requested is False
    assert runner._draining is False

    monkeypatch.undo()
    assert runner.request_restart(detached=False, via_service=True) is True
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_restart_cycle_setup_failure_rolls_back_completely():
    """The fresh-review Fix 2 probe: an exception out of
    ``_begin_restart_cycle()`` fires AFTER the flags were set but BEFORE any
    task exists. Every flag must come back — no started latch, no
    ``already_in_progress`` dead end, admission open — and a retry must
    work."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()

    def _exploding_cycle():
        raise RuntimeError("cycle setter wedged")

    runner._begin_restart_cycle = _exploding_cycle

    with pytest.raises(RuntimeError, match="cycle setter wedged"):
        runner.request_restart(detached=True, via_service=False)

    assert runner._restart_task is None
    assert runner._restart_task_started is False
    assert runner._restart_requested is False
    assert runner._restart_detached is False
    assert runner._restart_via_service is False
    assert runner._draining is False
    assert runner._restart_cycle_open is False

    # Retry succeeds and the real drain task runs to its one stop().
    runner._begin_restart_cycle = gateway_run.GatewayRunner._begin_restart_cycle.__get__(
        runner, gateway_run.GatewayRunner
    )
    assert runner.request_restart(detached=True, via_service=False) is True
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=True, service_restart=False
    )


@pytest.mark.asyncio
async def test_request_restart_cycle_setup_partial_mutation_rolls_back():
    """A cycle opener that HALF-runs (opens the cycle, then raises) must not
    leave the half-minted generation shadowing the next cycle: rollback
    closes what this request opened."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    real_cycle = gateway_run.GatewayRunner._begin_restart_cycle.__get__(
        runner, gateway_run.GatewayRunner
    )
    generation_before = runner._restart_generation

    def _cycle_opens_then_raises():
        real_cycle()  # mutates: _restart_cycle_open=True, generation bumped
        raise RuntimeError("wedged after opening")

    runner._begin_restart_cycle = _cycle_opens_then_raises

    with pytest.raises(RuntimeError, match="wedged after opening"):
        runner.request_restart(detached=False, via_service=True)

    # The half-opened cycle was closed again by the rollback.
    assert runner._restart_cycle_open is False
    assert runner._restart_task_started is False
    assert runner._draining is False

    # The retry mints a fresh cycle and completes.
    runner._begin_restart_cycle = real_cycle
    assert runner.request_restart(detached=False, via_service=True) is True
    assert runner._restart_cycle_open is True
    assert runner._restart_generation > generation_before
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_restart_create_task_failure_closes_partial_cycle(monkeypatch):
    """create_task failing AFTER the cycle opener ran: same transaction —
    the cycle the request opened is closed, the flags restored, retry OK."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()

    def _failing_create_task(coro):
        coro.close()
        raise RuntimeError("loop is closed")

    monkeypatch.setattr(gateway_run.asyncio, "create_task", _failing_create_task)

    with pytest.raises(RuntimeError, match="loop is closed"):
        runner.request_restart(detached=False, via_service=True)

    assert runner._restart_cycle_open is False
    assert runner._restart_task_started is False
    assert runner._draining is False

    monkeypatch.undo()
    assert runner.request_restart(detached=False, via_service=True) is True
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_restart_excluded_from_stop_cancel_loop():
    """Regression for #12875: _run_restart is held on self._restart_task and
    kept OUT of _background_tasks, and the _stop_impl cancel loop explicitly
    skips it. If it were in _background_tasks, the cancel loop (which fires
    while _run_restart is awaiting _stop_task) would propagate CancelledError
    into _stop_impl and skip _shutdown_event.set() / _exit_code = 75."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()

    # A decoy background task that SHOULD be cancelled, plus the restart task
    # that must NOT be.
    async def _decoy():
        await asyncio.sleep(0.2)

    decoy = asyncio.create_task(_decoy())
    runner._background_tasks.add(decoy)
    decoy.add_done_callback(runner._background_tasks.discard)

    assert runner.request_restart(detached=False, via_service=True) is True
    restart_task = runner._restart_task
    assert restart_task is not None
    assert restart_task not in runner._background_tasks

    # Run the real cancel loop body in isolation (mirrors _stop_impl:7234).
    runner._stop_task = None
    for _task in list(runner._background_tasks):
        if _task is runner._stop_task:
            continue
        if _task is runner._restart_task:
            continue
        _task.cancel()

    await asyncio.sleep(0)  # let cancellation settle
    assert decoy.cancelled()
    assert not restart_task.cancelled()

    await restart_task
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )


@pytest.mark.windows_only
@pytest.mark.asyncio
async def test_windows_detached_restart_scrubs_gateway_marker(monkeypatch, tmp_path):
    """Faking sys.platform="win32" on Linux could not reach the real Windows
    detach branch (msvcrt/creationflags spawn, Lib/site-packages venv layout);
    this runs on the Windows CI job instead."""
    runner, _adapter = make_restart_runner()
    popen_calls = []
    venv_dir = tmp_path / "venv"
    site_packages = venv_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)

    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_dir))

    import hermes_cli._subprocess_compat as subprocess_compat

    monkeypatch.setattr(
        subprocess_compat,
        "windows_detach_popen_kwargs",
        lambda: {},
    )

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # Popen is a recording fake here, so run the real (non-isolated) watcher
    # spawn path rather than the HERMES_TEST_ISOLATION no-op branch.
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)

    await runner._launch_detached_restart_watcher()

    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    # The watcher is a direct Python bootstrap of the repository watcher
    # body, never the legacy CLI: no "hermes", no "gateway"/"restart" argv
    # words, no service manager.
    assert cmd[0] == gateway_run.sys.executable
    assert cmd[1] == "-c"
    assert "run_detached_restart_watcher" in cmd[2]
    assert cmd[-2:] == ["321", str(gateway_run.Path(gateway_run.__file__).resolve().parent.parent)]
    assert "gateway restart" not in " ".join(cmd)
    assert "--replace" not in cmd
    assert kwargs["env"].get("_HERMES_GATEWAY") is None
    assert kwargs["env"]["VIRTUAL_ENV"] == str(venv_dir)
    assert str(site_packages) in kwargs["env"]["PYTHONPATH"].split(gateway_run.os.pathsep)
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


@pytest.mark.windows_only
@pytest.mark.asyncio
async def test_windows_detached_restart_watcher_keeps_console_python(monkeypatch, tmp_path):
    """The restart watcher must run sys.executable (console python) under the
    hidden-console detach kwargs — NOT swap in GUI-subsystem pythonw.exe,
    which would leave the watcher console-less and make its descendants
    flash visible conhosts (#54220/#56747).

    Faking sys.platform on Linux could not enter the Windows-only watcher
    spawn branch this asserts on, so it runs on the Windows CI job.
    """
    runner, _adapter = make_restart_runner()
    popen_calls = []
    venv_dir = tmp_path / "venv"
    site_packages = venv_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)

    monkeypatch.setattr(gateway_run.sys, "executable", r"C:\venv\Scripts\python.exe")
    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_dir))

    import hermes_cli._subprocess_compat as subprocess_compat

    monkeypatch.setattr(
        subprocess_compat,
        "windows_detach_popen_kwargs",
        lambda: {"creationflags": 0x08000200},
    )

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # Popen is a recording fake here, so run the real (non-isolated) watcher
    # spawn path rather than the HERMES_TEST_ISOLATION no-op branch.
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)

    await runner._launch_detached_restart_watcher()

    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    assert cmd[0] == r"C:\venv\Scripts\python.exe"
    assert cmd[1] == "-c"
    assert "run_detached_restart_watcher" in cmd[2]
    assert cmd[-2:] == ["321", str(gateway_run.Path(gateway_run.__file__).resolve().parent.parent)]
    assert kwargs["creationflags"] == 0x08000200


# ── Standalone detached watcher contract (Fix 3) ──────────────────────


@pytest.mark.asyncio
async def test_detached_restart_watcher_spawns_direct_gateway_run_bootstrap(
    monkeypatch,
):
    """POSIX watcher spawn: one Python ``-c`` bootstrap of the repository
    watcher body, carrying this exact PID and the project root — never the
    legacy ``hermes gateway restart`` CLI, ``--replace``, or a service
    manager."""
    runner, _adapter = make_restart_runner()
    popen_calls = []

    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    # Deterministic argv: no setsid wrapper on this box's PATH.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    await runner._launch_detached_restart_watcher()

    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    project_root = str(gateway_run.Path(gateway_run.__file__).resolve().parent.parent)
    assert cmd == [
        gateway_run.sys.executable,
        "-c",
        cmd[2],  # the bootstrap body, asserted below
        "321",
        project_root,
    ]
    # The bootstrap runs the shared watcher contract — the same function the
    # DI tests below exercise — and nothing else.
    assert "from gateway.restart import run_detached_restart_watcher" in cmd[2]
    assert "hermes" not in cmd
    assert "gateway restart" not in " ".join(cmd)
    assert "--replace" not in cmd
    assert "systemctl" not in cmd and "launchctl" not in cmd and "s6-svc" not in cmd
    assert kwargs["env"].get("_HERMES_GATEWAY") is None
    # The bootstrap's import of gateway.restart resolves from the spawned
    # cwd (-c puts it at sys.path[0]) — it must be pinned to the project
    # root, never inherited from the dying gateway's arbitrary cwd.
    assert kwargs["cwd"] == project_root
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


@pytest.mark.asyncio
async def test_detached_restart_watcher_launch_is_idempotent(monkeypatch):
    """Duplicate launcher calls (a second stop into the same teardown, a
    stray helper-style call) spawn exactly ONE watcher process."""
    runner, _adapter = make_restart_runner()
    popen_calls = []

    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: popen_calls.append(cmd))

    await runner._launch_detached_restart_watcher()
    await runner._launch_detached_restart_watcher()
    await runner._launch_detached_restart_watcher()

    assert len(popen_calls) == 1


@pytest.mark.asyncio
async def test_detached_restart_watcher_spawn_failure_reported_plainly(
    monkeypatch, caplog,
):
    """A failing watcher spawn is reported as exactly that — no claim a
    replacement was scheduled — and never crashes the caller."""
    runner, _adapter = make_restart_runner()

    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        MagicMock(side_effect=OSError(13, "Permission denied")),
    )

    async def _no_crash():
        await runner._launch_detached_restart_watcher()

    await asyncio.wait_for(_no_crash(), timeout=5.0)
    assert "will not be respawned" in caplog.text
    # The latch stays set: a second attempt must not re-enter a spawn loop
    # against an environment that just refused one.
    assert runner._detached_restart_watcher_started is True


@pytest.mark.linux_only
def test_pid_alive_fail_closed_reads_every_unknown_as_live(monkeypatch):
    """EPERM and unknown probe errors mean "cannot prove absent" — the
    watcher's only authority to replace is a proven-dead PID."""
    monkeypatch.setattr(
        gateway_restart.os,
        "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError(pid)),
    )
    assert pid_alive_fail_closed(4242) is False

    monkeypatch.setattr(
        gateway_restart.os,
        "kill",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError(pid)),
    )
    assert pid_alive_fail_closed(4242) is True

    monkeypatch.setattr(
        gateway_restart.os,
        "kill",
        lambda pid, sig: (_ for _ in ()).throw(OSError(11, "again")),
    )
    assert pid_alive_fail_closed(4242) is True

    monkeypatch.setattr(
        gateway_restart.os, "kill", lambda pid, sig: None
    )
    assert pid_alive_fail_closed(4242) is True


def test_wait_for_pid_exit_has_no_deadline(monkeypatch):
    """Elapsed time never ends the wait while the PID reads live: however
    many polls it takes, the loop keeps going until proven absence."""
    polls = {"n": 0}

    def alive_long_then_absent(_pid):
        polls["n"] += 1
        # A very long-lived old process: far beyond any plausible deadline.
        return polls["n"] < 500

    sleeps: list[float] = []

    wait_for_pid_exit(4242, poll_s=0.2, alive=alive_long_then_absent, sleep=sleeps.append)

    assert polls["n"] == 500  # every live poll was honoured, none expired
    assert sleeps == [0.2] * 499  # one sleep per live poll, then exit


def test_wait_for_pid_exit_blocks_while_probe_errors_persist():
    """Fail-closed probes (EPERM/unknown) keep the wait alive exactly like a
    live PID — a persistently unreadable process is never replaced — and no
    elapsed timeout releases it: the loop ends ONLY on proven absence.

    The real deadline-free loop runs on a bounded worker thread with a
    controllable release: the test drives it through far more fail-closed
    polls than any hidden deadline could survive, proves it is still
    blocked, then flips the probe to proven-absence and proves the wait
    ends. The worker is a daemon and the release is an Event, so a broken
    wait can fail this test but can never hang it.
    """
    proven_absent = threading.Event()
    finished = threading.Event()
    polls = {"n": 0}

    def eperm_reads_live_until_proven_absent(_pid):
        polls["n"] += 1
        # What pid_alive_fail_closed answers while EPERM/unknown probe
        # errors persist: still-live, until absence is PROVEN.
        return not proven_absent.is_set()

    def _wait():
        wait_for_pid_exit(
            4242,
            poll_s=0.0,
            alive=eperm_reads_live_until_proven_absent,
            sleep=lambda _s: None,
        )
        finished.set()

    worker = threading.Thread(target=_wait, daemon=True)
    worker.start()

    # Far more fail-closed polls than any plausible hidden deadline: every
    # one read "live/unknown", and not one released the wait.
    deadline = time.monotonic() + 5.0
    while polls["n"] < 100_000 and time.monotonic() < deadline:
        time.sleep(0)
    assert polls["n"] >= 100_000, "worker never got going — probe not driving"
    assert not finished.is_set()  # elapsed polls never force an exit

    # Only PROVEN ABSENCE ends the wait.
    proven_absent.set()
    worker.join(timeout=5.0)
    assert finished.is_set(), "wait outlived proven absence"
    assert polls["n"] >= 100_001  # exactly one more poll observed the absence


def test_spawn_replacement_gateway_uses_direct_entry_point(tmp_path):
    """The replacement launch is ``sys.executable -m gateway.run`` from the
    project root — the repository's own entry point, detached via the
    existing helpers, with no CLI/service-manager tokens anywhere."""
    calls = []

    def recording_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return MagicMock()

    proc = spawn_replacement_gateway(str(tmp_path), popen=recording_popen)

    assert proc is not None
    argv, kwargs = calls[0]
    assert argv == [gateway_run.sys.executable, "-m", "gateway.run"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert "hermes" not in argv
    assert "systemctl" not in argv and "launchctl" not in argv
    if gateway_run.os.name != "nt":
        assert kwargs["start_new_session"] is True
    else:
        assert "creationflags" in kwargs


def test_run_detached_restart_watcher_replaces_only_after_absence(monkeypatch):
    """The watcher body: one wait-for-absence, then exactly one direct
    replacement spawn — never before the PID is proven gone."""
    order: list[str] = []
    polls = {"n": 0}

    def alive_twice_then_absent(_pid):
        polls["n"] += 1
        order.append(f"poll:{polls['n']}")
        return polls["n"] < 3

    def wait_spy(pid, *, poll_s=0.2, alive=None, sleep=None):
        order.append("wait:start")
        wait_for_pid_exit(
            pid, alive=alive_twice_then_absent, sleep=lambda _s: None
        )
        order.append("wait:done")

    def spawn_spy(project_root, *, popen=subprocess.Popen):
        order.append(f"spawn:{project_root}")
        return MagicMock()

    monkeypatch.setattr(gateway_restart, "wait_for_pid_exit", wait_spy)
    monkeypatch.setattr(gateway_restart, "spawn_replacement_gateway", spawn_spy)

    run_detached_restart_watcher(4242, "/proj", poll_s=0.0)

    assert order == [
        "wait:start",
        "poll:1",
        "poll:2",
        "poll:3",
        "wait:done",
        "spawn:/proj",
    ]
    assert order.count("spawn:/proj") == 1


# ── Shutdown notification tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_notification_uses_persisted_origin_for_colon_ids():
    """Shutdown notifications should route from persisted origin, not reparsed keys."""
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock()
    source = make_restart_source(chat_id="!room123:example.org", chat_type="group")
    source.platform = gateway_run.Platform.MATRIX
    session_key = build_session_key(source)
    runner._running_agents[session_key] = MagicMock()
    runner.session_store._entries = {
        session_key: SessionEntry(
            session_key=session_key,
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            platform=source.platform,
            chat_type=source.chat_type,
        )
    }
    runner.adapters = {gateway_run.Platform.MATRIX: adapter}

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.send.await_count == 1


@pytest.mark.asyncio
async def test_drain_suppress_skips_home_channel_keeps_session_ping(tmp_path, monkeypatch):
    """A suppress_notification drain marker mutes ONLY the home-channel broadcast.

    The per-active-session interrupt ping MUST still fire (it carries the
    "your task was interrupted, message me to resume" hint). This is the core
    drain-notification-suppression contract.
    """
    from gateway.config import HomeChannel, Platform
    import gateway.drain_control as dc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    runner, adapter = make_restart_runner()
    # A home channel distinct from the active session's chat.
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    # One active session in a different chat.
    runner._running_agents["agent:main:telegram:dm:999"] = MagicMock()

    # NAS auto-update drain: marker present with suppress_notification=True.
    dc.write_drain_request(principal="nas", suppress_notification=True)

    await runner._notify_active_sessions_of_shutdown()

    # Exactly one send — the active-session ping to chat 999. The home-channel
    # broadcast to home-42 was suppressed.
    assert len(adapter.sent_calls) == 1
    sent_chat_ids = {chat_id for chat_id, _content, _meta in adapter.sent_calls}
    assert "999" in sent_chat_ids
    assert "home-42" not in sent_chat_ids
    assert "shutting down" in adapter.sent[0]




def _wedged_agent(idle_seconds: float = 4000.0) -> MagicMock:
    """Agent double whose activity summary reports it idle past the timeout."""
    agent = MagicMock()
    agent.get_activity_summary = MagicMock(
        return_value={"seconds_since_activity": idle_seconds}
    )
    return agent


def _live_agent(idle_seconds: float = 1.0) -> MagicMock:
    agent = MagicMock()
    agent.get_activity_summary = MagicMock(
        return_value={"seconds_since_activity": idle_seconds}
    )
    return agent


@pytest.mark.asyncio
async def test_request_restart_wedged_turn_still_blocks(monkeypatch):
    """A wedged-classified turn remains ACTIVE work: it blocks the restart.

    The wedge label is diagnostics-only for restart progress (#77184): a
    live-but-unresponsive process still holds in-flight work, and a
    user-requested restart never forces it. Recovery of a *provably dead*
    process stays on the separate crash/wedge-escalation path.
    """
    monkeypatch.delenv("HERMES_AGENT_TIMEOUT", raising=False)
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    key = "agent:main:whatsapp:dm:1"
    runner._running_agents[key] = _wedged_agent()
    assert runner._wedged_agent_count() == 1  # the label really is "wedged"

    assert runner.request_restart(detached=False, via_service=True) is True

    # The old cap (restart_after_turn_timeout, any value) is gone; even so,
    # nothing may force this.
    await asyncio.sleep(0.5)
    runner.stop.assert_not_awaited()
    assert key in runner._running_agents

    # The unit eventually clears (operator recovery, agent finish) → stop()
    # runs exactly once.
    del runner._running_agents[key]
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )


@pytest.mark.asyncio
async def test_request_restart_waits_for_live_and_wedged_together(monkeypatch):
    """Mixed live + wedged: BOTH are active work; both must clear first."""
    monkeypatch.delenv("HERMES_AGENT_TIMEOUT", raising=False)
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._launch_detached_restart_watcher = AsyncMock()
    live_key = "agent:main:telegram:dm:2"
    wedged_key = "agent:main:whatsapp:dm:1"
    runner._running_agents[wedged_key] = _wedged_agent()
    runner._running_agents[live_key] = _live_agent()

    assert runner.request_restart(detached=False, via_service=True) is True

    # Live turn active → stop() must not run yet.
    await asyncio.sleep(0.25)
    runner.stop.assert_not_awaited()

    # Live turn finishes but the wedged one remains → STILL blocked: the
    # wedge label never becomes authority to proceed.
    del runner._running_agents[live_key]
    await asyncio.sleep(0.3)
    runner.stop.assert_not_awaited()

    del runner._running_agents[wedged_key]
    await asyncio.wait_for(runner._restart_task, timeout=5.0)
    runner.stop.assert_awaited_once()


def test_wedged_agent_count_disabled_timeout_counts_nothing(monkeypatch):
    """gateway_timeout=0 (unbounded turns) disables wedge detection."""
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    runner, _adapter = make_restart_runner()
    runner._running_agents["agent:main:telegram:dm:1"] = _wedged_agent(10**6)
    assert runner._wedged_agent_count() == 0


def test_wedged_agent_count_ignores_sentinels_and_bad_summaries(monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_TIMEOUT", raising=False)
    runner, _adapter = make_restart_runner()
    broken = MagicMock()
    broken.get_activity_summary = MagicMock(side_effect=RuntimeError("boom"))
    non_dict = MagicMock()  # auto-attr summary returns a MagicMock, not a dict
    runner._running_agents.update(
        {
            "pending": gateway_run._AGENT_PENDING_SENTINEL,
            "broken": broken,
            "non_dict": non_dict,
            "wedged": _wedged_agent(),
            "live": _live_agent(),
        }
    )
    assert runner._wedged_agent_count() == 1
