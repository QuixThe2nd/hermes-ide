"""State-file persistence contracts for fallback_watch."""

from __future__ import annotations

import json
from pathlib import Path

from plugins.fallback_watch.core import load_state, save_state, state_path


class TestStateRoundTrip:
    def test_saved_state_loads_back_identically(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        state = {
            "last_alert_at": 1756135935.5,
            "last_line": "2026-08-25 15:32:15,579 INFO Fallback activated: a → b (p)",
            "suppressed_since_last": 2,
        }
        save_state(state)
        assert load_state() == state

    def test_state_lives_under_state_dir(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state({"last_alert_at": 1})
        assert state_path() == tmp_path / "state" / "fallback_watch.json"
        assert state_path().exists()

    def test_save_creates_state_dir_and_leaves_no_temp_files(
        self, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state({"last_alert_at": 1})
        leftovers = [p.name for p in (tmp_path / "state").iterdir()]
        assert leftovers == ["fallback_watch.json"]

    def test_load_missing_file_returns_empty(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert load_state() == {}

    def test_load_corrupt_file_returns_empty(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "fallback_watch.json").write_text(
            "{not json", encoding="utf-8"
        )
        assert load_state() == {}

    def test_repeated_saves_replace_not_accumulate(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        save_state({"last_alert_at": 1, "old_key": "gone"})
        save_state({"last_alert_at": 2})
        raw = json.loads(
            (tmp_path / "state" / "fallback_watch.json").read_text(encoding="utf-8")
        )
        assert raw == {"last_alert_at": 2}
