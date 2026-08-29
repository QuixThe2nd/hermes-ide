"""Cooldown, suppression-counting, and restart-durability contracts."""

from __future__ import annotations

from pathlib import Path

from plugins.fallback_watch.core import load_state, load_watch_config, watch_lines
from tests.plugins.fallback_watch._helpers import (
    RecordingSend,
    SAMPLE_LINE,
    fallback_config,
)


def _config(**overrides):
    return load_watch_config(fallback_config(**overrides))


def _line(seq: int) -> str:
    return (
        f"2026-08-25 15:{seq:02d}:00,000 INFO [20260825_1532_{seq:04x}] "
        f"agent.chat_completion_helpers: Fallback activated: m{seq} → f{seq} (p)"
    )


class TestFirstAlert:
    def test_first_event_alerts_immediately(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        send = RecordingSend()
        watch_lines([SAMPLE_LINE], _config(), {}, send=send, now_fn=lambda: 1000.0)
        assert len(send.sent) == 1
        assert "stealth/ox-alpha" in send.sent[0]


class TestCooldownSuppression:
    def test_second_event_inside_cooldown_is_suppressed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        send = RecordingSend()
        clock = {"now": 1000.0}
        state = watch_lines(
            [_line(1)], _config(), {}, send=send, now_fn=lambda: clock["now"]
        )
        clock["now"] = 1010.0  # 10s later, inside the 120s window
        state = watch_lines(
            [_line(2)],
            _config(cooldown_seconds=120),
            state,
            send=send,
            now_fn=lambda: clock["now"],
        )
        assert len(send.sent) == 1
        assert state["suppressed_since_last"] == 1

    def test_event_after_cooldown_alerts_and_reports_suppressed_count(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        send = RecordingSend()
        clock = {"now": 1000.0}
        state = watch_lines(
            [_line(1)], _config(), {}, send=send, now_fn=lambda: clock["now"]
        )
        clock["now"] = 1001.0
        state = watch_lines(
            [_line(2)], _config(), state, send=send, now_fn=lambda: clock["now"]
        )
        clock["now"] = 1002.0
        state = watch_lines(
            [_line(3)], _config(), state, send=send, now_fn=lambda: clock["now"]
        )
        # past the cooldown now
        clock["now"] = 1000.0 + 121.0
        watch_lines(
            [_line(4)], _config(), state, send=send, now_fn=lambda: clock["now"]
        )
        assert len(send.sent) == 2
        assert "Note: `2` additional fallback event(s)" in send.sent[1]

    def test_suppressed_counter_resets_after_a_successful_alert(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        send = RecordingSend()
        clock = {"now": 1000.0}
        state = watch_lines(
            [_line(1)], _config(), {}, send=send, now_fn=lambda: clock["now"]
        )
        clock["now"] = 1001.0
        state = watch_lines(
            [_line(2)], _config(), state, send=send, now_fn=lambda: clock["now"]
        )
        clock["now"] = 2000.0
        state = watch_lines(
            [_line(3)], _config(), state, send=send, now_fn=lambda: clock["now"]
        )
        assert "suppressed_since_last" not in state

    def test_zero_cooldown_alerts_every_event(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        send = RecordingSend()
        watch_lines(
            [_line(1), _line(2), _line(3)],
            _config(cooldown_seconds=0),
            {},
            send=send,
            now_fn=lambda: 1000.0,
        )
        assert len(send.sent) == 3


class TestReplayGuard:
    def test_identical_line_is_ignored_once_seen(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        send = RecordingSend()
        watch_lines(
            [SAMPLE_LINE, SAMPLE_LINE], _config(), {}, send=send, now_fn=lambda: 1000.0
        )
        assert len(send.sent) == 1

    def test_replayed_line_stays_ignored_across_restart(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        send = RecordingSend()
        watch_lines([SAMPLE_LINE], _config(), {}, send=send, now_fn=lambda: 1000.0)
        # a "restarted" process reloads persisted state
        watch_lines(
            [SAMPLE_LINE],
            _config(),
            load_state(),
            send=send,
            now_fn=lambda: 5000.0,
        )
        assert len(send.sent) == 1


class TestRestartDurability:
    def test_cooldown_survives_a_restart(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        send = RecordingSend()
        watch_lines([_line(1)], _config(), {}, send=send, now_fn=lambda: 1000.0)
        # new process, fresh in-memory state, same clock window
        watch_lines(
            [_line(2)], _config(), load_state(), send=send, now_fn=lambda: 1100.0
        )
        assert len(send.sent) == 1
        assert load_state()["suppressed_since_last"] == 1


class TestSendFailure:
    def test_failed_send_keeps_suppressed_tally_and_backs_off(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        clock = {"now": 1000.0}
        state = watch_lines(
            [_line(1)],
            _config(),
            {},
            send=RecordingSend(),
            now_fn=lambda: clock["now"],
        )
        clock["now"] = 1001.0
        state = watch_lines(
            [_line(2)],
            _config(),
            state,
            send=RecordingSend(),
            now_fn=lambda: clock["now"],
        )
        assert state["suppressed_since_last"] == 1

        sleeps: list[float] = []
        clock["now"] = 5000.0
        state = watch_lines(
            [_line(3)],
            _config(),
            state,
            send=RecordingSend(fail=True),
            now_fn=lambda: clock["now"],
            sleep_fn=sleeps.append,
            on_error=lambda msg: None,
        )
        # the event was dropped (not retried), the tally survives for the
        # next successful alert, and one backoff sleep happened
        assert state["suppressed_since_last"] == 1
        assert state["last_line"] == _line(3)
        assert sleeps == [10.0]

    def test_send_failure_does_not_raise_out_of_the_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        state = watch_lines(
            [_line(1), _line(2)],
            _config(),
            {},
            send=RecordingSend(fail=True),
            now_fn=lambda: 1000.0,
            sleep_fn=lambda seconds: None,
            on_error=lambda msg: None,
        )
        assert state["last_line"] == _line(2)
        assert state.get("last_alert_at", 0) == 0


class TestNonFallbackLines:
    def test_ordinary_lines_are_ignored_entirely(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        send = RecordingSend()
        noise = [
            "2026-08-25 15:00:00,000 INFO [20260825_153208_64d08c2b] agent: hello",
            "2026-08-25 15:00:01,000 INFO agent: Fallback deactivated: a → b (p)",
        ]
        state = watch_lines(noise, _config(), {}, send=send, now_fn=lambda: 1000.0)
        assert send.sent == []
        assert "last_line" not in state
