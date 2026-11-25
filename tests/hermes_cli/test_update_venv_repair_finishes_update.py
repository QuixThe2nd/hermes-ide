"""The current-checkout venv repair must finish — or correctly abandon — the update tail.

``_repair_venv_on_current_checkout`` runs in the post-swap hand-off child after a
Python dependency repair. A successful repair must still run the Node refresh and
the config migration before printing the verified completion; a failed one must
keep the incomplete marker so the next ``hermes update`` re-enters the repair
instead of declaring victory. (The retired shape also rebuilt the dashboard web
UI here — gone with the dashboard, 800bf3abc27 — and carried a Windows
gateway-resume token, gone with the POSIX-only collapse, ac10f28628a.)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pm

from hermes_cli import update_cmd


def _repair_kwargs():
    return dict(
        assume_yes=True,
        gateway_mode=False,
        pre_update_snapshot_id=None,
        active_lazy_features=(),
        active_tool_dependencies=(),
    )


def test_venv_repair_finishes_node_phase_and_completion():
    """A successful Python repair must not bypass the remaining update work."""
    completion = MagicMock(return_value=True)
    with (
        patch("pm.sync_venv"),
        patch.object(update_cmd, "_venv_core_imports_healthy", return_value=(True, "ok")),
        patch.object(update_cmd, "_write_update_incomplete_marker") as write_marker,
        patch.object(update_cmd, "_update_node_dependencies", return_value=[]) as update_node,
        patch.object(update_cmd, "_check_and_apply_config_migration") as migrate,
        patch.object(update_cmd, "_print_verified_update_completion", completion),
        patch.object(update_cmd, "_m") as m,
    ):
        complete = update_cmd._repair_venv_on_current_checkout(**_repair_kwargs())

    assert complete is True
    update_node.assert_called_once_with()
    migrate.assert_called_once_with(
        assume_yes=True, gateway_mode=False, pre_update_snapshot_id=None)
    # The incomplete marker brackets the repair: written up front, cleared only
    # once the replacement environment probes healthy.
    write_marker.assert_called_once_with()
    m.return_value._clear_update_incomplete_marker.assert_called_once()
    completion.assert_called_once_with("✓ Update complete!")


def test_venv_repair_failure_keeps_marker_and_skips_tail(capsys):
    """A failed sync surfaces the error, keeps the marker, and never starts the tail."""
    completion = MagicMock(return_value=True)
    with (
        patch("pm.sync_venv", side_effect=pm.InstallError("all", "no disk space")),
        patch.object(update_cmd, "_write_update_incomplete_marker") as write_marker,
        patch.object(update_cmd, "_update_node_dependencies", return_value=[]) as update_node,
        patch.object(update_cmd, "_print_verified_update_completion", completion),
        patch.object(update_cmd, "_m") as m,
    ):
        complete = update_cmd._repair_venv_on_current_checkout(**_repair_kwargs())

    assert complete is False
    out = capsys.readouterr().out
    assert "✗" in out
    assert "no disk space" in out
    m.return_value._clear_update_incomplete_marker.assert_not_called()
    update_node.assert_not_called()
    completion.assert_not_called()
