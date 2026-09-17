"""Regression: background tasks respect profile secret scope when multiplexing.

Issue #60726: /bg spawns _run_background_task as a fire-and-forget
asyncio task with no profile scope, so _resolve_session_agent_runtime()'s
credential reads raise UnscopedSecretError when multiplex_profiles is on.
The fix wraps the task body in _profile_runtime_scope, mirroring _run_agent.
"""
import asyncio
from pathlib import Path
from unittest import mock

from agent import secret_scope
from gateway.config import GatewayConfig
from gateway.run import GatewayRunner


def _make_runner(multiplex: bool) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=multiplex)
    return runner


class TestBackgroundTaskProfileScope:
    """_run_background_task installs _profile_runtime_scope when multiplexing is active."""

    def test_wraps_in_profile_scope_when_multiplex_active(self):
        runner = _make_runner(multiplex=True)
        inner = mock.AsyncMock(return_value=None)
        runner._run_background_task_inner = inner

        source = mock.MagicMock()
        source.profile = "test_profile"

        with mock.patch.object(
            GatewayRunner,
            "_resolve_profile_home_for_source",
            return_value=Path("/fake/profile"),
        ), mock.patch("gateway.run._profile_runtime_scope") as scope:
            scope.return_value.__enter__ = mock.MagicMock()
            scope.return_value.__exit__ = mock.MagicMock(return_value=False)
            asyncio.run(
                runner._run_background_task(
                    prompt="test", source=source, task_id="bg_test"
                )
            )

        scope.assert_called_once_with(Path("/fake/profile"))
        inner.assert_awaited_once()


def test_standalone_gateway_binds_default_scope_after_hosted_activation(
    tmp_path, monkeypatch
):
    """Regression for #112878: hosted rooms can activate fail-closed scopes globally.

    A gateway configured without multiplexing must still bind its default profile
    when a hosted room has already activated the process-wide secret guard.
    """
    from tui_gateway import launch_profile_policy

    home = tmp_path / "default"
    home.mkdir()
    (home / ".env").write_text("OPENAI_API_KEY=default-key\n", encoding="utf-8")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)

    runner = _make_runner(multiplex=False)
    source = mock.MagicMock()
    with mock.patch.object(runner, "_resolve_profile_home_for_source", return_value=home):
        launch_profile_policy.activate_multi_profile_hosting()
        with runner._profile_scope_for_source(source):
            assert secret_scope.get_secret("OPENAI_API_KEY") == "default-key"

