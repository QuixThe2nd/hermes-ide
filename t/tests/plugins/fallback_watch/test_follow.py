"""Tail-follow contracts: EOF start, rotation, truncation, stop event."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from plugins.fallback_watch.core import follow_from_eof

POLL_SECONDS = 0.01
WAIT_SECONDS = 5.0


class TailCollector:
    """Consume a follow_from_eof generator on a background thread.

    ``ready`` fires on the generator's first poll sleep, so the test knows
    the tail is live (opened, seeked to EOF) before it appends anything —
    a bare generator is lazy and would otherwise open the file only at the
    first ``next()``, after the test already wrote its lines.
    """

    def __init__(self, path: Path, **kwargs) -> None:
        self.lines: list[str] = []
        self.ready = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._generator = follow_from_eof(
            path,
            poll_seconds=kwargs.pop("poll_seconds", POLL_SECONDS),
            stop_event=self._stop,
            sleep_fn=self._sleep,
        )
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _sleep(self, seconds: float) -> None:
        self.ready.set()
        self._wake.wait(seconds)

    def wake(self) -> None:
        """Cut a parked poll short so the next filesystem check happens now."""
        self._wake.set()

    def _drain(self) -> None:
        for line in self._generator:
            self.lines.append(line)

    def wait_ready(self) -> None:
        assert self.ready.wait(WAIT_SECONDS), "tail never started polling"

    def wait_for(self, count: int) -> list[str]:
        deadline = time.monotonic() + WAIT_SECONDS
        while len(self.lines) < count:
            if time.monotonic() > deadline:
                pytest.fail(f"expected {count} lines, got {self.lines!r}")
            time.sleep(0.01)
        return list(self.lines)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(WAIT_SECONDS)
        assert not self._thread.is_alive(), "tail thread did not stop"


@pytest.fixture()
def log_file(tmp_path: Path) -> Path:
    path = tmp_path / "logs" / "agent.log"
    path.parent.mkdir(parents=True)
    path.write_text("2026-08-25 15:00:00,000 INFO old history\n", encoding="utf-8")
    return path


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


class TestStartsAtEof:
    def test_history_before_enable_is_never_replayed(self, log_file: Path):
        collector = TailCollector(log_file)
        collector.wait_ready()
        _append(log_file, "2026-08-25 15:00:01,000 INFO appended after start\n")
        assert collector.wait_for(1) == [
            "2026-08-25 15:00:01,000 INFO appended after start"
        ]
        collector.stop()

    def test_missing_log_is_created_and_followed(self, tmp_path: Path):
        path = tmp_path / "logs" / "agent.log"
        collector = TailCollector(path)
        collector.wait_ready()
        assert path.exists()
        _append(path, "fresh line\n")
        assert collector.wait_for(1) == ["fresh line"]
        collector.stop()


class TestRotation:
    def test_line_written_after_rotation_is_caught(self, log_file: Path):
        collector = TailCollector(log_file)
        collector.wait_ready()
        _append(log_file, "before rotation\n")
        assert collector.wait_for(1) == ["before rotation"]

        rotated = log_file.with_suffix(".1")
        log_file.replace(rotated)
        _append(log_file, "after rotation\n")
        assert collector.wait_for(2) == ["before rotation", "after rotation"]
        collector.stop()

    def test_lines_written_into_new_file_before_detection_are_kept(
        self, log_file: Path
    ):
        # rotate and fill the replacement file while the watcher is between
        # polls — it must read the new file from the start, not skip to its
        # EOF and silently drop everything written before it noticed
        collector = TailCollector(log_file, poll_seconds=3600.0)
        collector.wait_ready()
        # long poll: the watcher is parked in its sleep, blind to the fs
        rotated = log_file.with_suffix(".1")
        log_file.replace(rotated)
        log_file.write_text("first in new file\nsecond in new file\n", encoding="utf-8")
        collector.wake()
        deadline = time.monotonic() + WAIT_SECONDS
        while len(collector.lines) < 2:
            if time.monotonic() > deadline:
                pytest.fail(f"expected new-file lines, got {collector.lines!r}")
            time.sleep(0.01)
        assert collector.lines == ["first in new file", "second in new file"]
        collector.stop()

    def test_in_place_truncation_is_handled(self, log_file: Path):
        collector = TailCollector(log_file)
        collector.wait_ready()
        _append(log_file, "pre-truncate tail that is long enough\n")
        assert collector.wait_for(1) == ["pre-truncate tail that is long enough"]
        # same inode, file now shorter than our read position
        log_file.write_text("truncated\n", encoding="utf-8")
        _append(log_file, "post-truncate\n")
        assert collector.wait_for(3) == [
            "pre-truncate tail that is long enough",
            "truncated",
            "post-truncate",
        ]
        collector.stop()


class TestStopEvent:
    def test_stop_event_ends_the_generator_cleanly(self, log_file: Path):
        stop_event = threading.Event()
        lines = follow_from_eof(
            log_file, poll_seconds=POLL_SECONDS, stop_event=stop_event
        )
        stop_event.set()
        with pytest.raises(StopIteration):
            next(lines)

    def test_stop_event_breaks_a_pending_poll(self, log_file: Path):
        stop_event = threading.Event()

        def stop_after_one_sleep(seconds: float) -> None:
            time.sleep(seconds)
            stop_event.set()

        lines = follow_from_eof(
            log_file,
            poll_seconds=POLL_SECONDS,
            stop_event=stop_event,
            sleep_fn=stop_after_one_sleep,
        )
        with pytest.raises(StopIteration):
            next(lines)
