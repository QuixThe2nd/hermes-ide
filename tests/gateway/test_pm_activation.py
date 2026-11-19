"""Gateway main() must run the pm startup contract (gateway/run.py).

The gateway daemon boots via its own entrypoint and never passes through the
CLI dispatch in hermes_cli/main.py — without an explicit call here the
gateway's os.environ PATH would lack the pm store's tool dirs (git/bash/
ffmpeg/...), so every reactive ``shutil.which`` inside the daemon resolves
system binaries (or nothing) instead of the pinned store ones.

Behavior contract (mirrors CLI dispatch through hermes_cli.pm_startup):
- healthy store → ``pm.activate()`` provisions PATH, with no heal thread
- out-of-sync store → warning and daemon heal, while boot continues
- pm import/lookup/heal failure → debug-logged, boot continues
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import patch

import pytest


@pytest.fixture
def gateway_main(monkeypatch, tmp_path):
    """Import gateway.run.main with the heavy boot machinery neutralized."""
    # The real main() advertises env vars, registers process identity, runs
    # boot bootstrap, and FORCE-EXITS via _exit_after_graceful_shutdown
    # (os._exit — wedge-proof by design, #53107) — none of it is what we
    # assert on. Neutralize the exit + the async boot; keep the pm block's
    # imports real.
    import gateway.run as gr

    async def _no_gateway(_config=None, **_kwargs):
        return True

    exited: list[int] = []

    def _fake_exit(code):
        exited.append(code)

    monkeypatch.setattr(gr, "start_gateway", _no_gateway)
    monkeypatch.setattr(gr, "_exit_after_graceful_shutdown", _fake_exit)
    monkeypatch.setattr(
        "hermes_cli.boot_bootstrap.maybe_run_boot_bootstrap", lambda _root: None
    )

    def _main():
        gr.main()
        return exited

    return _main


@pytest.fixture
def thread_recorder(monkeypatch):
    threads = []

    class _RecordingThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False
            threads.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(threading, "Thread", _RecordingThread)
    return threads


def test_gateway_main_activates_pm_store(
    gateway_main, monkeypatch, thread_recorder
):
    """A healthy store: main() provisions PATH without starting a heal."""
    calls: list[str] = []

    class _FakePm:
        @staticmethod
        def adopt():
            calls.append("adopt")

        @staticmethod
        def check():
            calls.append("check")
            return []  # healthy

        @staticmethod
        def activate():
            calls.append("activate")

    monkeypatch.setitem(sys.modules, "pm", _FakePm)
    with patch("sys.argv", ["gateway"]):
        assert gateway_main() == [0]
    assert calls == ["adopt", "check", "activate"]
    assert not any(t.name == "hermes-pm-autoheal" for t in thread_recorder)


def test_gateway_main_warns_but_boots_when_store_out_of_sync(
    gateway_main, monkeypatch, caplog, thread_recorder
):
    """An out-of-sync store starts a daemon heal without blocking boot."""

    class _FakePm:
        @staticmethod
        def adopt():
            pass

        @staticmethod
        def check():
            return ["uv not installed", "ffmpeg outdated"]

        @staticmethod
        def activate():
            raise AssertionError("the unexecuted test worker must not activate")

    monkeypatch.setitem(sys.modules, "pm", _FakePm)
    with patch("sys.argv", ["gateway"]):
        assert gateway_main() == [0]
    assert any(
        "install out of sync" in rec.message for rec in caplog.records
    ), "the out-of-sync warning must be logged"
    healers = [t for t in thread_recorder if t.name == "hermes-pm-autoheal"]
    assert len(healers) == 1
    assert healers[0].daemon is True
    assert healers[0].started is True


def test_gateway_main_survives_pm_failure(
    gateway_main, monkeypatch, thread_recorder
):
    """A broken pm import is debug-logged; the gateway still boots."""

    class _ExplodingPm:
        @staticmethod
        def adopt():
            raise RuntimeError("pm exploded")

    monkeypatch.setitem(sys.modules, "pm", _ExplodingPm)
    with patch("sys.argv", ["gateway"]):
        assert gateway_main() == [0]


def test_gateway_main_survives_pm_heal_failure(gateway_main, monkeypatch):
    """A provisioning exception stays inside the daemon heal worker."""

    class _InlineHealThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name

        def start(self):
            if self.name == "hermes-pm-autoheal":
                self.target()

    class _FakePm:
        @staticmethod
        def adopt():
            pass

        @staticmethod
        def check():
            return ["uv not installed"]

        @staticmethod
        def activate():
            pass

    def _explode():
        raise RuntimeError("provisioning failed")

    monkeypatch.setattr(threading, "Thread", _InlineHealThread)
    monkeypatch.setattr(
        "hermes_cli.post_update.step_provision_runtimes", _explode
    )
    monkeypatch.setitem(sys.modules, "pm", _FakePm)
    with patch("sys.argv", ["gateway"]):
        assert gateway_main() == [0]
