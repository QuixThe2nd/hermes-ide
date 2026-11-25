"""Tests for the unified provider catalog (hermes_cli.provider_catalog).

These are invariant tests, not snapshots: they assert the parity *contract*
between what ``hermes model`` shows (``CANONICAL_PROVIDERS``) and what the
catalog exposes, plus how each provider's ``auth_type`` maps to a desktop tab —
never a specific provider count or a frozen vendor list (both change over time).
"""

from hermes_cli.models import CANONICAL_PROVIDERS
from hermes_cli.provider_catalog import (
    ProviderDescriptor,
    provider_catalog,
    provider_catalog_by_slug,
    tab_for_auth_type,
)






def test_profileless_providers_still_present():
    """Providers without a ProviderProfile must still resolve via fallbacks.

    lmstudio / openai-api / tencent-tokenhub / xai-oauth have no profile on
    main; they exist only as registry + canonical entries. The catalog must
    not require a profile to include a provider.
    """
    by = provider_catalog_by_slug()
    for slug in ("lmstudio", "openai-api", "tencent-tokenhub", "xai-oauth"):
        assert slug in by, f"{slug} dropped from catalog (profile-less provider)"
        assert by[slug].label, f"{slug} has empty label despite canonical fallback"
        assert by[slug].description, f"{slug} has empty description despite fallback"


def test_copilot_surfaces_as_a_provider_with_its_own_token_var():
    """Regression for the reported bug: a GitHub Copilot login showed up under
    tools, never as a provider, because the shared GITHUB_TOKEN is tool-category.

    Copilot authenticates via the `copilot`/api_key path, so it belongs on the
    keys tab — but its PRIMARY credential var must be the provider-owned
    COPILOT_GITHUB_TOKEN, not the shared tool-category GITHUB_TOKEN. That is what
    lets the desktop render Copilot as its own provider card.
    """
    by = provider_catalog_by_slug()
    assert "copilot" in by
    d = by["copilot"]
    assert d.tab == "keys"
    assert d.api_key_env_vars, "Copilot must expose a credential env var"
    assert d.api_key_env_vars[0] == "COPILOT_GITHUB_TOKEN", (
        "Copilot's primary var must be the provider-owned token, not shared GITHUB_TOKEN"
    )


def test_api_key_providers_expose_a_credential_env_var():
    """Every keys-tab provider that authenticates via a pasted API key must
    surface at least one env var to write the key into (otherwise the GUI can't
    configure it).

    Exemptions: the ``custom`` bring-your-own-endpoint pseudo-provider
    (configured inline via the ``local-endpoint`` flow). ``aws_sdk`` providers
    (out-of-tree plugins) authenticate via AWS_REGION/AWS_PROFILE, not a key.
    """
    exempt = {"custom"}
    for d in provider_catalog():
        if d.auth_type == "api_key" and d.slug not in exempt:
            assert d.api_key_env_vars, f"{d.slug} is api_key but exposes no env var"


def test_overlay_only_providers_expose_their_overlay_env_vars():
    """A provider whose only metadata source is a Hermes overlay (no PROVIDER_REGISTRY
    entry, no ProviderProfile) must surface the overlay's ``extra_env_vars`` as its
    credential vars — an api_key descriptor with no env var is a keys-tab provider the
    GUI cannot configure.

    Regression: fireworks' provider plugin was deleted in #719 ("keep only the LiteLLM
    model provider") but its OpenAI-compatible endpoint and the advertised
    FIREWORKS_API_KEY remain, so the overlay is the one source left.
    """
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.provider_catalog import _is_url_var
    from hermes_cli.providers import HERMES_OVERLAYS
    from providers import list_providers

    profiled = {p.name for p in list_providers()}
    checked = 0
    for slug, overlay in HERMES_OVERLAYS.items():
        if slug in PROVIDER_REGISTRY or slug in profiled:
            continue
        if overlay.auth_type != "api_key" or not overlay.extra_env_vars:
            continue
        d = provider_catalog_by_slug().get(slug)
        if d is None:
            continue  # not part of the hermes model universe; the catalog lists CANONICAL_PROVIDERS only
        assert d.api_key_env_vars == tuple(v for v in overlay.extra_env_vars if not _is_url_var(v)), (
            f"{slug} must expose exactly its overlay's credential env vars"
        )
        checked += 1
    assert checked >= 1, "no overlay-only provider found — the test lost its subject"



def test_tab_for_auth_type_helper():
    assert tab_for_auth_type("api_key") == "keys"
    assert tab_for_auth_type("aws_sdk") == "keys"
    assert tab_for_auth_type("oauth_external") == "accounts"
    assert tab_for_auth_type("oauth_device_code") == "accounts"
    assert tab_for_auth_type("copilot") == "accounts"
    assert tab_for_auth_type("external_process") == "accounts"
