"""Doctor reports the tools Hermes would actually run — the pm store first.

Hermes runs pinned tools out of the pm store. Nothing puts the store on an
interactive shell's PATH, so a PATH-only probe reports a perfectly healthy
managed install as "not found", and silently skips the checks that depend
on the tool being present.
"""
from __future__ import annotations

import sys

from hermes_cli import doctor_tools


class TestPmToolPath:
    """_pm_tool_path answers from facts.json + the pm store, nothing else."""

    def _store(self, tmp_path, monkeypatch, facts: dict):
        import json

        store = tmp_path / "tools"
        store.mkdir(parents=True, exist_ok=True)
        (store / "facts.json").write_text(
            json.dumps({"schema": 1, "packages": facts}), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))
        return store

    def test_staged_tool_resolves_to_its_store_binary(self, tmp_path, monkeypatch):
        store = self._store(
            tmp_path, monkeypatch,
            {"ripgrep": {"entry": "ripgrep-15.0.0-test", "version": "15.0.0", "env": {}}},
        )
        entry = store / "ripgrep-15.0.0-test"
        entry.mkdir()
        binary = entry / ("rg.exe" if sys.platform == "win32" else "rg")
        binary.write_text("", encoding="utf-8")

        assert doctor_tools._pm_tool_path("ripgrep") == binary

    def test_unstaged_tool_resolves_to_none(self, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch, {})
        assert doctor_tools._pm_tool_path("ripgrep") is None

    def test_recorded_but_deleted_binary_resolves_to_none(self, tmp_path, monkeypatch):
        self._store(
            tmp_path, monkeypatch,
            {"ripgrep": {"entry": "ripgrep-15.0.0-test", "version": "15.0.0", "env": {}}},
        )
        assert doctor_tools._pm_tool_path("ripgrep") is None


class TestGitAndRgWiring:
    """_check_git_and_rg probes the store first, so a managed install is
    never reported as "not found" just because the store is off PATH."""

    def test_staged_rg_is_found_and_labeled_pm_store(self, tmp_path, monkeypatch, capsys):
        store = tmp_path / "tools"
        store.mkdir()
        (store / "facts.json").write_text(
            '{"schema": 1, "packages": {"ripgrep": {"entry": "ripgrep-15.0.0-test"}}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))
        entry = store / "ripgrep-15.0.0-test"
        entry.mkdir()
        (entry / ("rg.exe" if sys.platform == "win32" else "rg")).write_text("", encoding="utf-8")
        # Nothing on PATH: the old probe alone reported "not found". Patched at
        # the real shutil.which boundary, not a doctor wrapper.
        monkeypatch.setattr(doctor_tools.shutil, "which", lambda _cmd: None)

        doctor_tools._check_git_and_rg(False)

        out = capsys.readouterr().out
        assert "✓ ripgrep (rg) (pm store)" in out
        assert "ripgrep (rg) not found" not in out
