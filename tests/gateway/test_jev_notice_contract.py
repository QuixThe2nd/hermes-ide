"""Runtime-bridge contract for Jev chat notice lines.

``gateway/jev_notice.py`` contains NO formatting logic: it builds record
dicts and pipes them through the canonical Go formatter
(``internal/jevnotices``, executed as ``cmd/jevnotice``). These tests run
the real bridge — the binary is resolved/built exactly as the gateway
runtime resolves it — against the shared table in
``internal/jevnotices/testdata/notice_cases.json``, which the Go tests also
execute. The record builders are pinned too: a no-call outcome must produce
NO record (no notice may claim an HTTP request that was never made).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.jev_notice import (
    format_notice,
    goal_intent_attempt_record,
    go_formatter_binary,
    reaction_gate_record,
    response_gate_record,
)

_TABLE = Path(__file__).resolve().parents[2] / "internal" / "jevnotices" / "testdata" / "notice_cases.json"

requires_go = pytest.mark.skipif(
    go_formatter_binary() is None,
    reason="jev notices need the Go formatter binary (cached or built from the checkout)",
)


def _shared_cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


@requires_go
@pytest.mark.parametrize("case", _shared_cases(), ids=lambda case: case["name"])
def test_bridge_renders_the_shared_table(case):
    assert format_notice(case["record"]) == case["expect"]


@requires_go
def test_format_notice_fails_closed_on_junk():
    line = format_notice({"purpose": None, "subject": "", "confidence": "junk"})
    assert line is not None
    assert line.startswith("🔎 jev ")
    assert "\n" not in line


def test_shared_table_covers_every_purpose_and_failure():
    purposes = {case["record"]["purpose"] for case in _shared_cases()}
    assert purposes == {"goal-intent", "response-gate", "reaction-gate"}
    failures = {
        case["record"]["purpose"] for case in _shared_cases() if "error" in case["record"]
    }
    assert failures == purposes


class TestGoalIntentAttemptRecord:
    def test_success_carries_label_and_confidence(self):
        record = goal_intent_attempt_record(
            attempt=1, attempts=2, label="task", confidence=0.9, error=None,
        )
        assert record["label"] == "task"
        assert record["confidence"] == 0.9
        assert "error" not in record

    def test_failure_carries_only_the_error_class(self):
        record = goal_intent_attempt_record(
            attempt=2, attempts=2, label=None, confidence=None,
            error="schema_mismatch: no answers object",
        )
        assert "label" not in record
        assert record["error"] == "schema_mismatch: no answers object"


class TestNoCallProducesNoRecord:
    def test_response_gate_missing_credential_is_not_a_request(self):
        decision = SimpleNamespace(
            allowed=False, error="missing_credential", scores={},
        )
        assert response_gate_record(decision) is None

    def test_reaction_gate_missing_credential_is_not_a_request(self):
        decision = SimpleNamespace(
            emoji=None, error="missing_credential", abstain_sum=None,
        )
        assert reaction_gate_record(decision) is None

    def test_every_other_error_was_a_real_request(self):
        for reason in ("timeout", "http_502", "transport_error", "schema_mismatch"):
            decision = SimpleNamespace(allowed=False, error=reason, scores={})
            assert response_gate_record(decision) == {
                "purpose": "response-gate",
                "subject": "user message",
                "author": "",
                "error": reason,
            }

    def test_denied_decision_with_scores_is_a_record(self):
        decision = SimpleNamespace(
            allowed=False, error=None,
            scores={
                "addresses_bot": 0.1, "continues_bot_thread": 0.2,
                "joins_bot_thread": 0.3, "noise": 0.4,
            },
        )
        record = response_gate_record(decision, author="Tek")
        assert record["allowed"] is False
        assert record["scores"]["noise"] == 0.4

    def test_reaction_abstention_is_a_record(self):
        decision = SimpleNamespace(emoji=None, error=None, abstain_sum=0.63)
        record = reaction_gate_record(decision, author="Alice")
        assert record == {
            "purpose": "reaction-gate", "subject": "user message",
            "author": "Alice", "abstain": 0.63,
        }
