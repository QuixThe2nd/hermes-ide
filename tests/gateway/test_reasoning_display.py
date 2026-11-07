"""Reasoning display — hardcoded one-line summary + serving-profile display resolution.

Two invariants:

1. Reasoning display is one ``💭 Reasoned about <describe>`` line: Discord
   subtext, italic elsewhere, using only the first non-empty reasoning line.
2. On a multiplexed gateway, the reasoning prepend and runtime footer resolve
   ``display.*`` from the SERVING profile's config.yaml — the home ``_run_agent``
   scoped the turn to — not the default profile home.
   ``GatewayRunner._display_config_scope`` is the seam that makes
   ``_load_gateway_config()`` at the prepend/footer site see that home.
"""

import yaml

REASONING_BODY = "First I considered the user's question.\nThen I checked the docs."
FINAL_ANSWER = "here is the answer"


def _render_final_response(
    config: dict,
    *,
    platform_key: str,
    last_reasoning: str | None,
) -> str:
    """Mirror the production ``show_reasoning`` gate and prefix prepend."""
    from gateway.display_config import format_reasoning_prefix, resolve_display_setting

    if not resolve_display_setting(config, platform_key, "show_reasoning", False):
        return FINAL_ANSWER
    prefix = format_reasoning_prefix(last_reasoning, platform_key)
    return f"{prefix}\n\n{FINAL_ANSWER}" if prefix else FINAL_ANSWER


def _reasoning_config(platform_key: str = "discord", **display) -> dict:
    display.setdefault("show_reasoning", True)
    return {"display": {"platforms": {platform_key: display}}}


class TestOneLineReasoningDisplay:
    def test_discord_renders_subtext_summary(self):
        rendered = _render_final_response(
            _reasoning_config("discord"),
            platform_key="discord",
            last_reasoning=REASONING_BODY,
        )
        assert rendered == (
            "-# 💭 Reasoned about First I considered the user's question.\n\n"
            f"{FINAL_ANSWER}"
        )

    def test_non_discord_renders_italic_summary(self):
        for platform_key in ("telegram", "slack", "matrix"):
            rendered = _render_final_response(
                _reasoning_config(platform_key),
                platform_key=platform_key,
                last_reasoning=REASONING_BODY,
            )
            assert rendered == (
                "_💭 Reasoned about First I considered the user's question._\n\n"
                f"{FINAL_ANSWER}"
            ), platform_key

    def test_first_meaningful_line_is_stripped_and_rest_is_omitted(self):
        from gateway.display_config import format_reasoning_prefix

        prefix = format_reasoning_prefix(
            "\n  \n\t\n  Compare the available options.  \nNever render this line.",
            "discord",
        )
        assert prefix == "-# 💭 Reasoned about Compare the available options."
        assert "\n" not in prefix
        assert "Never render this line" not in prefix

    def test_description_is_truncated_to_80_chars_with_ellipsis(self):
        from gateway.display_config import format_reasoning_prefix

        description = "x" * 81
        prefix = format_reasoning_prefix(description, "discord")
        assert prefix == f"-# 💭 Reasoned about {'x' * 80}…"
        assert "\n" not in prefix

        exact_limit = "y" * 80
        assert format_reasoning_prefix(exact_limit, "telegram") == (
            f"_💭 Reasoned about {exact_limit}_"
        )

    def test_empty_or_blank_reasoning_returns_empty_prefix(self):
        from gateway.display_config import format_reasoning_prefix

        for last_reasoning in (None, "", "  \n\t\n "):
            assert format_reasoning_prefix(last_reasoning, "discord") == ""
            assert _render_final_response(
                _reasoning_config("discord"),
                platform_key="discord",
                last_reasoning=last_reasoning,
            ) == FINAL_ANSWER

    def test_show_reasoning_false_suppresses_summary(self):
        rendered = _render_final_response(
            _reasoning_config("discord", show_reasoning=False),
            platform_key="discord",
            last_reasoning=REASONING_BODY,
        )
        assert rendered == FINAL_ANSWER
        assert "💭" not in rendered

    def test_no_fence_duration_or_token_suffix_is_rendered(self):
        from gateway.display_config import format_reasoning_prefix

        prefix = format_reasoning_prefix(
            "Compare the available options for 63 seconds and 598 tokens.\n"
            "```python\nprint('never rendered')\n```",
            "discord",
        )
        assert prefix == (
            "-# 💭 Reasoned about Compare the available options for 63 seconds and 598 tokens."
        )
        assert "```" not in prefix
        assert "thought for" not in prefix
        assert "(598 tokens)" not in prefix

    def test_gateway_helper_honors_gate_and_hardcoded_summary(self, monkeypatch):
        import gateway.run as run_mod
        from gateway.config import Platform
        from gateway.session import SessionSource

        runner = object.__new__(run_mod.GatewayRunner)
        runner._show_reasoning = False
        source = SessionSource(
            platform=Platform.DISCORD, chat_id="chat-1", profile="default"
        )
        config = _reasoning_config("discord")
        monkeypatch.setattr(run_mod, "_load_gateway_config", lambda: config)

        rendered = runner._hmwa_prepend_reasoning(
            {"last_reasoning": REASONING_BODY}, FINAL_ANSWER, source, False
        )
        assert rendered == (
            "-# 💭 Reasoned about First I considered the user's question.\n\n"
            f"{FINAL_ANSWER}"
        )

        config["display"]["platforms"]["discord"]["show_reasoning"] = False
        assert runner._hmwa_prepend_reasoning(
            {"last_reasoning": REASONING_BODY}, FINAL_ANSWER, source, False
        ) == FINAL_ANSWER

    def test_legacy_style_config_cannot_change_rendering(self):
        expected = _render_final_response(
            _reasoning_config("discord"),
            platform_key="discord",
            last_reasoning=REASONING_BODY,
        )
        for legacy_style in ("code", "blockquote", "subtext", "compact", "verbose"):
            config = _reasoning_config("discord", reasoning_style=legacy_style)
            assert _render_final_response(
                config,
                platform_key="discord",
                last_reasoning=REASONING_BODY,
            ) == expected, legacy_style

    def test_reasoning_style_is_not_an_overrideable_setting(self):
        from gateway.display_config import OVERRIDEABLE_KEYS

        assert "reasoning_style" not in OVERRIDEABLE_KEYS


class TestServingProfileDisplayScope:
    """Multiplexed display resolution reads the serving profile's config.yaml."""

    def _write_config(self, home, display: dict) -> None:
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(
            yaml.dump({"display": display}), encoding="utf-8"
        )

    def _make_runner(self, multiplex: bool):
        import gateway.run as run_mod

        runner = object.__new__(run_mod.GatewayRunner)
        runner.config = type(
            "Cfg", (), {"multiplex_profiles": multiplex, "profile_routes": None}
        )()
        return runner

    def _make_source(self, profile: str):
        from gateway.config import Platform
        from gateway.session import SessionSource

        return SessionSource(
            platform=Platform.DISCORD, chat_id="chat-1", profile=profile
        )

    def test_serving_profile_display_wins_over_default_home(
        self, tmp_path, monkeypatch
    ):
        """The serving profile's show_reasoning=false beats default-home true."""
        import gateway.run as run_mod
        from gateway.display_config import resolve_display_setting
        from gateway.runtime_footer import resolve_footer_config

        self._write_config(
            tmp_path,
            {"show_reasoning": True, "runtime_footer": {"enabled": True}},
        )
        self._write_config(
            tmp_path / "profiles" / "assistant",
            {"show_reasoning": False, "runtime_footer": {"enabled": False}},
        )

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

        runner = self._make_runner(multiplex=True)
        source = self._make_source(profile="assistant")

        default_cfg = run_mod._load_gateway_config()
        assert resolve_display_setting(default_cfg, "discord", "show_reasoning") is True
        assert resolve_footer_config(default_cfg, "discord")["enabled"] is True

        with runner._display_config_scope(source):
            scoped_cfg = run_mod._load_gateway_config()
            assert (
                resolve_display_setting(scoped_cfg, "discord", "show_reasoning")
                is False
            )
            assert resolve_footer_config(scoped_cfg, "discord")["enabled"] is False

        assert (
            resolve_display_setting(
                run_mod._load_gateway_config(), "discord", "show_reasoning"
            )
            is True
        )

    def test_single_profile_gateway_stays_unscoped(self, tmp_path, monkeypatch):
        """multiplex_profiles off → pass-through scope, default home unchanged."""
        import gateway.run as run_mod
        from contextlib import nullcontext
        from gateway.display_config import resolve_display_setting

        self._write_config(tmp_path, {"show_reasoning": True})

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

        runner = self._make_runner(multiplex=False)
        scope = runner._display_config_scope(self._make_source(profile="assistant"))
        assert isinstance(scope, nullcontext)
        with scope:
            cfg = run_mod._load_gateway_config()
        assert resolve_display_setting(cfg, "discord", "show_reasoning") is True
