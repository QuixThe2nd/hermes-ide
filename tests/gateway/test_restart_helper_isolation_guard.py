"""Detached restart/gateway spawns are fenced off under HERMES_TEST_ISOLATION.

The hermetic conftest exports ``HERMES_TEST_ISOLATION`` to mark the whole
process tree as a test run. The detached launchers — the gateway's
restart watcher (``GatewayRunner._launch_detached_restart_watcher``) and the
macOS launchd fallback (``hermes_cli.gateway._spawn_detached_gateway``)
— each leave behind a process that outlives their caller, so one that fires
inside a test escapes the sandbox and leaks past the session. Both
consult ``gateway.restart.detached_restart_spawn_blocked`` before any latch
or spawn and no-op with a warning instead.
"""

import logging
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from gateway.restart import detached_restart_spawn_blocked
from tests.gateway.restart_test_helpers import make_restart_runner


# --- the predicate -------------------------------------------------------

def test_predicate_blocks_on_any_nonblank_marker():
    assert detached_restart_spawn_blocked({"HERMES_TEST_ISOLATION": "1"}) is True
    # The marker's value is the isolation root, not a boolean flag.
    assert (
        detached_restart_spawn_blocked({"HERMES_TEST_ISOLATION": "/tmp/iso-root"})
        is True
    )


def test_predicate_ignores_blank_or_missing_marker():
    assert detached_restart_spawn_blocked({}) is False
    assert detached_restart_spawn_blocked({"HERMES_TEST_ISOLATION": ""}) is False
    assert detached_restart_spawn_blocked({"HERMES_TEST_ISOLATION": "   "}) is False


def test_predicate_reads_os_environ_when_environ_is_none(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "/tmp/pytest-isolation-root")
    assert detached_restart_spawn_blocked() is True
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    assert detached_restart_spawn_blocked() is False


# --- the launchers -------------------------------------------------------

def _no_spawn_popen(monkeypatch) -> MagicMock:
    """Replace ``subprocess.Popen`` process-wide with a recording mock.

    All three launchers reach the OS only through ``subprocess.Popen``
    (module-level or a local ``import subprocess`` — same module object),
    so one patched attribute sees every spawn attempt they could make.
    """
    popen = MagicMock(name="subprocess.Popen")
    monkeypatch.setattr(subprocess, "Popen", popen)
    return popen


def _warned_about_isolation(caplog) -> bool:
    return any(
        "HERMES_TEST_ISOLATION" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_gateway_runner_detached_restart_never_spawns_under_isolation(
    monkeypatch, caplog
):
    import gateway.run  # noqa: F401  # logger name anchor

    monkeypatch.setenv("HERMES_TEST_ISOLATION", "/tmp/pytest-isolation-root")
    popen = _no_spawn_popen(monkeypatch)
    runner, _adapter = make_restart_runner()
    # Guard-order proof WITHOUT intercepting logging: ``os.getpid`` is off
    # limits as an anchor because it lives on the SHARED os module — every
    # LogRecord creation calls it, so the guard's own warning would falsify
    # an assert_not_called on it. ``Path`` is a gateway.run module global the watcher
    # launcher touches only past the guard, while building the project root for the
    # watcher argv: recording it proves the refusal happens before any watcher argv is
    # even built, and logging never touches ``gateway.run.Path``.
    path_cls = MagicMock(name="gateway.run.Path")
    monkeypatch.setattr("gateway.run.Path", path_cls)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._launch_detached_restart_watcher()

    path_cls.assert_not_called()
    popen.assert_not_called()
    # The guard fires BEFORE the one-shot latch: this attempt was refused,
    # not consumed, so a later real attempt could still spawn.
    assert runner._detached_restart_watcher_started is False
    assert _warned_about_isolation(caplog)


def test_cli_detached_gateway_never_spawns_under_isolation(monkeypatch, caplog):
    from hermes_cli import gateway as cli_gateway

    monkeypatch.setenv("HERMES_TEST_ISOLATION", "/tmp/pytest-isolation-root")
    popen = _no_spawn_popen(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.gateway"):
        assert cli_gateway._spawn_detached_gateway() is False

    popen.assert_not_called()
    assert _warned_about_isolation(caplog)




# --- negative controls: the marker is what blocks, nothing else ----------

def test_cli_detached_gateway_spawns_again_once_marker_is_gone(monkeypatch):
    from hermes_cli import gateway as cli_gateway

    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    popen = _no_spawn_popen(monkeypatch)

    assert cli_gateway._spawn_detached_gateway() is True
    assert popen.call_count == 1


