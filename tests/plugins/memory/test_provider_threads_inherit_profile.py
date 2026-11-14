"""Multiplex invariant: every memory provider's background thread runs under the spawner's profile.

Profile isolation is a ContextVar-scoped HERMES_HOME override; a plain ``threading.Thread`` starts
with an EMPTY context, so a provider's prefetch/sync/writer thread would silently resolve the DEFAULT
profile's home (and fail closed on scoped secrets). Each case drives the provider's real spawn path
with a fake backend and asserts the thread saw the parent's home.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


def _probe_home(seen: dict, key: str = "home"):
    def _record(*_args, **_kwargs):
        seen[key] = get_hermes_home()
    return _record


def _hindsight(seen, tmp_path):
    import plugins.memory.hindsight as hindsight

    p = hindsight.HindsightMemoryProvider()
    p._recall_sync, p._memory_mode, p._auto_recall, p._prefetch_waits_for_retain = False, "hybrid", True, False
    p._do_recall = lambda query: (seen.setdefault("home", get_hermes_home()) and "text", 1)
    p.queue_prefetch("remember this")
    return [p._prefetch_thread]


_PROVIDERS = {
    "hindsight": _hindsight,
}


@pytest.mark.parametrize("name", sorted(_PROVIDERS))
def test_provider_background_thread_sees_spawner_profile_home(name, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "default"))
    profile_home = tmp_path / "profiles" / "b"
    profile_home.mkdir(parents=True)
    seen: dict = {}
    token = set_hermes_home_override(profile_home)
    try:
        threads = _PROVIDERS[name](seen, tmp_path)
    finally:
        reset_hermes_home_override(token)
    for t in threads:
        if t is not None:
            t.join(timeout=10)
    assert seen.get("home") == profile_home, f"{name}: background thread resolved {seen.get('home')}"
