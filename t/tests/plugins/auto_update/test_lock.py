"""Run lock behavior."""

from __future__ import annotations

import pytest

from plugins.auto_update.lock import lock_path, nonblocking_run_lock


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.mark.linux_only
def test_lock_file_survives_release(home):
    path = lock_path()
    with nonblocking_run_lock() as first:
        assert first is True
    assert path.is_file()


@pytest.mark.linux_only
def test_overlapping_run_lock_contends(home):
    with nonblocking_run_lock() as holder:
        assert holder is True
        with nonblocking_run_lock() as contender:
            assert contender is False
