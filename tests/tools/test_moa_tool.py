import json

import pytest


@pytest.fixture
def configured_moa(monkeypatch):
    config = {
        "moa": {
            "default_preset": "homelab",
            "presets": {
                "homelab": {
                    "enabled": True,
                    "reference_models": [
                        # Tree normalizer injects enabled on each slot (post-blob 947bd957a).
                        {"provider": "xai-oauth", "model": "grok-4.5", "enabled": True},
                        {"provider": "minimax-oauth", "model": "minimax-m3", "enabled": True},
                    ],
                    "aggregator": {
                        "provider": "openai-codex",
                        "model": "gpt-5.6-sol",
                    },
                    "reference_max_tokens": 500,
                }
            },
        }
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    return config


def test_moa_ask_uses_default_references_without_calling_aggregator(
    monkeypatch, configured_moa
):
    from agent.usage_pricing import CanonicalUsage
    from tools import moa_tool

    seen = {}

    def fake_run(
        reference_models,
        ref_messages,
        *,
        temperature=None,
        max_tokens=None,
        progress_callback=None,
    ):
        seen.update(
            reference_models=reference_models,
            ref_messages=ref_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            progress_callback=progress_callback,
        )
        return [
            ("xai-oauth:grok-4.5", "challenge the assumption", CanonicalUsage()),
            ("minimax-oauth:minimax-m3", "reuse the current stack", CanonicalUsage()),
        ]

    monkeypatch.setattr(moa_tool, "_run_references_parallel", fake_run)

    result = json.loads(
        moa_tool.moa_ask(
            question="Which architecture should we use?",
            evidence="The existing reverse proxy already supports forward auth.",
            decision_needed="Extend the proxy or deploy another stack.",
        )
    )

    assert result["success"] is True
    assert result["partial"] is False
    assert result["preset"] == "homelab"
    assert result["aggregator"] == "the acting Hermes model"
    assert [item["model"] for item in result["advisors"]] == [
        "grok-4.5",
        "minimax-m3",
    ]
    assert [item["status"] for item in result["advisors"]] == ["ok", "ok"]
    assert seen["reference_models"] == configured_moa["moa"]["presets"]["homelab"][
        "reference_models"
    ]
    assert seen["max_tokens"] == 500
    # Live advisor progress is wired: the fan-out receives a callable.
    assert callable(seen["progress_callback"])
    rendered = seen["ref_messages"][0]["content"]
    assert "Which architecture should we use?" in rendered
    assert "existing reverse proxy" in rendered
    assert "Extend the proxy" in rendered


def test_moa_ask_returns_partial_success_when_one_reference_fails(
    monkeypatch, configured_moa
):
    from agent.usage_pricing import CanonicalUsage
    from tools import moa_tool

    monkeypatch.setattr(
        moa_tool,
        "_run_references_parallel",
        lambda *_args, **_kwargs: [
            ("xai-oauth:grok-4.5", "use option a", CanonicalUsage()),
            (
                "minimax-oauth:minimax-m3",
                "[failed: provider unavailable]",
                CanonicalUsage(),
            ),
        ],
    )

    result = json.loads(moa_tool.moa_ask(question="Choose an option"))

    assert result["success"] is True
    assert result["partial"] is True
    assert result["usable_advisors"] == 1
    assert result["failed_advisors"] == 1
    assert result["advisors"][1]["status"] == "failed"


def test_moa_ask_rejects_empty_question_without_model_calls(
    monkeypatch, configured_moa
):
    from tools import moa_tool

    called = False

    def should_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(moa_tool, "_run_references_parallel", should_not_run)

    result = json.loads(moa_tool.moa_ask(question="   "))

    assert result["success"] is False
    assert "question" in result["error"].lower()
    assert called is False


def test_moa_ask_rejects_disabled_default_preset(monkeypatch, configured_moa):
    from tools import moa_tool

    configured_moa["moa"]["presets"]["homelab"]["enabled"] = False
    monkeypatch.setattr(
        moa_tool,
        "_run_references_parallel",
        lambda *_args, **_kwargs: pytest.fail("disabled preset must not run"),
    )

    result = json.loads(moa_tool.moa_ask(question="Choose an option"))

    assert result["success"] is False
    assert "disabled" in result["error"].lower()


def test_moa_ask_is_registered_as_a_core_tool():
    from tools import moa_tool  # noqa: F401
    from tools.registry import registry
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    entry = registry.get_entry("moa_ask")

    assert entry is not None
    assert entry.toolset == "moa"
    assert "moa_ask" in _HERMES_CORE_TOOLS
    assert "moa_ask" in TOOLSETS["moa"]["tools"]
    assert entry.schema["parameters"]["required"] == ["question"]
    assert registry.get_entry("consult_moa") is None
