"""The current-checkout repair path must finish the generic Node/web phases."""

from __future__ import annotations

from unittest.mock import patch

from hermes_cli import update_cmd


def test_handoff_venv_repair_finishes_node_and_web_phase(tmp_path):
    """A successful Python repair must not bypass the remaining update work."""
    project_root = tmp_path / "hermes"
    venv_python = project_root / "venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()

    with (
        patch("hermes_cli.update_cmd_deps.venv_python_path", return_value=venv_python),
        patch("pm.sync_venv"),
        patch.object(update_cmd, "_venv_core_imports_healthy", return_value=(True, "ok")),
        patch.object(update_cmd, "_write_update_incomplete_marker"),
        patch.object(update_cmd, "_update_node_dependencies", return_value=[]) as update_node,
        patch.object(update_cmd, "_check_and_apply_config_migration"),
        patch.object(update_cmd, "_print_verified_update_completion", return_value=True) as completion,
        patch.object(update_cmd, "_m") as m,
    ):
        m.return_value.PROJECT_ROOT = project_root
        complete = update_cmd._repair_venv_on_current_checkout(
            assume_yes=True,
            gateway_mode=False,
            pre_update_snapshot_id=None,
            active_lazy_features=(),
            active_tool_dependencies=(),
            _windows_gateway_resume=None,
        )

    assert complete is True
    update_node.assert_called_once_with()
    m.return_value._build_web_ui.assert_called_once_with(project_root / "web")
    completion.assert_called_once_with("✓ Update complete!")
