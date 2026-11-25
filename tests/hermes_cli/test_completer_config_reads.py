"""Measured-work pins for the slash-completer config reads.

The /tools completer runs on every keystroke while the user types the command
(complete_while_typing) and re-reads the config to reflect live toolset state.
It used to pay load_config()'s defensive deepcopy (~345us) per keystroke; the
pin holds it to the read-only loader. /personality was retired (e5b347e4989)
along with its memoised config parse; the remaining dynamic argument
completion paths are /skin, /tools and /handoff, and /skin is pinned to its
real source (built-in skins + a scan of the user skins dir) with no config
re-parse per keystroke.
"""

from __future__ import annotations

from unittest.mock import patch

import hermes_cli.commands_completion as commands_mod


class TestToolsCompletionsReadonlyConfig:
    def test_uses_readonly_loader(self):
        """_tools_completions must not pay the defensive deepcopy.

        The completer only reads the config (toolset enable state + MCP
        server names). Using load_config_readonly() skips the ~345us
        deepcopy that load_config() applies on every cache hit — a
        per-keystroke cost while completing /tools enable|disable.
        """
        calls = {"deepcopy": 0, "readonly": 0}

        def counting_deepcopy(*a, **k):
            calls["deepcopy"] += 1
            return {}

        def counting_readonly(*a, **k):
            calls["readonly"] += 1
            return {}

        # The completer imports the loader inside the function, so patch the
        # source module. Portable-MCP lookup is stubbed because it triggers
        # one-time plugin discovery (which legitimately reads the config
        # during process init) — this test asserts on the completer's own
        # per-keystroke reads, not discovery's one-off startup reads.
        with patch("hermes_cli.config.load_config", counting_deepcopy), \
             patch("hermes_cli.config.load_config_readonly", counting_readonly), \
             patch("hermes_cli.plugins.get_portable_mcp_server_names_nowait", lambda: set()), \
             patch("hermes_cli.tools_config._get_plugin_toolset_keys", lambda: set()):
            list(commands_mod.SlashCommandCompleter._tools_completions("enable ", "enable "))

        assert calls["readonly"] == 1, "completer should use the readonly loader"
        assert calls["deepcopy"] == 0, (
            "completer must not call the deepcopy loader on a read-only path"
        )


class TestSkinCompletionsAvailablePath:
    """/skin argument completion through the real dynamic-completion dispatch."""

    def _completions(self, text):
        from prompt_toolkit.document import Document

        completer = commands_mod.SlashCommandCompleter()
        return list(completer.get_completions(Document(text), None))

    def test_lists_user_and_builtin_skins_without_config_reads(self, tmp_path):
        """The actual available completion path: /skin args complete from
        built-in + user-installed skins, and a keystroke costs no config.yaml
        parse at all (the retired /personality memo's job done better)."""
        from hermes_constants import get_hermes_home

        skins_dir = get_hermes_home() / "skins"
        skins_dir.mkdir(parents=True, exist_ok=True)
        (skins_dir / "neon.yaml").write_text(
            "name: neon\ndescription: Neon city glow\n", encoding="utf-8")
        # A user skin named like a built-in must not shadow it.
        (skins_dir / "mono.yaml").write_text(
            "name: mono\ndescription: shadow attempt\n", encoding="utf-8")

        def _fail(*a, **k):
            raise AssertionError("/skin completions must not re-parse config.yaml")

        with patch("hermes_cli.config.load_config", _fail), \
             patch("hermes_cli.config.load_config_readonly", _fail):
            completions = self._completions("/skin ")

        by_text = {c.text: c for c in completions}
        assert "neon" in by_text, "user-installed skin must be offered"
        assert by_text["neon"].display_meta_text == "Neon city glow"
        # Built-ins are offered and never shadowed by a same-named user skin.
        assert {"default", "mono", "slate"} <= set(by_text)
        assert by_text["mono"].display_meta_text != "shadow attempt"

    def test_filters_on_the_typed_prefix(self):
        completions = self._completions("/skin sl")
        assert {c.text for c in completions} == {"slate"}
