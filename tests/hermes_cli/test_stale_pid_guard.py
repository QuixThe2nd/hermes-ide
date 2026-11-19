# -*- coding: utf-8 -*-
"""Regression tests for the fail-closed PID-ownership guard.

Refs #90471 / #89614.  The three patched Windows ``taskkill`` boundaries:

- ``hermes_cli/dashboard_procs._kill_stale_dashboard_processes`` (win32)

Acceptance from #90471:
1. missing / unreadable / non-matching identity fails closed -> no taskkill
2. a recycled or foreign PID control process remains untouched
3. probe failure or timeout is never converted into permission to kill
"""
import sys
from unittest import mock

import pytest

from hermes_cli import dashboard_procs


class TestKillStaleDashboardProcesses:
    """dashboard_procs stale-kill scope behaviour."""

    def test_stop_only_targets_the_invoking_hermes_home(self, monkeypatch):
        """An argv match from another profile is never a ``--stop`` target."""
        own_home = "/tmp/hermes-own"
        foreign_home = "/tmp/hermes-foreign"
        monkeypatch.setenv("HERMES_HOME", own_home)

        with mock.patch.object(
            dashboard_procs, "_scan_dashboard_processes",
            return_value=[(12345, "hermes serve"), (12346, "hermes serve"), (12347, "hermes serve")],
        ), mock.patch.object(dashboard_procs, "_caller_ancestor_pids", return_value=set()), mock.patch.object(
            dashboard_procs, "_hermes_home_for_pid",
            side_effect=lambda pid: {
                12345: own_home,
                12346: foreign_home,
                12347: None,
            }[pid],
        ), mock.patch.object(
            dashboard_procs, "_kill_pids_posix"
        ) as kill:
            result = dashboard_procs._kill_stale_dashboard_processes(scope_home=own_home)

        kill.assert_called_once()
        assert kill.call_args.args[0] == [12345]
        assert result["matched"] == [12345]


class TestHermesHomeForPid:
    """Tri-state owner resolution: a readable environment always names a home."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX default home is $HOME/.hermes")
    def test_readable_env_without_var_resolves_to_that_process_default_home(self, monkeypatch, tmp_path):
        """The common install shape exports no HERMES_HOME: the backend lives in its user's
        platform default home, and a default-home ``--stop`` must still find it (#113978)."""
        home = str(tmp_path / "alice")
        monkeypatch.setattr(dashboard_procs, "_pid_environ", lambda pid: {"HOME": home})
        monkeypatch.setattr(dashboard_procs, "_dashboard_cmdline_for_pid",
                            lambda pid: ["hermes", "--profile", "work", "serve"] if pid == 2 else ["hermes", "serve"])

        assert dashboard_procs._hermes_home_for_pid(1) == f"{home}/.hermes"
        # ``-p``/``--profile`` is applied to os.environ after exec, invisible in /proc environ.
        assert dashboard_procs._hermes_home_for_pid(2) == f"{home}/.hermes/profiles/work"
        assert dashboard_procs._pids_owned_by_hermes_home([1, 2], f"{home}/.hermes") == [1]

    def test_root_shaped_hermes_home_follows_the_flag_and_the_sticky_active_profile(self, monkeypatch, tmp_path):
        """Mirror ``_apply_profile_override``: an exported root ``HERMES_HOME`` is the root, not the
        home — ``-p work`` and ``hermes profile use work`` both land in ``<root>/profiles/work``."""
        root = tmp_path / ".hermes"
        root.mkdir()
        monkeypatch.setattr(dashboard_procs, "_pid_environ",
                            lambda pid: {"HOME": str(tmp_path), "HERMES_HOME": str(root)})
        monkeypatch.setattr(dashboard_procs, "_dashboard_cmdline_for_pid",
                            lambda pid: ["hermes", "-p", "work", "serve"] if pid == 2 else ["hermes", "serve"])

        assert dashboard_procs._hermes_home_for_pid(2) == str(root / "profiles" / "work")
        assert dashboard_procs._hermes_home_for_pid(1) == str(root)  # no flag, no active_profile
        (root / "active_profile").write_text("work", encoding="utf-8")
        assert dashboard_procs._hermes_home_for_pid(1) == str(root / "profiles" / "work")
        # A profile-shaped HERMES_HOME without a flag is the home itself (root = its grandparent).
        monkeypatch.setattr(dashboard_procs, "_pid_environ",
                            lambda pid: {"HERMES_HOME": str(root / "profiles" / "ops")})
        assert dashboard_procs._hermes_home_for_pid(1) == str(root / "profiles" / "ops")

    def test_unreadable_env_is_none_and_spared(self, monkeypatch):
        monkeypatch.setattr(dashboard_procs, "_pid_environ", lambda pid: None)
        assert dashboard_procs._hermes_home_for_pid(7) is None
        assert dashboard_procs._pids_owned_by_hermes_home([7], "/home/alice/.hermes") == []
