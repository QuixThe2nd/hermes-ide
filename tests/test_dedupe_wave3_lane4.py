"""Structural and behavioral contracts for wave-3 lane-4 deduplication.

Every manifest group is consolidated, so there are no sanctioned AST-pinned
exceptions. Identity assertions reject local wrapper bodies as well as stale
copies; partials are permitted only when they call the canonical function.
"""

from functools import partial
import importlib
import inspect

import pytest


# (manifest symbol, canonical definition, consuming compatibility surfaces)
_GROUPS = (
    ("_windows_runtime_self_lock", "hermes_cli.runtime_repair:_windows_runtime_self_lock", ("hermes_cli.managed_uv:_windows_runtime_self_lock",)),
    ("_sweep_stale_runtime_backups", "hermes_cli.runtime_repair:_sweep_stale_runtime_backups", ("hermes_cli.managed_uv:_sweep_stale_runtime_backups",)),
    ("_classify_is_enabled_probe", "plugins.auto_update.systemd:_classify_is_enabled_probe", ("plugins.drift_watch.systemd:_classify_is_enabled_probe",)),
    ("_classify_probe", "plugins.llm_usage_proxy.systemd:_classify_probe", ("plugins.claude_viewer.systemd:_classify_probe",)),
    ("managed_gateway_auth_headers", "tools.managed_tool_gateway:managed_gateway_auth_headers", ("tools.managed_gateway_auth:managed_gateway_auth_headers",)),
    ("_classify_is_active_probe", "plugins.auto_update.systemd:_classify_is_active_probe", ("plugins.drift_watch.systemd:_classify_is_active_probe",)),
    ("_default_live_venv", "hermes_cli.runtime_repair:_default_live_venv", ("hermes_cli.managed_uv:_default_live_venv",)),
    ("_reload_hermes_constants", "hermes_cli.runtime_repair:_reload_hermes_constants", ("hermes_cli.managed_uv:_reload_hermes_constants",)),
    ("_safe_getcwd", "tools.terminal_tool_config:_safe_getcwd", ("tools.terminal_tool:_safe_getcwd",)),
    ("write_units_if_changed", "plugins.auto_update.systemd:write_units_if_changed", ("plugins.drift_watch.systemd:write_units_if_changed",)),
    ("_reconcile_probe_fields", "plugins.auto_update.systemd:_reconcile_probe_fields", ("plugins.drift_watch.systemd:_reconcile_probe_fields",)),
    ("disable_timer", "plugins.auto_update.systemd:disable_timer", ("plugins.drift_watch.systemd:disable_timer",)),
    ("_report_runtime_repair_failure", "hermes_cli.runtime_repair:_report_runtime_repair_failure", ("hermes_cli.managed_uv:_report_runtime_repair_failure",)),
    ("_systemd_quote", "plugins.auto_update.systemd:_systemd_quote", ("plugins.llm_usage_proxy.systemd:_systemd_quote", "plugins.claude_viewer.systemd:_systemd_quote", "plugins.drift_watch.systemd:_systemd_quote")),
    ("_rename_with_retry", "hermes_cli.runtime_repair:_rename_with_retry", ("hermes_cli.managed_uv:_rename_with_retry",)),
    ("_tenv", "tools.terminal_tool_config:_tenv", ("tools.terminal_tool:_tenv",)),
    ("check_requirements", "plugins.hermes_starts:check_requirements", ("plugins.discord_guests:check_requirements",)),
    ("default_systemctl_runner", "plugins.auto_update.systemd:default_systemctl_runner", ("plugins.drift_watch.systemd:default_systemctl_runner",)),
    ("_in_delegated_child_context", "tools.terminal_tool_sudo:_in_delegated_child_context", ("tools.terminal_tool:_in_delegated_child_context",)),
    ("_safe_command_preview", "tools.terminal_tool_guards:_safe_command_preview", ("tools.terminal_tool:_safe_command_preview",)),
    ("user_linger_enabled", "plugins.auto_update.systemd:user_linger_enabled", ("plugins.drift_watch.systemd:user_linger_enabled",)),
    ("_looks_like_help_or_version_command", "tools.terminal_tool_guards:_looks_like_help_or_version_command", ("tools.terminal_tool:_looks_like_help_or_version_command",)),
    ("_read_discord_token", "plugins.hermes_starts:_read_discord_token", ("plugins.discord_guests:_read_discord_token",)),
    ("linger_warning", "plugins.auto_update.systemd:linger_warning", ("plugins.drift_watch.systemd:linger_warning",)),
    ("_set_cached_sudo_password", "tools.terminal_tool_sudo:_set_cached_sudo_password", ("tools.terminal_tool:_set_cached_sudo_password",)),
    ("_clamp_timeout_seconds", "tools.agent_cli_runner:_clamp_timeout_seconds", ("tools.claude_agent_tool:_clamp_timeout_seconds", "tools.cursor_agent_tool:_clamp_timeout_seconds",)),
    ("_coerce_bool", "plugins.auto_update.config:_coerce_bool", ("plugins.llm_usage_proxy.config:_coerce_bool", "plugins.fallback_watch.core:_coerce_bool", "plugins.claude_viewer.config:_coerce_bool", "plugins.drift_watch.config:_coerce_bool")),
    ("register", "plugins.fallback_watch:register", ("plugins.home_server:register",)),
    ("probe_timer_is_active", "plugins.auto_update.systemd:probe_timer_is_active", ("plugins.drift_watch.systemd:probe_timer_is_active",)),
    ("probe_timer_is_enabled", "plugins.auto_update.systemd:probe_timer_is_enabled", ("plugins.drift_watch.systemd:probe_timer_is_enabled",)),
    ("_venv_python", "hermes_cli.runtime_repair:_venv_python", ("hermes_cli.managed_uv:_venv_python",)),
    ("_remove_tree", "hermes_cli.runtime_repair:_remove_tree", ("hermes_cli.managed_uv:_remove_tree",)),
    ("_check_interrupted", "tools.agent_cli_runner:_check_interrupted", ("tools.cursor_agent_tool:_check_interrupted",)),
    ("_load_config_mapping", "plugins.speed_channels.tool:_load_config_mapping", ("plugins.quota_channels.tool:_load_config_mapping",)),
    ("_looks_like_env_assignment", "tools.terminal_tool_sudo:_looks_like_env_assignment", ("tools.terminal_tool:_looks_like_env_assignment",)),
    ("_coerce_int", "plugins.auto_update.config:_coerce_int", ("plugins.fallback_watch.core:_coerce_int", "plugins.drift_watch.config:_coerce_int")),
    ("atomic_write", "plugins.auto_update.systemd:atomic_write", ("plugins.drift_watch.systemd:atomic_write",)),
    ("expected_timer_disable_argv", "plugins.auto_update.systemd:expected_timer_disable_argv", ("plugins.drift_watch.systemd:expected_timer_disable_argv",)),
)


def _resolve(reference: str):
    module_name, attribute = reference.split(":", 1)
    return getattr(importlib.import_module(module_name), attribute)


@pytest.mark.parametrize(
    ("symbol", "canonical_reference", "consumer_references"),
    _GROUPS,
    ids=[group[0] for group in _GROUPS],
)
def test_manifest_group_has_one_definition(
    symbol: str,
    canonical_reference: str,
    consumer_references: tuple[str, ...],
) -> None:
    canonical = _resolve(canonical_reference)
    assert inspect.isfunction(canonical), f"{symbol}: canonical owner is not a function"
    for reference in consumer_references:
        consumer = _resolve(reference)
        if consumer is canonical:
            continue
        assert isinstance(consumer, partial), f"{symbol}: local definition remains at {reference}"
        assert consumer.func is canonical, f"{symbol}: partial bypasses canonical owner at {reference}"


def test_drift_watch_binds_its_own_unit_names(tmp_path) -> None:
    from plugins.auto_update.platform import InstallScope
    from plugins.drift_watch import systemd as drift

    scope = InstallScope(False, tmp_path, ("systemctl", "--user"))
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "active\n", ""

    assert drift.probe_timer_is_active(scope, run_systemctl=runner).as_bool
    assert calls == [["systemctl", "--user", "is-active", drift.TIMER_NAME]]
    assert drift.expected_timer_disable_argv(scope) == [
        ["systemctl", "--user", "stop", drift.TIMER_NAME],
        ["systemctl", "--user", "disable", drift.TIMER_NAME],
    ]
    assert drift.write_units_if_changed(
        scope, service_body="service\n", timer_body="timer\n"
    )
    assert (tmp_path / drift.SERVICE_NAME).read_text(encoding="utf-8") == "service\n"
    assert (tmp_path / drift.TIMER_NAME).read_text(encoding="utf-8") == "timer\n"
    assert not drift.write_units_if_changed(
        scope, service_body="service\n", timer_body="timer\n"
    )


def test_probe_result_types_are_shared() -> None:
    from plugins.auto_update import systemd as auto
    from plugins.claude_viewer import systemd as viewer
    from plugins.drift_watch import systemd as drift
    from plugins.llm_usage_proxy import systemd as proxy

    assert drift.ProbeOutcome is auto.ProbeOutcome
    assert drift.ProbeResult is auto.ProbeResult
    assert viewer.ProbeOutcome is proxy.ProbeOutcome
    assert viewer.ProbeResult is proxy.ProbeResult


def test_gateway_auth_preserves_connector_trust_and_exact_origins() -> None:
    from tools import managed_gateway_auth as auth
    from tools import managed_tool_gateway as gateway

    def builder(vendor: str) -> str:
        return f"https://{vendor}.example.test"

    tokens = iter(("first-token", "second-token"))
    reader = lambda: next(tokens)
    connector_url = "https://connector.example.test/v1"
    assert auth.managed_gateway_auth_headers(connector_url, builder, reader) == {
        "Authorization": "Bearer first-token"
    }
    assert auth.managed_gateway_auth_headers(connector_url, builder, reader) == {
        "Authorization": "Bearer second-token"
    }
    assert gateway.managed_gateway_auth_headers(
        connector_url, builder, lambda: "must-not-be-used"
    ) == {}
    for untrusted in (
        "http://connector.example.test",
        "https://connector.example.test.attacker.invalid",
        "https://connector.example.test@attacker.invalid",
    ):
        assert auth.managed_gateway_auth_headers(
            untrusted, builder, lambda: "must-not-be-used"
        ) == {}


def test_agent_timeout_bounds_remain_tool_specific() -> None:
    from tools import claude_agent_tool as claude
    from tools import cursor_agent_tool as cursor

    assert claude._clamp_timeout_seconds(9999) == 3600
    assert cursor._clamp_timeout_seconds(9999) == 1800
    assert claude._clamp_timeout_seconds(1) == 60
    assert cursor._clamp_timeout_seconds(1) == 60
    for clamp in (claude._clamp_timeout_seconds, cursor._clamp_timeout_seconds):
        assert clamp(0) == 0
        assert clamp(-1) == 0
        assert clamp("invalid") == 0


def test_sudo_password_cache_is_shared_and_session_scoped(monkeypatch) -> None:
    from tools import terminal_tool as terminal
    from tools import terminal_tool_sudo as sudo

    current = {"session": "lane4-a"}
    monkeypatch.setattr(terminal, "_current_session_key", lambda: current["session"])
    terminal._reset_cached_sudo_passwords()
    try:
        terminal._set_cached_sudo_password("placeholder-a")
        assert sudo._get_cached_sudo_password() == "placeholder-a"
        current["session"] = "lane4-b"
        assert terminal._get_cached_sudo_password() == ""
        sudo._set_cached_sudo_password("placeholder-b")
        assert terminal._get_cached_sudo_password() == "placeholder-b"
        current["session"] = "lane4-a"
        assert sudo._get_cached_sudo_password() == "placeholder-a"
        terminal._reset_cached_sudo_passwords()
        assert sudo._get_cached_sudo_password() == ""
    finally:
        sudo._reset_cached_sudo_passwords()
