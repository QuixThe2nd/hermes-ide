"""Install scope detection behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.auto_update.platform import detect_install_scope


def test_unsupported_when_not_linux(monkeypatch):
    scope = detect_install_scope(is_linux_fn=lambda: False)
    assert scope is None


def test_user_scope_when_home_owned_by_current_user(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_system_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_user_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._user_systemd_reachable", lambda: True
    )
    scope = detect_install_scope(
        hermes_home=home,
        euid=home.stat().st_uid or 1000,
        gateway_system_unit_exists=lambda: False,
        gateway_user_unit_exists=lambda: False,
        user_systemd_reachable=lambda: True,
    )
    assert scope is not None
    assert scope.system is False
    assert scope.unit_dir == Path.home() / ".config" / "systemd" / "user"


def test_system_scope_when_gateway_system_unit_exists(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_system_unit_exists", lambda: True
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_user_unit_exists", lambda: False
    )
    scope = detect_install_scope(
        hermes_home=home,
        euid=1000,
        gateway_system_unit_exists=lambda: True,
        gateway_user_unit_exists=lambda: False,
    )
    assert scope is not None
    assert scope.system is True
    assert scope.unit_dir == Path("/etc/systemd/system")


def test_user_scope_unavailable_without_user_manager(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_system_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._gateway_user_unit_exists", lambda: False
    )
    monkeypatch.setattr(
        "plugins.auto_update.platform._user_systemd_reachable", lambda: False
    )
    assert detect_install_scope(
        hermes_home=home,
        euid=home.stat().st_uid or 1000,
        gateway_system_unit_exists=lambda: False,
        gateway_user_unit_exists=lambda: False,
        user_systemd_reachable=lambda: False,
    ) is None
