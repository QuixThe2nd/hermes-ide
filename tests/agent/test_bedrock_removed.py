"""The AWS Bedrock provider is removed from this fork.

Regression guard for the strip: Bedrock must be neither importable nor
discoverable — no adapter/transport/setup-flow modules, no model-provider
plugin profile, no transport registration for ``bedrock_converse``, no CLI
provider overlay — and a user ``config.yaml`` that still carries a legacy
``bedrock:`` section must keep loading (unknown sections are ignored, never
fatal).
"""

import importlib.util
from pathlib import Path

import pytest

REMOVED_MODULES = (
    "agent.bedrock_adapter",
    "agent.transports.bedrock",
    "hermes_cli.model_setup_flows_bedrock",
)


@pytest.mark.parametrize("module_name", REMOVED_MODULES)
def test_bedrock_module_removed(module_name: str) -> None:
    assert importlib.util.find_spec(module_name) is None, f"{module_name} still exists"


def test_bedrock_provider_plugin_removed() -> None:
    import providers

    names = {profile.name for profile in providers.list_providers()}
    assert "bedrock" not in names
    assert providers.get_provider_profile("bedrock") is None
    assert providers.get_provider_profile("aws") is None


def test_bedrock_transport_not_registered() -> None:
    from agent.transports import get_transport

    assert get_transport("bedrock_converse") is None


def test_bedrock_not_a_cli_provider() -> None:
    from hermes_cli import providers as cli_providers

    assert cli_providers.get_provider("bedrock") is None
    assert cli_providers.determine_api_mode("bedrock") == "chat_completions"


def test_legacy_bedrock_config_section_still_loads(tmp_path, monkeypatch) -> None:
    """A config.yaml written before the strip (with ``bedrock:`` keys) must load."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        """
model:
  provider: openrouter
  model: some/model
bedrock:
  region: us-east-1
  discovery:
    enabled: true
  guardrail:
    guardrail_identifier: abc123
    guardrail_version: "1"
""",
        encoding="utf-8",
    )
    from hermes_cli.config import load_config

    cfg = load_config()
    assert cfg["model"]["provider"] == "openrouter"
    assert cfg["model"]["default"] == "some/model"
    assert "region" not in cfg["model"]


def test_bedrock_docs_removed() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    assert not (repo_root / "website" / "docs" / "guides" / "aws-bedrock.md").exists()
    assert not (repo_root / "plugins" / "model-providers" / "bedrock").exists()
