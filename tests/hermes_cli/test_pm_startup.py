"""Shared pm startup behavior for CLI dispatch and the gateway daemon."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from hermes_cli import pm_startup


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

    monkeypatch.setattr(pm_startup.threading, "Thread", _RecordingThread)
    return threads


def test_healthy_store_activates_without_spawning_a_heal(monkeypatch, thread_recorder):
    calls = []

    def _check():
        calls.append("check")
        return []

    pm = SimpleNamespace(
        adopt=lambda: calls.append("adopt"),
        check=_check,
        activate=lambda: calls.append("activate"),
    )
    monkeypatch.setitem(sys.modules, "pm", pm)

    assert pm_startup.run_pm_startup() == []
    assert calls == ["adopt", "check", "activate"]
    assert thread_recorder == []


def test_out_of_sync_store_warns_and_starts_daemon_heal(
    monkeypatch, thread_recorder
):
    warnings = []
    pm = SimpleNamespace(
        adopt=lambda: None,
        check=lambda: ["uv not installed", "ffmpeg outdated"],
        activate=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "pm", pm)

    problems = pm_startup.run_pm_startup(warn=warnings.append)

    assert problems == ["uv not installed", "ffmpeg outdated"]
    assert warnings == [
        "install out of sync (uv not installed; ffmpeg outdated) — "
        "auto-healing in background"
    ]
    assert len(thread_recorder) == 1
    worker = thread_recorder[0]
    assert worker.target is pm_startup._heal_and_activate
    assert worker.name == "hermes-pm-autoheal"
    assert worker.daemon is True
    assert worker.started is True


def test_startup_failure_is_swallowed(monkeypatch):
    def _explode():
        raise RuntimeError("pm exploded")

    monkeypatch.setitem(sys.modules, "pm", SimpleNamespace(adopt=_explode))

    assert pm_startup.run_pm_startup() is None


@pytest.mark.parametrize("skipped", [None, "sealed", "lazy-installs-disabled"])
def test_heal_reuses_post_update_step_then_activates(monkeypatch, skipped):
    calls = []
    result = {"ok": True}
    if skipped is not None:
        result["skipped"] = skipped

    def _provision():
        calls.append("provision")
        return result

    monkeypatch.setitem(
        sys.modules, "pm", SimpleNamespace(activate=lambda: calls.append("activate"))
    )
    monkeypatch.setattr(
        "hermes_cli.post_update.step_provision_runtimes", _provision
    )

    pm_startup._heal_and_activate()

    assert calls == ["provision", "activate"]


def test_heal_failure_is_swallowed_and_does_not_skip_activation(monkeypatch):
    calls = []

    def _explode():
        raise RuntimeError("provisioning failed")

    monkeypatch.setitem(
        sys.modules, "pm", SimpleNamespace(activate=lambda: calls.append("activate"))
    )
    monkeypatch.setattr(
        "hermes_cli.post_update.step_provision_runtimes", _explode
    )

    pm_startup._heal_and_activate()

    assert calls == ["activate"]
