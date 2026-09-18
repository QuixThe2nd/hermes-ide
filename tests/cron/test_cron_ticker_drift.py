"""Regression tests for #114467 — cron ticker drifts: sleep-after-work means jobs fire progressively later.

When the ticker loops used ``stop_event.wait(interval)``, each tick slept for ``interval``
AFTER executing the tick work. Because tick work takes non-zero duration (typically 0.1s - 0.5s),
the ticker loop drifted forward every single cycle (~11-14s/hour). Over several hours,
the cumulative drift caused jobs to fire later and later until sawtooth phase-wrap
caused back-to-back rapid firings.

The fix tracks a target monotonic deadline ``next_tick += wait_for`` and sleeps
``max(0.0, next_tick - now)``. If tick execution overruns the interval or the host
suspends, the deadline is re-anchored to ``now + wait_for`` to avoid bursting.
"""
import errno
import threading
from unittest.mock import patch
import pytest

from cron.scheduler_provider import InProcessCronScheduler


class TestTickerLoopDrift:
    def test_single_ticker_sleeps_to_deadline_compensating_for_work(self):
        """Single ticker must subtract tick duration from sleep so period stays exact."""
        prov = InProcessCronScheduler()
        stop = threading.Event()
        simulated_time = [1000.0]
        recorded_waits = []

        def fake_monotonic():
            return simulated_time[0]

        # Tick durations for 3 cycles: 0.2s, 0.5s, 0.1s
        durations = [0.2, 0.5, 0.1]
        cycle = [0]

        def fake_tick(*args, **kwargs):
            d = durations[cycle[0] % len(durations)]
            simulated_time[0] += d
            cycle[0] += 1
            return 0

        def fake_wait(timeout=None):
            recorded_waits.append(timeout)
            if timeout is not None:
                simulated_time[0] += timeout
            if len(recorded_waits) >= 3:
                stop.set()
                return True
            return False

        with patch("cron.scheduler_provider.time.monotonic", side_effect=fake_monotonic), \
             patch("cron.scheduler.tick", side_effect=fake_tick), \
             patch("cron.jobs.record_ticker_heartbeat"), \
             patch("cron.jobs.clear_ticker_error"), \
             patch.object(prov, "recover_interrupted", return_value=0), \
             patch.object(stop, "wait", side_effect=fake_wait):
            prov.start(stop, interval=60.0)

        assert len(recorded_waits) == 3
        # First cycle took 0.2s, so wait should be 60.0 - 0.2 = 59.8s
        assert pytest.approx(recorded_waits[0], abs=1e-3) == 59.8
        # Second cycle took 0.5s, so wait should be 60.0 - 0.5 = 59.5s
        assert pytest.approx(recorded_waits[1], abs=1e-3) == 59.5
        # Third cycle took 0.1s, so wait should be 60.0 - 0.1 = 59.9s
        assert pytest.approx(recorded_waits[2], abs=1e-3) == 59.9

        # Total elapsed simulated time across all 3 cycles must be exactly 180.0s (zero drift!)
        assert pytest.approx(simulated_time[0] - 1000.0, abs=1e-3) == 180.0

    def test_single_ticker_reanchors_on_overrun(self):
        """When a tick overruns the interval, deadline re-anchors to now + wait_for."""
        prov = InProcessCronScheduler()
        stop = threading.Event()
        simulated_time = [1000.0]
        recorded_waits = []

        def fake_monotonic():
            return simulated_time[0]

        # Tick takes 75s, overrunning the 60s interval
        def fake_overrun_tick(*args, **kwargs):
            simulated_time[0] += 75.0
            return 0

        def fake_wait(timeout=None):
            recorded_waits.append(timeout)
            if timeout is not None:
                simulated_time[0] += timeout
            stop.set()
            return True

        with patch("cron.scheduler_provider.time.monotonic", side_effect=fake_monotonic), \
             patch("cron.scheduler.tick", side_effect=fake_overrun_tick), \
             patch("cron.jobs.record_ticker_heartbeat"), \
             patch("cron.jobs.clear_ticker_error"), \
             patch.object(prov, "recover_interrupted", return_value=0), \
             patch.object(stop, "wait", side_effect=fake_wait):
            prov.start(stop, interval=60.0)

        assert len(recorded_waits) == 1
        # Overran by 15s; next_tick was 1060.0, now is 1075.0 (< now).
        # Re-anchoring sets next_tick = 1075.0 + 60.0 = 1135.0, so wait is 60.0s.
        assert pytest.approx(recorded_waits[0], abs=1e-3) == 60.0

    def test_multiplex_ticker_sleeps_to_deadline(self, tmp_path):
        """Multiplex ticker must subtract total profile tick durations from sleep."""
        prov = InProcessCronScheduler()
        stop = threading.Event()
        simulated_time = [2000.0]
        recorded_waits = []

        def fake_monotonic():
            return simulated_time[0]

        # Each profile tick takes 0.3s -> total 0.6s per cycle for 2 profiles
        def fake_tick(*args, **kwargs):
            simulated_time[0] += 0.3
            return 0

        def fake_wait(timeout=None):
            recorded_waits.append(timeout)
            if timeout is not None:
                simulated_time[0] += timeout
            if len(recorded_waits) >= 2:
                stop.set()
                return True
            return False

        h1 = tmp_path / "profile1"
        h2 = tmp_path / "profile2"
        h1.mkdir()
        h2.mkdir()
        profile_homes = [("p1", h1), ("p2", h2)]

        with patch("cron.scheduler_provider.time.monotonic", side_effect=fake_monotonic), \
             patch("cron.scheduler.tick", side_effect=fake_tick), \
             patch("cron.jobs.record_ticker_heartbeat"), \
             patch("cron.jobs.clear_ticker_error"), \
             patch.object(stop, "wait", side_effect=fake_wait):
            prov._start_multiplex(stop, profile_homes=profile_homes, interval=60.0)

        assert len(recorded_waits) == 2
        # Each cycle took 2 * 0.3s = 0.6s of work, so wait should be 60.0 - 0.6 = 59.4s
        assert pytest.approx(recorded_waits[0], abs=1e-3) == 59.4
        assert pytest.approx(recorded_waits[1], abs=1e-3) == 59.4
        # Total elapsed across 2 cycles is exactly 120.0s
        assert pytest.approx(simulated_time[0] - 2000.0, abs=1e-3) == 120.0

    def test_multiplex_ticker_reanchors_on_overrun(self, tmp_path):
        """Multiplex ticker re-anchors deadline when total cycle work overruns interval."""
        prov = InProcessCronScheduler()
        stop = threading.Event()
        simulated_time = [2000.0]
        recorded_waits = []

        def fake_monotonic():
            return simulated_time[0]

        # Cycle takes 80s total
        def fake_overrun_tick(*args, **kwargs):
            simulated_time[0] += 80.0
            return 0

        def fake_wait(timeout=None):
            recorded_waits.append(timeout)
            if timeout is not None:
                simulated_time[0] += timeout
            stop.set()
            return True

        h1 = tmp_path / "profile1"
        h1.mkdir()
        profile_homes = [("p1", h1)]

        with patch("cron.scheduler_provider.time.monotonic", side_effect=fake_monotonic), \
             patch("cron.scheduler.tick", side_effect=fake_overrun_tick), \
             patch("cron.jobs.record_ticker_heartbeat"), \
             patch("cron.jobs.clear_ticker_error"), \
             patch.object(stop, "wait", side_effect=fake_wait):
            prov._start_multiplex(stop, profile_homes=profile_homes, interval=60.0)

        assert len(recorded_waits) == 1
        assert pytest.approx(recorded_waits[0], abs=1e-3) == 60.0

    def test_ticker_preserves_emfile_backoff(self):
        """EMFILE backoff doubles wait_for while still accounting for work duration."""
        prov = InProcessCronScheduler()
        stop = threading.Event()
        simulated_time = [1000.0]
        recorded_waits = []

        def fake_monotonic():
            return simulated_time[0]

        # Tick fails with EMFILE after 0.2s
        def fake_emfile_tick(*args, **kwargs):
            simulated_time[0] += 0.2
            raise OSError(errno.EMFILE, "Too many open files")

        def fake_wait(timeout=None):
            recorded_waits.append(timeout)
            if timeout is not None:
                simulated_time[0] += timeout
            if len(recorded_waits) >= 2:
                stop.set()
                return True
            return False

        with patch("cron.scheduler_provider.time.monotonic", side_effect=fake_monotonic), \
             patch("cron.scheduler.tick", side_effect=fake_emfile_tick), \
             patch("cron.jobs.record_ticker_heartbeat"), \
             patch("cron.jobs.record_ticker_error"), \
             patch("cron.scheduler._reclaim_fds_best_effort"), \
             patch.object(prov, "recover_interrupted", return_value=0), \
             patch.object(stop, "wait", side_effect=fake_wait):
            prov.start(stop, interval=60.0)

        assert len(recorded_waits) == 2
        # First failure: consecutive_failures=1, backoff is interval = 60.0s. Work took 0.2s -> 59.8s
        assert pytest.approx(recorded_waits[0], abs=1e-3) == 59.8
        # Second failure: consecutive_failures=2, backoff is 60 * 2 = 120.0s. Work took 0.2s -> 119.8s
        assert pytest.approx(recorded_waits[1], abs=1e-3) == 119.8
