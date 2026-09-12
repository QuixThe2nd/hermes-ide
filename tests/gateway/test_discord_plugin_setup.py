"""Tests for the Discord plugin's interactive_setup wizard.

The interactive_setup wizard lazy-imports its CLI helpers from
``hermes_cli.config`` (get_env_value / save_env_value / remove_env_value) and
``hermes_cli.cli_output`` (prompt / prompt_yes_no / print_*); we patch those
source modules.
"""
import hermes_cli.config as config_mod
import hermes_cli.cli_output as cli_output_mod
from plugins.platforms.discord.adapter import interactive_setup


def _patch_setup_io(monkeypatch, prompts, saved, removed, existing, infos=None):
    prompt_iter = iter(prompts)
    monkeypatch.setattr(config_mod, "get_env_value", lambda key: existing.get(key, ""))
    monkeypatch.setattr(config_mod, "save_env_value", lambda k, v: saved.update({k: v}))

    def _remove(key):
        removed.append(key)
        return existing.pop(key, None) is not None

    monkeypatch.setattr(config_mod, "remove_env_value", _remove)
    monkeypatch.setattr(cli_output_mod, "prompt", lambda *_a, **_kw: next(prompt_iter))
    monkeypatch.setattr(cli_output_mod, "prompt_yes_no", lambda *_a, **_kw: False)
    for name in ("print_header", "print_success", "print_warning"):
        monkeypatch.setattr(cli_output_mod, name, lambda *_a, **_kw: None)

    def _info(*args, **_kw):
        if infos is not None:
            infos.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(cli_output_mod, "print_info", _info)


# Discord prompts: bot_token (password), allowed_users.
_PROMPTS_BLANK = ["«redacted:discord-bot-token»", ""]


class TestDiscordSetupPrivilegedIntentsGuidance:
    """Setup must name Privileged Gateway Intents before asking for the token (#79430)."""

    def test_setup_mentions_message_content_intent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        saved, removed, infos = {}, [], []
        _patch_setup_io(
            monkeypatch,
            _PROMPTS_BLANK,
            saved,
            removed,
            existing={},
            infos=infos,
        )
        interactive_setup()
        joined = "\n".join(infos)
        assert "Message Content Intent" in joined
        assert "Privileged Gateway Intents" in joined
        assert "discord.com/developers/applications" in joined
