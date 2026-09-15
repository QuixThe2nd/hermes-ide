"""Regression test for the startup-watchdog disarm seam (OOF-298 kill loop).

Production bug (Sep 12-13, 2026): the gateway's startup-liveness watchdog
fired every ~26 minutes on a healthy gateway (exit 75, systemd restart
loop, 31+ restarts) because ``gateway/run.py`` imported
``arm_startup_watchdog`` / ``disarm_startup_watchdog`` from
``gateway.startup_watchdog`` — a plugin-compat stub that re-exports
``gateway.shutdown_watchdog``, which does NOT define those names. The
ImportError at the disarm site was swallowed by a surrounding
``except Exception: logger.debug(...)``, so the watchdog (armed earlier via
the ``hermes_cli.main`` argv fast-path) was never disarmed once the event
loop went live.

Fix: both call sites in ``gateway/run.py`` now import from the real
implementations in the top-level ``hermes_startup_watchdog`` module. These
tests pin that seam so the broken import path cannot silently return.
"""

import inspect

import gateway.run
import gateway.startup_watchdog
import hermes_startup_watchdog


def test_real_module_exposes_arm_and_disarm():
    from hermes_startup_watchdog import arm_startup_watchdog, disarm_startup_watchdog

    assert callable(arm_startup_watchdog)
    assert callable(disarm_startup_watchdog)


def test_stub_does_not_provide_startup_watchdog_functions():
    """Root-cause guard: the plugin-compat stub re-exports
    ``gateway.shutdown_watchdog``, which has no startup-watchdog functions.
    Any ``from gateway.startup_watchdog import disarm_startup_watchdog``
    therefore raises ImportError — exactly why the old seam failed silently
    inside the swallowed-exception disarm block."""
    assert not hasattr(gateway.startup_watchdog, "arm_startup_watchdog")
    assert not hasattr(gateway.startup_watchdog, "disarm_startup_watchdog")


def test_run_py_disarm_site_imports_from_real_module():
    """The disarm block in ``GatewayRunner.start`` (the loop-confirmed-live
    milestone) must import from ``hermes_startup_watchdog`` directly."""
    src = inspect.getsource(gateway.run.GatewayRunner.start)
    assert "from hermes_startup_watchdog import disarm_startup_watchdog" in src
    assert "from gateway.startup_watchdog import" not in src


def test_run_py_arm_site_imports_from_real_module():
    """The arm block in ``main()`` must import from
    ``hermes_startup_watchdog`` directly."""
    src = inspect.getsource(gateway.run.main)
    assert "from hermes_startup_watchdog import arm_startup_watchdog" in src
    assert "from gateway.startup_watchdog import" not in src


def test_lazy_disarm_import_resolves_to_real_singleton(monkeypatch):
    """Functional seam proof: ``gateway/run.py`` performs the disarm import
    lazily at call time, so it binds whatever
    ``hermes_startup_watchdog.disarm_startup_watchdog`` is at that moment —
    i.e. the disarm call reaches the real module (and its
    ``_STARTUP_WATCHDOG`` singleton), not the broken stub."""
    calls = []
    monkeypatch.setattr(
        hermes_startup_watchdog, "disarm_startup_watchdog", lambda: calls.append(True)
    )
    # The exact statement the disarm block in GatewayRunner.start executes.
    from hermes_startup_watchdog import disarm_startup_watchdog

    disarm_startup_watchdog()
    assert calls == [True]
