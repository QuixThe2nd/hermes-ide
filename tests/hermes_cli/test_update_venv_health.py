"""Tests for the half-updated-venv hardening (July 2026 incident).

Covers two additions to ``hermes update``:

1. ``_venv_core_imports_healthy`` — the venv health probe that lets an
   "Already up to date" checkout still repair a broken dependency install.
2. The commit_count == 0 repair branch wiring in ``_cmd_update_impl``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


from hermes_cli import main as cli_main
from hermes_cli import update_cmd


# ---------------------------------------------------------------------------
# _venv_core_imports_healthy
# ---------------------------------------------------------------------------




def _fake_venv_python(tmp_path):
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    py = bin_dir / "python"
    py.write_bytes(b"")
    return py


def test_venv_core_imports_probe_excludes_fastapi(tmp_path):
    """PR #552 removed fastapi (dashboard web UI removal); the probe must not require it."""
    from hermes_cli import update_cmd_deps

    venv_python = _fake_venv_python(tmp_path)
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), patch.object(
        update_cmd_deps.subprocess, "run", fake_run
    ):
        healthy, detail = update_cmd_deps._venv_core_imports_healthy()
        assert healthy is True
        assert detail == ""

    assert len(captured) == 1
    probe_cmd = captured[0]
    assert probe_cmd[0] == str(venv_python)
    assert probe_cmd[1] == "-c"
    check_src = probe_cmd[2]
    assert "'fastapi'" not in check_src
    for mod in ("'uvicorn'", "'pydantic'", "'openai'", "'yaml'"):
        assert mod in check_src


def test_sibling_candidate_venv_probes_exclude_fastapi():
    """PR #552 removed fastapi (dashboard removal); the sibling candidate-venv smoke
    probes in runtime_repair and managed_uv must not require it either."""
    import inspect

    from hermes_cli import managed_uv, runtime_repair

    assert "fastapi" not in inspect.getsource(runtime_repair._smoke_candidate_venv)
    assert "fastapi" not in inspect.getsource(managed_uv._smoke_candidate_venv)


# ---------------------------------------------------------------------------
# "Already up to date" must not hide a venv the last pull never re-synced (#97208)
# ---------------------------------------------------------------------------


def test_venv_dependency_set_stale_compares_installed_distribution_with_checkout(tmp_path):
    """Reporter's state: git current at 0.21.2, venv metadata still 0.20.6 (the hand-off child
    refused the sync); core imports pass, so only the distribution version reveals the drift."""
    from hermes_cli import update_cmd_deps

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "hermes-agent"\nversion = "0.21.2"\n', encoding="utf-8")
    venv_python = _fake_venv_python(tmp_path)
    probes = []

    def fake_run(cmd, **kwargs):
        probes.append(cmd)
        return SimpleNamespace(returncode=0, stdout=f"{fake_run.installed}\n", stderr="")

    with patch.object(cli_main, "PROJECT_ROOT", tmp_path), \
            patch.object(update_cmd_deps.subprocess, "run", fake_run):
        fake_run.installed = "0.20.6"
        assert update_cmd._venv_dependency_set_stale() == (True, "installed hermes-agent 0.20.6, checkout is 0.21.2")
        fake_run.installed = "0.21.2"
        assert update_cmd._venv_dependency_set_stale() == (False, "")
    # Asked in the venv's own interpreter (the updater may run under another Python).
    assert {cmd[0] for cmd in probes} == {str(venv_python)}


def test_current_checkout_with_stale_dependency_set_runs_the_sync(monkeypatch, capsys):
    """Drift on the commit_count == 0 path triggers the same repair as an unhealthy venv instead
    of ``✓ Already up to date!``; a synced venv keeps the cheap node-only path."""
    calls = []
    monkeypatch.setattr(update_cmd, "_venv_core_imports_healthy", lambda: (True, ""))
    monkeypatch.setattr("pm.ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(update_cmd, "_repair_venv_on_current_checkout",
                        lambda **kwargs: calls.append("sync") or True)
    monkeypatch.setattr(update_cmd, "_repair_node_deps_on_current_checkout",
                        lambda *a, **kwargs: calls.append(kwargs["completion_message"]) or True)

    def run(stale):
        monkeypatch.setattr(update_cmd, "_venv_dependency_set_stale", lambda: stale)
        return update_cmd._repair_current_checkout(
            assume_yes=True, gateway_mode=False, pre_update_snapshot_id=None,
            active_lazy_features=[], active_tool_dependencies=[])

    assert run((True, "installed hermes-agent 0.20.6, checkout is 0.21.2"))[0] is True
    assert calls == ["sync"]
    assert "never synced after the last pull" in capsys.readouterr().out
    assert run((False, ""))[0] is True
    assert calls == ["sync", "✓ Already up to date!"]
