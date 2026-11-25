"""The auxiliary recovery ladder must not let a failed auth-refresh retry escape.

The credential-refresh rung in ``agent/auxiliary_client.py`` performs its retry
inside the ladder generator:

            return (yield _LadderStep(
                "retry_same_provider", ...)), None   # generic credential rung

``_rung()`` exists to convert a retry failure into ``(None, exc)`` -- but only when the
rung's accept predicate claims the error -- so the caller can fall through to the next
rung. An unclaimed failure (a 500, a malformed response) re-raises on purpose, since
``_ladder_provider_fallback`` only acts on the reasons in ``_FALLBACK_REASONS``.
Used this way no exception is caught: when the
refreshed client also fails, the error would escape ``_aux_recovery_ladder`` and
``_ladder_provider_fallback`` never runs -- the configured
``auxiliary.<task>.fallback_chain`` is silently skipped. The
rung right below (credential-pool rotation) documents the intended behavior with "then
fall through to the provider fallback" and guards its retry with try/except.

The first test drives the real ladder generator with a scripted driver; the rest pin
the explicit-provider auth boundary.
"""

import pytest

import agent.auxiliary_client as aux

AUX_MODEL = "z-ai/glm-5.3-flash"
FALLBACK_MODEL = "fallback-model"


class _ApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _auth_error():
    return _ApiError("Error code: 401 - Unauthorized", status_code=401)


def _credit_error():
    return _ApiError(
        "Error code: 404 - Model '%s' requires available credits. "
        "Your account balance is too low to use paid models." % AUX_MODEL,
        status_code=404,
    )


class _FakeClient:
    api_key = "sk-test"
    base_url = "https://openrouter.ai/api/v1"


class _ExplicitProviderClient:
    api_key = "stale-key"
    base_url = "https://vertex.example/v1"


def _ladder(base_info="https://openrouter.ai/api/v1", resolved_provider="openrouter"):
    return aux._aux_recovery_ladder(
        _auth_error(),
        client=_FakeClient(),
        kwargs={"model": AUX_MODEL},
        task="compression",
        async_mode=False,
        base_info=base_info,
        resolved_provider=resolved_provider,
        resolved_model=AUX_MODEL,
        resolved_base_url=None,
        resolved_api_key=None,
        resolved_api_mode=None,
        final_model=AUX_MODEL,
        max_tokens=None,
        main_runtime=None,
        route_info={},
    )


@pytest.fixture
def hermetic(monkeypatch):
    """Keep the ladder off the network and record the provider-fallback rung."""
    chain_calls = []

    def _fake_provider_fallback(first_err, route):
        """Stands in for the last rung: a generator that performs no steps."""
        chain_calls.append(first_err)
        yield from ()
        return "chain-response"

    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **kw: None)

    monkeypatch.setattr(aux, "_ladder_provider_fallback", _fake_provider_fallback)
    return chain_calls


@pytest.mark.parametrize("retry_succeeds", [False, True])
def test_post_refresh_retry_owns_the_ladder_outcome(
        retry_succeeds, monkeypatch, hermetic):
    """A failed retry resumes the ladder; a successful one returns its response."""
    monkeypatch.setattr(aux, "_auth_refresh_provider_for_route",
                        lambda *a, **kw: "codex")
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *a, **kw: True)
    monkeypatch.setattr(aux, "_evict_cached_clients", lambda *a, **kw: None)
    expected_step = "retry_same_provider"

    ladder = _ladder()
    performed = []
    failure = _credit_error()

    def perform(step):
        performed.append(step.kind)
        if retry_succeeds:
            return "refreshed-client-response"
        raise failure

    if retry_succeeds:
        assert aux._drive_ladder(ladder, perform) == "refreshed-client-response"
        assert hermetic == [], "a successful retry must not reach the fallback chain"
        return

    try:
        result = aux._drive_ladder(ladder, perform)
    except _ApiError as exc:
        pytest.fail(
            "the ladder let %r escape instead of falling through to the configured "
            "fallback chain (steps performed: %s)" % (exc, performed)
        )

    assert performed == [expected_step], "the post-refresh retry is the only request"
    assert hermetic, "the configured fallback chain must be consulted"
    assert "requires available credits" in str(hermetic[0])
    assert result == "chain-response"


@pytest.mark.parametrize("spare_survives", [True, False])
def test_explicit_provider_auth_uses_its_configured_task_fallback(monkeypatch, spare_survives):
    """An explicit route may leave a 401 only through its own configured chain — including the
    re-walk after a chain entry is quarantined mid-request; an exhausted chain raises the primary
    error instead of spilling onto discovery / the main model."""
    dead_client, fallback_client = _ExplicitProviderClient(), _FakeClient()
    chain = [("fallback_chain[0](custom:dead)", dead_client)]
    if spare_survives:
        chain.append(("fallback_chain[1](custom:backup)", fallback_client))
    monkeypatch.setattr(
        aux,
        "_get_auxiliary_task_config",
        lambda task: {"fallback_chain": [{"provider": "custom:dead"}, {"provider": "custom:backup"}]},
    )
    monkeypatch.setattr(aux, "_auth_refresh_provider_for_route", lambda *args, **kwargs: "vertex")
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *args, **kwargs: False)
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *args, **kwargs: None)
    for name in ("_try_payment_fallback", "_try_main_fallback_chain", "_try_main_agent_model_fallback"):
        monkeypatch.setattr(aux, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} must stay gated for explicit auth"))

    def configured_chain(*args, **kwargs):
        if not chain:
            return None, None, ""
        label, client = chain.pop(0)
        return client, FALLBACK_MODEL, label

    monkeypatch.setattr(aux, "_try_configured_fallback_chain", configured_chain)
    ladder = aux._aux_recovery_ladder(
        _auth_error(),
        client=_ExplicitProviderClient(),
        kwargs={"model": AUX_MODEL},
        task="compression",
        async_mode=False,
        base_info="https://vertex.example/v1",
        resolved_provider="vertex",
        resolved_model=AUX_MODEL,
        resolved_base_url=None,
        resolved_api_key=None,
        resolved_api_mode=None,
        final_model=AUX_MODEL,
        max_tokens=None,
        main_runtime=None,
        route_info={},
    )

    def perform(step):
        assert step.kind == "fallback"
        if step.args[0] is dead_client:
            return None  # quarantined mid-request → the ladder re-walks the chain
        assert step.args == (fallback_client, FALLBACK_MODEL, "fallback_chain[1](custom:backup)")
        return "fallback-response"

    if spare_survives:
        assert aux._drive_ladder(ladder, perform) == "fallback-response"
    else:
        with pytest.raises(_ApiError, match="Unauthorized"):
            aux._drive_ladder(ladder, perform)
    assert not chain


def test_explicit_provider_auth_never_uses_an_unconfigured_fallback(monkeypatch):
    """A 401 without a task chain preserves the explicit-provider boundary."""
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    monkeypatch.setattr(aux, "_auth_refresh_provider_for_route", lambda *args, **kwargs: "vertex")
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *args, **kwargs: False)
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        aux,
        "_try_main_agent_model_fallback",
        lambda *args, **kwargs: pytest.fail("explicit auth must not use the main-agent fallback"),
    )
    ladder = aux._aux_recovery_ladder(
        _auth_error(),
        client=_ExplicitProviderClient(),
        kwargs={"model": AUX_MODEL},
        task="compression",
        async_mode=False,
        base_info="https://vertex.example/v1",
        resolved_provider="vertex",
        resolved_model=AUX_MODEL,
        resolved_base_url=None,
        resolved_api_key=None,
        resolved_api_mode=None,
        final_model=AUX_MODEL,
        max_tokens=None,
        main_runtime=None,
        route_info={},
    )

    with pytest.raises(_ApiError, match="Unauthorized"):
        aux._drive_ladder(ladder, lambda step: pytest.fail("no fallback request expected"))



def test_exhausted_ladder_raises_the_narrowed_error(monkeypatch, hermetic):
    """No chain answers: the retry's own failure surfaces, not the healed 401."""
    monkeypatch.setattr(aux, "_auth_refresh_provider_for_route",
                        lambda *a, **kw: "codex")
    monkeypatch.setattr(aux, "_refresh_provider_credentials", lambda *a, **kw: True)
    monkeypatch.setattr(aux, "_evict_cached_clients", lambda *a, **kw: None)

    def _no_chain(first_err, route):
        hermetic.append(first_err)
        yield from ()
        return None

    monkeypatch.setattr(aux, "_ladder_provider_fallback", _no_chain)
    failure = _credit_error()

    def perform(step):
        raise failure

    with pytest.raises(_ApiError) as raised:
        aux._drive_ladder(_ladder(), perform)

    assert raised.value is failure, (
        "the ladder must surface the actionable retry failure, got %r" % (raised.value,))
    assert hermetic == [failure]
