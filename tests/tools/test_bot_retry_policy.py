"""Tests: bot-turn retry session policy (#93091 item 5).

Maintainer ruling (2026-08-23): a retried bot turn never mints a fresh
session. Transient classes resume; context_overflow re-runs the same session
so the retried turn's pre-API compaction pass compacts first; auth/quota/
config classes never auto-retry. These tests pin the policy function and the
local delivery runner that consumes it — same-session argv identity is the load-bearing assertion.
"""

from __future__ import annotations

import pytest

from tools import bot_failure_reasons as bfr


# ── policy function ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reason",
    sorted(bfr.AUTO_RETRYABLE),
)
def test_transient_reasons_resume(reason):
    assert bfr.retry_action(reason) == bfr.RETRY_RESUME


def test_context_overflow_compresses_then_resumes():
    assert bfr.retry_action(bfr.CONTEXT_OVERFLOW) == bfr.RETRY_COMPRESS_THEN_RESUME


@pytest.mark.parametrize(
    "reason",
    [
        bfr.PROVIDER_AUTH_OR_ACCESS,
        bfr.PROVIDER_QUOTA_LIMIT,
        bfr.MISSING_CONFIG,
        bfr.MODEL_UNAVAILABLE,
        bfr.AGENT_BLOCKED,
        bfr.CANCELLED,
        bfr.QUEUED_EXPIRED,
        bfr.UNKNOWN,
        "",
        "not-a-reason",
    ],
)
def test_non_retryable_reasons_stop(reason):
    assert bfr.retry_action(reason) == bfr.RETRY_NONE


def test_every_reason_has_a_defined_action():
    """Invariant: the policy is total over the closed reason vocabulary."""
    for reason in bfr.ALL_REASONS:
        assert bfr.retry_action(reason) in {
            bfr.RETRY_RESUME,
            bfr.RETRY_COMPRESS_THEN_RESUME,
            bfr.RETRY_NONE,
        }


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── local delivery runner consumes the policy ────────────────────────────────


def test_run_delivery_retries_transient_and_reemits_stdout(monkeypatch, tmp_path, capsys):
    from tools import bot_mode_dm

    dm = tmp_path / "dm.txt"
    dm.write_text("hello")
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        if len(calls) == 1:
            return _Proc(1, stderr="server error - overloaded")
        return _Proc(0, stdout="the reply text")

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", _fake_run)
    rc = bot_mode_dm._run_delivery(
        ["hermes", "-p", "ops", "chat"], str(dm), stdin_file=False
    )
    assert rc == 0
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert "the reply text" in capsys.readouterr().out
    assert not dm.exists(), "dm file must be cleaned up"


def test_run_delivery_no_retry_for_missing_config(monkeypatch, tmp_path):
    from tools import bot_mode_dm

    dm = tmp_path / "dm.txt"
    dm.write_text("hello")
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _Proc(1, stderr="No LLM provider configured")

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", _fake_run)
    rc = bot_mode_dm._run_delivery(
        ["hermes", "-p", "ops", "chat"], str(dm), stdin_file=False
    )
    assert rc == 1
    assert len(calls) == 1


# ── the streams a real failed `-Q` turn writes (#111721) ─────────────────────

# `hermes … -Q` prints the turn's final_response (the provider prose) on STDOUT and the session
# bookkeeping on STDERR — on every run, so a `stderr or stdout` read never saw the provider error.
_REAL_FAILED_STDOUT = (
    "Custom endpoint reported it was overloaded on all 1 attempts — it looks temporarily "
    "unavailable. Wait a minute and send /retry.\n\nProvider said: HTTP 503: Overloaded\n"
)
_REAL_FAILED_STDERR = "Session 20260916_095917_b1a5cd found but has no messages. Starting fresh.\n\nsession_id: 20260916_095917_b1a5cd\n"


def test_run_local_turn_retry_reads_the_stream_the_cli_writes_and_resumes_the_persisted_row(monkeypatch, tmp_path, capsys):
    """Same invariant on the same-install `message_agent` runner: the real stdout/stderr split opens
    the retry gate once, and only the re-run carries the resume marker (the first attempt's env is
    otherwise kept)."""
    from tools import bot_mode_dm
    from tools.bot_relay import RESUME_UNANSWERED_TURN_ENV

    dm = tmp_path / "dm.txt"
    dm.write_text("hello")
    envs = []

    def _fake_run(argv, **kwargs):
        envs.append(kwargs.get("env") or {})
        if len(envs) == 1:
            return _Proc(1, stdout=_REAL_FAILED_STDOUT, stderr=_REAL_FAILED_STDERR)
        return _Proc(0, stdout="the reply text")

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", _fake_run)
    rc = bot_mode_dm._run_local_turn(["hermes", "-p", "ops", "chat"], str(dm), env={"HERMES_HOME": str(tmp_path)})
    assert rc == 0
    assert envs[0] == {"HERMES_HOME": str(tmp_path)}
    assert envs[1] == {"HERMES_HOME": str(tmp_path), RESUME_UNANSWERED_TURN_ENV: "1"}
    assert "the reply text" in capsys.readouterr().out
