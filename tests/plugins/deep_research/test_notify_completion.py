"""Completion re-entry: one event per terminal job, recoverable, test-gated.

Covers the lifecycle half of TASK.md test area 3 — durable jobs survive a lost
in-memory notification, and ``status``/``result`` recover them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

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


class TestOriginContext:
    def test_origin_without_session_is_empty_but_safe(self) -> None:
        origin = notify.origin_context(None, None)
        assert origin == {"session_id": "", "task_id": "", "session_key": ""}

    def test_origin_with_unknown_session_degrades_to_no_key(self) -> None:
        origin = notify.origin_context("no-such-session-id")
        assert origin["session_id"] == "no-such-session-id"
        # No session store row → empty key, never a crash.
        assert origin["session_key"] == ""
