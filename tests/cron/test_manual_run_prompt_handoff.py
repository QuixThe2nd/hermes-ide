"""Regression tests: transient ``extra_prompt`` across the external cron worker.

Manual runs pass ``extra_prompt`` (``cronjob(action='run', prompt=...)``) as a
function argument. On a managed gateway the job is handed to a restart-safe
external worker process serialized as a JSON payload; a function argument does
not survive a process boundary, so the transient prompt was silently dropped
(pc_687f93b27a81) and the job ran its stored prompt only. ``run_one_job`` now
stamps the prompt onto the job (``manual_run_prompt``/``manual_run_at``) before
the handoff — the payload carries the whole job dict — and the existing
worker-side pickup consumes it exactly like a CLI-stamped manual run.
"""

import contextlib
import json
from unittest.mock import Mock

import pytest

import cron.scheduler as s


def test_manual_run_prompt_stamped_before_external_handoff(monkeypatch):
    """run_one_job hands the transient prompt to the external worker by
    stamping it on the job instead of dropping it at the process boundary."""
    import cron.scheduler as scheduler

    launch = Mock(return_value=True)
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", launch)
    job = {"id": "job-1", "execution_id": "exec-1"}

    assert scheduler.run_one_job(job, extra_prompt="UNIQUE-MARKER-123") is True

    launch.assert_called_once_with(job)
    handed = launch.call_args.args[0]
    assert handed["manual_run_prompt"] == "UNIQUE-MARKER-123"
    assert handed["manual_run_at"]


def _stub_worker_io(monkeypatch):
    """Stub the external-IO collaborators of _run_external_worker_payload."""
    import agent.secret_scope as scope
    import cron.executions as executions
    import hermes_cli.env_loader as env_loader

    monkeypatch.setattr(s, "use_cron_store", lambda home: contextlib.nullcontext())
    monkeypatch.setattr(executions, "adopt_claimed_execution", lambda eid: {"id": eid})
    monkeypatch.setattr(scope, "build_profile_secret_scope", lambda *a, **k: object())
    monkeypatch.setattr(scope, "set_secret_scope", lambda tok: object())
    monkeypatch.setattr(scope, "reset_secret_scope", lambda tok: None)
    monkeypatch.setattr(scope, "is_multiplex_active", lambda: False)
    monkeypatch.setattr(scope, "set_multiplex_active", lambda v: None)
    monkeypatch.setattr(env_loader, "hydrate_profile_secret_sources", lambda home: None)


def _pipeline_with_captured_extra_prompt(monkeypatch):
    """Patch the run_job pipeline and record the extra_prompt run_job saw."""
    seen = {}

    def fake_run_job(job, *, defer_agent_teardown=None, **kw):
        seen["extra_prompt"] = kw.get("extra_prompt")
        return (True, "out", "final", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", lambda job, content, **kw: None)
    monkeypatch.setattr(s, "mark_job_run", lambda jid, ok, err=None, **_kw: None)
    return seen


def test_worker_payload_stamp_reaches_run_job(monkeypatch, tmp_path):
    """Worker-side: a handoff payload whose job carries the stamped manual-run
    prompt delivers it to run_job through the shared pickup."""
    seen = _pipeline_with_captured_extra_prompt(monkeypatch)
    _stub_worker_io(monkeypatch)
    monkeypatch.setattr(s, "_launch_external_cron_worker", lambda job: False)

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({
        "job": {
            "id": "j1",
            "execution_id": "e1",
            "manual_run_prompt": "UNIQUE-MARKER-456",
            "manual_run_at": "2026-09-27T00:00:00+00:00",
        },
        "profile_home": str(tmp_path),
        "multiplex_active": False,
    }))
    ack_path = tmp_path / "ack.ready"

    assert s._run_external_worker_payload(payload_path, ack_path) is True
    assert seen["extra_prompt"] == "UNIQUE-MARKER-456"


def test_worker_payload_without_stamp_runs_without_extra_prompt(monkeypatch, tmp_path):
    """Backward compat: a payload job with no stamped prompt runs without one."""
    seen = _pipeline_with_captured_extra_prompt(monkeypatch)
    _stub_worker_io(monkeypatch)
    monkeypatch.setattr(s, "_launch_external_cron_worker", lambda job: False)

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({
        "job": {"id": "j1", "execution_id": "e1"},
        "profile_home": str(tmp_path),
        "multiplex_active": False,
    }))
    ack_path = tmp_path / "ack.ready"

    assert s._run_external_worker_payload(payload_path, ack_path) is True
    assert seen["extra_prompt"] is None
