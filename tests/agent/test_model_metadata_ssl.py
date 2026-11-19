"""Tests for _resolve_requests_verify() trust-source contract.

The requests-based ``/models`` probes resolve TLS from per-provider config only
(``ssl_verify: false``, then ``ssl_ca_cert``); otherwise the OS trust store via
the process-wide ``truststore`` install. Ambient CA env vars
(HERMES_CA_BUNDLE, REQUESTS_CA_BUNDLE, SSL_CERT_FILE) were removed as a trust
authority — these tests assert they no longer steer verification, and that
missing files / config failures degrade to plain "verify against the OS store".

No network I/O: CA env vars point at tmp_path stand-in files only; the OS
trust store is never written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.model_metadata import _resolve_requests_verify


_CA_ENV_VARS = ("HERMES_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")


@pytest.fixture
def clean_env(monkeypatch):
    """Clear all three SSL env vars so each test starts from a known state."""
    for var in _CA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def bundle_file(tmp_path: Path) -> str:
    """Create a placeholder CA bundle file and return its absolute path."""
    path = tmp_path / "ca.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n")
    return str(path)


class TestResolveRequestsVerify:
    def test_no_env_returns_true(self, clean_env):
        assert _resolve_requests_verify() is True

    def test_env_vars_do_not_steer_trust(self, clean_env, tmp_path, bundle_file):
        """Every legacy CA env var is ignored: trust comes from the OS store, not ambient env."""
        other = tmp_path / "other.pem"
        other.write_text("stub")
        clean_env.setenv("HERMES_CA_BUNDLE", bundle_file)
        clean_env.setenv("REQUESTS_CA_BUNDLE", str(other))
        clean_env.setenv("SSL_CERT_FILE", str(other))
        assert _resolve_requests_verify() is True

    def test_stale_env_var_path_returns_true_not_the_path(self, clean_env, bundle_file):
        """HERMES_CA_BUNDLE pointing at a real file still resolves to plain ``True`` —
        the path must never leak into ``verify=`` as the trust source."""
        clean_env.setenv("HERMES_CA_BUNDLE", bundle_file)
        assert _resolve_requests_verify() is not bundle_file
        assert _resolve_requests_verify() is True
