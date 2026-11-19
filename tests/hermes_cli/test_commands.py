"""Tests for the central command registry and autocomplete."""

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from hermes_cli.commands import COMMAND_REGISTRY, COMMANDS, COMMANDS_BY_CATEGORY, CommandDef, GATEWAY_KNOWN_COMMANDS, SUBCOMMANDS, command_desktop_meta, gateway_help_lines, infer_argument_mode, resolve_command
from hermes_cli.commands_completion import SlashCommandAutoSuggest, SlashCommandCompleter
from hermes_cli.commands_platforms import _CMD_NAME_LIMIT, _clamp_command_names


def _completions(completer: SlashCommandCompleter, text: str):
    return list(
        completer.get_completions(
            Document(text=text),
            CompleteEvent(completion_requested=True),
        )
    )


# ---------------------------------------------------------------------------
# CommandDef registry tests
# ---------------------------------------------------------------------------

class TestCommandRegistry:


    def test_save_command_supports_formats(self):
        cmd = resolve_command("save")
        assert cmd is not None
        assert cmd.name == "save"
        # /save is a cross-platform session export: json (default), md, html
        assert not cmd.cli_only
        for token in ("json", "md", "html"):
            assert token in (cmd.args_hint or "")

    def test_no_duplicate_canonical_names(self):
        names = [cmd.name for cmd in COMMAND_REGISTRY]
        assert len(names) == len(set(names)), f"Duplicate names: {[n for n in names if names.count(n) > 1]}"

    def test_no_alias_collides_with_canonical_name(self):
        """An alias must not shadow another command's canonical name."""
        canonical_names = {cmd.name for cmd in COMMAND_REGISTRY}
        for cmd in COMMAND_REGISTRY:
            for alias in cmd.aliases:
                if alias in canonical_names:
                    # reset -> new is intentional (reset IS an alias for new)
                    target = next(c for c in COMMAND_REGISTRY if c.name == alias)
                    # This should only happen if the alias points to the same entry
                    assert resolve_command(alias).name == cmd.name or alias == cmd.name, \
                        f"Alias '{alias}' of '{cmd.name}' shadows canonical '{target.name}'"

    def test_desktop_meta_lives_on_the_command_def(self):
        review = resolve_command("review")
        assert review is not None
        assert review.argument_mode is None
        assert infer_argument_mode(review) == "text"
        assert command_desktop_meta(review) == {"argument_mode": "text", "desktop": None}

        clear = resolve_command("clear")
        assert clear is not None
        assert clear.desktop == "terminal"

        queue = resolve_command("queue")
        assert queue is not None
        assert infer_argument_mode(queue) == "mixed"

    def test_argument_mode_infers_text_from_any_args_hint(self):
        assert infer_argument_mode(CommandDef("demo", "Demo", "Session", args_hint="<prompt>")) == "text"
        assert infer_argument_mode(CommandDef("ask", "Ask", "Session", args_hint="<query>")) == "text"
        assert infer_argument_mode(CommandDef("note", "Note", "Session", args_hint="[message]")) == "text"


# ---------------------------------------------------------------------------
# resolve_command tests
# ---------------------------------------------------------------------------

class TestResolveCommand:



    def test_context_command_registered_with_ctx_alias(self):
        ctx = resolve_command("context")
        assert ctx is not None
        assert ctx.name == "context"
        assert resolve_command("ctx").name == "context"
        assert "all" in (ctx.subcommands or ())
        # Available on both CLI and gateway surfaces
        assert not ctx.cli_only and not ctx.gateway_only
        assert "context" in GATEWAY_KNOWN_COMMANDS


# ---------------------------------------------------------------------------
# Derived dicts (backwards compat)
# ---------------------------------------------------------------------------

class TestDerivedDicts:


    def test_commands_dict_includes_aliases(self):
        assert "/bg" in COMMANDS
        assert "/exit" in COMMANDS
        assert "/q" in COMMANDS
        assert "/reload_mcp" in COMMANDS
        assert "/gateway" in COMMANDS

    def test_commands_by_category_covers_all_categories(self):
        registry_categories = {cmd.category for cmd in COMMAND_REGISTRY if not cmd.gateway_only}
        assert set(COMMANDS_BY_CATEGORY.keys()) == registry_categories


# ---------------------------------------------------------------------------
# Gateway helpers
# ---------------------------------------------------------------------------

class TestGatewayKnownCommands:

    def test_includes_config_gated_cli_only(self):
        """Commands with gateway_config_gate are always in GATEWAY_KNOWN_COMMANDS."""
        for cmd in COMMAND_REGISTRY:
            if cmd.gateway_config_gate:
                assert cmd.name in GATEWAY_KNOWN_COMMANDS, \
                    f"config-gated command '{cmd.name}' should be in GATEWAY_KNOWN_COMMANDS"


    def test_is_frozenset(self):
        assert isinstance(GATEWAY_KNOWN_COMMANDS, frozenset)


class TestGatewayHelpLines:

    def test_excludes_cli_only_commands_without_config_gate(self):
        import re
        lines = gateway_help_lines()
        joined = "\n".join(lines)
        for cmd in COMMAND_REGISTRY:
            if cmd.cli_only and not cmd.gateway_config_gate:
                # Word-boundary match so `/reload` doesn't match `/reload-mcp`
                pattern = rf'`/{re.escape(cmd.name)}(?![-_\w])'
                assert not re.search(pattern, joined), \
                    f"cli_only command /{cmd.name} should not be in gateway help"

    def test_bg_and_btw_are_separate_commands(self):
        lines = gateway_help_lines()
        joined = "\n".join(lines)
        assert "`/bg" in joined
        assert "`/btw" in joined
        # The retired /background canonical name must be gone.
        bg_line = [l for l in lines if "/background" in l]
        assert not bg_line

# ---------------------------------------------------------------------------
# Config-gated gateway commands
# ---------------------------------------------------------------------------

class TestGatewayConfigGate:
    """Tests for the gateway_config_gate mechanism on CommandDef."""


    def test_verbose_in_gateway_known_commands(self):
        """Config-gated commands are always recognized by the gateway."""
        assert "verbose" in GATEWAY_KNOWN_COMMANDS

    def test_config_gate_excluded_from_help_when_off(self, tmp_path, monkeypatch):
        """When the config gate is falsy, the command should not appear in help."""
        # Write a config with the gate off (default)
        config_file = tmp_path / "config.yaml"
        config_file.write_text("display:\n  tool_progress_command: false\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        lines = gateway_help_lines()
        joined = "\n".join(lines)
        assert "`/verbose" not in joined


    def test_config_gate_included_in_help_when_on(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("display:\n  tool_progress_command: true\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        lines = gateway_help_lines()
        joined = "\n".join(lines)
        assert "`/verbose" in joined


# ---------------------------------------------------------------------------
# Autocomplete (SlashCommandCompleter)
# ---------------------------------------------------------------------------

class TestSlashCommandCompleter:
    # -- basic prefix completion -----------------------------------------


    # -- exact-match trailing space --------------------------------------


    # -- non-slash input returns nothing ---------------------------------


    # -- skill commands via provider ------------------------------------

    def test_skill_commands_are_completed_from_provider(self):
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: {
                "/gif-search": {"description": "Search for GIFs across providers"},
            }
        )

        completions = _completions(completer, "/gif")

        assert len(completions) == 1
        assert completions[0].text == "gif-search"
        assert completions[0].display_text == "/gif-search"
        assert completions[0].display_meta_text == "⚡ Search for GIFs across providers"


    def test_skill_provider_exception_is_swallowed(self):
        """A broken provider should not crash autocomplete."""
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # Should return builtin matches only, no crash
        completions = _completions(completer, "/he")
        texts = {item.text for item in completions}
        assert "help" in texts


# ── Stacked slash-skill completion ──────────────────────────────────────


def _stacked_completer(**extra_skills):
    skills = {
        "/skill-a": {"description": "Skill A"},
        "/skill-b": {"description": "Skill B"},
        "/skill-c": {"description": "Skill C"},
        **extra_skills,
    }
    return SlashCommandCompleter(skill_commands_provider=lambda: skills)


class TestStackedSkillCompletion:
    """Second+ leading skill tokens keep getting completions (stacked
    slash-skill invocations, Claude Code v2.1.199 port follow-up)."""


    def test_no_completions_for_instruction_text(self):
        assert _completions(_stacked_completer(), "/skill-a do the") == []
        assert _completions(_stacked_completer(), "/skill-a ") == []


    def test_cap_stops_completions(self):
        skills = {f"/stk-{i}": {"description": f"S{i}"} for i in range(8)}
        completer = SlashCommandCompleter(skill_commands_provider=lambda: skills)
        text = " ".join(f"/stk-{i}" for i in range(5)) + " /stk-"
        assert _completions(completer, text) == []


# ── SUBCOMMANDS extraction ──────────────────────────────────────────────


class TestSubcommands:
    def test_explicit_subcommands_extracted(self):
        """Commands with explicit subcommands on CommandDef are extracted."""
        assert "/skills" in SUBCOMMANDS
        assert "install" in SUBCOMMANDS["/skills"]


    def test_commands_without_subcommands_not_in_dict(self):
        """Plain commands should not appear in SUBCOMMANDS."""
        assert "/help" not in SUBCOMMANDS
        assert "/quit" not in SUBCOMMANDS
        assert "/clear" not in SUBCOMMANDS


# ── Subcommand tab completion ───────────────────────────────────────────


class TestSubcommandCompletion:


    def test_tools_enable_skips_already_listed(self, monkeypatch):
        """If the user already typed a name, don't suggest it again."""
        monkeypatch.setattr(
            "hermes_cli.tools_config._get_platform_tools",
            lambda *_a, **_k: set(),
        )
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
        monkeypatch.setattr(
            "hermes_cli.tools_config._get_plugin_toolset_keys",
            lambda: set(),
        )

        completions = _completions(SlashCommandCompleter(), "/tools enable spotify ")
        texts = {c.text for c in completions}
        assert "spotify" not in texts


    def _fake_gateway(self, monkeypatch, platforms):
        """Patch load_gateway_config with a fake whose connected platforms are
        the keys of `platforms` (name -> notification channel as None or a
        (chat_id, name) tuple).
        """
        from types import SimpleNamespace

        enums = {name: SimpleNamespace(value=name) for name in platforms}
        channels = {
            name: (None if channel is None else SimpleNamespace(chat_id=channel[0], name=channel[1]))
            for name, channel in platforms.items()
        }
        fake = SimpleNamespace(
            get_connected_platforms=lambda: list(enums.values()),
            get_notification_channel=lambda p: channels[p.value],
        )
        monkeypatch.setattr("gateway.config.load_gateway_config", lambda: fake)

    def test_handoff_completes_connected_platforms(self, monkeypatch):
        """`/handoff ` offers connected platforms, with or without a notification channel."""
        self._fake_gateway(
            monkeypatch,
            {
                "slack": ("123", "Me"),
                "discord": None,  # no notification channel yet -> still listed
            },
        )

        texts = {c.text for c in _completions(SlashCommandCompleter(), "/handoff ")}
        assert texts == {"slack", "discord"}


# ── Ghost text (SlashCommandAutoSuggest) ────────────────────────────────


def _suggestion(text: str, completer=None) -> str | None:
    """Get ghost text suggestion for given input."""
    suggest = SlashCommandAutoSuggest(completer=completer)
    doc = Document(text=text)

    class FakeBuffer:
        pass

    result = suggest.get_suggestion(FakeBuffer(), doc)
    return result.text if result else None


class TestGhostText:
    def test_command_name_suggestion(self):
        """/he → 'lp'"""
        assert _suggestion("/he") == "lp"


    # -- stacked slash-skill ghost text -----------------------------------


    def test_stacked_skill_ghost_text_skips_used(self):
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: {
                "/alpha": {"description": "A"},
                "/beta": {"description": "B"},
            }
        )
        assert _suggestion("/alpha /a", completer=completer) is None
        assert _suggestion("/alpha /b", completer=completer) == "eta"


# ---------------------------------------------------------------------------
# Gateway command name clamping (32-char limit)
# ---------------------------------------------------------------------------


class TestClampCommandNames:
    """Tests for _clamp_command_names() — 32-char enforcement + collision."""


    def test_collision_between_entries_gets_incrementing_digits(self):
        # Two long names that truncate to the same 32-char prefix
        base = "y" * 40
        entries = [(base + "_alpha", "d1"), (base + "_beta", "d2")]
        result = _clamp_command_names(entries, set())
        assert len(result) == 2
        assert result[0][0] == "y" * _CMD_NAME_LIMIT
        assert result[1][0] == "y" * (_CMD_NAME_LIMIT - 1) + "0"


    def test_all_digits_exhausted_drops_entry(self):
        prefix = "w" * _CMD_NAME_LIMIT
        # Reserve the plain truncation + all 10 digit slots
        reserved = {prefix} | {"w" * (_CMD_NAME_LIMIT - 1) + str(d) for d in range(10)}
        long_name = "w" * 50
        result = _clamp_command_names([(long_name, "d")], reserved)
        assert result == []


class TestClampCommandNamesTriples:
    """Tests for _clamp_command_names with 3-tuples (name, desc, cmd_key).

    Skill entries pass through _clamp_command_names as 3-tuples so the
    original cmd_key survives name truncation.  Before the fix in PR #18951,
    the code stripped cmd_key into a side-dict keyed by the *original*
    (name, desc) pair — after truncation the lookup key no longer matched,
    silently losing the cmd_key.
    """


    def test_long_name_preserves_cmd_key(self):
        long = "a" * 50
        cmd_key = f"/{long}"
        result = _clamp_command_names([(long, "desc", cmd_key)], set())
        assert len(result) == 1
        name, desc, key = result[0]
        assert len(name) == _CMD_NAME_LIMIT
        assert key == cmd_key, "cmd_key must survive name clamping"

    def test_collision_preserves_cmd_key(self):
        prefix = "x" * _CMD_NAME_LIMIT
        long = "x" * 50
        result = _clamp_command_names(
            [(long, "desc", "/long-skill")], reserved={prefix},
        )
        assert len(result) == 1
        name, _desc, key = result[0]
        assert name == "x" * (_CMD_NAME_LIMIT - 1) + "0"
        assert key == "/long-skill"


class TestGatewaySkillCollector:
    """_collect_gateway_skill_entries: shared plugin+skill collector for gateway menus."""

    def test_long_skill_name_clamped_but_cmd_key_retained(self, tmp_path):
        from unittest.mock import patch
        from hermes_cli.commands_platforms import _collect_gateway_skill_entries

        long_name = "this-is-a-very-long-skill-name-that-exceeds-limit"
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        fake_cmds = {
            f"/{long_name}": {
                "name": long_name,
                "description": "A skill with a long name",
                "skill_md_path": f"{skills_dir.resolve()}/{long_name}/SKILL.md",
            },
        }
        with (
            patch("agent.skill_commands.get_skill_commands", return_value=fake_cmds),
            patch("tools.skills_tool.SKILLS_DIR", skills_dir),
            patch("agent.skill_utils.get_external_skills_dirs", return_value=[]),
        ):
            entries, hidden = _collect_gateway_skill_entries(
                platform="discord", max_slots=100, reserved_names=set(), desc_limit=100,
            )
        assert hidden == 0
        name, _desc, cmd_key, raw_name = entries[0]
        assert len(name) == _CMD_NAME_LIMIT
        assert cmd_key == f"/{long_name}", "cmd_key must survive name clamping"
        assert raw_name == long_name

    def test_cap_trims_skills_only(self, tmp_path):
        from unittest.mock import patch
        from hermes_cli.commands_platforms import _collect_gateway_skill_entries

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        fake_cmds = {
            f"/skill-{i:03d}": {
                "name": f"skill-{i:03d}",
                "description": f"Skill {i}",
                "skill_md_path": f"{skills_dir.resolve()}/skill-{i:03d}/SKILL.md",
            }
            for i in range(20)
        }
        with (
            patch("agent.skill_commands.get_skill_commands", return_value=fake_cmds),
            patch("tools.skills_tool.SKILLS_DIR", skills_dir),
            patch("hermes_cli.plugins.get_plugin_commands", return_value={"plug": {"description": "p"}}),
        ):
            entries, hidden = _collect_gateway_skill_entries(
                platform="discord", max_slots=5, reserved_names=set(), desc_limit=100,
            )
        assert [e[0] for e in entries] == ["plug", "skill-000", "skill-001", "skill-002", "skill-003"]
        assert hidden == 16


# ---------------------------------------------------------------------------
# Discord skill commands grouped by category
# ---------------------------------------------------------------------------

from hermes_cli.commands_platforms import discord_skill_commands_by_category


class TestDiscordSkillCommandsByCategory:
    """Tests for discord_skill_commands_by_category() — /skill group registration."""


    def test_no_legacy_25x25_cap(self, tmp_path, monkeypatch):
        """The old nested-layout caps (25 groups × 25 skills/group) are gone.

        The live caller flattens categories into a single autocomplete list,
        which Discord fetches dynamically — the per-command 8KB payload
        concern from the old nested layout (#11321, #10259) no longer applies.
        Guards against accidentally re-introducing the caps, which would
        silently drop skills in the 26th+ alphabetical category (the exact
        failure mode users were hitting with 29 category dirs on real
        installs).
        """
        from unittest.mock import patch

        fake_skills_dir = str(tmp_path / "skills")

        # Build 30 categories (> old _MAX_GROUPS=25) each with 30 skills
        # (> old _MAX_PER_GROUP=25).
        fake_cmds = {}
        for c in range(30):
            cat = f"cat{c:02d}"  # cat00, cat01, ..., cat29 — 30 categories
            for s in range(30):
                name = f"skill-{c:02d}-{s:02d}"
                skill_subdir = tmp_path / "skills" / cat / name
                skill_subdir.mkdir(parents=True, exist_ok=True)
                (skill_subdir / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
                fake_cmds[f"/{name}"] = {
                    "name": name,
                    "description": f"Category {cat} skill {s}",
                    "skill_md_path": f"{fake_skills_dir}/{cat}/{name}/SKILL.md",
                }

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with (
            patch("agent.skill_commands.get_skill_commands", return_value=fake_cmds),
            patch("tools.skills_tool.SKILLS_DIR", tmp_path / "skills"),
        ):
            categories, uncategorized, hidden = discord_skill_commands_by_category(
                reserved_names=set(),
            )

        # Every category should be present — no 25-group cap
        assert len(categories) == 30, (
            f"expected all 30 categories, got {len(categories)} "
            f"(cap from old nested layout must be removed)"
        )
        # Every skill in every category must be present — no 25-per-group cap
        for cat_name, entries in categories.items():
            assert len(entries) == 30, (
                f"category {cat_name}: expected 30 skills, got {len(entries)} "
                f"(cap from old nested layout must be removed)"
            )
        # Nothing should be reported hidden for the cap reason (the only
        # legitimate hidden reason now is name clamp collisions, which
        # don't happen here since all names are unique).
        assert hidden == 0

    def test_external_dirs_skills_included(self, tmp_path, monkeypatch):
        """Skills in ``skills.external_dirs`` must appear in /skill autocomplete.

        #18741 fixed this for the flat ``discord_skill_commands`` collector
        but left ``discord_skill_commands_by_category`` (the live caller for
        Discord's ``/skill`` command) still filtering by
        ``SKILLS_DIR`` prefix only. Regression guard that both collectors
        now accept external-dir skills.
        """
        from unittest.mock import patch

        local_skills_dir = tmp_path / "local-skills"
        external_dir = tmp_path / "external-skills"

        (local_skills_dir / "creative" / "local-skill").mkdir(parents=True)
        (local_skills_dir / "creative" / "local-skill" / "SKILL.md").write_text("", encoding="utf-8")

        (external_dir / "mlops" / "external-skill").mkdir(parents=True)
        (external_dir / "mlops" / "external-skill" / "SKILL.md").write_text("", encoding="utf-8")

        fake_cmds = {
            "/local-skill": {
                "name": "local-skill",
                "description": "Local",
                "skill_md_path": str(local_skills_dir / "creative" / "local-skill" / "SKILL.md"),
            },
            "/external-skill": {
                "name": "external-skill",
                "description": "External",
                "skill_md_path": str(external_dir / "mlops" / "external-skill" / "SKILL.md"),
            },
        }
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        with (
            patch("agent.skill_commands.get_skill_commands", return_value=fake_cmds),
            patch("tools.skills_tool.SKILLS_DIR", local_skills_dir),
            patch(
                "agent.skill_utils.get_external_skills_dirs",
                return_value=[external_dir],
            ),
        ):
            categories, uncategorized, hidden = discord_skill_commands_by_category(
                reserved_names=set(),
            )

        # Local skill → grouped under "creative"
        assert "creative" in categories
        assert any(n == "local-skill" for n, _d, _k in categories["creative"])
        # External skill → grouped under its own top-level dir "mlops"
        assert "mlops" in categories, (
            "external-dir skills must be included — the old SKILLS_DIR-only "
            "prefix check was broken for by_category (completes #18741)"
        )
        assert any(n == "external-skill" for n, _d, _k in categories["mlops"])
        assert uncategorized == []
        assert hidden == 0


# ---------------------------------------------------------------------------
# Plugin slash command integration
# ---------------------------------------------------------------------------

class TestPluginCommandEnumeration:
    """Plugin commands registered via ctx.register_command() must be surfaced
    by gateway enumerators (gateway help, Discord menus, etc.).
    """

    def _patch_plugin_commands(self, monkeypatch, commands):
        """Monkeypatch hermes_cli.plugins.get_plugin_commands() to a fixed dict."""
        from hermes_cli import plugins as _plugins_mod

        monkeypatch.setattr(
            _plugins_mod, "get_plugin_commands", lambda: dict(commands)
        )

    def test_plugin_command_surfaces_in_gateway_enumerators(self, monkeypatch):
        self._patch_plugin_commands(monkeypatch, {
            "my-plugin-cmd": {
                "handler": lambda _a: "ok",
                "description": "desc",
                "args_hint": "",
                "plugin": "p",
            }
        })
        from hermes_cli.commands import _iter_plugin_command_entries

        names = {name for name, _desc, _hint in _iter_plugin_command_entries()}
        assert "my-plugin-cmd" in names, (
            "plugin commands must be surfaced by gateway enumerators"
        )

    def test_plugin_enumerator_handles_missing_plugin_manager(self, monkeypatch):
        """Enumerators must never raise when plugin discovery raises."""
        from hermes_cli import plugins as _plugins_mod

        def _boom():
            raise RuntimeError("plugin system down")

        monkeypatch.setattr(_plugins_mod, "get_plugin_commands", _boom)

        # The enumerator swallows the failure and yields nothing.
        from hermes_cli.commands import _iter_plugin_command_entries

        assert _iter_plugin_command_entries() == []
