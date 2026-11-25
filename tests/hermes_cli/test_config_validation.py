"""Tests for config.yaml structure validation (validate_config_structure)."""


import pytest

from hermes_cli.config import (
    DEFAULT_CONFIG,
    _EXTRA_KNOWN_ROOT_KEYS,
    _KNOWN_ROOT_KEYS,
    validate_config_structure,
    ConfigIssue,
)


class TestCustomProvidersValidation:
    """custom_providers must be a YAML list, not a dict."""

    def test_dict_instead_of_list(self):
        """The exact Discord user scenario — custom_providers as flat dict."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "Generativelanguage.googleapis.com",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "xxx",
                "model": "models/gemini-2.5-flash",
                "rate_limit_delay": 2.0,
                "fallback_model": {
                    "provider": "openrouter",
                    "model": "qwen/qwen3.6-plus:free",
                },
            },
            "fallback_providers": [],
        })
        errors = [i for i in issues if i.severity == "error"]
        assert any("dict" in i.message and "list" in i.message for i in errors), (
            "Should detect custom_providers as dict instead of list"
        )

    def test_scalar_is_an_error_naming_key_and_type(self):
        """A non-list scalar (a bad `config set`) makes every endpoint vanish — name the key and the type."""
        issues = validate_config_structure({"custom_providers": "oops", "model": {"provider": "openrouter"}})
        errors = [i.message for i in issues if i.severity == "error"]
        assert any(m.startswith("custom_providers is a str") and "list" in m for m in errors), errors
        assert not [i for i in validate_config_structure(
            {"custom_providers": [{"name": "x", "base_url": "http://h/v1"}], "model": {"provider": "custom"}})
            if i.severity == "error"]

    def test_dict_detects_misplaced_fields(self):
        """When custom_providers is a dict, detect fields that look misplaced."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "test",
                "base_url": "https://example.com",
                "api_key": "xxx",
            },
        })
        warnings = [i for i in issues if i.severity == "warning"]
        # Should flag base_url, api_key as looking like custom_providers entry fields
        misplaced = [i for i in warnings if "custom_providers entry fields" in i.message]
        assert len(misplaced) == 1


    def test_list_entry_not_dict(self):
        """Non-dict list entries should warn."""
        issues = validate_config_structure({
            "custom_providers": ["not-a-dict"],
            "model": {"provider": "custom"},
        })
        assert any("not a dict" in i.message for i in issues)




class TestMissingModelSection:
    """Warn when custom_providers exists but model section is missing."""


    def test_custom_providers_with_model(self):
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "test", "base_url": "https://example.com/v1"},
            ],
            "model": {"provider": "custom", "default": "test-model"},
        })
        # Should not warn about missing model section
        assert not any("no 'model' section" in i.message for i in issues)


class TestConfigIssueDataclass:
    """ConfigIssue should be a proper dataclass."""

    def test_fields(self):
        issue = ConfigIssue(severity="error", message="test msg", hint="test hint")
        assert issue.severity == "error"
        assert issue.message == "test msg"
        assert issue.hint == "test hint"

    def test_equality(self):
        a = ConfigIssue("error", "msg", "hint")
        b = ConfigIssue("error", "msg", "hint")
        assert a == b


class TestVoiceSubmitModeValidation:
    """The voice section was retired with the audio strip (3695ba63b0e #687,
    2a4da411696 #717): no shipped default and no enum to validate anymore. What must
    hold on the live surface: a legacy voice section still loads without validation
    errors — the audio strip's stated contract that old config keys keep loading —
    and the invalid-value-with-hint contract it used to carry lives on in the other
    structural validators (fallback_model below)."""

    def test_retired_voice_section_loads_without_errors(self):
        for mode in ("direct", "draft", "refine"):
            issues = validate_config_structure({"voice": {"submit_mode": mode}})
            assert not any("voice.submit_mode" in issue.message for issue in issues)


    def test_invalid_fallback_model_shape_is_reported(self):
        issues = validate_config_structure({"fallback_model": "claude"})

        [issue] = [i for i in issues if "fallback_model" in i.message]
        assert issue.severity == "error"
        assert "got str" in issue.message
        assert "provider" in issue.hint and "model" in issue.hint


def _has_tz_database() -> bool:
    try:
        import zoneinfo
        zoneinfo.ZoneInfo("UTC")
        return True
    except Exception:
        return False


def _tz_issues(config):
    return [i for i in validate_config_structure(config) if "timezone" in i.message]


class TestTimezoneValidation:
    """An invalid ``timezone`` silently puts the agent clock and every cron
    schedule on server-local time (hermes_time._get_zoneinfo falls back with
    one log warning). validate_config_structure must report it (#111725)."""

    @pytest.mark.skipif(not _has_tz_database(), reason="no tz database in this interpreter")
    def test_invalid_or_non_string_zone_is_an_error(self):
        [issue] = _tz_issues({"timezone": "Asia/Tokio", "model": {"provider": "nous"}})
        assert issue.severity == "error"
        assert "Asia/Tokio" in issue.message
        assert "IANA" in issue.hint and "HERMES_TIMEZONE" in issue.hint
        [issue] = _tz_issues({"timezone": 9, "model": {"provider": "nous"}})
        assert issue.severity == "error" and "string" in issue.message

    def test_valid_blank_missing_or_unverifiable_zone_is_silent(self, monkeypatch):
        for cfg in ({"timezone": "Asia/Tokyo"}, {}, {"timezone": ""}, {"timezone": "   "}, {"timezone": None}):
            assert _tz_issues({**cfg, "model": {"provider": "nous"}}) == []
        # Bare Windows without tzdata: ZoneInfo cannot load anything, including UTC.
        # A name that cannot be checked must not be flagged.
        import zoneinfo

        def no_db(_key):
            raise zoneinfo.ZoneInfoNotFoundError("no tz database")

        monkeypatch.setattr(zoneinfo, "ZoneInfo", no_db)
        assert _tz_issues({"timezone": "Asia/Tokio", "model": {"provider": "nous"}}) == []


class TestUnknownTopLevelKeys:
    """Arbitrary top-level keys must NOT warn — they are bridged to os.environ.

    Top-level scalars in config.yaml are forwarded into the environment
    (gateway/run.py, hermes send) so users can feed skills and external apps
    env-style keys like DISCORD_BOT_TOKEN or MY_APP_TOKEN. A closed-world
    allowlist can never enumerate those, so no "Unknown top-level config key"
    warning may exist.
    """


    def test_known_root_keys_derived_from_default_config(self):
        """_KNOWN_ROOT_KEYS must be DEFAULT_CONFIG.keys() plus extras — single source of truth."""
        assert set(DEFAULT_CONFIG.keys()).issubset(_KNOWN_ROOT_KEYS)
        assert _EXTRA_KNOWN_ROOT_KEYS.issubset(_KNOWN_ROOT_KEYS)
        assert _KNOWN_ROOT_KEYS == frozenset(DEFAULT_CONFIG.keys()) | _EXTRA_KNOWN_ROOT_KEYS

    def test_provider_like_unknown_root_keeps_misplaced_message(self):
        """Preserve existing base_url/api_key root-level guidance."""
        issues = validate_config_structure({
            "base_url": "https://example.com/v1",
            "api_key": "secret",
        })
        misplaced = [
            i for i in issues
            if i.severity == "warning" and "looks misplaced" in i.message
        ]
        assert any("base_url" in i.message for i in misplaced)
        assert any("api_key" in i.message for i in misplaced)

