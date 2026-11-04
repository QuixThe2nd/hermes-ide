"""Generic update-summary and gateway completion behavior."""

from __future__ import annotations

from hermes_cli import update_cmd
from hermes_cli.update_cmd import _print_update_summary, _write_gateway_update_exit_code


def test_summary_keeps_success_banner_when_runtime_is_healthy(capsys, monkeypatch):
    monkeypatch.setattr(
        update_cmd, "_update_complete_message", lambda _v: "✓ Update complete! (v0.20.2)"
    )
    monkeypatch.setattr(update_cmd, "_branch_head_suffix", lambda *a, **k: "")
    monkeypatch.setattr(
        update_cmd, "_post_update_sqlite_runtime_status", lambda: (True, None)
    )
    _print_update_summary(node_failures=[], pre_update_version="0.20.1")
    out = capsys.readouterr().out
    assert "✓ Update complete!" in out
    assert "partially complete" not in out


def test_summary_reports_node_failures(capsys, monkeypatch):
    monkeypatch.setattr(
        update_cmd, "_post_update_sqlite_runtime_status", lambda: (True, None)
    )
    _print_update_summary(node_failures=["dashboard"], pre_update_version="0.20.1")
    out = capsys.readouterr().out
    assert "Update complete" not in out
    assert "partially complete" in out
    assert "dashboard" in out


def test_gateway_exit_code_file_tracks_update_success(tmp_path, monkeypatch):
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: tmp_path)
    _write_gateway_update_exit_code(True)
    assert (tmp_path / ".update_exit_code").read_text(encoding="utf-8") == "0"
    _write_gateway_update_exit_code(False)
    assert (tmp_path / ".update_exit_code").read_text(encoding="utf-8") == "1"
