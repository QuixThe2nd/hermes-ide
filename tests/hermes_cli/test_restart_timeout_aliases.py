"""Old-key alias contract for the ONE ``gateway.drain_timeout``.

Resolution orders under test (behavior contract, not snapshots):

* ``_get_restart_drain_timeout``: deprecated ``HERMES_RESTART_DRAIN_TIMEOUT``
  env → canonical ``gateway.drain_timeout`` → legacy
  ``agent.restart_drain_timeout`` → shared default.
* ``_get_cron_drain_timeout``: deprecated ``HERMES_CRON_DRAIN_TIMEOUT`` env →
  legacy ``agent.cron_drain_timeout`` → ``gateway.drain_timeout`` → default.
* ``warn_deprecated_timeout_env`` fires once per process per variable.

User config comes from ``read_raw_config()``; these tests write a real
config.yaml into the isolated temp HERMES_HOME (never ~/.hermes).
"""

import logging

import pytest

import gateway.restart as gateway_restart
from hermes_constants import get_hermes_home
from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.gateway import _get_cron_drain_timeout, _get_restart_drain_timeout


@pytest.fixture(autouse=True)
def _fresh_deprecation_state(monkeypatch):
    """Each test sees an unwarned process, and no leaked timeout env vars."""
    monkeypatch.setattr(gateway_restart, "_deprecated_env_warned", set())
    for name in (
        "HERMES_RESTART_DRAIN_TIMEOUT",
        "HERMES_CRON_DRAIN_TIMEOUT",
        "HERMES_RESTART_AFTER_TURN_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def write_config():
    def _write(text: str) -> None:
        home = get_hermes_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(text, encoding="utf-8")

    return _write


@pytest.fixture(autouse=True)
def _bust_raw_config_cache(write_config):
    """read_raw_config caches on the file signature; the tests below change
    content faster than mtime granularity on some filesystems, so cache misses
    are forced by clearing the raw-config cache between tests."""
    from hermes_cli import config as hc_config

    hc_config._RAW_CONFIG_CACHE.clear()
    yield
    hc_config._RAW_CONFIG_CACHE.clear()


# ── _get_restart_drain_timeout ───────────────────────────────────────────────


def test_restart_drain_prefers_gateway_key_over_legacy_agent_key(write_config):
    write_config(
        "gateway:\n  drain_timeout: 33\nagent:\n  restart_drain_timeout: 22\n"
    )
    assert _get_restart_drain_timeout() == 33.0


def test_restart_drain_falls_back_to_legacy_agent_key(write_config):
    write_config("agent:\n  restart_drain_timeout: 22\n")
    assert _get_restart_drain_timeout() == 22.0


def test_restart_drain_default_when_nothing_set(write_config):
    write_config("model:\n  provider: openrouter\n")
    assert (
        _get_restart_drain_timeout()
        == gateway_restart.DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    )
    # The shared default and the shipped config default are the same knob.
    assert (
        gateway_restart.DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
        == float(DEFAULT_CONFIG["gateway"]["drain_timeout"])
    )


def test_restart_drain_env_wins_with_once_warning(write_config, monkeypatch, caplog):
    write_config("gateway:\n  drain_timeout: 33\n")
    monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "11")
    with caplog.at_level(logging.WARNING, logger="gateway.restart"):
        assert _get_restart_drain_timeout() == 11.0
        # Second read must not re-warn (once per process).
        assert _get_restart_drain_timeout() == 11.0
    warnings = [
        r
        for r in caplog.records
        if "HERMES_RESTART_DRAIN_TIMEOUT" in r.getMessage()
        and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1


# ── _get_cron_drain_timeout ──────────────────────────────────────────────────


def test_cron_drain_prefers_legacy_agent_key_over_gateway_key(write_config):
    write_config(
        "gateway:\n  drain_timeout: 33\nagent:\n  cron_drain_timeout: 44\n"
    )
    assert _get_cron_drain_timeout() == 44.0


def test_cron_drain_falls_back_to_gateway_key(write_config):
    write_config("gateway:\n  drain_timeout: 33\n")
    assert _get_cron_drain_timeout() == 33.0


def test_cron_drain_default_when_nothing_set(write_config):
    write_config("display:\n  skin: mono\n")
    assert (
        _get_cron_drain_timeout() == gateway_restart.DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
    )


def test_cron_drain_env_wins_with_once_warning(write_config, monkeypatch, caplog):
    write_config("agent:\n  cron_drain_timeout: 44\n")
    monkeypatch.setenv("HERMES_CRON_DRAIN_TIMEOUT", "11")
    with caplog.at_level(logging.WARNING, logger="gateway.restart"):
        assert _get_cron_drain_timeout() == 11.0
        assert _get_cron_drain_timeout() == 11.0
    warnings = [
        r
        for r in caplog.records
        if "HERMES_CRON_DRAIN_TIMEOUT" in r.getMessage()
        and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1


# ── warn_deprecated_timeout_env ──────────────────────────────────────────────


def test_warn_deprecated_timeout_env_fires_once(monkeypatch, caplog):
    monkeypatch.setenv("HERMES_RESTART_AFTER_TURN_TIMEOUT", "9")
    with caplog.at_level(logging.WARNING, logger="gateway.restart"):
        gateway_restart.warn_deprecated_timeout_env("HERMES_RESTART_AFTER_TURN_TIMEOUT")
        gateway_restart.warn_deprecated_timeout_env("HERMES_RESTART_AFTER_TURN_TIMEOUT")
    warnings = [
        r
        for r in caplog.records
        if "HERMES_RESTART_AFTER_TURN_TIMEOUT" in r.getMessage()
        and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1


def test_warn_deprecated_timeout_env_silent_when_unset(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="gateway.restart"):
        gateway_restart.warn_deprecated_timeout_env("HERMES_RESTART_DRAIN_TIMEOUT")
    assert not [
        r
        for r in caplog.records
        if "HERMES_RESTART_DRAIN_TIMEOUT" in r.getMessage()
        and r.levelno == logging.WARNING
    ]


def test_warn_deprecated_timeout_env_ignores_unknown_names(caplog):
    with caplog.at_level(logging.WARNING, logger="gateway.restart"):
        gateway_restart.warn_deprecated_timeout_env("HERMES_NOT_A_TIMEOUT")
    assert not caplog.records
