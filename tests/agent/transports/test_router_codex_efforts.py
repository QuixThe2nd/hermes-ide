"""Gateway-declared reasoning-effort vocabularies on the codex transport.

A gateway that validates ``reasoning.effort`` per model — Ramp Router
(api.router.com) was the motivating vendor — 400s on an unsupported level and
on any reasoning field for a non-reasoning model. The vendor-agnostic seam is
``ProviderProfile.supported_reasoning_efforts`` (providers/base.py): a
registered profile declares each model's vocabulary and the Responses
transport clamps onto it. Router's bundled plugin — the ``/v1/models`` effort
parser, its disk mirror, the background warmer, and the credentialed catalog
fetch — was retired in db4ffa25327; what survives is this hook, the real
provider registry, and the transport clamp pinned here through both.

Tri-state declaration (providers/base.py):

- non-empty tuple — clamp onto it (nearest weaker, never escalate);
- ``()`` — the model takes no reasoning field; the transport omits it;
- ``None`` — unknown/undeclared: the default route vocabulary applies.

No network: the fixture profiles raise if their catalog is fetched on the
request hot path.
"""

import pytest

import agent.model_metadata
from agent.transports import get_transport
from providers import get_provider_profile
from providers.base import ProviderProfile

DECLARED_HOST = "https://gw-clamp.test/v1"
FOREIGN_HOST = "https://generic.example.com/v1"


class _GatewayProfile(ProviderProfile):
    """Minimal externally-provided gateway profile: a per-model declaration.

    Below the public integration boundary the same way the retired Router
    plugin was above it: a real ``ProviderProfile`` registered through the
    normal ``providers.register_provider`` seam, consulted by the real
    transport. ``fetch_models`` doubles as the hot-path tripwire.
    """

    def __init__(self, efforts_by_model, name="effort-gw"):
        super().__init__(name=name, api_mode="codex_responses")
        self._efforts_by_model = efforts_by_model

    def supported_reasoning_efforts(self, model):
        return self._efforts_by_model.get(model)

    def fetch_models(self, **_kwargs):
        raise AssertionError("request hot path fetched the provider catalog")


@pytest.fixture
def transport():
    import agent.transports.codex  # noqa: F401
    return get_transport("codex_responses")


@pytest.fixture
def registered():
    """Register profiles for one test and restore the registry afterwards."""
    import providers as _providers

    _providers._discover_providers()
    snapshot = (dict(_providers._REGISTRY), dict(_providers._ALIASES), _providers._PROVIDER_LIST_CACHE)
    yield _providers.register_provider
    _providers._REGISTRY.clear()
    _providers._REGISTRY.update(snapshot[0])
    _providers._ALIASES.clear()
    _providers._ALIASES.update(snapshot[1])
    _providers._PROVIDER_LIST_CACHE = snapshot[2]


class TestProfileContract:
    def test_declared_vocabulary_resolves_through_the_real_registry(self, registered):
        registered(_GatewayProfile({
            "gw-reasoner": ("minimal", "low", "medium", "high", "xhigh"),
            "gw-vision": (),
        }))
        profile = get_provider_profile("effort-gw")
        assert profile is not None, "registered profile must resolve by name"
        assert profile.supported_reasoning_efforts("gw-reasoner") == (
            "minimal", "low", "medium", "high", "xhigh",
        )
        # () is definitive (non-reasoning), unknown model and the base-class
        # default are None (undeclared -> transport keeps its own vocabulary).
        assert profile.supported_reasoning_efforts("gw-vision") == ()
        assert profile.supported_reasoning_efforts("some-byok-route") is None
        assert ProviderProfile(name="bare").supported_reasoning_efforts("gw-reasoner") is None

    def test_unregistered_provider_is_none_and_keeps_default_behavior(self, transport, registered):
        assert get_provider_profile("never-registered") is None
        kw = transport.build_kwargs(
            model="some-byok-route",
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            base_url=FOREIGN_HOST,
            session_id="sid",
            provider="never-registered",
            reasoning_config={"effort": "max"},
        )
        # No declaration -> the default codex vocabulary (legacy ladder has
        # xhigh but no max: max clamps to xhigh).
        assert kw["reasoning"]["effort"] == "xhigh"


class TestTransportClamp:
    def _kwargs(self, transport, model, reasoning_config=None, provider="effort-gw",
                base_url=DECLARED_HOST):
        return transport.build_kwargs(
            model=model,
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            base_url=base_url,
            session_id="sid",
            provider=provider,
            reasoning_config=reasoning_config,
        )

    def test_clamps_to_declared_vocabulary(self, transport, registered):
        registered(_GatewayProfile({
            "gw-reasoner": ("minimal", "low", "medium", "high", "xhigh"),
        }))
        # No "max" in the declaration — nearest weaker supported is xhigh.
        kw = self._kwargs(transport, "gw-reasoner", {"effort": "max"})
        assert kw["reasoning"]["effort"] == "xhigh"

    def test_supported_effort_passes_through(self, transport, registered):
        registered(_GatewayProfile({
            "gw-reasoner": ("minimal", "low", "medium", "high", "xhigh"),
        }))
        kw = self._kwargs(transport, "gw-reasoner", {"effort": "high"})
        assert kw["reasoning"]["effort"] == "high"

    def test_non_reasoning_model_suppresses_reasoning(self, transport, registered):
        registered(_GatewayProfile({"gw-vision": ()}))
        # Default reasoning_config is enabled — the () verdict must strip the
        # reasoning field entirely (the gateway 400s rather than ignoring it).
        kw = self._kwargs(transport, "gw-vision")
        assert "reasoning" not in kw
        assert kw.get("include") == []

    def test_unknown_model_falls_back_to_codex_default(self, transport, registered):
        registered(_GatewayProfile({
            "gw-reasoner": ("minimal", "low", "medium", "high", "xhigh"),
        }))
        # Not in the declaration -> None -> default codex vocabulary (legacy
        # has xhigh but no max: max clamps to xhigh, medium is untouched).
        kw = self._kwargs(transport, "some-byok-route", {"effort": "max"})
        assert kw["reasoning"]["effort"] == "xhigh"
        kw = self._kwargs(transport, "some-byok-route", {"effort": "medium"})
        assert kw["reasoning"]["effort"] == "medium"

    def test_unrecognized_declared_levels_are_dropped_at_clamp(self, transport, registered):
        # The retired ingest dropped unknown vendor tiers at parse time; the
        # surviving policy is clamp-time: only recognized ladder levels clamp,
        # so a new tier in the declaration is ignored, not sent.
        registered(_GatewayProfile({"future-model": ("low", "hyperthink", "high")}))
        kw = self._kwargs(transport, "future-model", {"effort": "max"})
        assert kw["reasoning"]["effort"] == "high"

    def test_fully_unrecognized_declaration_passes_the_request_through(
        self, transport, registered
    ):
        # A declaration with no recognized level cannot clamp; the transport
        # fails open and forwards the requested level verbatim instead of
        # suppressing reasoning or silently substituting the route default.
        registered(_GatewayProfile({"alien-model": ("hyperthink", "galaxy")}))
        kw = self._kwargs(transport, "alien-model", {"effort": "max"})
        assert kw["reasoning"]["effort"] == "max"

    def test_undeclared_route_never_blocks_and_never_fetches(self, transport, registered):
        # Cold/empty declaration -> None -> default vocabulary, and the
        # request hot path must not hit the provider catalog (fetch_models
        # raises on any attempt).
        registered(_GatewayProfile({}))
        kw = self._kwargs(transport, "gw-reasoner", {"effort": "xhigh"})
        assert kw["reasoning"]["effort"] == "xhigh"


class TestRouteIsolation:
    def test_other_providers_unaffected(self, transport, registered):
        registered(_GatewayProfile({"gw-vision": ()}))
        kw = transport.build_kwargs(
            model="gw-vision",
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            base_url=FOREIGN_HOST,
            session_id="sid",
            provider="other-relay",
            reasoning_config={"effort": "medium"},
        )
        # The declaration's () verdict for gw-vision must not leak into other
        # providers' requests.
        assert kw["reasoning"]["effort"] == "medium"

    def test_declared_vocab_cannot_hijack_native_codex_backend(self, transport, registered):
        registered(_GatewayProfile({"gpt-5.4": ()}))
        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            base_url="https://chatgpt.com/backend-api/codex",
            session_id="sid",
            provider="openai-codex",
            is_codex_backend=True,
            reasoning_config={"effort": "max"},
        )
        # The native route keeps its own per-model ladder (gpt-5.4 -> legacy,
        # max clamps to xhigh); a profile declaration never suppresses it.
        assert kw["reasoning"]["effort"] == "xhigh"


class TestHostResolvedProfile:
    def test_named_custom_route_at_declared_host_gets_the_vocabulary(
        self, transport, registered, monkeypatch
    ):
        # A ``custom:<name>`` entry pointed at a host whose profile is
        # registered rides the host mapping onto this transport: the
        # vocabulary follows the host, not the config-entry name and not the
        # generic custom declaration (which would have let "max" through).
        registered(_GatewayProfile({
            "gw-reasoner": ("minimal", "low", "medium", "high", "xhigh"),
        }))
        monkeypatch.setitem(agent.model_metadata._URL_TO_PROVIDER, "gw-clamp.test", "effort-gw")
        kw = transport.build_kwargs(
            model="gw-reasoner",
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            base_url=DECLARED_HOST,
            session_id="sid",
            provider="custom:my-proxy",
            reasoning_config={"effort": "max"},
        )
        assert kw["reasoning"]["effort"] == "xhigh"

    def test_generic_custom_route_keeps_openai_compat_vocabulary(self, transport):
        # Shipped custom declaration: an undiscoverable endpoint's ceiling is
        # the widest OpenAI-compat set, so a configured "max" is not demoted.
        kw = transport.build_kwargs(
            model="some-relay-model",
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            base_url=FOREIGN_HOST,
            session_id="sid",
            provider="custom:my-relay",
            reasoning_config={"effort": "max"},
        )
        assert kw["reasoning"]["effort"] == "max"

    def test_foreign_host_does_not_borrow_the_declared_vocabulary(
        self, transport, registered, monkeypatch
    ):
        registered(_GatewayProfile({
            "gw-reasoner": ("minimal", "low", "medium", "high", "xhigh"),
        }))
        monkeypatch.setitem(agent.model_metadata._URL_TO_PROVIDER, "gw-clamp.test", "effort-gw")
        kw = transport.build_kwargs(
            model="gw-reasoner",
            messages=[{"role": "user", "content": "Hi"}],
            tools=[],
            base_url=FOREIGN_HOST,
            session_id="sid",
            provider="my-proxy",
            reasoning_config={"effort": "max"},
        )
        # No host match and "my-proxy" is unregistered -> default codex
        # vocabulary (legacy: no max -> xhigh).
        assert kw["reasoning"]["effort"] == "xhigh"
