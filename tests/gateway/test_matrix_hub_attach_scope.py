"""Matrix hub attach must resolve secrets like MatrixAdapter (scoped + default-profile fallback)."""

from __future__ import annotations

import pytest

from agent import secret_scope as ss
from hermes_constants import (
    get_process_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from plugins.platforms.matrix.hub_client import MatrixHubClientAdapter


@pytest.fixture
def multiplex_no_scope():
    ss.set_multiplex_active(True)
    assert ss.current_secret_scope() is None
    yield
    ss.set_multiplex_active(False)


def test_attach_secret_scope_reads_default_profile_from_os_environ(multiplex_no_scope, monkeypatch):
    """Live outage: bare get_secret raises when multiplex is on and no scope is installed."""
    homeserver = "https://systemd-injected.matrix.example"
    token = "systemd-injected-matrix-token"
    monkeypatch.setenv("MATRIX_HOMESERVER", homeserver)
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", token)
    # Isolated HERMES_HOME (autouse) has no .env covering Matrix keys.
    from hermes_constants import get_hermes_home

    assert get_hermes_home().resolve() == get_process_hermes_home().resolve()

    mapping = MatrixHubClientAdapter._attach_secret_scope()

    assert mapping["MATRIX_HOMESERVER"] == homeserver
    assert mapping["MATRIX_ACCESS_TOKEN"] == token


def _write_profile_env(home, *, homeserver: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(f"MATRIX_HOMESERVER={homeserver}\n", encoding="utf-8")
    ss.invalidate_env_file_cache(home / ".env")


def test_attach_secret_scope_isolates_secondary_profiles(multiplex_no_scope, tmp_path, monkeypatch):
    polluted = "https://wrong-profile-leak.matrix.example"
    monkeypatch.setenv("MATRIX_HOMESERVER", polluted)

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    hs_a = "https://profile-a.matrix.example"
    hs_b = "https://profile-b.matrix.example"
    _write_profile_env(home_a, homeserver=hs_a)
    _write_profile_env(home_b, homeserver=hs_b)

    def attach_for(home):
        token = set_hermes_home_override(home)
        try:
            ss.invalidate_env_file_cache(home / ".env")
            return MatrixHubClientAdapter._attach_secret_scope()
        finally:
            reset_hermes_home_override(token)

    m_a1 = attach_for(home_a)
    assert m_a1["MATRIX_HOMESERVER"] == hs_a
    assert m_a1["MATRIX_ACCESS_TOKEN"] == ""

    m_b = attach_for(home_b)
    assert m_b["MATRIX_HOMESERVER"] == hs_b
    assert m_b["MATRIX_HOMESERVER"] != hs_a
    assert m_b["MATRIX_HOMESERVER"] != polluted
    assert m_b["MATRIX_ACCESS_TOKEN"] == ""

    m_a2 = attach_for(home_a)
    assert m_a2["MATRIX_HOMESERVER"] == hs_a
    assert m_a2["MATRIX_HOMESERVER"] != hs_b
    assert m_a2["MATRIX_HOMESERVER"] != polluted
    assert m_a2["MATRIX_ACCESS_TOKEN"] == ""
