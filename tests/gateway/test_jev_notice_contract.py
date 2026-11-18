"""Shared-contract parity for Jev chat notice lines.

``internal/jevnotices`` (Go, canonical) and ``gateway/jev_notice.py`` (Python
twin, what the runtime actually formats with) must render byte-identical
lines. The twin is pinned against the RUNNING Go implementation, not merely
the shared table: when a Go toolchain is available the real
``cmd/jevnotice`` binary renders every shared-table record and the Python
output must match it byte for byte. The record builders here are also
pinned: a no-call outcome must produce NO record (no notice may claim an
HTTP request that was never made).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.jev_notice import (
    format_notice,
    goal_intent_attempt_record,
    reaction_gate_record,
    response_gate_record,
)

_REPO = Path(__file__).resolve().parents[2]
_TABLE = _REPO / "internal" / "jevnotices" / "testdata" / "notice_cases.json"


def _shared_cases():
    return json.loads(_TABLE.read_text(encoding="utf-8"))["cases"]


def _go_binary(tmp_path_factory) -> str | None:
    """Build cmd/jevnotice once per session; None when no Go toolchain."""
    if shutil.which("go") is None:
        return None
    binary = tmp_path_factory.mktemp("jevnotice", numbered=True) / "jevnotice"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/jevnotice"],
        cwd=_REPO / "internal" / "jevnotices",
        capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/jevnotice failed:\n{build.stderr}")
    return str(binary)


@pytest.fixture(scope="session")
def go_jevnotice(tmp_path_factory) -> str | None:
    return _go_binary(tmp_path_factory)


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda case: case["name"])
def test_python_twin_matches_the_shared_table(case):
    assert format_notice(case["record"]) == case["expect"]


@pytest.mark.parametrize("case", _shared_cases(), ids=lambda case: case["name"])
def test_python_twin_matches_the_running_go_formatter(case, go_jevnotice):
    """The real Go binary renders the same record: Python twin == Go output.

    Skipped only where no Go toolchain exists (CI builds it; the go-line-balance
    lane runs the Go tests on the same table as a second cross-check).
    """
    if go_jevnotice is None:
        pytest.skip("no Go toolchain available")
    run = subprocess.run(
        [go_jevnotice], input=json.dumps(case["record"]),
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert format_notice(case["record"]) == run.stdout.rstrip("\n")


def test_shared_table_covers_every_purpose_and_failure():
    purposes = {case["record"]["purpose"] for case in _shared_cases()}
    assert purposes == {"goal-intent", "response-gate", "reaction-gate"}
    failures = {
        case["record"]["purpose"] for case in _shared_cases() if "error" in case["record"]
    }
    assert failures == purposes


def test_format_notice_fails_closed_on_junk():
    line = format_notice({"purpose": None, "subject": "", "confidence": "junk"})
    assert line.startswith("🔎 jev ")
    assert "\n" not in line


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
        assert format_notice(record) == (
            "🔎 jev response-gate user message from Tek: deny "
            "(addresses_bot=0.10 continues_bot_thread=0.20 joins_bot_thread=0.30 noise=0.40)"
        )

    def test_reaction_abstention_is_a_record(self):
        decision = SimpleNamespace(emoji=None, error=None, abstain_sum=0.63)
        record = reaction_gate_record(decision, author="Alice")
        assert format_notice(record) == (
            "🔎 jev reaction-gate user message from Alice: no reaction (abstain 0.63)"
        )
