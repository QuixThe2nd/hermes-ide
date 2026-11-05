"""Regression tests for #100489 — desktop multiplex ticker must not deliver a
secondary profile's cron output through the default profile's identity.

Two halves:

1. ``_deliver_result``'s standalone fallback pool (taken when the caller has a
   RUNNING event loop — the desktop dashboard shape) spawns a fresh thread that
   did not inherit the profile ContextVars; it must run inside a copy of the
   active context so the sender reads THIS profile's home + secrets.
2. The multiplex ticker must stand down, per tick, for a profile whose OWN
   gateway is running — that gateway ticks it with live adapters.
"""
import asyncio
import threading
from unittest.mock import patch

import pytest



def test_standalone_fallback_pool_keeps_profile_scope(tmp_path, monkeypatch):
    from agent.secret_scope import (
        get_secret,
        set_multiplex_active,
        set_secret_scope,
    )
    from hermes_constants import get_hermes_home, set_hermes_home_override
    import cron.scheduler as sched
    import tools.send_message_tool as smt

    default_home = tmp_path / "default"
    sec_home = tmp_path / "profiles" / "ops"
    for home in (default_home, sec_home):
        (home / "cron").mkdir(parents=True)
        (home / "config.yaml").write_text("platforms:\n  telegram:\n    enabled: true\n")
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "DEFAULT-TOKEN")
    set_multiplex_active(True)

    seen = {}

    async def fake_send(platform, pconfig, chat_id, message, **kwargs):
        seen["home"] = str(get_hermes_home())
        seen["token"] = get_secret("TELEGRAM_BOT_TOKEN", None)
        return {"success": True, "message_id": "1"}

    job = {"id": "j1", "name": "probe", "deliver": "telegram:12345", "schedule": {"kind": "cron"}}

    async def _inside_running_loop():
        # Emulate the multiplex ticker's per-profile scope on the caller.
        set_hermes_home_override(str(sec_home))
        set_secret_scope({"TELEGRAM_BOT_TOKEN": "OPS-TOKEN"})
        return sched._deliver_result(job, "hello", adapters={}, loop=None)

    try:
        with patch.object(smt, "_send_to_platform", fake_send):
            err = asyncio.run(_inside_running_loop())
    finally:
        set_multiplex_active(False)

    assert err is None, err
    assert seen["home"] == str(sec_home.resolve())
    assert seen["token"] == "OPS-TOKEN"


def test_multiplex_ticker_profile_gate_skips_rejected_profile(tmp_path):
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_constants import get_hermes_home

    own_gateway = tmp_path / "own-gateway"
    orphan = tmp_path / "orphan"
    for home in (own_gateway, orphan):
        (home / "cron").mkdir(parents=True)

    stop = threading.Event()
    ticked: list[str] = []

    def _tick(*args, **kwargs):
        ticked.append(str(get_hermes_home()))
        if len(ticked) >= 3:
            stop.set()
        return 0

    provider = InProcessCronScheduler()
    with patch("cron.scheduler.tick", side_effect=_tick):
        thread = threading.Thread(
            target=provider.start,
            args=(stop,),
            kwargs={
                "interval": 0,
                "profile_homes": [("own-gateway", own_gateway), ("orphan", orphan)],
                "profile_gate": lambda name, home: name != "own-gateway",
            },
            daemon=True,
        )
        thread.start()
        thread.join(timeout=5)
        stop.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert set(ticked) == {str(orphan)}
    # The gated profile gets no tick-loop success marker either: its own
    # gateway owns that status surface.
    assert not (own_gateway / "cron" / "ticker_last_success").exists()
    assert (orphan / "cron" / "ticker_last_success").exists()
