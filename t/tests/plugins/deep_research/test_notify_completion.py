"""Completion re-entry: one event per terminal job, recoverable, test-gated.

Covers the lifecycle half of TASK.md test area 3 — durable jobs survive a lost
in-memory notification, and ``status``/``result`` recover them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import hermes_constants
import pytest

from plugins.deep_research import jobs, notify


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _make_job(home: Path, *, origin=None, questions=None) -> tuple[str, Path]:
    created = jobs.create_job(
        brief="brief",
        research_questions=questions,
        timeout_minutes=10,
        max_parallel=1,
        worker_profile="researcher",
        origin=origin,
        hermes_home=home,
    )
    return created["job_id"], created["dir"]


class Queue:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def put(self, event: dict) -> None:
        self.events.append(event)


class TestCompletionEvent:
    def test_event_shape_routes_to_the_origin_session(self, home: Path) -> None:
        job_id, directory = _make_job(
            home, origin={"session_id": "sess-7", "session_key": "discord:dm:42"}
        )
        jobs.mark_running(directory, {})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        event = notify.completion_event(directory, jobs.read_status(directory), jobs.read_request(directory))
        assert event is not None
        assert event["type"] == "async_delegation"
        assert event["session_key"] == "discord:dm:42"
        assert event["parent_session_id"] == "sess-7"
        assert event["delegation_id"] == f"research-{job_id}"
        assert event["status"] == "completed" and event["error"] is None
        assert event["research_job_id"] == job_id

    def test_failed_job_maps_to_error_status(self, home: Path) -> None:
        _job_id, directory = _make_job(home, origin={"session_id": "s", "session_key": ""})
        jobs.finish_job(directory, jobs.STATE_FAILED, error="lane failure: boom")
        event = notify.completion_event(directory, jobs.read_status(directory), jobs.read_request(directory))
        assert event["status"] == "error"
        assert "lane failure" in event["error"]
        assert "failed" in event["summary"]

    def test_unroutable_job_has_no_event(self, home: Path) -> None:
        _job_id, directory = _make_job(home, origin={})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        assert notify.completion_event(directory, jobs.read_status(directory), jobs.read_request(directory)) is None

    def test_active_job_has_no_event(self, home: Path) -> None:
        _job_id, directory = _make_job(home, origin={"session_id": "s"})
        assert notify.completion_event(directory, jobs.read_status(directory), jobs.read_request(directory)) is None


class TestNotifyPending:
    def test_notifies_each_terminal_job_once(self, home: Path) -> None:
        queue = Queue()
        for index in range(3):
            job_id, directory = _make_job(home, origin={"session_id": f"s{index}"})
            jobs.finish_job(directory, jobs.STATE_COMPLETED)
        assert notify.notify_pending(home, queue_put=queue.put) is not None
        assert len(queue.events) == 3
        # A second sweep delivers nothing: notified flipped exactly once.
        assert notify.notify_pending(home, queue_put=queue.put) == []
        assert len(queue.events) == 3

    def test_active_jobs_are_not_notified(self, home: Path) -> None:
        queue = Queue()
        _make_job(home, origin={"session_id": "s"})
        assert notify.notify_pending(home, queue_put=queue.put) == []
        assert queue.events == []

    def test_unroutable_jobs_stay_unnotified_and_recoverable(self, home: Path) -> None:
        # No origin: no event — and crucially no notified flip, so the job
        # remains discoverable via status/result forever.
        queue = Queue()
        job_id, directory = _make_job(home, origin={})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        assert notify.notify_pending(home, queue_put=queue.put) == []
        assert jobs.read_status(directory).get("notified") is False
        assert (directory / "status.json").exists()  # durable, still readable

    def test_durable_state_survives_a_lost_notification(self, home: Path) -> None:
        # Gateway died before delivering: the next process still reads results.
        job_id, directory = _make_job(home, origin={"session_id": "s"})
        jobs.publish_report(directory, "# Report\n\n[s](https://example.org/a)\n")
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        status = jobs.read_status(directory)
        assert status["state"] == "completed" and status["notified"] is False
        assert "example.org" in (directory / "report.md").read_text(encoding="utf-8")


class TestDeliveryRetry:
    """A rejected queue delivery must release the claim, not lose the event."""

    def test_rejected_delivery_rolls_back_the_claim(self, home: Path) -> None:
        job_id, directory = _make_job(home, origin={"session_id": "s"})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)

        class RejectingQueue:
            def put(self, event: dict) -> None:
                raise RuntimeError("completion queue is closed")

        assert notify.notify_pending(home, queue_put=RejectingQueue().put) == []
        # The claim was rolled back, so the completion is not lost forever.
        assert jobs.read_status(directory)["notified"] is False

    def test_rolled_back_claim_is_retried_and_delivered_once(self, home: Path) -> None:
        job_id, directory = _make_job(home, origin={"session_id": "s"})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)

        class RejectingQueue:
            def put(self, event: dict) -> None:
                raise RuntimeError("completion queue is closed")

        notify.notify_pending(home, queue_put=RejectingQueue().put)
        queue = Queue()
        assert notify.notify_pending(home, queue_put=queue.put) == [job_id]
        assert [event["research_job_id"] for event in queue.events] == [job_id]
        # Delivered now, and never re-delivered by a later sweep.
        assert notify.notify_pending(home, queue_put=queue.put) == []
        assert len(queue.events) == 1

    def test_unmark_notified_is_refused_on_an_active_job(self, home: Path) -> None:
        # Only a terminal job's claim can be released — an active job never
        # had one, and unmarking must not touch its status.
        _job_id, directory = _make_job(home, origin={"session_id": "s"})
        assert jobs.unmark_notified(directory) is False
        assert jobs.read_status(directory)["notified"] is False


class TestNotifyUnderLockRefusal:
    """A locked job is skipped unclaimed; the sweep still delivers the rest."""

    def test_locked_job_is_never_claimed_and_the_sweep_continues(
        self, home: Path, monkeypatch
    ) -> None:
        fcntl = pytest.importorskip("fcntl")
        locked_id, locked_dir = _make_job(home, origin={"session_id": "s-locked"})
        jobs.finish_job(locked_dir, jobs.STATE_COMPLETED)
        free_id, free_dir = _make_job(home, origin={"session_id": "s-free"})
        jobs.finish_job(free_dir, jobs.STATE_COMPLETED)
        monkeypatch.setattr(jobs, "_JOB_LOCK_TIMEOUT_SECONDS", 0.2)
        holder = open(locked_dir / ".status.lock", "a+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        queue = Queue()
        try:
            # The free job is delivered; the locked one is neither claimed
            # nor delivered — no success is invented for it.
            assert notify.notify_pending(home, queue_put=queue.put) == [free_id]
            assert jobs.read_status(locked_dir)["notified"] is False
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        # Lock released: a later sweep delivers the deferred job exactly once.
        assert notify.notify_pending(home, queue_put=queue.put) == [locked_id]
        assert [event["research_job_id"] for event in queue.events] == [free_id, locked_id]


class TestWatcherLifecycle:
    def test_watcher_refuses_to_start_under_test_isolation(self, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
        assert notify.start_gateway_watcher(interval_seconds=0.05, queue_put=Queue().put) is False
        assert notify.stop_gateway_watcher() is None  # idempotent no-op

    def test_watcher_sweeps_until_stopped(self, home: Path, monkeypatch) -> None:
        monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
        queue = Queue()
        job_id, directory = _make_job(home, origin={"session_id": "s"})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        watcher = notify.CompletionWatcher(interval_seconds=0.05, hermes_home=home, queue_put=queue.put)
        assert watcher.start() is True
        assert watcher.start() is False  # already running
        deadline = time.monotonic() + 5
        while not queue.events and time.monotonic() < deadline:
            time.sleep(0.02)
        watcher.stop()
        assert [event["research_job_id"] for event in queue.events] == [job_id]
        assert watcher._thread is None or not watcher._thread.is_alive()

    def test_watcher_recovers_stale_jobs_at_startup(self, home: Path, monkeypatch) -> None:
        monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
        queue = Queue()
        job_id, directory = _make_job(home, origin={"session_id": "s"})
        jobs.mark_running(directory, {"runner_mode": "fallback", "runner_pid": 999_999_999})
        status = jobs.read_status(directory)
        status["updated_at"] = time.time() - 10_000
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")

        watcher = notify.CompletionWatcher(interval_seconds=0.05, hermes_home=home, queue_put=queue.put)
        watcher.start()
        deadline = time.monotonic() + 5
        while not queue.events and time.monotonic() < deadline:
            time.sleep(0.02)
        watcher.stop()
        # The interrupted job was failed AND notified about it.
        assert jobs.read_status(directory)["state"] == "failed"
        assert "interrupted" in jobs.read_status(directory)["error"]
        assert queue.events[0]["research_job_id"] == job_id


class TestNotifyStarvation:
    """The per-tick bound must never strand an older completion."""

    def test_the_eleventh_completion_is_reached_by_the_next_sweep(self, home: Path) -> None:
        queue = Queue()
        job_ids = []
        for index in range(11):
            job_id, directory = _make_job(home, origin={"session_id": f"s{index}"})
            jobs.finish_job(directory, jobs.STATE_COMPLETED)
            job_ids.append(job_id)
        first = notify.notify_pending(home, limit=10, queue_put=queue.put)
        assert len(first) == 10
        # Not lost behind the bound: the second sweep reaches job 11 even
        # though ten terminal jobs already exist above it.
        second = notify.notify_pending(home, limit=10, queue_put=queue.put)
        assert len(second) == 1
        assert set(first) | set(second) == set(job_ids)
        assert len(queue.events) == 11
        assert notify.notify_pending(home, limit=10, queue_put=queue.put) == []

    def test_unroutable_jobs_do_not_consume_the_bound(self, home: Path) -> None:
        # Jobs with no origin generate no event; they must not eat delivery
        # slots that terminal-and-routable jobs need.
        for _ in range(12):
            _job_id, directory = _make_job(home, origin={})
            jobs.finish_job(directory, jobs.STATE_COMPLETED)
        job_id, directory = _make_job(home, origin={"session_id": "s"})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        queue = Queue()
        assert notify.notify_pending(home, limit=5, queue_put=queue.put) == [job_id]


class TestWatcherHomes:
    """The watcher sweeps the process home plus its live named profiles."""

    def test_process_home_plus_live_profile_children(self, home: Path) -> None:
        alpha = home / "profiles" / "alpha"
        alpha.mkdir(parents=True)
        dead = home / "profiles" / "beta"
        dead.mkdir(parents=True)
        (home / "profiles" / ".deleted" / "beta").mkdir(parents=True)  # tombstone
        (home / "profiles" / "not a profile!").mkdir()
        (home / "profiles" / ".hidden").mkdir()
        (home / "profiles" / "default").mkdir()  # the default IS the process home
        (home / "profiles" / "stray.txt").write_text("x", encoding="utf-8")
        assert notify.watcher_hermes_homes() == [home, alpha]

    def test_tombstoned_and_missing_profiles_are_skipped(self, home: Path) -> None:
        dead = home / "profiles" / "gone"
        dead.mkdir(parents=True)
        (home / "profiles" / ".deleted" / "gone").mkdir(parents=True)
        assert notify.watcher_hermes_homes() == [home]

    def test_enumeration_stays_under_the_process_home(self, home: Path) -> None:
        # HERMES_HOME points at a tmpdir; the enumerator must never reach out
        # to the operator's live ~/.hermes to enumerate profiles.
        for path in notify.watcher_hermes_homes():
            assert str(path).startswith(str(home))

    def test_missing_profiles_root_is_just_the_process_home(self, home: Path) -> None:
        assert notify.watcher_hermes_homes() == [home]
        assert notify.watcher_hermes_homes(home) == [home]


class TestWatcherSymlinkConfinement:
    """A planted symlink must never aim the sweep at a foreign home.

    ``Path.is_dir()`` follows symlinks, so a ``profiles/alpha`` pointed at
    another operator's home would otherwise have the watcher read and deliver
    that home's jobs (fresh-review blocker 1).
    """

    def _completed_job(self, home_path: Path) -> tuple[str, Path]:
        job_id, directory = _make_job(home_path, origin={"session_id": "foreign-session"})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        return job_id, directory

    def _sweep(self, process_home: Path) -> list[dict]:
        """The watcher's exact sweep: notify every home it would enumerate."""
        queue = Queue()
        for sweep_home in notify.watcher_hermes_homes(process_home):
            notify.notify_pending(sweep_home, queue_put=queue.put)
        return queue.events

    def test_symlinked_profile_to_a_foreign_home_is_excluded_and_never_notified(
        self, home: Path, tmp_path: Path
    ) -> None:
        foreign = tmp_path / "foreign-operator-home"
        _job_id, directory = self._completed_job(foreign)
        (home / "profiles").mkdir(parents=True)
        (home / "profiles" / "alpha").symlink_to(foreign)  # valid name, real target
        assert notify.watcher_hermes_homes(home) == [home]
        assert self._sweep(home) == []
        # The foreign job was never claimed or touched.
        assert jobs.read_status(directory)["notified"] is False

    def test_symlinked_profiles_root_refuses_the_whole_enumeration(
        self, home: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        alpha = elsewhere / "profiles" / "alpha"
        _job_id, directory = self._completed_job(alpha)
        home.mkdir()  # only the link's parent must exist; the target is real too
        (home / "profiles").symlink_to(elsewhere / "profiles")
        assert notify.watcher_hermes_homes(home) == [home]
        assert self._sweep(home) == []
        assert jobs.read_status(directory)["notified"] is False

    def test_symlink_with_a_target_inside_the_process_home_is_still_rejected(
        self, home: Path
    ) -> None:
        real = home / "profiles" / "gamma"
        real.mkdir(parents=True)
        (home / "profiles" / "alpha").symlink_to(real)  # target IS under profiles/
        assert notify.watcher_hermes_homes(home) == [home, real]

    def test_broken_symlink_is_skipped_not_followed(self, home: Path) -> None:
        (home / "profiles").mkdir(parents=True)
        (home / "profiles" / "alpha").symlink_to(home / "profiles" / "gone")
        assert notify.watcher_hermes_homes(home) == [home]

    def test_real_profiles_are_still_swept_alongside_a_planted_symlink(
        self, home: Path, tmp_path: Path
    ) -> None:
        (tmp_path / "foreign").mkdir()
        alpha = home / "profiles" / "alpha"
        alpha.mkdir(parents=True)
        (home / "profiles" / "beta").symlink_to(tmp_path / "foreign")
        assert notify.watcher_hermes_homes(home) == [home, alpha]


class TestCallbackSwapConfinement:
    """A profile-path swap performed inside the tombstone lookup must never
    reach a use.

    ``named_profile_is_deleted`` runs on the watcher thread and can re-enter
    the filesystem, so it stands in for any watcher callback that synchronously
    renames a real profile and plants a symlink to a foreign home. Two shapes:
    the *same-entry* swap (the entry being looked up is replaced under the
    watcher) and the *cross-entry* swap (a later entry's lookup replaces an
    entry the same enumeration already retained).
    """

    def _completed_foreign_job(self, tmp_path: Path) -> tuple[Path, Path]:
        foreign = tmp_path / "foreign-operator-home"
        _job_id, directory = _make_job(foreign, origin={"session_id": "foreign-session"})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        return foreign, directory

    def _stale_foreign_job(self, tmp_path: Path) -> tuple[Path, Path]:
        foreign = tmp_path / "foreign-operator-home"
        _job_id, directory = _make_job(foreign, origin={"session_id": "foreign-session"})
        jobs.mark_running(directory, {"runner_mode": "fallback", "runner_pid": 999_999_999})
        status = jobs.read_status(directory)
        status["updated_at"] = time.time() - 10_000
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
        return foreign, directory

    def _swap_on_lookup(
        self, monkeypatch, trigger: Path, victim: Path, foreign: Path
    ) -> dict:
        """Tombstone lookup that swaps ``victim`` for a symlink at ``trigger``."""
        state = {"swapped": False}

        def lookup(entry: Path) -> bool:
            if entry == trigger and not state["swapped"]:
                victim.rename(victim.with_name(f"{victim.name}-original"))
                victim.symlink_to(foreign, target_is_directory=True)
                state["swapped"] = True
            return False

        monkeypatch.setattr(hermes_constants, "named_profile_is_deleted", lookup)
        return state

    @staticmethod
    def _unswap(victim: Path) -> None:
        """Undo a swap: drop the symlink, put the renamed original back."""
        victim.unlink()
        victim.with_name(f"{victim.name}-original").rename(victim)

    def test_same_entry_swap_is_excluded_from_the_enumeration(
        self, home: Path, tmp_path: Path, monkeypatch
    ) -> None:
        # Exact reviewer probe shape: the tombstone lookup renames the real
        # alpha and plants a symlink to a foreign home, then returns False.
        foreign, directory = self._completed_foreign_job(tmp_path)
        alpha = home / "profiles" / "alpha"
        alpha.mkdir(parents=True)
        state = self._swap_on_lookup(monkeypatch, trigger=alpha, victim=alpha, foreign=foreign)
        assert notify.watcher_hermes_homes(home) == [home]
        # The swap really fired inside the lookup, and was still dropped.
        assert state["swapped"] is True
        assert alpha.is_symlink()
        assert jobs.read_status(directory)["notified"] is False

    def test_same_entry_swap_delivers_nothing_through_the_watchers_sweep(
        self, home: Path, tmp_path: Path, monkeypatch
    ) -> None:
        # The watcher's exact sweep, as the reviewer probe drives it: notify
        # every home the enumeration returns. Pre-fix this delivered the
        # foreign job through the retained symlink.
        foreign, directory = self._completed_foreign_job(tmp_path)
        alpha = home / "profiles" / "alpha"
        alpha.mkdir(parents=True)
        state = self._swap_on_lookup(monkeypatch, trigger=alpha, victim=alpha, foreign=foreign)
        queue = Queue()
        for sweep_home in notify.watcher_hermes_homes(home):
            notify.notify_pending(sweep_home, queue_put=queue.put)
        assert state["swapped"] is True
        assert queue.events == []
        assert jobs.read_status(directory)["notified"] is False

    def test_cross_entry_swap_of_a_retained_profile_is_notified_nowhere(
        self, home: Path, tmp_path: Path, monkeypatch
    ) -> None:
        # alpha is retained before beta's tombstone lookup swaps it, so only
        # the use-boundary revalidation can stop the foreign delivery.
        foreign, directory = self._completed_foreign_job(tmp_path)
        alpha = home / "profiles" / "alpha"
        beta = home / "profiles" / "beta"
        alpha.mkdir(parents=True)
        beta.mkdir()
        queue = Queue()
        watcher = notify.CompletionWatcher(
            interval_seconds=0.05, hermes_home=home, queue_put=queue.put
        )
        # Pass 1: prove the enumeration itself retains alpha across beta's
        # swap — the use boundary is the only remaining defense.
        state = self._swap_on_lookup(monkeypatch, trigger=beta, victim=alpha, foreign=foreign)
        assert [target.path for target in watcher._watch_targets()] == [home, alpha, beta]
        assert state["swapped"] is True
        self._unswap(alpha)
        # Pass 2: same swap inside _notify_once's own enumeration.
        state = self._swap_on_lookup(monkeypatch, trigger=beta, victim=alpha, foreign=foreign)
        watcher._notify_once()
        assert state["swapped"] is True
        assert queue.events == []
        assert jobs.read_status(directory)["notified"] is False

    def test_cross_entry_swap_of_a_retained_profile_recovers_nothing_foreign(
        self, home: Path, tmp_path: Path, monkeypatch
    ) -> None:
        # Same cross-entry shape against startup recovery: the foreign home's
        # stale running job must not be failed through the swapped profile.
        foreign, directory = self._stale_foreign_job(tmp_path)
        alpha = home / "profiles" / "alpha"
        beta = home / "profiles" / "beta"
        alpha.mkdir(parents=True)
        beta.mkdir()
        watcher = notify.CompletionWatcher(
            interval_seconds=0.05, hermes_home=home, queue_put=Queue().put
        )
        state = self._swap_on_lookup(monkeypatch, trigger=beta, victim=alpha, foreign=foreign)
        assert [target.path for target in watcher._watch_targets()] == [home, alpha, beta]
        self._unswap(alpha)
        state = self._swap_on_lookup(monkeypatch, trigger=beta, victim=alpha, foreign=foreign)
        watcher._recover_once()
        assert state["swapped"] is True
        status = jobs.read_status(directory)
        assert status["state"] == "running"  # untouched: no interrupted-failure
        assert status["notified"] is False

    def test_benign_tombstone_lookup_still_sweeps_the_real_profile(
        self, home: Path, monkeypatch
    ) -> None:
        # Control: an extra callback in the lookup must not make the
        # revalidation overzealous — a real profile still recovers/notifies.
        monkeypatch.setattr(hermes_constants, "named_profile_is_deleted", lambda entry: False)
        alpha = home / "profiles" / "alpha"
        stale_id, stale_dir = _make_job(alpha, origin={"session_id": "s-stale"})
        jobs.mark_running(stale_dir, {"runner_mode": "fallback", "runner_pid": 999_999_999})
        stale = jobs.read_status(stale_dir)
        stale["updated_at"] = time.time() - 10_000
        (stale_dir / "status.json").write_text(json.dumps(stale), encoding="utf-8")
        done_id, done_dir = _make_job(alpha, origin={"session_id": "s-done"})
        jobs.finish_job(done_dir, jobs.STATE_COMPLETED)
        queue = Queue()
        watcher = notify.CompletionWatcher(
            interval_seconds=0.05, hermes_home=home, queue_put=queue.put
        )
        assert notify.watcher_hermes_homes(home) == [home, alpha]
        watcher._recover_once()
        watcher._notify_once()
        assert jobs.read_status(stale_dir)["state"] == "failed"
        assert {event["research_job_id"] for event in queue.events} == {stale_id, done_id}
        assert jobs.read_status(done_dir)["notified"] is True

    def test_tombstoned_profile_is_still_skipped_under_a_delegating_callback(
        self, home: Path, monkeypatch
    ) -> None:
        # Control: the revalidation ordering must not bypass a real tombstone.
        real_lookup = hermes_constants.named_profile_is_deleted
        calls: list[Path] = []

        def delegating_lookup(entry: Path) -> bool:
            calls.append(entry)
            return real_lookup(entry)

        monkeypatch.setattr(hermes_constants, "named_profile_is_deleted", delegating_lookup)
        alpha = home / "profiles" / "alpha"
        alpha.mkdir(parents=True)
        dead = home / "profiles" / "gone"
        dead.mkdir(parents=True)
        (home / "profiles" / ".deleted" / "gone").mkdir(parents=True)
        assert notify.watcher_hermes_homes(home) == [home, alpha]
        assert dead in calls  # the tombstone was consulted, not bypassed


class TestProfileHomeSweep:
    def test_watcher_notifies_a_job_in_a_named_profile_home(self, home: Path, monkeypatch) -> None:
        monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
        queue = Queue()
        alpha = home / "profiles" / "alpha"
        job_id, directory = _make_job(alpha, origin={"session_id": "s"})
        jobs.finish_job(directory, jobs.STATE_COMPLETED)
        watcher = notify.CompletionWatcher(interval_seconds=0.05, hermes_home=home, queue_put=queue.put)
        assert [target.path for target in watcher._watch_targets()] == [home, alpha]
        watcher.start()
        deadline = time.monotonic() + 5
        while not queue.events and time.monotonic() < deadline:
            time.sleep(0.02)
        watcher.stop()
        assert [event["research_job_id"] for event in queue.events] == [job_id]

    def test_watcher_recovers_stale_jobs_in_a_named_profile_home(
        self, home: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
        queue = Queue()
        alpha = home / "profiles" / "beta"
        job_id, directory = _make_job(alpha, origin={"session_id": "s"})
        jobs.mark_running(directory, {"runner_mode": "fallback", "runner_pid": 999_999_999})
        status = jobs.read_status(directory)
        status["updated_at"] = time.time() - 10_000
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")

        watcher = notify.CompletionWatcher(interval_seconds=0.05, hermes_home=home, queue_put=queue.put)
        watcher.start()
        deadline = time.monotonic() + 5
        while not queue.events and time.monotonic() < deadline:
            time.sleep(0.02)
        watcher.stop()
        # The profile-home job was failed AND notified about it.
        assert jobs.read_status(directory)["state"] == "failed"
        assert "interrupted" in jobs.read_status(directory)["error"]
        assert queue.events[0]["research_job_id"] == job_id


class TestOriginContext:
    def test_origin_without_session_is_empty_but_safe(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        origin = notify.origin_context(None, None)
        assert origin == {
            "session_id": "",
            "task_id": "",
            "session_key": "",
            "hermes_home": str(tmp_path),
        }

    def test_origin_with_unknown_session_degrades_to_no_key(self, home: Path) -> None:
        origin = notify.origin_context("no-such-session-id")
        assert origin["session_id"] == "no-such-session-id"
        # No session store row → empty key, never a crash.
        assert origin["session_key"] == ""

    def test_session_key_resolves_from_the_active_profile_state_db(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Multiplexed shape: the process home is the root, but the session
        # row lives in the ACTIVE profile home — the same home `start` uses.
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_state import SessionDB

        process_home = tmp_path / "process-home"
        profile_home = process_home / "profiles" / "alpha"
        profile_home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(process_home))
        SessionDB(db_path=profile_home / "state.db").create_session(
            "sess-42", "gateway:discord", session_key="discord:dm:42"
        )
        assert (profile_home / "state.db").exists()
        assert not (process_home / "state.db").exists()  # the row is profile-local

        token = set_hermes_home_override(str(profile_home))
        try:
            origin = notify.origin_context("sess-42")
        finally:
            reset_hermes_home_override(token)
        assert origin["session_key"] == "discord:dm:42"
        assert origin["hermes_home"] == str(profile_home)

    def test_origin_data_is_frozen_for_post_restart_routing(self, home: Path) -> None:
        # request.json carries the resolved key and the start home, so a
        # restarted gateway routes the completion without re-resolving.
        origin = notify.origin_context("sess-9", "task-9")
        _job_id, directory = _make_job(home, origin=origin)
        frozen = jobs.read_request(directory)["origin"]
        assert frozen["session_id"] == "sess-9"
        assert frozen["task_id"] == "task-9"
        assert frozen["hermes_home"] == str(home)
