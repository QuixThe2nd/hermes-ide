"""Multiplexed-gateway parity for what a TURN sees: a profile served by the default multiplexer
(``_profile_runtime_scope``) must observe the same tool-side policy its standalone gateway
(``HERMES_HOME=<profile>``) would — never the launch profile's values frozen into process caches or
read from the process env.

Every test warms the site under launch home A, then reads under routed profile B with different
config (real temp homes, real config.yaml, real terminal scope; no mocks of the thing under test).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def two_homes(tmp_path, monkeypatch):
    """Launch home A (HERMES_HOME) and routed profile B, differing in every setting under test."""
    a = tmp_path / ".hermes"
    b = a / "profiles" / "b"
    b.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(a))
    for name, home in (("a", a), ("b", b)):
        (home / f"cred_{name}.txt").write_text("x", encoding="utf-8")
        (home / "config.yaml").write_text(yaml.safe_dump({
            "terminal": {"backend": "local" if name == "a" else "docker",
                         "credential_files": [f"cred_{name}.txt"]},
            "security": {"redact_secrets": name == "b"},
            "browser": {"command_timeout": 11 if name == "a" else 17},
            "lsp": {"enabled": name == "b"},
        }), encoding="utf-8")
    return a, b


def _under(home: Path, fn):
    token = set_hermes_home_override(str(home))
    try:
        return fn()
    finally:
        reset_hermes_home_override(token)


def test_routed_local_profile_cwd_matches_standalone_gateway(tmp_path, two_homes):
    """A standalone gateway resolves an unset ``terminal.cwd`` on a local backend to ``$HOME`` at
    import; the routed profile's terminal scope must yield the same cwd, not the multiplexer's
    process cwd — otherwise the system prompt, context files and the terminal all start in
    wherever ``hermes gateway`` happened to be launched from."""
    from tools.terminal_scope import build_profile_terminal_scope, install_and_reset_profile_terminal_scope
    from agent.runtime_cwd import resolve_agent_cwd

    a, _ = two_homes
    assert build_profile_terminal_scope(a)["TERMINAL_CWD"] == str(tmp_path)
    with install_and_reset_profile_terminal_scope(a):
        assert resolve_agent_cwd() == tmp_path
    # Non-local backends stay unset (sandbox default), exactly like gateway/run.py's placeholder rule.
    assert "TERMINAL_CWD" not in build_profile_terminal_scope(two_homes[1])


def test_terminal_backend_consumers_read_the_routed_scope(two_homes):
    """Every ``TERMINAL_ENV`` reader that shapes a turn (image-source locality, credential-file path
    translation, image-gen cache base, skill readiness) resolves the ROUTED profile's backend."""
    from gateway.run import _profile_runtime_scope
    from tools import credential_files, image_generation_tool, image_source

    a, b = two_homes

    def observe():
        return (
            image_source._is_local_terminal_backend(),
            image_generation_tool._agent_cache_base_for_env(None),
            credential_files.to_agent_visible_cache_path("/host/.hermes/x.png", "/root/.hermes"),
            sorted(Path(m["host_path"]).name for m in credential_files.get_credential_file_mounts()),
        )

    with _profile_runtime_scope(a):
        assert observe() == (True, None, "/host/.hermes/x.png", ["cred_a.txt"])
    with _profile_runtime_scope(b):
        local, cache_base, translated, mounts = observe()
    assert local is False
    assert cache_base == "/root/.hermes"
    assert translated != "/host/.hermes/x.png" or mounts == ["cred_b.txt"]
    assert mounts == ["cred_b.txt"]



def test_profile_scoped_process_caches_follow_routed_home(two_homes, monkeypatch):
    """Config-derived singletons (redaction switch, aux unhealthy marks, LSP service,
    Camofox command timeout, MCP stderr log path) are keyed by routed profile home."""
    from agent import auxiliary_client, lsp, redact
    from tools import browser_camofox, mcp_tool_config

    a, b = two_homes
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)  # launch profile opted out
    redact._REDACT_ENABLED_BY_HOME.clear()
    token = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    assert redact.redact_sensitive_text(token) == token
    assert _under(b, lambda: redact.redact_sensitive_text(token)) != token

    auxiliary_client._reset_aux_unhealthy_cache()
    _under(a, lambda: auxiliary_client._mark_provider_unhealthy("openrouter"))
    assert _under(a, lambda: auxiliary_client._is_provider_unhealthy("openrouter")) is True
    assert _under(b, lambda: auxiliary_client._is_provider_unhealthy("openrouter")) is False

    lsp.shutdown_service()
    try:
        assert _under(a, lsp.get_service) is None
        assert _under(b, lsp.get_service) is not None
    finally:
        lsp.shutdown_service()

    browser_camofox._cached_cmd_timeout = None
    browser_camofox._cmd_timeout_resolved = False
    assert _under(a, browser_camofox._get_command_timeout) == 11
    assert _under(b, browser_camofox._get_command_timeout) == 17
    assert _under(a, browser_camofox._get_command_timeout) == 11

    mcp_tool_config._mcp_stderr_log_fh.clear()
    try:
        assert Path(_under(a, mcp_tool_config._get_mcp_stderr_log).name).parent == a / "logs"
        assert Path(_under(b, mcp_tool_config._get_mcp_stderr_log).name).parent == b / "logs"
    finally:
        for fh in mcp_tool_config._mcp_stderr_log_fh.values():
            fh.close()
        mcp_tool_config._mcp_stderr_log_fh.clear()
