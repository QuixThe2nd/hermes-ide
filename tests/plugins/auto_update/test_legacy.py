"""Legacy unit migration safety."""

from __future__ import annotations

import pytest

from plugins.auto_update.legacy import (
    LEGACY_KNOWN_REFERENCE_SERVICE,
    LEGACY_KNOWN_REFERENCE_TIMER,
    LEGACY_SERVICE,
    LEGACY_TIMER,
    duplicate_scheduler_present,
    identify_legacy_service,
    identify_legacy_timer,
    migrate_legacy_units,
)
from plugins.auto_update.platform import InstallScope


@pytest.fixture
def scope(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    return InstallScope(system=False, unit_dir=unit_dir, systemctl_prefix=("systemctl", "--user"))


def test_reference_legacy_timer_is_positively_identified():
    assert identify_legacy_timer(LEGACY_KNOWN_REFERENCE_TIMER) == "known"


def test_reference_legacy_service_is_positively_identified():
    assert identify_legacy_service(LEGACY_KNOWN_REFERENCE_SERVICE) == "known"


def test_known_legacy_timer_disabled_not_deleted(scope, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = scope.unit_dir / LEGACY_TIMER
    path.write_text(LEGACY_KNOWN_REFERENCE_TIMER, encoding="utf-8")
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        return 0, "", ""

    result = migrate_legacy_units(scope, run_systemctl=fake_systemctl)
    assert path.exists()
    assert LEGACY_TIMER in result.disabled_units
    assert LEGACY_TIMER in result.backed_up_units
    assert (home / "auto-update" / "legacy-units" / LEGACY_TIMER).exists()
    assert any("disable" in " ".join(c) for c in calls)


def test_known_legacy_service_disabled_not_deleted(scope, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = scope.unit_dir / LEGACY_SERVICE
    path.write_text(LEGACY_KNOWN_REFERENCE_SERVICE, encoding="utf-8")
    calls: list[list[str]] = []

    def fake_systemctl(args):
        calls.append(list(args))
        return 0, "", ""

    result = migrate_legacy_units(scope, run_systemctl=fake_systemctl)
    assert path.exists()
    assert LEGACY_SERVICE in result.disabled_units
    assert any("disable" in " ".join(c) for c in calls)


def test_unknown_legacy_timer_warns_and_refuses(scope):
    path = scope.unit_dir / LEGACY_TIMER
    path.write_text("[Unit]\nDescription=admin custom\n", encoding="utf-8")
    result = migrate_legacy_units(scope, run_systemctl=lambda args: (0, "", ""))
    assert path.exists()
    assert LEGACY_TIMER in result.refused
    assert result.warnings


def test_admin_custom_legacy_service_with_substring_refused(scope):
    path = scope.unit_dir / LEGACY_SERVICE
    path.write_text(
        "[Unit]\nDescription=Hermes auto-update custom\n"
        "[Service]\nExecStart=/usr/local/bin/custom-auto-update.sh\n",
        encoding="utf-8",
    )
    assert identify_legacy_service(path.read_text(encoding="utf-8")) == "unknown"
    result = migrate_legacy_units(scope, run_systemctl=lambda args: (0, "", ""))
    assert path.exists()
    assert LEGACY_SERVICE in result.refused


def test_exact_known_exec_start_line_is_positively_identified():
    body = "[Service]\nExecStart=/opt/hermes/bin/hermes-auto-update.sh\n"
    assert identify_legacy_service(body) == "known"


def test_noncanonical_wrapper_path_is_unknown():
    body = "[Service]\nExecStart=/root/.local/bin/hermes-auto-update.sh\n"
    assert identify_legacy_service(body) == "unknown"


def test_duplicate_scheduler_requires_enabled_legacy_timer(scope):
    (scope.unit_dir / LEGACY_TIMER).write_text(
        LEGACY_KNOWN_REFERENCE_TIMER, encoding="utf-8"
    )
    assert duplicate_scheduler_present(
        scope, run_systemctl=lambda args: (0, "enabled\n", "")
    )
    assert not duplicate_scheduler_present(
        scope, run_systemctl=lambda args: (1, "disabled\n", "")
    )


def test_migration_is_idempotent(scope, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (scope.unit_dir / LEGACY_TIMER).write_text(
        LEGACY_KNOWN_REFERENCE_TIMER, encoding="utf-8"
    )

    def fake_systemctl(args):
        return 0, "", ""

    first = migrate_legacy_units(scope, run_systemctl=fake_systemctl)
    second = migrate_legacy_units(scope, run_systemctl=fake_systemctl)
    assert LEGACY_TIMER in first.disabled_units
    assert LEGACY_TIMER in second.disabled_units
