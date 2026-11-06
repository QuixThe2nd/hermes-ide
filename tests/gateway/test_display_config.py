"""Tests for gateway.display_config — per-platform display/verbosity resolver."""


# ---------------------------------------------------------------------------
# Resolver: resolution order
# ---------------------------------------------------------------------------

class TestToolProgressProvenance:
    def test_winning_source_controls_mode_and_intent(self):
        from gateway.display_config import resolve_tool_progress

        cases = [
            ({}, None, ("off", False)),
            ({}, "all", ("all", True)),
            ({"tool_progress": None}, "all", ("all", True)),
            ({"platforms": {"whatsapp_cloud": {"tool_progress": None}}}, "off", ("off", True)),
            ({"tool_progress_overrides": {"whatsapp_cloud": None}}, "new", ("new", True)),
            ({"tool_progress": False}, "all", ("off", True)),
            ({"tool_progress": "all", "platforms": {"whatsapp_cloud": {"tool_progress": None}}}, "off", ("all", True)),
            ({"tool_progress": "off", "tool_progress_overrides": {"whatsapp_cloud": "new"}}, "all", ("new", True)),
            ({"tool_progress_overrides": {"whatsapp_cloud": "off"}, "platforms": {"whatsapp_cloud": {"tool_progress": "all"}}}, None, ("all", True)),
        ]
        for display, env, expected in cases:
            assert resolve_tool_progress({"display": display}, "whatsapp_cloud", env) == expected


class TestResolveDisplaySetting:
    """resolve_display_setting() resolves with correct priority."""

    def test_explicit_platform_override_wins(self):
        """display.platforms.<plat>.<key> takes top priority."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "tool_progress": "all",
                "platforms": {
                    "telegram": {"tool_progress": "verbose"},
                },
            }
        }
        assert resolve_display_setting(config, "telegram", "tool_progress") == "verbose"

    def test_global_setting_when_no_platform_override(self):
        """Falls back to display.<key> when no platform override exists."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "tool_progress": "new",
                "platforms": {},
            }
        }
        assert resolve_display_setting(config, "telegram", "tool_progress") == "new"


    def test_platform_override_only_affects_that_platform(self):
        """Other platforms are unaffected by a specific platform override."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "tool_progress": "all",
                "platforms": {
                    "slack": {"tool_progress": "off"},
                },
            }
        }
        assert resolve_display_setting(config, "slack", "tool_progress") == "off"
        assert resolve_display_setting(config, "telegram", "tool_progress") == "all"


# ---------------------------------------------------------------------------
# Backward compatibility: tool_progress_overrides
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    """Legacy tool_progress_overrides is still respected as a fallback."""

    def test_legacy_overrides_read(self):
        """tool_progress_overrides is read when no platforms entry exists."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "tool_progress": "all",
                "tool_progress_overrides": {
                    "signal": "off",
                    "telegram": "verbose",
                },
            }
        }
        assert resolve_display_setting(config, "signal", "tool_progress") == "off"
        assert resolve_display_setting(config, "telegram", "tool_progress") == "verbose"


# ---------------------------------------------------------------------------
# YAML normalisation
# ---------------------------------------------------------------------------

class TestYAMLNormalisation:
    """YAML 1.1 quirks (bare off → False, on → True) are handled."""

    def test_tool_progress_false_normalised_to_off(self):
        """YAML's bare `off` parses as False — normalised to 'off' string."""
        from gateway.display_config import resolve_display_setting

        config = {"display": {"tool_progress": False}}
        assert resolve_display_setting(config, "telegram", "tool_progress") == "off"


    def test_only_long_running_visibility_accepts_generic_mode(self):
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "platforms": {
                    "whatsapp": {
                        "thinking_progress": "generic",
                        "interim_assistant_messages": "generic",
                        "long_running_notifications": "generic",
                    }
                }
            }
        }
        assert resolve_display_setting(config, "whatsapp", "thinking_progress") is False
        assert resolve_display_setting(config, "whatsapp", "interim_assistant_messages") is False
        assert resolve_display_setting(config, "whatsapp", "long_running_notifications") == "generic"

    def test_long_running_notifications_accepts_phase_mode(self):
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "platforms": {
                    "whatsapp": {
                        "thinking_progress": "phase",
                        "interim_assistant_messages": "phase",
                        "long_running_notifications": "phase",
                    }
                }
            }
        }
        assert resolve_display_setting(config, "whatsapp", "thinking_progress") is False
        assert resolve_display_setting(config, "whatsapp", "interim_assistant_messages") is False
        assert resolve_display_setting(config, "whatsapp", "long_running_notifications") == "phase"

    def test_thinking_progress_string_false_normalised_to_false(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"platforms": {"whatsapp": {"thinking_progress": "false"}}}}
        assert resolve_display_setting(config, "whatsapp", "thinking_progress") is False


# ---------------------------------------------------------------------------
# Built-in platform defaults (tier system)
# ---------------------------------------------------------------------------

class TestPlatformDefaults:
    """Built-in defaults reflect platform capability tiers."""

    def test_high_tier_platforms(self):
        """Discord (tier_high) defaults to 'all'; matrix (tier_medium) to 'new'."""
        from gateway.display_config import resolve_display_setting

        # Discord: pure tier_high.
        assert resolve_display_setting({}, "discord", "tool_progress") == "all"
        # Matrix: tier_medium — editing support, but quieter default.
        assert resolve_display_setting({}, "matrix", "tool_progress") == "new"


    def test_low_tier_platforms(self):
        """No-edit / batch platforms default to 'off' (or minimal) tool progress."""
        from gateway.display_config import resolve_display_setting

        for plat in ("whatsapp_cloud", "webhook"):
            assert resolve_display_setting({}, plat, "tool_progress") == "off", plat


    def test_discord_chatter_defaults(self):
        """Discord keeps interim commentary and busy-ack detail on
        (desktop-first, more vertical space), but its long-running
        heartbeat defaults to the phase line ("⏳ terminal 1m42s" /
        "⏳ grok-4.6 38s" / "⏳ packing 12s") rather than the raw
        diagnostic join."""
        from gateway.display_config import resolve_display_setting

        assert resolve_display_setting({}, "discord", "interim_assistant_messages") is True
        assert resolve_display_setting({}, "discord", "long_running_notifications") == "phase"
        assert resolve_display_setting({}, "discord", "busy_ack_detail") is True

    def test_discord_long_running_generic_still_resolves_when_configured(self):
        """Users can opt Discord back into the catalog phrase ("generic")."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "platforms": {"discord": {"long_running_notifications": "generic"}},
            }
        }
        assert (
            resolve_display_setting(config, "discord", "long_running_notifications")
            == "generic"
        )

    def test_webhook_batch_chatter_defaults(self):
        """Tier-minimal batch delivery (webhook) leaves no mid-turn breadcrumbs."""
        from gateway.display_config import resolve_display_setting

        assert resolve_display_setting({}, "webhook", "tool_progress") == "off"
        assert resolve_display_setting({}, "webhook", "long_running_notifications") is False
        assert resolve_display_setting({}, "webhook", "busy_ack_detail") is False


# ---------------------------------------------------------------------------
# Config migration: tool_progress_overrides → display.platforms
# ---------------------------------------------------------------------------

class TestConfigMigration:
    """Version 16 migration moves tool_progress_overrides into display.platforms."""

    def test_migration_creates_platforms_entries(self, tmp_path, monkeypatch):
        """Old overrides are migrated into display.platforms.<plat>.tool_progress."""
        import yaml

        config_path = tmp_path / "config.yaml"
        config = {
            "_config_version": 15,
            "display": {
                "tool_progress_overrides": {
                    "signal": "off",
                    "telegram": "all",
                },
            },
        }
        config_path.write_text(yaml.dump(config), encoding="utf-8")

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Re-import to pick up the new HERMES_HOME
        import importlib
        import hermes_cli.config as cfg_mod
        importlib.reload(cfg_mod)

        result = cfg_mod.migrate_config(interactive=False, quiet=True)
        # Re-read config
        updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        platforms = updated.get("display", {}).get("platforms", {})
        assert platforms.get("signal", {}).get("tool_progress") == "off"
        assert platforms.get("telegram", {}).get("tool_progress") == "all"


# ---------------------------------------------------------------------------
# Streaming per-platform (None = follow global)
# ---------------------------------------------------------------------------

class TestStreamingPerPlatform:
    """Streaming per-platform override semantics."""


    def test_explicit_false_disables(self):
        """Explicit False disables streaming for that platform."""
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "platforms": {"telegram": {"streaming": False}},
            }
        }
        assert resolve_display_setting(config, "telegram", "streaming") is False


# ---------------------------------------------------------------------------
# cleanup_progress — opt-in deletion of temporary progress bubbles
# ---------------------------------------------------------------------------

class TestCleanupProgress:
    """``cleanup_progress`` is off by default and resolvable per-platform."""

    def test_default_off_for_all_platforms(self):
        """No config set → cleanup_progress resolves to False everywhere."""
        from gateway.display_config import resolve_display_setting

        for plat in ("telegram", "discord", "slack", "email"):
            assert resolve_display_setting({}, plat, "cleanup_progress") is False


    def test_yaml_true_string_normalises_to_true(self):
        """String 'true'/'yes'/'on' all resolve to True."""
        from gateway.display_config import resolve_display_setting

        for val in ("true", "yes", "on", "1"):
            config = {
                "display": {
                    "platforms": {"telegram": {"cleanup_progress": val}},
                }
            }
            assert resolve_display_setting(config, "telegram", "cleanup_progress") is True, val


class TestToolProgressGrouping:
    """resolve_display_setting() for the tool_progress_grouping knob."""

    def test_default_is_accumulate(self):
        """No config anywhere → global default 'accumulate'."""
        from gateway.display_config import resolve_display_setting

        assert (
            resolve_display_setting({}, "telegram", "tool_progress_grouping")
            == "accumulate"
        )

    def test_global_separate(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"tool_progress_grouping": "separate"}}
        assert (
            resolve_display_setting(config, "discord", "tool_progress_grouping")
            == "separate"
        )


class TestReasoningStyle:
    """Per-platform reasoning render style (code | blockquote | subtext | compact)."""

    def test_discord_defaults_to_code(self):
        from gateway.display_config import resolve_display_setting

        assert resolve_display_setting({}, "discord", "reasoning_style") == "code"

    def test_other_platforms_default_to_code(self):
        from gateway.display_config import resolve_display_setting

        for plat in ("telegram", "slack", "matrix", "api_server"):
            assert (
                resolve_display_setting({}, plat, "reasoning_style") == "code"
            ), plat

    def test_compact_is_a_valid_style(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"reasoning_style": "compact"}}
        assert resolve_display_setting(config, "discord", "reasoning_style") == "compact"

    def test_unknown_style_still_coerces_to_code(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"reasoning_style": "summary"}}
        assert resolve_display_setting(config, "discord", "reasoning_style") == "code"


class TestLiveStatusSetting:
    """display.live_status — tri-state normalisation + platform overrides."""

    def test_default_is_full(self):
        from gateway.display_config import resolve_display_setting

        assert resolve_display_setting({}, "slack", "live_status") == "full"


