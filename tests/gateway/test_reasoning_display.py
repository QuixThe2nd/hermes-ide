"""Reasoning display — compact style + serving-profile display resolution.

Two invariants:

1. ``reasoning_style: compact`` shows ONLY a one-line "💭 thought for Xs"
   duration note (Discord subtext / italic elsewhere), optionally suffixed
   with this turn's "(N tokens)" count, and never the chain-of-thought body.
2. On a multiplexed gateway, the reasoning prepend and runtime footer
   resolve ``display.*`` from the SERVING profile's config.yaml — the home
   ``_run_agent`` scoped the turn to — not the default profile home.
   ``GatewayRunner._display_config_scope`` is the seam that makes
   ``_load_gateway_config()`` at the prepend/footer site see that home.
"""

import yaml

# Stand-in chain-of-thought; compact must never leak any of it.
REASONING_BODY = "internal scratch deliberation about the answer"
FINAL_ANSWER = "here is the answer"


def _render_final_response(
    config: dict,
    *,
    platform_key: str,
    last_reasoning: str,
    turn_seconds,
    thought_tokens: int | None = None,
) -> str:
    """Mirror the ``_handle_message_with_agent`` prepend gate + helper call.

    Same contract as the production site: resolve show_reasoning, resolve
    reasoning_style, render the prefix, prepend it to the final answer.
    """
    from gateway.display_config import format_reasoning_prefix, resolve_display_setting

    if not resolve_display_setting(config, platform_key, "show_reasoning", False):
        return FINAL_ANSWER
    style = resolve_display_setting(config, platform_key, "reasoning_style", "code")
    prefix = format_reasoning_prefix(
        style, last_reasoning, turn_seconds, platform_key, thought_tokens=thought_tokens
    )
    return f"{prefix}\n\n{FINAL_ANSWER}" if prefix else FINAL_ANSWER


def _compact_config(platform_key: str = "discord", **display) -> dict:
    display.setdefault("show_reasoning", True)
    display.setdefault("reasoning_style", "compact")
    return {"display": {"platforms": {platform_key: display}}}


class TestCompactReasoningStyle:
    """``compact`` renders one duration line, never the reasoning body."""

    def test_discord_renders_subtext_duration_line(self):
        rendered = _render_final_response(
            _compact_config("discord"),
            platform_key="discord",
            last_reasoning=REASONING_BODY,
            turn_seconds=11.0,
        )
        assert rendered.startswith("-# 💭 thought for 11s")
        assert REASONING_BODY not in rendered
        assert rendered.endswith(f"\n\n{FINAL_ANSWER}")

    def test_non_discord_renders_italic_duration_line(self):
        for platform_key in ("telegram", "slack", "matrix"):
            rendered = _render_final_response(
                _compact_config(platform_key),
                platform_key=platform_key,
                last_reasoning=REASONING_BODY,
                turn_seconds=11.0,
            )
            assert rendered.startswith("_💭 thought for 11s_"), platform_key
            assert REASONING_BODY not in rendered

    def test_show_reasoning_false_suppresses_thought_line(self):
        config = _compact_config("discord", show_reasoning=False)
        rendered = _render_final_response(
            config,
            platform_key="discord",
            last_reasoning=REASONING_BODY,
            turn_seconds=11.0,
        )
        assert rendered == FINAL_ANSWER
        assert "thought for" not in rendered

    def test_empty_duration_omits_thought_line(self):
        for turn_seconds in (None, -1.0):
            rendered = _render_final_response(
                _compact_config("discord"),
                platform_key="discord",
                last_reasoning=REASONING_BODY,
                turn_seconds=turn_seconds,
            )
            assert rendered == FINAL_ANSWER, turn_seconds
            assert "thought for" not in rendered

    def test_no_reasoning_text_omits_thought_line(self):
        rendered = _render_final_response(
            _compact_config("discord"),
            platform_key="discord",
            last_reasoning="",
            turn_seconds=11.0,
        )
        assert rendered == FINAL_ANSWER

    def test_duration_reuses_runtime_footer_formatting(self):
        from gateway.runtime_footer import _format_duration

        for turn_seconds, expected in (
            (0.4, "0.4s"),
            (11.0, "11s"),
            (63.0, "1m03s"),
            (4980.0, "1h23m"),
        ):
            rendered = _render_final_response(
                _compact_config("discord"),
                platform_key="discord",
                last_reasoning=REASONING_BODY,
                turn_seconds=turn_seconds,
            )
            assert rendered.startswith(f"-# 💭 thought for {expected}"), turn_seconds
            assert expected == _format_duration(turn_seconds)


class TestCompactThoughtTokens:
    """``compact`` appends this turn's token count when the provider reports one.

    The gateway diffs cumulative ``session_*_tokens`` counters around
    ``run_conversation()`` and passes the per-turn delta in as
    ``thought_tokens``. The 💭 marker is always present; a positive count
    appends an ``(N tokens)`` suffix, while a missing/zero count leaves
    the bare duration line.
    """

    def test_discord_appends_token_count(self):
        rendered = _render_final_response(
            _compact_config("discord"),
            platform_key="discord",
            last_reasoning=REASONING_BODY,
            turn_seconds=3.9,
            thought_tokens=598,
        )
        assert rendered.startswith("-# 💭 thought for 3.9s (598 tokens)")
        assert REASONING_BODY not in rendered
        assert rendered.endswith(f"\n\n{FINAL_ANSWER}")

    def test_non_discord_appends_token_count(self):
        for platform_key in ("telegram", "slack", "matrix"):
            rendered = _render_final_response(
                _compact_config(platform_key),
                platform_key=platform_key,
                last_reasoning=REASONING_BODY,
                turn_seconds=3.9,
                thought_tokens=598,
            )
            assert rendered.startswith("_💭 thought for 3.9s (598 tokens)_"), platform_key
            assert REASONING_BODY not in rendered

    def test_missing_or_zero_tokens_keep_plain_duration_line(self):
        for thought_tokens in (None, 0):
            rendered = _render_final_response(
                _compact_config("discord"),
                platform_key="discord",
                last_reasoning=REASONING_BODY,
                turn_seconds=3.9,
                thought_tokens=thought_tokens,
            )
            assert rendered.startswith("-# 💭 thought for 3.9s"), thought_tokens
            assert "tokens" not in rendered, thought_tokens
            assert "💭" in rendered, thought_tokens

    def test_helper_contract_exact_strings(self):
        from gateway.display_config import format_reasoning_prefix

        assert (
            format_reasoning_prefix(
                "compact", "scratch", 3.9, "discord", thought_tokens=598
            )
            == "-# 💭 thought for 3.9s (598 tokens)"
        )
        assert (
            format_reasoning_prefix(
                "compact", "scratch", 3.9, "telegram", thought_tokens=598
            )
            == "_💭 thought for 3.9s (598 tokens)_"
        )
        assert (
            format_reasoning_prefix("compact", "scratch", 3.9, "discord")
            == "-# 💭 thought for 3.9s"
        )
        assert (
            format_reasoning_prefix("compact", "scratch", 3.9, "telegram")
            == "_💭 thought for 3.9s_"
        )

    def test_large_counts_render_plain_integers(self):
        rendered = _render_final_response(
            _compact_config("discord"),
            platform_key="discord",
            last_reasoning=REASONING_BODY,
            turn_seconds=63.0,
            thought_tokens=123456,
        )
        assert rendered.startswith("-# 💭 thought for 1m03s (123456 tokens)")

    def test_non_compact_styles_ignore_token_count(self):
        from gateway.display_config import format_reasoning_prefix

        for style in ("code", "blockquote", "subtext"):
            rendered = format_reasoning_prefix(
                style, REASONING_BODY, 3.9, "discord", thought_tokens=598
            )
            assert "598" not in rendered, style


class TestReasoningStyleAcceptsCompact:
    """resolve_display_setting: compact is valid; garbage still coerces."""

    def test_compact_accepted(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"reasoning_style": "compact"}}
        assert (
            resolve_display_setting(config, "discord", "reasoning_style") == "compact"
        )

    def test_garbage_coerces_to_code(self):
        from gateway.display_config import resolve_display_setting

        for bad in ("fancy", "SUMMARY", "inline"):
            config = {"display": {"reasoning_style": bad}}
            assert (
                resolve_display_setting(config, "discord", "reasoning_style") == "code"
            ), bad


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
        """The live defect: default home says show_reasoning=true, the serving
        profile says false — only the scoped read honors the serving profile."""
        import gateway.run as run_mod
        from gateway.display_config import resolve_display_setting
        from gateway.runtime_footer import resolve_footer_config

        # Default home (the multiplexer's own) turns everything ON.
        self._write_config(
            tmp_path,
            {
                "show_reasoning": True,
                "reasoning_style": "subtext",
                "runtime_footer": {"enabled": True},
            },
        )
        # Serving profile for this turn turns it all OFF + compact.
        self._write_config(
            tmp_path / "profiles" / "assistant",
            {
                "show_reasoning": False,
                "reasoning_style": "compact",
                "runtime_footer": {"enabled": False},
            },
        )

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

        runner = self._make_runner(multiplex=True)
        source = self._make_source(profile="assistant")

        # Unscoped (pre-fix behavior): reads the DEFAULT home — reasoning on.
        default_cfg = run_mod._load_gateway_config()
        assert resolve_display_setting(default_cfg, "discord", "show_reasoning") is True
        assert resolve_footer_config(default_cfg, "discord")["enabled"] is True

        # Scoped to the serving profile (the fix's seam): reasoning off,
        # compact style, footer disabled.
        with runner._display_config_scope(source):
            scoped_cfg = run_mod._load_gateway_config()
            assert (
                resolve_display_setting(scoped_cfg, "discord", "show_reasoning")
                is False
            )
            assert (
                resolve_display_setting(scoped_cfg, "discord", "reasoning_style")
                == "compact"
            )
            assert resolve_footer_config(scoped_cfg, "discord")["enabled"] is False

        # Scope resets — later turns still see the default home.
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

        self._write_config(
            tmp_path, {"show_reasoning": True, "reasoning_style": "subtext"}
        )

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(run_mod, "_hermes_home", tmp_path)

        runner = self._make_runner(multiplex=False)
        scope = runner._display_config_scope(self._make_source(profile="assistant"))
        assert isinstance(scope, nullcontext)
        with scope:
            cfg = run_mod._load_gateway_config()
        assert resolve_display_setting(cfg, "discord", "show_reasoning") is True
