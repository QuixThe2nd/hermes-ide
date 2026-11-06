"""Per-profile isolation for the remaining process-global sinks under gateway.multiplex_profiles:
the config ``terminal.env_passthrough`` allowlist must follow the active profile."""
from __future__ import annotations

import os

from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _under_secondary(home, fn):
    set_multiplex_active(True)
    home_token = set_hermes_home_override(str(home))
    secret_token = set_secret_scope({})
    try:
        return fn()
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)
        set_multiplex_active(False)


def test_env_passthrough_allowlist_follows_the_active_profile(tmp_path, monkeypatch):
    import tools.env_passthrough as ep

    default_home = tmp_path / "hermes"
    secondary = default_home / "profiles" / "b2"
    secondary.mkdir(parents=True)
    (default_home / "config.yaml").write_text("terminal:\n  env_passthrough: [FOO_DEFAULT]\n")
    (secondary / "config.yaml").write_text("terminal:\n  env_passthrough: [FOO_B2]\n")
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    ep._config_passthrough.clear()

    assert ep._load_config_passthrough() == {"FOO_DEFAULT"}
    assert _under_secondary(secondary, ep._load_config_passthrough) == {"FOO_B2"}
    assert ep._load_config_passthrough() == {"FOO_DEFAULT"}
