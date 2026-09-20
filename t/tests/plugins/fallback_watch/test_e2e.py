"""End-to-end contracts: real log file, real state file, real service process.

The only mocked boundary is the Discord REST call (and only in the
in-process test) — config loading, tailing, parsing, cooldown, and state
persistence all run for real against a temp HERMES_HOME.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Tuple

import pytest

from plugins.fallback_watch import core
from plugins.fallback_watch.core import (
    follow_from_eof,
    load_config,
    load_state,
    watch_lines,
)
from tests.plugins.fallback_watch._helpers import (
    CHAT_ID,
    SAMPLE_LINE,
    fallback_config,
    write_home,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
POLL_SECONDS = 0.01


def _wait_until(predicate, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(0.01)


def _later_line(seq: int) -> str:
    return (
        f"2026-08-25 16:{seq:02d}:00,000 INFO [20260825_1600_{seq:04x}] "
        f"agent.chat_completion_helpers: Fallback activated: primary-a → backup-b (zai)"
    )


def _alert_for(line: str) -> str:
    event = core.parse_fallback_line(line)
    assert event is not None
    return core.format_alert(event)


class WatchThread:
    """Drive the real follow + watch pipeline on a background thread."""

    def __init__(self, log: Path, config, send: Callable[[str], None]) -> None:
        self.started = threading.Event()
        self.stop_event = threading.Event()
        self.done = threading.Event()
        self._send = send
        self._config = config
        self._log = log
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _sleep(self, seconds: float) -> None:
        self.started.set()
        self.stop_event.wait(seconds)

    def _run(self) -> None:
        watch_lines(
            follow_from_eof(
                self._log,
                poll_seconds=POLL_SECONDS,
                stop_event=self.stop_event,
                sleep_fn=self._sleep,
            ),
            self._config,
            load_state(),
            send=self._send,
            sleep_fn=self.stop_event.wait,
        )
        self.done.set()

    def wait_started(self) -> None:
        _wait_until(self.started.is_set, what="tail to open and start polling")

    def stop(self) -> None:
        self.stop_event.set()
        assert self.done.wait(5.0), "watch loop did not finish after stop"


class TestPipelineEndToEnd:
    def test_alert_flows_from_log_line_to_discord_send(self, tmp_path, monkeypatch):
        home = write_home(tmp_path, config=fallback_config())
        monkeypatch.setenv("HERMES_HOME", str(home))
        logs = home / "logs"
        logs.mkdir()
        log = logs / "agent.log"
        # history includes a fallback line that must NOT replay on enable
        log.write_text(SAMPLE_LINE + "\n", encoding="utf-8")

        sent: List[Tuple[str, str]] = []
        monkeypatch.setattr(
            core,
            "send_discord_alert",
            lambda content, chat_id, **kwargs: sent.append((content, chat_id)),
        )
        config = load_config(home / "config.yaml")
        watcher = WatchThread(
            log,
            config,
            send=lambda message: core.send_discord_alert(message, config.chat_id),
        )
        watcher.wait_started()

        with log.open("a", encoding="utf-8") as handle:
            handle.write(_later_line(1) + "\n")
        _wait_until(
            lambda: load_state().get("last_line") == _later_line(1),
            what="state to record the fresh fallback line",
        )
        watcher.stop()

        assert sent == [(_alert_for(_later_line(1)), CHAT_ID)]
        state = load_state()
        assert state["last_line"] == _later_line(1)
        assert state["last_alert_at"] > 0

    def test_history_fallback_line_never_alerts_on_enable(self, tmp_path, monkeypatch):
        home = write_home(tmp_path, config=fallback_config())
        monkeypatch.setenv("HERMES_HOME", str(home))
        logs = home / "logs"
        logs.mkdir()
        log = logs / "agent.log"
        log.write_text(SAMPLE_LINE + "\n", encoding="utf-8")

        sent: List[str] = []
        config = load_config(home / "config.yaml")
        watcher = WatchThread(log, config, send=sent.append)
        watcher.wait_started()
        # a few real poll cycles over the pre-existing content
        time.sleep(0.2)
        watcher.stop()

        assert sent == []
        assert load_state() == {}


class TestServiceProcessLifecycle:
    @pytest.mark.live_system_guard_bypass
    def test_sigterm_shuts_the_service_down_cleanly(self, tmp_path):
        home = write_home(tmp_path, config=fallback_config())
        env = dict(os.environ)
        env["HERMES_HOME"] = str(home)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "plugins.fallback_watch.run",
                "--config",
                str(home / "config.yaml"),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            log_file = home / "logs" / "agent.log"
            _wait_until(log_file.exists, what="service to start tailing agent.log")
            assert process.poll() is None, "service exited before SIGTERM"
        finally:
            process.terminate()
        _stdout, stderr = process.communicate(timeout=10.0)
        assert process.returncode == 0, (
            f"service did not exit cleanly: rc={process.returncode} stderr={stderr!r}"
        )
        assert not (home / "state" / "fallback_watch.json").exists(), (
            "idle service must not write state"
        )
