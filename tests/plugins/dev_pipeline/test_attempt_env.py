"""Tests for dev-pipeline attempt environment sanitization."""

from __future__ import annotations

import pytest

from plugins.dev_pipeline.pipeline import build_attempt_env


def test_strips_github_and_api_key_vars():
    base = {
        "HOME": "/root",
        "PATH": "/usr/bin",
        "GH_TOKEN": "secret",
        "GITHUB_TOKEN": "secret",
        "OPENAI_API_KEY": "secret",
        "MY_OAUTH_TOKEN": "secret",
        "LANG": "C.UTF-8",
    }
    env = build_attempt_env(base, lane="cursor-bounded")
    assert env["HOME"] == "/root"
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C.UTF-8"
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "MY_OAUTH_TOKEN" not in env


def test_preserves_lc_and_cursor_vars():
    base = {
        "LC_ALL": "C.UTF-8",
        "CURSOR_CONFIG_DIR": "/root/.cursor",
        "RANDOM_SECRET": "drop-me",
    }
    env = build_attempt_env(base, lane="cursor-bounded")
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["CURSOR_CONFIG_DIR"] == "/root/.cursor"
    assert "RANDOM_SECRET" not in env


def test_claude_endurance_sanitizes_like_cursor_bounded():
    base = {
        "HOME": "/root",
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "secret",
        "LANG": "C.UTF-8",
    }
    env = build_attempt_env(base, lane="claude-endurance")
    assert env["HOME"] == "/root"
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C.UTF-8"
    assert "OPENAI_API_KEY" not in env


def test_unknown_lane_raises_value_error():
    with pytest.raises(ValueError, match="unknown dev-pipeline lane"):
        build_attempt_env({}, lane="glm-endurance")
