"""Config normalization + plugin enablement contracts."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest
import yaml

from plugins.pr_intent_watch.core import (
    DEFAULT_LISTEN_HOST,
    DEFAULT_LISTEN_PORT,
    DEFAULT_REPO,
    DEFAULT_WEBHOOK_PATH,
    PrIntentWatchError,
    load_config_section,
    load_watch_config,
    watch_config_from_raw,
)
from plugins.pr_intent_watch.lifecycle import plugin_explicitly_disabled


def test_defaults_when_section_absent():
    config = watch_config_from_raw({})
    assert config.enabled is True
    assert config.repo == DEFAULT_REPO == "QuixThe2nd/hermes-ide"
    assert config.poll_seconds == 300
    assert config.skip_drafts is False
    assert config.skip_authors == ()
    assert config.comment is True
    assert config.max_file_names == 40
    assert config.max_commits == 20
    assert config.listen_host == DEFAULT_LISTEN_HOST == "0.0.0.0"
    assert config.listen_port == DEFAULT_LISTEN_PORT == 8645
    assert config.webhook_path == DEFAULT_WEBHOOK_PATH == "/webhooks/pr-intent-watch"


def test_defaults_when_raw_is_not_a_mapping():
    assert watch_config_from_raw(None).repo == DEFAULT_REPO
    assert watch_config_from_raw("junk").enabled is True  # type: ignore[arg-type]


def test_explicit_values_are_kept():
    config = watch_config_from_raw(
        {
            "pr_intent_watch": {
                "enabled": True,
                "repo": "someone/else-repo",
                "poll_seconds": 900,
                "skip_drafts": True,
                "skip_authors": ["Dependabot[bot]", "  renovate  "],
                "comment": False,
                "max_file_names": 10,
                "max_commits": 5,
                "listen_host": "127.0.0.1",
                "listen_port": 9000,
                "webhook_path": "/hooks/pr-intent",
            }
        }
    )
    assert config.repo == "someone/else-repo"
    assert config.poll_seconds == 900
    assert config.skip_drafts is True
    assert config.skip_authors == ("dependabot[bot]", "renovate")  # lowercased
    assert config.comment is False
    assert config.max_file_names == 10
    assert config.max_commits == 5
    assert config.listen_host == "127.0.0.1"
    assert config.listen_port == 9000
    assert config.webhook_path == "/hooks/pr-intent"


def test_invalid_types_fall_back_to_defaults():
    config = watch_config_from_raw(
        {
            "pr_intent_watch": {
                "enabled": "yes",  # type: ignore[dict-item]
                "repo": 42,  # type: ignore[dict-item]
                "poll_seconds": "300",  # type: ignore[dict-item]
                "skip_drafts": "true",  # type: ignore[dict-item]
                "skip_authors": "dependabot",  # type: ignore[dict-item]
                "comment": 1,  # type: ignore[dict-item]
                "max_file_names": "40",  # type: ignore[dict-item]
                "max_commits": None,  # type: ignore[dict-item]
                "listen_host": 7,  # type: ignore[dict-item]
                "listen_port": "8645",  # type: ignore[dict-item]
                "webhook_path": 12,  # type: ignore[dict-item]
            }
        }
    )
    assert config.enabled is True
    assert config.repo == DEFAULT_REPO  # non-str → default (42 has no "/")
    assert config.poll_seconds == 300
    assert config.skip_drafts is False
    assert config.skip_authors == ()
    assert config.comment is True
    assert config.max_file_names == 40
    assert config.max_commits == 20
    assert config.listen_host == DEFAULT_LISTEN_HOST
    assert config.listen_port == DEFAULT_LISTEN_PORT
    assert config.webhook_path == DEFAULT_WEBHOOK_PATH


# ── listener settings ───────────────────────────────────────────────────────


def test_listen_port_is_clamped_into_the_valid_range():
    assert watch_config_from_raw({"pr_intent_watch": {"listen_port": 0}}).listen_port == 1
    assert (
        watch_config_from_raw({"pr_intent_watch": {"listen_port": -5}}).listen_port == 1
    )
    assert (
        watch_config_from_raw({"pr_intent_watch": {"listen_port": 70000}}).listen_port
        == 65535
    )
    assert watch_config_from_raw({"pr_intent_watch": {"listen_port": 1}}).listen_port == 1
    assert (
        watch_config_from_raw({"pr_intent_watch": {"listen_port": 65535}}).listen_port
        == 65535
    )


def test_listen_port_bool_is_not_an_int():
    # bool is an int subclass — True must not become port 1.
    assert (
        watch_config_from_raw({"pr_intent_watch": {"listen_port": True}}).listen_port
        == DEFAULT_LISTEN_PORT
    )


def test_webhook_path_requires_a_leading_slash():
    # A path GitHub can never POST to is a typo, not an address.
    assert (
        watch_config_from_raw({"pr_intent_watch": {"webhook_path": "hooks/pr"}}).webhook_path
        == DEFAULT_WEBHOOK_PATH
    )
    assert (
        watch_config_from_raw({"pr_intent_watch": {"webhook_path": ""}}).webhook_path
        == DEFAULT_WEBHOOK_PATH
    )
    assert (
        watch_config_from_raw({"pr_intent_watch": {"webhook_path": "/webhooks/pr"}}).webhook_path
        == "/webhooks/pr"
    )


def test_poll_seconds_floored_at_sixty():
    assert watch_config_from_raw({"pr_intent_watch": {"poll_seconds": 5}}).poll_seconds == 60
    assert watch_config_from_raw({"pr_intent_watch": {"poll_seconds": 60}}).poll_seconds == 60


def test_repo_without_owner_slash_name_falls_back():
    assert watch_config_from_raw({"pr_intent_watch": {"repo": "hermes-ide"}}).repo == DEFAULT_REPO
    assert watch_config_from_raw({"pr_intent_watch": {"repo": ""}}).repo == DEFAULT_REPO


def test_section_enabled_false_is_respected():
    config = watch_config_from_raw({"pr_intent_watch": {"enabled": False}})
    assert config.enabled is False


def test_load_watch_config_from_file(tmp_path):
    from tests.plugins.pr_intent_watch._helpers import write_config

    path = write_config(tmp_path, {"poll_seconds": 1200})
    assert load_watch_config(path).poll_seconds == 1200
    assert load_watch_config(path).enabled is True  # absent key → default


def test_load_config_section_missing_file_raises(tmp_path):
    with pytest.raises(PrIntentWatchError):
        load_config_section(tmp_path / "nope.yaml")


def test_load_config_section_invalid_yaml_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("pr_intent_watch: [unclosed", encoding="utf-8")
    with pytest.raises(PrIntentWatchError):
        load_config_section(path)


def test_plugin_disable_forms():
    # Explicit section flag (either nesting) and plugins.disabled both win.
    assert plugin_explicitly_disabled({"pr_intent_watch": {"enabled": False}})
    assert plugin_explicitly_disabled(
        {"plugins": {"pr_intent_watch": {"enabled": False}}}
    )
    assert plugin_explicitly_disabled({"plugins": {"disabled": ["pr_intent_watch"]}})
    # Not disabled: absent, true, truthy-but-not-bool, other names.
    assert not plugin_explicitly_disabled({})
    assert not plugin_explicitly_disabled(None)
    assert not plugin_explicitly_disabled({"pr_intent_watch": {"enabled": True}})
    assert not plugin_explicitly_disabled({"pr_intent_watch": {"enabled": "false"}})
    assert not plugin_explicitly_disabled({"plugins": {"disabled": ["other"]}})


# ── Plugin discovery: bundled + default_enabled, no warnings ────────────────


def _bundled_copy(tmp_path: Path) -> Path:
    """A bundled-plugins dir containing only pr_intent_watch (fast discovery)."""
    repo_plugins = Path(__file__).resolve().parents[3] / "plugins"
    bundled = tmp_path / "bundled-plugins"
    bundled.mkdir()
    shutil.copytree(repo_plugins / "pr_intent_watch", bundled / "pr_intent_watch")
    return bundled


def test_manifest_declares_default_enabled(tmp_path):
    manifest = yaml.safe_load(
        (_bundled_copy(tmp_path) / "pr_intent_watch" / "plugin.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["name"] == "pr_intent_watch"
    assert manifest["default_enabled"] is True
    assert manifest["kind"] == "backend"
    assert manifest.get("provides_tools") is None  # no model tools, ever


def test_bundled_plugin_loads_enabled_without_warnings(tmp_path, monkeypatch, caplog):
    from hermes_cli.plugins import PluginManager

    home = tmp_path / "hermes_home"
    (home / "plugins").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(_bundled_copy(tmp_path)))

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        manager = PluginManager()
        manager.discover_and_load()

    loaded = manager._plugins.get("pr_intent_watch")
    assert loaded is not None, f"plugin not discovered: {sorted(manager._plugins)}"
    assert loaded.enabled, f"bundled default_enabled plugin disabled: {loaded.error}"
    assert loaded.error is None
    assert "pr_intent_watch" not in caplog.text
