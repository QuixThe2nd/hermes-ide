"""Cron/API work probes fail CLOSED at every restart decision (#77184).

``_active_cron_job_count``/``_active_api_run_count`` swallowed their own
lookup errors and reported 0 — so an unreadable scheduler or adapter could
satisfy the drain wait and AUTHORIZE the final stop boundary while real
work was in flight. The regressions here pin all three layers: the strict
helpers re-raise, the drain view turns a failure into "still busy"
(None, never 0), and the stop boundary retries on a failed probe instead
of proceeding — for BOTH helpers.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner


def _poison_cron(runner, monkeypatch):
    def _boom(*, strict=False):
        raise RuntimeError("cron scheduler unreachable")

    runner._active_cron_job_count = _boom


def _poison_api(runner, monkeypatch):
    def _boom(*, strict=False):
        raise RuntimeError("api adapter unreachable")

    runner._active_api_run_count = _boom


# ── the helpers: tolerant by default, strict re-raises ──────────────────────


def test_cron_probe_strict_raises_while_tolerant_degrades_to_zero(monkeypatch):
    monkeypatch.setattr(
        "cron.scheduler.get_running_job_ids",
        lambda: (_ for _ in ()).throw(RuntimeError("scheduler down")),
    )
    runner, _adapter = make_restart_runner()

    assert runner._active_cron_job_count() == 0  # best-effort, as before
    with pytest.raises(RuntimeError):
        runner._active_cron_job_count(strict=True)


def test_api_probe_strict_raises_while_tolerant_degrades_to_zero():
    runner, _adapter = make_restart_runner()
    runner.adapters[Platform.API_SERVER] = SimpleNamespace(
        active_agent_work_count=lambda: (_ for _ in ()).throw(
            RuntimeError("api down")
        )
    )

    assert runner._active_api_run_count() == 0  # best-effort, as before
    with pytest.raises(RuntimeError):
        runner._active_api_run_count(strict=True)


@pytest.mark.parametrize("poison", [_poison_cron, _poison_api])
def test_probed_active_work_count_reads_failure_as_busy_not_zero(
    poison, monkeypatch
):
    """The drain's view of the count: a failed probe is None — the drain
    keeps waiting and retrying, it never reads the failure as "0 work"."""
    runner, _adapter = make_restart_runner()
    assert runner._probed_active_work_count() == 0

    poison(runner, monkeypatch)
    assert runner._probed_active_work_count() is None

    # And the authoritative count itself raises — never silently zero.
    with pytest.raises(RuntimeError):
        runner._authoritative_active_work_count()


# ── end to end: a failing probe keeps the whole restart blocked ─────────────


@pytest.mark.parametrize("poison", [_poison_cron, _poison_api])
@pytest.mark.asyncio
async def test_probe_failure_keeps_the_drain_blocked_then_heals(monkeypatch, poison):
    """With the probe failing for the whole drain, stop() is never called;
    once the probe answers zero again the restart proceeds normally."""
    monkeypatch.setattr(gateway_run, "_RESTART_WAIT_RETRY_SLEEP_S", 0.01)
    runner, _adapter = make_restart_runner()
    stop = AsyncMock()
    runner.stop = stop
    poison(runner, monkeypatch)

    assert runner.request_restart() is True  # admission closes synchronously
    assert runner._draining is True

    await asyncio.sleep(0.3)  # many drain iterations under the failed probe
    stop.assert_not_called()
    assert runner._draining is True  # still closed, still waiting

    # Heal: the probe answers again — with genuinely zero work — and the
    # restart proceeds through the normal boundary.
    runner._active_cron_job_count = lambda *, strict=False: 0
    runner._active_api_run_count = lambda *, strict=False: 0

    for _ in range(100):  # bounded wait for the drain + boundary to see it
        await asyncio.sleep(0.05)
        if stop.await_count:
            break
    stop.assert_awaited_once()
    stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=False
    )


# ── the final stop boundary recheck itself ──────────────────────────────────


@pytest.mark.parametrize("poison", [_poison_cron, _poison_api])
@pytest.mark.asyncio
async def test_probe_exception_at_the_final_recheck_never_authorizes_stop(
    monkeypatch, poison
):
    """Direct hit on the boundary: the drain wait has returned (zero work
    observed), and the STRICT final recheck raises. The boundary must treat
    that as busy-and-retryable — never as zero — and only stop() once the
    probe answers again with a provable zero."""
    monkeypatch.setattr(gateway_run, "_RESTART_WAIT_RETRY_SLEEP_S", 0.01)
    runner, _adapter = make_restart_runner()
    stop = AsyncMock()
    runner.stop = stop

    async def _drained():
        return True  # the wait is over; the boundary recheck decides now

    runner._await_active_work_before_restart = _drained
    poison(runner, monkeypatch)

    assert runner.request_restart() is True

    # Far more boundary cycles than one retry sleep each: the exception is
    # retried, and none of them authorizes stop().
    await asyncio.sleep(0.3)
    stop.assert_not_called()
    assert runner._draining is True

    # Heal — the very next boundary cycle may finally proceed.
    runner._active_cron_job_count = lambda *, strict=False: 0
    runner._active_api_run_count = lambda *, strict=False: 0
    for _ in range(100):
        await asyncio.sleep(0.05)
        if stop.await_count:
            break
    stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=False
    )
