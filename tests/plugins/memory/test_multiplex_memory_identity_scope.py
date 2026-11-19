"""Multiplex invariant: memory-provider identity/tenant/endpoint never comes from the default profile.

Under ``gateway.multiplex_profiles`` ``os.environ`` is the DEFAULT profile's ``.env``. When a secondary
profile's scope does not define HINDSIGHT_BANK_ID, the provider must fall back to its own default
(per-profile partition), NOT write the secondary's memories into the default profile's account.
"""
from __future__ import annotations

import pytest

from agent import secret_scope

_DEFAULT_ENV = {
    "HINDSIGHT_BANK_ID": "bank-default", "HINDSIGHT_MODE": "local_external", "HINDSIGHT_API_URL": "http://hs.default",
    "HINDSIGHT_RETAIN_TAGS": "tag-default", "HINDSIGHT_RETAIN_OBSERVATION_SCOPES": "per_tag",
    "HINDSIGHT_RETAIN_SOURCE": "source-default", "HINDSIGHT_RETAIN_USER_PREFIX": "UserDefault",
    "HINDSIGHT_RETAIN_ASSISTANT_PREFIX": "AssistantDefault",
    "OPENAI_API_KEY": "sk-default", "OPENAI_BASE_URL": "https://openai.default/v1",
}


@pytest.fixture
def secondary_profile(monkeypatch, tmp_path):
    """Multiplex ON; default profile's values in environ; secondary profile `b` scope with only its
    own API keys (no identity/tenant/endpoint vars)."""
    for k, v in _DEFAULT_ENV.items():
        monkeypatch.setenv(k, v)
    home = tmp_path / ".hermes"
    prof_b = home / "profiles" / "b"
    prof_b.mkdir(parents=True)
    (prof_b / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(prof_b))
    secret_scope.set_multiplex_active(True)
    token = secret_scope.set_secret_scope({"HINDSIGHT_API_KEY": "hs-b"})
    try:
        yield prof_b
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.set_multiplex_active(False)


def test_secondary_profile_memory_identity_never_inherits_default_environ(secondary_profile):
    import plugins.memory.hindsight as hindsight

    hcfg = hindsight._load_config()
    assert (hcfg["banks"]["hermes"]["bankId"], hcfg["mode"]) == ("hermes", "cloud")


def test_hindsight_retain_shaping_is_not_re_read_from_the_default_profile_environ(secondary_profile):
    """The provider must retain with ITS OWN shaping, not the default profile's.

    ``_load_config`` resolves retain shaping through the secret scope, but the values it produces
    pass through ``_apply_retain_settings``, whose ``cfg_or_env`` half re-read ``os.environ`` — so
    every scoped miss came back as the default profile's tag, scope, source and speaker prefixes.
    ``metadata.source`` is opt-in by AGENTS.md, and tags are the retrieval partition, so a secondary
    profile's memories were both mislabelled and selectable by the default profile's tag filters.
    """
    import plugins.memory.hindsight as hindsight
    from plugins.memory.hindsight.settings import _DEFAULT_RETAIN_SOURCE

    cfg = hindsight._load_config()
    assert (cfg["retain_source"], cfg["retain_user_prefix"], cfg["retain_assistant_prefix"]) == (
        _DEFAULT_RETAIN_SOURCE, "User", "Assistant")

    provider = hindsight.HindsightMemoryProvider()
    provider._apply_retain_settings(cfg)
    assert provider._retain_tags == []
    assert provider._observation_scopes is None
    assert provider._retain_source == _DEFAULT_RETAIN_SOURCE
    assert (provider._retain_user_prefix, provider._retain_assistant_prefix) == ("User", "Assistant")

    # A config.json install never reaches ``_load_config``'s scoped read at all, so the fallback
    # inside ``_apply_retain_settings`` is the only gate for it.
    provider._apply_retain_settings({})
    assert provider._retain_tags == []
    assert provider._retain_source == _DEFAULT_RETAIN_SOURCE

    # The profile's OWN scoped values still win — this is isolation, not a blindfold.
    token = secret_scope.set_secret_scope({"HINDSIGHT_RETAIN_TAGS": "tag-b", "HINDSIGHT_RETAIN_SOURCE": "source-b"})
    try:
        provider._apply_retain_settings({})
    finally:
        secret_scope.reset_secret_scope(token)
    assert provider._retain_tags == ["tag-b"]
    assert provider._retain_source == "source-b"


def test_hindsight_retain_shaping_still_reads_the_process_env_for_a_single_profile(monkeypatch, tmp_path):
    """No multiplexing: the process env IS this profile's own .env, so it must keep being read."""
    import plugins.memory.hindsight as hindsight

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HINDSIGHT_RETAIN_TAGS", "solo-tag")
    monkeypatch.setenv("HINDSIGHT_RETAIN_SOURCE", "solo-source")
    monkeypatch.setenv("HINDSIGHT_RETAIN_USER_PREFIX", "Operator")

    provider = hindsight.HindsightMemoryProvider()
    provider._apply_retain_settings({})
    assert provider._retain_tags == ["solo-tag"]
    assert provider._retain_source == "solo-source"
    assert provider._retain_user_prefix == "Operator"
