from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "migration"
    / "openclaw-migration"
    / "scripts"
    / "openclaw_to_hermes.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("openclaw_to_hermes", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_skills_guard():
    spec = importlib.util.spec_from_file_location(
        "skills_guard_local",
        Path(__file__).resolve().parents[2] / "tools" / "skills_guard.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_extract_markdown_entries_promotes_heading_context():
    mod = load_module()
    text = """# MEMORY.md - Long-Term Memory

## Tyler Williams

- Founder of VANTA Research
- Timezone: America/Los_Angeles

### Active Projects

- Hermes Agent
"""
    entries = mod.extract_markdown_entries(text)
    assert "Tyler Williams: Founder of VANTA Research" in entries
    assert "Tyler Williams: Timezone: America/Los_Angeles" in entries
    assert "Tyler Williams > Active Projects: Hermes Agent" in entries




def test_merge_entries_respects_limit_and_reports_overflow():
    mod = load_module()
    existing = ["alpha"]
    incoming = ["beta", "gamma is too long"]
    merged, stats, overflowed = mod.merge_entries(existing, incoming, limit=12)
    assert merged == ["alpha", "beta"]
    assert stats["added"] == 1
    assert stats["overflowed"] == 1
    assert overflowed == ["gamma is too long"]










def test_migrator_copies_skill(tmp_path: Path):
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    target.mkdir()

    (source / "workspace" / "skills" / "demo-skill").mkdir(parents=True)
    (source / "workspace" / "skills" / "demo-skill" / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: demo\n---\n\nbody\n",
        encoding="utf-8",
    )

    migrator = mod.Migrator(
        source_root=source,
        target_root=target,
        execute=True,
        workspace_target=None,
        overwrite=False,
        migrate_secrets=False,
        output_dir=target / "migration-report",
    )
    report = migrator.migrate()

    imported_skill = target / "skills" / mod.SKILL_CATEGORY_DIRNAME / "demo-skill" / "SKILL.md"
    assert imported_skill.exists()
    assert report["summary"]["migrated"] >= 1


def _mcp_migrator(mod, tmp_path: Path, existing_config: str):
    """Migrator wired to merge an MCP server into config.yaml."""
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    target.mkdir()
    source.mkdir(parents=True)
    (source / "openclaw.json").write_text(
        json.dumps({
            "mcp": {
                "servers": {
                    "imported": {"command": "uvx", "args": ["imported-mcp"]},
                },
            },
        }),
        encoding="utf-8",
    )
    (target / "config.yaml").write_text(existing_config, encoding="utf-8")
    return mod.Migrator(
        source_root=source,
        target_root=target,
        execute=True,
        workspace_target=None,
        overwrite=False,
        migrate_secrets=False,
        output_dir=target / "migration-report",
    ), target / "config.yaml"


MALFORMED_HERMES_CONFIG = """\
model: hermes-4-405b
api_key_env: OPENROUTER_API_KEY
mcp_servers: [unclosed
telegram:
  enabled: true
"""


def test_unreadable_config_is_refused_not_overwritten(tmp_path: Path):
    """A present-but-unparseable config.yaml must survive the migration."""
    mod = load_module()
    migrator, config_path = _mcp_migrator(
        mod, tmp_path, MALFORMED_HERMES_CONFIG)
    before = config_path.read_bytes()

    report = migrator.migrate()

    assert config_path.read_bytes() == before
    items = [i for i in report["items"] if i["kind"] == "mcp-servers"]
    assert items and items[0]["status"] == mod.STATUS_ERROR
    assert "not valid YAML" in items[0]["reason"]


def test_unreadable_config_blocks_later_config_steps_instead_of_partial_writes(
        tmp_path: Path):
    """One refusal flips the existing _config_apply_blocked short-circuit."""
    mod = load_module()
    migrator, config_path = _mcp_migrator(
        mod, tmp_path, MALFORMED_HERMES_CONFIG)

    report = migrator.migrate()

    assert migrator._config_apply_blocked is True
    statuses = {
        i["status"] for i in report["items"]
        if i["kind"] in mod.Migrator._CONFIG_MUTATING_OPTIONS
    }
    assert "migrated" not in statuses
    assert config_path.read_text(encoding="utf-8") == MALFORMED_HERMES_CONFIG


def test_readable_config_keeps_every_pre_existing_key(tmp_path: Path):
    mod = load_module()
    migrator, config_path = _mcp_migrator(
        mod,
        tmp_path,
        "model: hermes-4-405b\n"
        "api_key_env: OPENROUTER_API_KEY\n"
        "mcp_servers:\n  local:\n    command: uvx\n",
    )

    migrator.migrate()

    import yaml

    merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert merged["model"] == "hermes-4-405b"
    assert merged["api_key_env"] == "OPENROUTER_API_KEY"
    assert "local" in merged["mcp_servers"]
    assert "imported" in merged["mcp_servers"]


def test_absent_config_is_still_created(tmp_path: Path):
    """The guard must not break first-time creation.

    Only ``absent`` may read as ``{}``; ``model-config`` creates config.yaml
    from scratch when the target has none.
    """
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    target.mkdir()
    source.mkdir()
    (source / "openclaw.json").write_text(
        json.dumps({"agents": {"defaults": {"model": "anthropic/claude-sonnet-4"}}}),
        encoding="utf-8",
    )
    config_path = target / "config.yaml"
    assert not config_path.exists()

    mod.Migrator(
        source_root=source, target_root=target, execute=True,
        workspace_target=None, overwrite=True, migrate_secrets=False,
        output_dir=None, selected_options={"model-config"},
    ).migrate()

    assert "anthropic/claude-sonnet-4" in config_path.read_text(encoding="utf-8")


@pytest.mark.require_symlinks
def test_symlinked_config_stays_a_symlink(tmp_path: Path):
    """Managed deployments symlink ~/.hermes/config.yaml into a dotfiles repo."""
    mod = load_module()
    real = tmp_path / "dotfiles" / "config.yaml"
    real.parent.mkdir(parents=True)
    real.write_text("model: hermes-4-405b\n", encoding="utf-8")
    migrator, config_path = _mcp_migrator(mod, tmp_path, "placeholder: true\n")
    config_path.unlink()
    config_path.symlink_to(real)

    migrator.migrate()

    assert config_path.is_symlink()
    assert config_path.resolve() == real.resolve()
    assert "imported" in real.read_text(encoding="utf-8")
    assert "hermes-4-405b" in real.read_text(encoding="utf-8")


def test_unreadable_config_refused_by_model_config_too(tmp_path: Path):
    """The refusal is at the shared helper, so every config step inherits it."""
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    target.mkdir()
    source.mkdir()
    (source / "openclaw.json").write_text(
        json.dumps({"agents": {"defaults": {"model": "anthropic/claude-sonnet-4"}}}),
        encoding="utf-8",
    )
    config_path = target / "config.yaml"
    config_path.write_text(MALFORMED_HERMES_CONFIG, encoding="utf-8")

    report = mod.Migrator(
        source_root=source, target_root=target, execute=True,
        workspace_target=None, overwrite=True, migrate_secrets=False,
        output_dir=None, selected_options={"model-config"},
    ).migrate()

    assert config_path.read_text(encoding="utf-8") == MALFORMED_HERMES_CONFIG
    items = [i for i in report["items"] if i["kind"] == "model-config"]
    assert items and items[0]["status"] == mod.STATUS_ERROR


def test_migrator_optionally_imports_supported_secrets_and_messaging_settings(tmp_path: Path):
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"

    (source / "credentials").mkdir(parents=True)
    (source / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {"workspace": "/tmp/openclaw-workspace"}},
                "channels": {"telegram": {"botToken": "123:abc"}},
            }
        ),
        encoding="utf-8",
    )
    (source / "credentials" / "telegram-default-allowFrom.json").write_text(
        json.dumps({"allowFrom": ["111", "222"]}),
        encoding="utf-8",
    )
    target.mkdir()

    migrator = mod.Migrator(
        source_root=source,
        target_root=target,
        execute=True,
        workspace_target=None,
        overwrite=False,
        migrate_secrets=True,
        output_dir=target / "migration-report",
    )
    migrator.migrate()

    env_text = (target / ".env").read_text(encoding="utf-8")
    assert "MESSAGING_CWD=/tmp/openclaw-workspace" in env_text
    assert "TELEGRAM_ALLOWED_USERS=111,222" in env_text
    assert "TELEGRAM_BOT_TOKEN=123:abc" in env_text








def test_source_candidate_finds_files_in_custom_workspace(tmp_path: Path):
    """When agents.defaults.workspace points outside ~/.openclaw, files should
    be discovered there as a fallback."""
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    custom_ws = tmp_path / "my-custom-workspace"

    target.mkdir()
    source.mkdir()
    custom_ws.mkdir()

    # No workspace/ directory inside .openclaw — files live in custom workspace
    (custom_ws / "MEMORY.md").write_text("# Memory\n\n- custom workspace entry\n", encoding="utf-8")
    (custom_ws / "SOUL.md").write_text("# Soul\n\nI am me.\n", encoding="utf-8")
    (custom_ws / "skills" / "my-skill").mkdir(parents=True)
    (custom_ws / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: test\n---\n\nbody\n",
        encoding="utf-8",
    )
    (custom_ws / "memory").mkdir()
    (custom_ws / "memory" / "2026-01-01.md").write_text("- daily note\n", encoding="utf-8")

    (source / "openclaw.json").write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(custom_ws)}}}),
        encoding="utf-8",
    )

    migrator = mod.Migrator(
        source_root=source,
        target_root=target,
        execute=True,
        workspace_target=None,
        overwrite=False,
        migrate_secrets=False,
        output_dir=target / "migration-report",
        selected_options={"soul", "memory", "skills", "daily-memory"},
    )
    report = migrator.migrate()

    # SOUL.md should have been found and migrated
    assert (target / "SOUL.md").exists()

    # MEMORY.md should have been found and migrated
    assert (target / "memories" / "MEMORY.md").exists()
    mem_content = (target / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert "custom workspace entry" in mem_content

    # Skills should have been found and migrated
    imported_skill = target / "skills" / mod.SKILL_CATEGORY_DIRNAME / "my-skill" / "SKILL.md"
    assert imported_skill.exists()

    migrated_kinds = {item["kind"] for item in report["items"] if item["status"] == "migrated"}
    assert "soul" in migrated_kinds
    assert "memory" in migrated_kinds
    assert "skill" in migrated_kinds












def test_slack_settings_migrated(tmp_path: Path):
    """Slack bot/app tokens and allowlist migrate to .env."""
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    target.mkdir()
    source.mkdir()

    (source / "openclaw.json").write_text(
        json.dumps({
            "channels": {
                "slack": {
                    "botToken": "xoxb-slack-bot",
                    "appToken": "xapp-slack-app",
                    "allowFrom": ["U111", "U222"],
                }
            }
        }),
        encoding="utf-8",
    )

    migrator = mod.Migrator(
        source_root=source, target_root=target, execute=True,
        workspace_target=None, overwrite=False, migrate_secrets=False, output_dir=None,
        selected_options={"slack-settings"},
    )
    report = migrator.migrate()
    env_text = (target / ".env").read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN=xoxb-slack-bot" in env_text
    assert "SLACK_APP_TOKEN=xapp-slack-app" in env_text
    assert "SLACK_ALLOWED_USERS=U111,U222" in env_text




def test_model_config_migrated(tmp_path: Path):
    """Default model setting migrates to config.yaml."""
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    target.mkdir()
    source.mkdir()

    (source / "openclaw.json").write_text(
        json.dumps({
            "agents": {"defaults": {"model": "anthropic/claude-sonnet-4"}}
        }),
        encoding="utf-8",
    )
    # config.yaml must exist for YAML merge to work
    (target / "config.yaml").write_text("model: openrouter/auto\n", encoding="utf-8")

    migrator = mod.Migrator(
        source_root=source, target_root=target, execute=True,
        workspace_target=None, overwrite=True, migrate_secrets=False, output_dir=None,
        selected_options={"model-config"},
    )
    report = migrator.migrate()
    config_text = (target / "config.yaml").read_text(encoding="utf-8")
    assert "anthropic/claude-sonnet-4" in config_text






def test_shared_skills_migrated(tmp_path: Path):
    """Shared skills from ~/.openclaw/skills/ are migrated."""
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    target.mkdir()

    # Create a shared skill (not in workspace/skills/)
    (source / "skills" / "my-shared-skill").mkdir(parents=True)
    (source / "skills" / "my-shared-skill" / "SKILL.md").write_text(
        "---\nname: my-shared-skill\ndescription: shared\n---\n\nbody\n",
        encoding="utf-8",
    )

    migrator = mod.Migrator(
        source_root=source, target_root=target, execute=True,
        workspace_target=None, overwrite=False, migrate_secrets=False, output_dir=None,
        selected_options={"shared-skills"},
    )
    report = migrator.migrate()
    imported = target / "skills" / mod.SKILL_CATEGORY_DIRNAME / "my-shared-skill" / "SKILL.md"
    assert imported.exists()


def test_daily_memory_merged(tmp_path: Path):
    """Daily memory notes from workspace/memory/*.md are merged into MEMORY.md."""
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    target.mkdir()

    mem_dir = source / "workspace" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "2026-03-01.md").write_text(
        "# March 1 Notes\n\n- User prefers dark mode\n- Timezone: PST\n",
        encoding="utf-8",
    )
    (mem_dir / "2026-03-02.md").write_text(
        "# March 2 Notes\n\n- Working on migration project\n",
        encoding="utf-8",
    )

    migrator = mod.Migrator(
        source_root=source, target_root=target, execute=True,
        workspace_target=None, overwrite=False, migrate_secrets=False, output_dir=None,
        selected_options={"daily-memory"},
    )
    report = migrator.migrate()
    mem_path = target / "memories" / "MEMORY.md"
    assert mem_path.exists()
    content = mem_path.read_text(encoding="utf-8")
    assert "dark mode" in content
    assert "migration project" in content


def test_provider_keys_require_migrate_secrets_flag(tmp_path: Path):
    """Provider keys migration is double-gated: needs option + --migrate-secrets."""
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    target.mkdir()
    source.mkdir()

    (source / "openclaw.json").write_text(
        json.dumps({
            "models": {
                "providers": {
                    "openrouter": {
                        "apiKey": "sk-or-test-key",
                        "baseUrl": "https://openrouter.ai/api/v1",
                    }
                }
            }
        }),
        encoding="utf-8",
    )

    # Without --migrate-secrets: should skip
    migrator = mod.Migrator(
        source_root=source, target_root=target, execute=True,
        workspace_target=None, overwrite=False, migrate_secrets=False, output_dir=None,
        selected_options={"provider-keys"},
    )
    report = migrator.migrate()
    env_path = target / ".env"
    if env_path.exists():
        assert "sk-or-test-key" not in env_path.read_text(encoding="utf-8")

    # With --migrate-secrets: should import
    migrator2 = mod.Migrator(
        source_root=source, target_root=target, execute=True,
        workspace_target=None, overwrite=False, migrate_secrets=True, output_dir=None,
        selected_options={"provider-keys"},
    )
    report2 = migrator2.migrate()
    env_text = (target / ".env").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-test-key" in env_text






def test_skill_installs_cleanly_under_skills_guard():
    skills_guard = load_skills_guard()
    result = skills_guard.scan_skill(
        SCRIPT_PATH.parents[1],
        source="official/migration/openclaw-migration",
    )

    # The migration script's references to agent config files are legitimate:
    # it mentions AGENTS.md to migrate workspace instructions and points the
    # user at ~/.hermes/config.yaml in its post-migration summary — it never
    # writes to either. Under skills-guard-v2 (#92021) these score as
    # informational _ref findings (the old critical agent_config_mod /
    # hermes_config_mod findings no longer fire for bare mentions), so the
    # verdict is "safe" with no modification-intent findings at all.
    assert result.verdict == "safe", f"Unexpected verdict: {result.verdict}"
    KNOWN_FALSE_POSITIVES = {"agent_config_ref", "hermes_config_ref"}
    for f in result.findings:
        assert f.pattern_id in KNOWN_FALSE_POSITIVES, f"Unexpected finding: {f}"


# ── rebrand_text tests ────────────────────────────────────────


def test_rebrand_text_replaces_openclaw_variants():
    mod = load_module()
    # Mixed-case / capitalized matches → capital-H ``Hermes``.
    assert mod.rebrand_text("OpenClaw prefers Python 3.11") == "Hermes prefers Python 3.11"
    assert mod.rebrand_text("I told Open Claw to use dark mode") == "I told Hermes to use dark mode"
    assert mod.rebrand_text("Open-Claw config is great") == "Hermes config is great"
    assert mod.rebrand_text("OPENCLAW uses tools well") == "Hermes uses tools well"
    # All-lowercase matches → lowercase ``hermes``; this preserves the
    # real filesystem path ``~/.hermes`` (Hermes home) when rebranding
    # memory entries that reference ``~/.openclaw`` or ``openclaw`` prose.
    assert mod.rebrand_text("openclaw should always respond concisely") == "hermes should always respond concisely"














# ── migrate_model_config: alias resolution (issue #16745) ──────────────────

def _run_model_migration(tmp_path: Path, openclaw_json: dict) -> dict:
    """Helper: run just migrate_model_config on an openclaw.json and return
    the parsed destination config.yaml."""
    import yaml

    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (source / "openclaw.json").write_text(json.dumps(openclaw_json), encoding="utf-8")

    migrator = mod.Migrator(
        source_root=source,
        target_root=target,
        execute=True,
        workspace_target=None,
        overwrite=True,
        migrate_secrets=False,
        output_dir=target / "migration-report",
    )
    migrator.migrate_model_config()

    cfg_path = target / "config.yaml"
    if not cfg_path.exists():
        return {}
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def _extract_model(parsed: dict) -> str | None:
    model = parsed.get("model")
    if isinstance(model, dict):
        return model.get("default")
    return model














# ── non-UTF-8 tolerance (issue #8901) ───────────────────────────────────────


def _write_invalid_utf8_json(path: Path, prefix: bytes, valid_value: bytes, suffix: bytes) -> None:
    """Write a JSON-shaped file containing one invalid UTF-8 byte (0xB3) inside
    a string value, alongside a separate, validly-encoded value. Used to check
    that a single bad byte does not prevent the rest of the file's data from
    being read (relies on read_text(..., errors="replace"))."""
    path.write_bytes(prefix + b"\xb3" + valid_value + suffix)




def test_messaging_settings_handles_invalid_utf8_in_telegram_allowlist(tmp_path: Path):
    """Telegram allowFrom file with a non-UTF-8 byte should not abort migration;
    valid user IDs elsewhere in the same file must still be imported."""
    mod = load_module()
    source = tmp_path / ".openclaw"
    target = tmp_path / ".hermes"
    source.mkdir()
    target.mkdir()

    creds_dir = source / "credentials"
    creds_dir.mkdir()
    _write_invalid_utf8_json(
        creds_dir / "telegram-default-allowFrom.json",
        prefix=b'{"allowFrom": ["bad',
        valid_value=b'", "123456789"]}',
        suffix=b"",
    )

    migrator = mod.Migrator(
        source_root=source, target_root=target, execute=True,
        workspace_target=None, overwrite=False, migrate_secrets=False, output_dir=None,
        selected_options={"messaging-settings"},
    )
    report = migrator.migrate()

    items = [i for i in report["items"] if i["kind"] == "messaging-settings"]
    assert items and items[0]["status"] == "migrated"
    env_text = (target / ".env").read_text(encoding="utf-8")
    assert "123456789" in env_text




