"""Tests for GitHub Copilot entries shown in the /model picker."""

import os
import stat
from unittest.mock import patch

import pytest

import providers
from providers import register_provider
from providers.base import ProviderProfile

from hermes_cli.model_switch import list_authenticated_providers
from hermes_cli import model_switch_providers
from hermes_cli import models
from hermes_cli.models import provider_model_ids


@patch.dict(os.environ, {"GH_TOKEN": "test-key"}, clear=False)
def test_copilot_picker_uses_live_catalog_when_available():
    live_models = ["gpt-5.4", "claude-sonnet-4.6", "gemini-3.1-pro-preview"]

    with patch("agent.models_dev.fetch_models_dev", return_value={}), \
         patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value="gh-token"), \
         patch("hermes_cli.models._fetch_github_models", return_value=live_models):
        providers_rows = list_authenticated_providers(current_provider="openrouter", max_models=50)

    copilot = next((p for p in providers_rows if p["slug"] == "copilot"), None)

    assert copilot is not None
    assert copilot["models"] == live_models
    assert copilot["total_models"] == len(live_models)


# --- copilot-acp: external_process availability (#63662) -------------------
#
# copilot-acp holds no API key, OAuth token, or credential-pool entry by
# design — a registered ACP provider's subprocess brings its own auth. The
# bundled first-party profile was stripped (#719, db4ffa25327): the static
# registry row is identity residue, NOT a launch spec, so an unregistered
# copilot-acp must stay hidden and undiscoverable no matter what executable or
# retired env vars the host carries. Registering an out-of-tree ProviderProfile
# is the only seam that configures it (381dfb190bf).

# Env var names carried by the retired first-party CopilotACPProfile; core no
# longer reads them, they are here as residue for the fail-closed tests.
_RETIRED_LAUNCH_ENV = ("HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH",
                       "HERMES_COPILOT_ACP_ARGS", "COPILOT_ACP_BASE_URL")


@pytest.fixture()
def _no_other_copilot_creds(monkeypatch):
    """Make sure copilot-acp visibility comes ONLY from a registered profile:
    no env tokens, no configured ACP endpoint, no auth-store entry, no seeded
    credential pool, none of the retired launch env vars."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN", *_RETIRED_LAUNCH_ENV):
        monkeypatch.delenv(var, raising=False)
    import hermes_cli.auth as auth
    import hermes_cli.model_switch as model_switch

    monkeypatch.setattr(auth, "_load_auth_store", lambda: {})
    monkeypatch.setattr(model_switch_providers, "_credential_pool_is_usable", lambda *a, **k: False)


@pytest.fixture
def fake_copilot_bin(tmp_path, monkeypatch):
    """A deterministic no-op ``copilot`` executable first on PATH — never the host's."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "copilot"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bindir


@pytest.fixture
def registered_copilot_acp(monkeypatch):
    """Register an out-of-tree copilot-acp profile with an injected deterministic probe.

    ``fetch_models`` is the entire model-discovery surface for this provider:
    the retired first-party profile drove its subprocess from exactly this
    hook, and ``hermes_cli.models._copilot_acp_session_models`` calls nothing
    else — no real binary, network or credentials are involved. The registry is
    process-global with no unregister, so it is snapshotted and restored to
    keep the fail-closed tests in this file honest.
    """
    providers._discover_providers()
    snapshot = (dict(providers._REGISTRY), dict(providers._ALIASES), providers._PROVIDER_LIST_CACHE)
    monkeypatch.setattr(models, "_copilot_acp_session_memo", None)
    register_provider(
        _ProbedCopilotACPProfile(
            name="copilot-acp",
            display_name="Copilot ACP (out-of-tree)",
            base_url="acp://copilot",
            auth_type="external_process",
            process_command="copilot",
            process_args=("--acp", "--stdio"),
        )
    )
    yield _ProbedCopilotACPProfile
    providers._REGISTRY.clear()
    providers._REGISTRY.update(snapshot[0])
    providers._ALIASES.clear()
    providers._ALIASES.update(snapshot[1])
    providers._PROVIDER_LIST_CACHE = snapshot[2]
    _ProbedCopilotACPProfile.result = None
    _ProbedCopilotACPProfile.error = None
    _ProbedCopilotACPProfile.calls = 0


class _ProbedCopilotACPProfile(ProviderProfile):
    """Out-of-tree copilot-acp stand-in whose catalog probe is injected per test."""

    # Plain class attributes (no annotations) so the dataclass sees no new fields.
    result = None   # list[str] | None — the probe's catalog answer
    error = None    # Exception | None — the probe's failure mode
    calls = 0       # how many times the probe ran

    def fetch_models(self, **kwargs):
        cls = type(self)
        cls.calls += 1
        if cls.error is not None:
            raise cls.error
        return cls.result


def test_unregistered_copilot_acp_stays_hidden_with_cli_and_env_residue(
        fake_copilot_bin, monkeypatch, _no_other_copilot_creds):
    """Executable on PATH, retired launch env vars set, no registered profile → hidden.

    The env residue is exactly what the deleted first-party profile used to
    read; residue is not registration."""
    for var in _RETIRED_LAUNCH_ENV:
        monkeypatch.setenv(var, str(fake_copilot_bin / "copilot")
                           if var != "HERMES_COPILOT_ACP_ARGS" else "--acp --stdio")

    with patch("agent.models_dev.fetch_models_dev", return_value={}), \
         patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=None), \
         patch("hermes_cli.models._fetch_github_models", return_value=[]):
        rows = list_authenticated_providers(current_provider="openrouter", max_models=50)

    assert all(p["slug"] != "copilot-acp" for p in rows), \
        "copilot-acp must stay hidden when no provider profile is registered"


def test_unregistered_copilot_acp_discovers_no_session_models(monkeypatch, _no_other_copilot_creds):
    """With no registered profile there is no discovery path at all: no probe hook
    exists, nothing launches, and the catalog degrades to GitHub/static sources."""
    monkeypatch.setattr(models, "_copilot_acp_session_memo", None)

    with patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=""), \
         patch("hermes_cli.models._fetch_github_models", return_value=[]) as github:
        catalog = provider_model_ids("copilot-acp")

    assert providers.get_provider_profile("copilot-acp") is None, \
        "unregistered copilot-acp must resolve to no profile"
    assert github.called, "the GitHub catalog is the only remaining source"
    assert catalog == list(models._PROVIDER_MODELS.get("copilot", [])), \
        "no session models may leak in; only the static fallback remains"
    assert models._copilot_acp_session_memo[2] is None, "the miss must be memoized as a failure"


def test_registered_copilot_acp_is_listed_when_its_executable_resolves(
        fake_copilot_bin, monkeypatch, _no_other_copilot_creds, registered_copilot_acp):
    with patch("agent.models_dev.fetch_models_dev", return_value={}), \
         patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=None), \
         patch("hermes_cli.models._fetch_github_models", return_value=[]):
        rows = list_authenticated_providers(current_provider="openrouter", max_models=50)

    acp = next((p for p in rows if p["slug"] == "copilot-acp"), None)

    assert acp is not None, "copilot-acp must be listed once its registered profile resolves an executable"
    assert acp["models"], "copilot-acp row must offer at least the curated fallback models"


@pytest.mark.parametrize(
    ("session_probe", "github_token", "github_models", "expected"),
    [
        # Signed-in session (probe answers), no GitHub token anywhere: the session list wins,
        # the API is never asked.
        ({"result": ["auto", "gpt-5.6-sol", "claude-sonnet-5"]}, "", [], ["auto", "gpt-5.6-sol", "claude-sonnet-5"]),
        # Session probe fails: token-based GitHub discovery is still the next source.
        ({"error": TimeoutError("probe timeout")}, "catalog-token", ["api-fallback-model"], ["api-fallback-model"]),
    ],
    ids=["session-wins-without-token", "github-fallback-when-probe-fails"],
)
def test_copilot_acp_catalog_prefers_registered_profile_session(
        registered_copilot_acp, monkeypatch, session_probe, github_token, github_models, expected):
    probe = registered_copilot_acp
    probe.result = session_probe.get("result")
    probe.error = session_probe.get("error")

    with patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=github_token), \
         patch("hermes_cli.models._fetch_github_models", return_value=github_models) as github:
        assert provider_model_ids("copilot-acp", force_refresh=True) == expected

    assert probe.calls == 1
    assert github.called is bool(github_token)


def test_registered_profile_session_probe_is_memoized_across_model_switch_validation(registered_copilot_acp):
    """``/model`` validation reads the catalog uncached on every switch; each miss runs the
    probe. A run of switches must pay one probe, and a failed probe must not be retried per
    switch."""
    from hermes_cli.models_validate import validate_requested_model

    probe = registered_copilot_acp
    probe.result = ["gpt-5.6-terra"]
    with patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=""), \
         patch("hermes_cli.models._fetch_github_models", return_value=[]):
        for _ in range(3):
            verdict = validate_requested_model("gpt-5.6-terra", "copilot-acp", api_key="copilot-acp", base_url="acp://copilot")
            assert verdict["accepted"] and verdict["recognized"]
    assert probe.calls == 1

    models._copilot_acp_session_memo = None  # (teardown in registered_copilot_acp restores it)
    probe.result, probe.error = None, RuntimeError("not signed in")
    with patch("hermes_cli.models._resolve_copilot_catalog_api_key", return_value=""), \
         patch("hermes_cli.models._fetch_github_models", return_value=[]):
        for _ in range(3):
            provider_model_ids("copilot-acp")
    assert probe.calls == 2, "the failure memo must absorb the repeated switches (one fresh probe)"
