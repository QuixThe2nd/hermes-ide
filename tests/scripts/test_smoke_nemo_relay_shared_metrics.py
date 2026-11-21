"""Tests for the shared-metrics smoke artifact."""

from pathlib import Path

import pytest

from scripts import smoke_nemo_relay_shared_metrics as smoke


def test_resolve_hermes_executable_from_repository_venv(tmp_path, monkeypatch):
    executable = tmp_path / ".venv" / "bin" / "hermes"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(smoke.shutil, "which", lambda _name: None)
    assert smoke._resolve_hermes_executable(tmp_path) == executable


def test_resolve_hermes_executable_falls_back_to_path(tmp_path, monkeypatch):
    executable = tmp_path / "bin" / "hermes"
    monkeypatch.setattr(smoke.shutil, "which", lambda _name: str(executable))

    assert smoke._resolve_hermes_executable(tmp_path / "repo") == executable


def test_resolve_hermes_executable_reports_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke.shutil, "which", lambda _name: None)

    with pytest.raises(SystemExit, match="or on PATH"):
        smoke._resolve_hermes_executable(tmp_path)
