"""Tests for dev-pipeline plan contract validation."""

from __future__ import annotations

import copy

import pytest

from plugins.dev_pipeline.pipeline import validate_plan_contract


def _valid_contract() -> dict:
    return {
        "task_summary": "Add health check endpoint",
        "lane_hint": "cursor",
        "estimated_minutes": 20,
        "allowed_paths": ["src/**", "tests/**"],
        "acceptance_commands": ["pytest tests/ -q"],
        "broad_flags": {
            "migration": False,
            "repo_wide_change": False,
            "toolchain_change": False,
            "multi_subsystem": False,
            "long_verification": False,
        },
        "blocked_reasons": [],
        "step_plan": [
            {"id": "s1", "description": "Add route", "verifiable": True},
        ],
        "assumptions": ["Tests run offline"],
    }


def test_valid_contract_passes():
    contract, errors = validate_plan_contract(_valid_contract())
    assert errors == []
    assert contract is not None
    assert contract["lane_hint"] == "cursor"


@pytest.mark.parametrize(
    "missing_key",
    [
        "task_summary",
        "lane_hint",
        "estimated_minutes",
        "allowed_paths",
        "acceptance_commands",
        "broad_flags",
        "blocked_reasons",
        "step_plan",
        "assumptions",
    ],
)
def test_missing_required_key_rejected(missing_key):
    data = _valid_contract()
    del data[missing_key]
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("missing required keys" in e for e in errors)


def test_unknown_top_level_key_rejected():
    data = _valid_contract()
    data["extra_field"] = True
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("unknown top-level keys" in e for e in errors)


def test_invalid_task_summary_rejected():
    data = _valid_contract()
    data["task_summary"] = ""
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("task_summary" in e for e in errors)


def test_invalid_lane_hint_rejected():
    data = _valid_contract()
    data["lane_hint"] = "glm"
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("lane_hint" in e for e in errors)


@pytest.mark.parametrize("minutes", [0, 481, "20"])
def test_estimated_minutes_out_of_range(minutes):
    data = _valid_contract()
    data["estimated_minutes"] = minutes
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("estimated_minutes" in e for e in errors)


def test_absolute_allowed_path_rejected():
    data = _valid_contract()
    data["allowed_paths"] = ["/etc/passwd"]
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("absolute" in e for e in errors)


def test_parent_traversal_allowed_path_rejected():
    data = _valid_contract()
    data["allowed_paths"] = ["../secrets"]
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any(".." in e for e in errors)


def test_empty_acceptance_commands_rejected():
    data = _valid_contract()
    data["acceptance_commands"] = []
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("acceptance_commands" in e for e in errors)


def test_oversized_acceptance_command_rejected():
    data = _valid_contract()
    data["acceptance_commands"] = ["x" * 501]
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("500" in e for e in errors)


def test_chained_acceptance_command_allowed():
    data = _valid_contract()
    data["acceptance_commands"] = ["npm test && npm run lint"]
    contract, errors = validate_plan_contract(data)
    assert errors == []
    assert contract is not None


@pytest.mark.parametrize("flag_key", ["migration", "repo_wide_change"])
def test_non_bool_broad_flag_rejected(flag_key):
    data = _valid_contract()
    data["broad_flags"] = copy.deepcopy(_valid_contract()["broad_flags"])
    data["broad_flags"][flag_key] = "yes"
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any(f"broad_flags.{flag_key}" in e for e in errors)


def test_blocked_reasons_superset_rejected():
    data = _valid_contract()
    data["blocked_reasons"] = ["missing_credentials", "unknown_reason"]
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("blocked_reasons" in e for e in errors)


def test_step_plan_requires_verifiable_bool():
    data = _valid_contract()
    data["step_plan"] = [{"id": "s1", "description": "x", "verifiable": "yes"}]
    contract, errors = validate_plan_contract(data)
    assert contract is None
    assert any("verifiable" in e for e in errors)
