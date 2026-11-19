"""Lifecycle guard path resolution must fail closed when the home directory is unresolvable.

pc_784c5c93dd4b9: under launchd/systemd ``Path.expanduser`` can raise ``RuntimeError: Could not
determine home directory``; guard evaluation must treat home-relative candidates as non-matching
instead of crashing the terminal tool.
"""
from pathlib import Path

import pytest


@pytest.fixture
def home_unresolvable(monkeypatch):
    """Force every ``Path.expanduser`` to fail the way launchd/systemd without HOME can."""

    def _raise(self):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "expanduser", _raise)


class TestLifecycleGuardHomeUnset:
    def test_expand_candidate_path_returns_none(self, home_unresolvable):
        from cron.lifecycle_guard import _expand_candidate_path

        assert _expand_candidate_path("~/kanban_create.py") is None

    def test_home_relative_script_command_does_not_crash(self, home_unresolvable):
        """The exact papercut call: a non-lifecycle helper on a ~/ path is scanned, not crashed on."""
        from cron.lifecycle_guard import (
            contains_gateway_lifecycle_command_or_referenced_script,
        )

        assert (
            contains_gateway_lifecycle_command_or_referenced_script(
                "python3 ~/kanban_create.py --title x"
            )
            is False
        )

    def test_direct_lifecycle_command_still_blocked(self, home_unresolvable):
        """Unresolvable home must not blind the guard to a literal lifecycle command."""
        from cron.lifecycle_guard import (
            contains_gateway_lifecycle_command_or_referenced_script,
        )

        assert (
            contains_gateway_lifecycle_command_or_referenced_script(
                "hermes gateway restart"
            )
            is True
        )
