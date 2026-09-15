"""Tests for Discord thread participation persistence.

Verifies that _threads (ThreadParticipationTracker) survives adapter restarts by
being persisted to ~/.hermes/discord_threads.json, and that marks written by a
SEPARATE instance (hermes_starts marks new threads from a fresh tracker) are
visible to an already-running adapter without a restart.
"""

import json
import os
from unittest.mock import patch


class TestDiscordThreadPersistence:
    """Thread IDs are saved to disk and reloaded on init."""

    def _make_adapter(self, tmp_path):
        """Build a minimal DiscordAdapter with HERMES_HOME pointed at tmp_path."""
        from gateway.config import PlatformConfig
        from plugins.platforms.discord.adapter import DiscordAdapter

        config = PlatformConfig(enabled=True, token="test-token")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            return DiscordAdapter(config=config)

    def test_starts_empty_when_no_state_file(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        assert "$nonexistent" not in adapter._threads

    def test_track_thread_persists_to_disk(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            adapter._threads.mark("111")
            adapter._threads.mark("222")

        state_file = tmp_path / "discord_threads.json"
        assert state_file.exists()
        saved = json.loads(state_file.read_text())
        assert set(saved) == {"111", "222"}

    def test_threads_survive_restart(self, tmp_path):
        """Threads tracked by one adapter instance are visible to the next."""
        adapter1 = self._make_adapter(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            adapter1._threads.mark("aaa")
            adapter1._threads.mark("bbb")

        adapter2 = self._make_adapter(tmp_path)
        assert "aaa" in adapter2._threads
        assert "bbb" in adapter2._threads

    def test_marks_from_separate_instance_seen_without_rebuild(self, tmp_path):
        """Regression: the gateway's long-lived tracker must notice threads marked
        by another process. hermes_starts creates start threads via REST and marks
        them with a FRESH ThreadParticipationTracker; the owner's no-mention reply
        in that thread was silently dropped until the next gateway restart."""
        from gateway.platforms.helpers import ThreadParticipationTracker

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            adapter = self._make_adapter(tmp_path)
            adapter._threads.mark("own-thread")
            # A separate instance, as plugins/hermes_starts does it.
            ThreadParticipationTracker("discord").mark("start-thread")
            # Visible via `in` on the ALREADY-CONSTRUCTED adapter (no rebuild).
            assert "start-thread" in adapter._threads
            assert "own-thread" in adapter._threads
            # mark() after a foreign write merges instead of clobbering it.
            adapter._threads.mark("later-thread")
            saved = json.loads((tmp_path / "discord_threads.json").read_text())
            assert set(saved) == {"own-thread", "start-thread", "later-thread"}

    def test_state_file_deletion_between_operations_is_fail_safe(self, tmp_path):
        """A vanished or corrupt state file keeps the in-memory set; later marks
        still work (and self-heal the file)."""
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            adapter = self._make_adapter(tmp_path)
            adapter._threads.mark("111")
            (tmp_path / "discord_threads.json").unlink()

            assert "111" in adapter._threads
            adapter._threads.mark("222")
            assert "222" in adapter._threads
            assert "111" in adapter._threads

            # Corrupt on-disk content must not wipe the in-memory set either.
            (tmp_path / "discord_threads.json").write_text("{not json", encoding="utf-8")
            assert "111" in adapter._threads
            adapter._threads.mark("333")  # overwrite heals the file
            saved = json.loads((tmp_path / "discord_threads.json").read_text())
            assert set(saved) == {"111", "222", "333"}


