"""Code-flow grid sweep (NOUS-368, wave 1): automatic gateway diagnostics that bypassed the
warning boundary. absent/false = legacy bytes; true suppresses ONLY the diagnostic; requested
outcomes (the requested background result) are untouched.

The shutdown-notice and unauthorized-owner-hint grid arms were removed with the home-channel
feature: ``HomeChannel``/``_home_channel_transports`` no longer exist and the ⚠️ pings to active
sessions are deliberately NOT gated by ``display.suppress_warning_notifications`` (only the
notification-channel broadcast is, via ``gateway_restart_notification`` / the drain marker).
The in-chat restart ack contract lives in ``test_gateway_shutdown.py``."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.session import SessionSource


def _configure(tmp_path, monkeypatch, setting):
    home = tmp_path / f"home-{setting}"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    display = {} if setting is None else {"suppress_warning_notifications": setting}
    (home / "config.yaml").write_text(json.dumps({"display": display}))
    return home


MODES = (None, False, True)


def _bg_runner():
    from tests.gateway.test_background_command import _make_runner
    return _make_runner()


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", MODES)
async def test_background_task_failure_notice_bypasses_policy(tmp_path, monkeypatch, setting):
    _configure(tmp_path, monkeypatch, setting)
    runner = _bg_runner()
    from gateway.platforms.base import BasePlatformAdapter
    adapter = MagicMock(spec=BasePlatformAdapter)
    adapter.send = AsyncMock()
    adapter.emit_warning = BasePlatformAdapter.emit_warning.__get__(adapter, BasePlatformAdapter)
    adapter.warning_notifications_enabled = BasePlatformAdapter.warning_notifications_enabled.__get__(adapter, BasePlatformAdapter)
    adapter.platform = Platform.DISCORD
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="1", chat_id="c1", user_name="u")

    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "k"}), \
         patch.object(runner, "_resolve_session_agent_runtime", side_effect=RuntimeError("BG_REAL_CRASH")):
        await runner._run_background_task("do the thing", source, "bg_test")

    # #569 resolved the diverged _run_background_task_inner pair facade-wins: the failure
    # notice is a plain adapter.send, deliberately NOT gated by suppress_warning_notifications.
    adapter.send.assert_called_once()
    args, kwargs = adapter.send.call_args
    content = kwargs.get("content") or (args[1] if len(args) > 1 else "")
    assert "❌ Background task bg_test failed: BG_REAL_CRASH" in content


@pytest.mark.asyncio
async def test_background_task_requested_result_is_never_suppressed(tmp_path, monkeypatch):
    """Control: a successful background task's requested result is delivered under true."""
    _configure(tmp_path, monkeypatch, True)
    from tests.gateway import test_background_command as tbc
    # Reuse the existing success-path test body under the suppressing config.
    t = tbc.TestRunBackgroundTask()
    await t.test_successful_task_sends_result()
