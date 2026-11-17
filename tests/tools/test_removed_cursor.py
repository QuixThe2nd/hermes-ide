"""Regression contract for the removed Cursor (cursor.com) integration.

The ``delegate_cursor_agent`` tool, the ``backend="cursor"`` dispatch path,
the ``cursor`` lane harness, and every Cursor Cloud Agents / My Machines
module were deleted. These assertions pin the removal end to end: the cursor
modules are gone, no cursor tool is registered or toolset-listed, the
delegate schema no longer offers the cursor backend, the cursor harness is
rejected, and leftover user-config keys from installs that used Cursor stay
loadable and inert (they must never crash config load or the quota plugin).
"""

import pathlib
import pytest


def test_cursor_tool_modules_are_gone():
    """Deleting the modules is the contract; no shim may re-add them.

    Checked by file absence in this checkout rather than by import failure:
    a developer worktree sharing the main checkout's venv resolves the main
    checkout's editable install for deleted names, so an import probe is
    environment-dependent.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    for rel in (
        "tools/cursor_agent_tool.py",
        "tools/cursor_run_receipts.py",
        "tools/cursor_artifact_egress.py",
    ):
        assert not (repo_root / rel).exists(), rel


def test_delegate_cursor_agent_not_registered():
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    assert registry.get_entry("delegate_cursor_agent") is None
    names = {entry.name for entry in registry.get_all_entries()}
    assert "delegate_cursor_agent" not in names
    # The delegation surfaces that remain.
    assert "delegate_agent" in names
    assert "delegate_claude_agent" in names


def test_delegate_cursor_agent_in_no_toolset():
    from toolsets import TOOLSETS

    for toolset, tools in TOOLSETS.items():
        assert "delegate_cursor_agent" not in tools, toolset


def test_delegate_agent_backend_enum_has_no_cursor():
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    entry = registry.get_entry("delegate_agent")
    assert entry is not None
    schema = entry.schema
    backends = schema["parameters"]["properties"]["backend"]["enum"]
    assert "cursor" not in backends
    assert "subagent" in backends
    assert "claude" in backends


def test_cursor_harness_removed_but_existing_harnesses_stay():
    from tools.delegate_lane_specs import HARNESSES, resolve_lane_spec

    assert "cursor" not in HARNESSES
    for harness in ("omp", "claude", "subagent"):
        assert harness in HARNESSES, harness
    # A config left with harness: cursor gets the clear unsupported-harness
    # validation error, not a crash or a silent fallback.
    with pytest.raises(ValueError, match="unsupported harness"):
        resolve_lane_spec("backend_dev", {"backend_dev": {"harness": "cursor"}})


def test_leftover_cursor_config_keys_load_and_stay_inert():
    """Configs written while Cursor existed must load; the dead cursor
    quota provider must resolve to nothing instead of raising."""
    import yaml

    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "timeouts": {
                    "tools": {
                        "unbounded_tools": [
                            "delegate_agent",
                            "delegate_cursor_agent",
                        ]
                    }
                },
                "quota_channels": {
                    "guild_id": "111111",
                    "category_id": "222222",
                    "channel_ids": {"cursor": "333333", "codex": "444444"},
                    "enabled_providers": {"cursor": True, "codex": True},
                },
            }
        ),
        encoding="utf-8",
    )

    from hermes_cli.config import load_config

    config = load_config()  # must not raise
    # The stale unbounded_tools row survives verbatim; it simply names a tool
    # that no longer exists, so it exempts nothing.
    assert "delegate_cursor_agent" in config["timeouts"]["tools"]["unbounded_tools"]

    from plugins.quota_channels.core import validate_quota_config

    resolved = validate_quota_config(config["quota_channels"])
    provider_keys = {key for key, _label, _cid in resolved["providers"]}
    assert "cursor" not in provider_keys
    assert "codex" in provider_keys
