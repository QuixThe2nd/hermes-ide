"""Cloud-SDK credential clients under ``gateway.multiplex_profiles``: azure-identity freezes the
credential chain into the client at construction, so a slot keyed by config alone would sign a
served profile's calls with the launch profile's keys. Each test warms the client under profile A, reads
under routed profile B whose ``.env`` differs (real temp homes, real secret scope; no mocks of the cache).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    a = tmp_path / ".hermes"
    b = a / "profiles" / "b"
    b.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(a))
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    for name, home in (("A", a), ("B", b)):
        (home / ".env").write_text(
            f"AWS_ACCESS_KEY_ID=AKIA{name * 16}\nAWS_SECRET_ACCESS_KEY=secret-{name}\n"
            f"AZURE_TENANT_ID=tenant-{name}\nAZURE_CLIENT_ID=client-{name}\nAZURE_CLIENT_SECRET=s-{name}\n",
            encoding="utf-8")
    return a, b


def _under(home: Path, fn):
    home_token = set_hermes_home_override(str(home))
    scope_token = set_secret_scope(build_profile_secret_scope(home))
    try:
        return fn()
    finally:
        reset_secret_scope(scope_token)
        reset_hermes_home_override(home_token)



def test_azure_entra_credential_is_built_from_the_routed_profiles_scope(two_profiles):
    from agent import azure_identity_adapter as az

    a, b = two_profiles
    seen: list[tuple] = []

    class _FakeSDK:
        def ClientSecretCredential(self, tenant, client, secret):
            seen.append((tenant, client))
            return object()

        def DefaultAzureCredential(self, **kwargs):
            seen.append(("default-chain",))
            return object()

    az.reset_credential_cache()
    try:
        cfg = az.EntraIdentityConfig()
        import unittest.mock as mock
        with mock.patch.object(az, "_require_azure_identity", lambda: _FakeSDK()):
            cred_a = _under(a, lambda: az.build_credential(cfg))
            cred_b = _under(b, lambda: az.build_credential(cfg))
            assert cred_b is not cred_a
            assert seen == [("tenant-A", "client-A"), ("tenant-B", "client-B")]
            assert _under(a, lambda: az.build_credential(cfg)) is cred_a  # cached per profile, not rebuilt
    finally:
        az.reset_credential_cache()
