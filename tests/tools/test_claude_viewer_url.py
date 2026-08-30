"""Tests for the claude-viewer URL module and its host allowlist."""

from __future__ import annotations

import pytest

from tools.claude_viewer_url import (
    DEFAULT_PORT,
    RUN_STEM_RE,
    detect_public_host,
    extra_hosts,
    is_allowed_watch_url,
    public_base_url,
    watch_url,
)


STEM = "20260829-115024-2006506"

RFC1918_AND_TAILSCALE_HOSTS = [
    "192.168.30.20",  # RFC1918 — this fork's original homelab address
    "100.109.12.0",  # Tailscale CGNAT — the original tailnet address
    "10.1.2.3",
    "172.16.0.9",
    "172.31.255.254",
    "127.0.0.1",
    "100.64.0.1",
    "100.127.255.254",
    "[::1]",  # IPv6 literals are bracketed in URLs
]

PUBLIC_AND_JUNK_HOSTS = [
    "8.8.8.8",
    "1.2.3.4",
    "100.128.0.1",  # just past the CGNAT /10
    "100.63.255.255",  # just below the CGNAT /10
    "172.32.0.1",
    "192.169.0.1",
    "example.com",
    "cursor.com",
]


@pytest.fixture
def auto_host(monkeypatch):
    """Pin host auto-detection so tests never depend on the real network."""

    def _pin(host):
        monkeypatch.setattr(
            "tools.claude_viewer_url._tailscale_ipv4", lambda: None
        )
        monkeypatch.setattr(
            "tools.claude_viewer_url._default_route_ipv4",
            (lambda: host) if host else (lambda: None),
        )
        return host

    return _pin


# ---------------------------------------------------------------------------
# public_base_url / watch_url
# ---------------------------------------------------------------------------


def test_public_base_url_uses_configured_public_host_first(auto_host):
    auto_host("10.9.9.9")
    assert (
        public_base_url(cfg={"public_host": "viewer.example.internal"})
        == "http://viewer.example.internal:8787"
    )


def test_public_base_url_prefers_tailscale_over_lan(auto_host, monkeypatch):
    auto_host("192.168.1.50")
    monkeypatch.setattr(
        "tools.claude_viewer_url._tailscale_ipv4", lambda: "100.101.102.103"
    )
    assert public_base_url() == "http://100.101.102.103:8787"


def test_public_base_url_falls_back_to_default_route(auto_host):
    auto_host("192.168.1.50")
    assert public_base_url() == "http://192.168.1.50:8787"


def test_public_base_url_final_fallback_is_loopback(auto_host):
    auto_host(None)
    assert public_base_url() == f"http://127.0.0.1:{DEFAULT_PORT}"


def test_public_base_url_never_returns_a_foreign_hardcoded_ip(auto_host):
    auto_host(None)
    assert "192.168.30.20" not in public_base_url()


def test_watch_url_carries_stem_in_fragment(auto_host):
    auto_host("10.0.0.7")
    url = watch_url(STEM)
    assert url == f"http://10.0.0.7:8787/#{STEM}"
    assert url.rsplit("#", 1)[1] == STEM


def test_watch_url_stem_roundtrips_through_run_stem_re(auto_host):
    auto_host("10.0.0.7")
    assert RUN_STEM_RE.fullmatch(watch_url(STEM).rsplit("#", 1)[1])


def test_config_port_is_honored(auto_host):
    auto_host("10.0.0.7")
    assert public_base_url(cfg={"port": 9999}) == "http://10.0.0.7:9999"


def test_detect_public_host_survives_raising_probes(monkeypatch):
    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("tools.claude_viewer_url._tailscale_ipv4", _boom)
    monkeypatch.setattr("tools.claude_viewer_url._default_route_ipv4", _boom)
    assert detect_public_host() == "127.0.0.1"


# ---------------------------------------------------------------------------
# is_allowed_watch_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", RFC1918_AND_TAILSCALE_HOSTS)
def test_allows_private_network_hosts(host):
    assert is_allowed_watch_url(f"http://{host}:8787/")
    assert is_allowed_watch_url(f"http://{host}:8787/#{STEM}")


@pytest.mark.parametrize("host", PUBLIC_AND_JUNK_HOSTS)
def test_rejects_public_and_junk_hosts(host):
    assert not is_allowed_watch_url(f"http://{host}:8787/")
    assert not is_allowed_watch_url(f"http://{host}:8787/#{STEM}")


@pytest.mark.parametrize(
    "url",
    [
        f"http://user@192.168.30.20:8787/",
        f"http://user:pw@192.168.30.20:8787/",
        f"http://192.168.30.20:8787/watch live",
        f"http://192.168.30.20:8787/api/runs",
        f"http://192.168.30.20:8787/?x=1",
        f"http://192.168.30.20:8787/#",
        "http://192.168.30.20:8787/#not-a-stem",
        "http://192.168.30.20:8787/#2026-08-29",
        "javascript:alert(1)",
        "ftp://192.168.30.20:8787/",
        "",
        "   ",
        "http://192.168.30.20:8787/../etc/passwd",
        "http://192.168.30.20:8787/%2e%2e/etc/passwd",
    ],
)
def test_rejects_malformed_or_unsafe_urls(url):
    assert not is_allowed_watch_url(url)


def test_allows_extra_host_from_config():
    url = "http://viewer.lan:8787/"
    assert not is_allowed_watch_url(url)
    assert is_allowed_watch_url(url, cfg={"extra_hosts": ["viewer.lan"]})


def test_extra_hosts_are_normalized():
    assert extra_hosts({"extra_hosts": [" Viewer.LAN ", "viewer.lan"]}) == (
        "viewer.lan",
    )


def test_extra_hosts_accepts_a_bare_string():
    assert extra_hosts({"extra_hosts": "viewer.lan"}) == ("viewer.lan",)


def test_empty_config_uses_defaults():
    assert extra_hosts({}) == ()
    assert is_allowed_watch_url(f"http://127.0.0.1:8787/#{STEM}")
