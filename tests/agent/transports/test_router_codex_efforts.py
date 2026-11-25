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

``DECLARED`` deliberately disagrees with every transport default for the
same request — it has ``minimal`` (which the legacy codex ladder lacks) and
stops below ``xhigh`` — so a declaration that is silently ignored cannot
reproduce the asserted outcomes. No network: fixture profiles count (and
raise on) any catalog fetch on the request hot path, because the resolver's
fail-open ``except`` would swallow the raise alone.
"""

import pytest

import agent.model_metadata
from agent.transports import get_transport
from providers import get_provider_profile
from providers.base import ProviderProfile

DECLARED_HOST = "https://gw-clamp.test/v1"
FOREIGN_HOST = "https://generic.example.com/v1"
DECLARED = ("minimal", "low", "medium", "high")


class _GatewayProfile(ProviderProfile):
    """Minimal externally-provided gateway profile: a per-model declaration.

    Below the public integration boundary the same way the retired Router
    plugin was above it: a real ``ProviderProfile`` registered through the
    normal ``providers.register_provider`` seam, consulted by the real
    transport. ``fetch_models`` doubles as the hot-path tripwire, counting
    before it raises so the observation survives an exception-catching
    lookup.
    """

    def __init__(self, efforts_by_model, name="effort-gw"):
        super().__init__(name=name, api_mode="codex_responses")
        self._efforts_by_model = efforts_by_model
        self.fetch_calls = 0

    def supported_reasoning_efforts(self, model):
        return self._efforts_by_model.get(model)

    def fetch_models(self, **_kwargs):
        self.fetch_calls += 1
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


def _gateway(registered, efforts):
    profile = _GatewayProfile(efforts)
    registered(profile)
    return profile


def _build(transport, model, *, effort="max", provider="effort-gw",
           base_url=DECLARED_HOST, gw=None, **params):
    """One real ``build_kwargs`` request; a registered profile must resolve
    its declaration without fetching the catalog on the hot path."""
    kwargs = transport.build_kwargs(
        model=model,
        messages=[{"role": "user", "content": "Hi"}],
        tools=[],
        base_url=base_url,
        session_id="sid",
        provider=provider,
        reasoning_config=None if effort is None else {"effort": effort},
        **params,
    )
    if gw is not None:
        assert gw.fetch_calls == 0, "request hot path fetched the provider catalog"
    return kwargs


class TestProfileContract:
    def test_declared_vocabulary_resolves_through_the_real_registry(self, registered):
        registered(_GatewayProfile({
            "gw-reasoner": DECLARED,
            "gw-vision": (),
        }))
        profile = get_provider_profile("effort-gw")
        assert profile is not None, "registered profile must resolve by name"
        assert profile.supported_reasoning_efforts("gw-reasoner") == DECLARED
        # () is definitive (non-reasoning), unknown model and the base-class
        # default are None (undeclared -> transport keeps its own vocabulary).
        assert profile.supported_reasoning_efforts("gw-vision") == ()
        assert profile.supported_reasoning_efforts("some-byok-route") is None
        assert ProviderProfile(name="bare").supported_reasoning_efforts("gw-reasoner") is None

    def test_fetch_tripwire_counts_a_forced_fetch(self, registered):
        # Alive control for the zero-fetch asserts in ``_build``: a forced
        # fetch must be counted (and raise) even where a lookup catches
        # exceptions, or "no fetch happened" would prove nothing.
        gw = _gateway(registered, {"gw-reasoner": DECLARED})
        with pytest.raises(AssertionError):
            gw.fetch_models()
        assert gw.fetch_calls == 1

    def test_unregistered_provider_is_none_and_keeps_default_behavior(self, transport):
        assert get_provider_profile("never-registered") is None
        kw = _build(transport, "some-byok-route", provider="never-registered",
                    base_url=FOREIGN_HOST)
        # No declaration -> the default codex vocabulary (legacy ladder has
        # xhigh but no max: max clamps to xhigh).
        assert kw["reasoning"]["effort"] == "xhigh"


class TestTransportClamp:
    def test_clamps_to_declared_vocabulary(self, transport, registered):
        gw = _gateway(registered, {"gw-reasoner": DECLARED})
        # "max" is above the declaration's ceiling -> nearest weaker is high,
        # not the default ladder's xhigh: an ignored declaration fails this.
        kw = _build(transport, "gw-reasoner", gw=gw)
        assert kw["reasoning"]["effort"] == "high"

    def test_supported_effort_passes_through(self, transport, registered):
        gw = _gateway(registered, {"gw-reasoner": DECLARED})
        # "minimal" is declared but absent from every transport default: it
        # reaches the wire only when the declaration was actually consulted.
        kw = _build(transport, "gw-reasoner", effort="minimal", gw=gw)
        assert kw["reasoning"]["effort"] == "minimal"

    def test_non_reasoning_model_suppresses_reasoning(self, transport, registered):
        gw = _gateway(registered, {"gw-vision": ()})
        # Default reasoning_config is enabled — the () verdict must strip the
        # reasoning field entirely (the gateway 400s rather than ignoring it).
        kw = _build(transport, "gw-vision", effort=None, gw=gw)
        assert "reasoning" not in kw
        assert kw.get("include") == []

    def test_unknown_model_falls_back_to_codex_default(self, transport, registered):
        gw = _gateway(registered, {"gw-reasoner": DECLARED})
        # Not in the declaration -> None -> default codex vocabulary (legacy
        # has xhigh but no max: max clamps to xhigh, medium is untouched).
        kw = _build(transport, "some-byok-route", gw=gw)
        assert kw["reasoning"]["effort"] == "xhigh"
        kw = _build(transport, "some-byok-route", effort="medium", gw=gw)
        assert kw["reasoning"]["effort"] == "medium"

    def test_unrecognized_declared_levels_are_dropped_at_clamp(self, transport, registered):
        # The retired ingest dropped unknown vendor tiers at parse time; the
        # surviving policy is clamp-time: only recognized ladder levels clamp,
        # so a new tier in the declaration is ignored, not sent.
        gw = _gateway(registered, {"future-model": ("low", "hyperthink", "high")})
        kw = _build(transport, "future-model", gw=gw)
        assert kw["reasoning"]["effort"] == "high"

    def test_fully_unrecognized_declaration_passes_the_request_through(
        self, transport, registered
    ):
        # A declaration with no recognized level cannot clamp; the transport
        # fails open and forwards the requested level verbatim instead of
        # suppressing reasoning or silently substituting the route default.
        gw = _gateway(registered, {"alien-model": ("hyperthink", "galaxy")})
        kw = _build(transport, "alien-model", gw=gw)
        assert kw["reasoning"]["effort"] == "max"

    def test_undeclared_route_never_blocks_and_never_fetches(self, transport, registered):
        # Cold/empty declaration -> None -> default vocabulary, and the
        # request hot path must not fetch the provider catalog.
        gw = _gateway(registered, {})
        kw = _build(transport, "gw-reasoner", effort="xhigh", gw=gw)
        assert kw["reasoning"]["effort"] == "xhigh"


class TestRouteIsolation:
    def test_other_providers_unaffected(self, transport, registered):
        gw = _gateway(registered, {"gw-vision": ()})
        kw = _build(transport, "gw-vision", effort="medium", provider="other-relay",
                    base_url=FOREIGN_HOST, gw=gw)
        # The declaration's () verdict for gw-vision must not leak into other
        # providers' requests.
        assert kw["reasoning"]["effort"] == "medium"

    def test_declared_vocab_cannot_hijack_native_codex_backend(self, transport, registered):
        gw = _gateway(registered, {"gpt-5.4": ()})
        kw = _build(transport, "gpt-5.4", provider="openai-codex",
                    base_url="https://chatgpt.com/backend-api/codex",
                    is_codex_backend=True, gw=gw)
        # The native route keeps its own per-model ladder (gpt-5.4 -> legacy,
        # max clamps to xhigh); a profile declaration never suppresses it.
        assert kw["reasoning"]["effort"] == "xhigh"


class TestHostResolvedProfile:
    # The declared-host and foreign-host rows issue the identical request
    # (model gw-reasoner, effort max); only the endpoint host differs, and
    # the outcomes must differ with it: the vocabulary follows the host
    # mapping, not the config-entry name.

    def test_named_custom_route_at_declared_host_gets_the_vocabulary(
        self, transport, registered, monkeypatch
    ):
        # A ``custom:<name>`` entry pointed at a host whose profile is
        # registered rides the host mapping onto this transport, clamping
        # "max" to the declaration's high — where both the generic custom
        # declaration (max passes) and the default ladder (xhigh) would not.
        gw = _gateway(registered, {"gw-reasoner": DECLARED})
        monkeypatch.setitem(agent.model_metadata._URL_TO_PROVIDER, "gw-clamp.test", "effort-gw")
        kw = _build(transport, "gw-reasoner", provider="custom:my-proxy", gw=gw)
        assert kw["reasoning"]["effort"] == "high"

    def test_generic_custom_route_keeps_openai_compat_vocabulary(self, transport):
        # Shipped custom declaration: an undiscoverable endpoint's ceiling is
        # the widest OpenAI-compat set, so a configured "max" is not demoted.
        kw = _build(transport, "some-relay-model", provider="custom:my-relay",
                    base_url=FOREIGN_HOST)
        assert kw["reasoning"]["effort"] == "max"

    def test_foreign_host_does_not_borrow_the_declared_vocabulary(
        self, transport, registered, monkeypatch
    ):
        gw = _gateway(registered, {"gw-reasoner": DECLARED})
        monkeypatch.setitem(agent.model_metadata._URL_TO_PROVIDER, "gw-clamp.test", "effort-gw")
        kw = _build(transport, "gw-reasoner", provider="my-proxy",
                    base_url=FOREIGN_HOST, gw=gw)
        # No host match and "my-proxy" is unregistered -> default codex
        # vocabulary (legacy: no max -> xhigh), unlike the declared host's
        # "high" for the identical request.
        assert kw["reasoning"]["effort"] == "xhigh"
