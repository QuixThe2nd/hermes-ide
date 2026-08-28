"""CLI entrypoint contracts: flags, summary lines, exit codes, module smoke."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from plugins.pr_intent_watch import run as run_module

REPO_ROOT = Path(__file__).resolve().parents[3]


def _tick_stub(monkeypatch, summary=None, error=None):
    from plugins.pr_intent_watch import core

    recorded: list[dict] = []

    def _fake_tick(**kwargs):
        recorded.append(kwargs)
        if error is not None:
            raise error
        return dict(summary or {})

    monkeypatch.setattr(core, "run_tick", _fake_tick)
    return recorded


# ── flags ───────────────────────────────────────────────────────────────────


def test_config_flag_is_forwarded_as_path(monkeypatch, tmp_path):
    config = tmp_path / "elsewhere.yaml"
    recorded = _tick_stub(monkeypatch, {"reviewed": 0, "commented": 0, "skipped": 0})
    assert run_module.main(["--config", str(config), "--dry-run"]) == 0
    assert recorded == [
        {"config_path": config, "dry_run": True}
    ]


def test_defaults_to_hermes_home_config_and_no_dry_run(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    recorded = _tick_stub(monkeypatch, {"reviewed": 0, "commented": 0, "skipped": 0})
    assert run_module.main([]) == 0
    assert recorded == [
        {"config_path": get_hermes_home() / "config.yaml", "dry_run": False}
    ]


def test_help_exits_zero(capsys):
    try:
        run_module.main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("--help must exit")
    assert "usage" in capsys.readouterr().out.lower()


# ── summary lines ───────────────────────────────────────────────────────────


def test_prints_disabled_line(monkeypatch, capsys):
    _tick_stub(monkeypatch, {"disabled": True})
    assert run_module.main([]) == 0
    out = capsys.readouterr().out
    assert "disabled" in out and "nothing to do" in out


def test_prints_no_token_line(monkeypatch, capsys):
    _tick_stub(monkeypatch, {"no_token": True})
    assert run_module.main([]) == 0
    assert "no GitHub token" in capsys.readouterr().out


def test_prints_baseline_line_with_zero_comments(monkeypatch, capsys):
    _tick_stub(monkeypatch, {"baseline": True, "new": 4})
    assert run_module.main([]) == 0
    out = capsys.readouterr().out
    assert "baseline" in out
    assert "new=4" in out and "commented=0" in out


def test_prints_review_summary_line(monkeypatch, capsys):
    _tick_stub(
        monkeypatch,
        {"reviewed": 2, "commented": 1, "skipped": 1, "new": 2},
    )
    assert run_module.main([]) == 0
    out = capsys.readouterr().out
    assert "reviewed=2" in out
    assert "commented=1" in out
    assert "skipped=1" in out


def test_unexpected_error_exits_one(monkeypatch, capsys):
    _tick_stub(monkeypatch, error=RuntimeError("boom"))
    assert run_module.main([]) == 1
    assert "unexpected error" in capsys.readouterr().out.lower()


# ── --serve ─────────────────────────────────────────────────────────────────


def test_serve_flag_runs_the_webhook_listener(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home
    from plugins.pr_intent_watch import webhook

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    recorded: list[dict] = []
    monkeypatch.setattr(
        webhook, "serve", lambda **kwargs: recorded.append(kwargs) or 0
    )

    assert run_module.main(["--serve"]) == 0
    assert recorded == [{"config_path": get_hermes_home() / "config.yaml"}]


def test_serve_exit_code_is_forwarded(monkeypatch):
    from plugins.pr_intent_watch import webhook

    monkeypatch.setattr(webhook, "serve", lambda **kwargs: 1)
    assert run_module.main(["--serve"]) == 1


def test_serve_never_reconciles_the_scheduler(monkeypatch):
    """The serve process IS the schedule — arming units from inside it would
    fight the gateway's reconcile."""
    from plugins.pr_intent_watch import lifecycle
    from plugins.pr_intent_watch import webhook

    def boom(*args, **kwargs):
        raise AssertionError("serve must not reconcile the scheduler")

    monkeypatch.setattr(lifecycle, "reconcile_scheduler_on_load", boom)
    monkeypatch.setattr(webhook, "serve", lambda **kwargs: 0)
    assert run_module.main(["--serve"]) == 0


def test_serve_flag_suppresses_the_one_shot_tick(monkeypatch, capsys):
    recorded = _tick_stub(monkeypatch, {"reviewed": 1})
    from plugins.pr_intent_watch import webhook

    monkeypatch.setattr(webhook, "serve", lambda **kwargs: 0)
    assert run_module.main(["--serve"]) == 0
    assert recorded == []  # serve owns the loop, not a single pass
    assert "pr-intent-watch:" not in capsys.readouterr().out


# ── --print-webhook-secret ──────────────────────────────────────────────────


def test_print_webhook_secret_prints_and_exits_zero(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert run_module.main(["--print-webhook-secret"]) == 0
    first = capsys.readouterr().out.strip()
    assert len(first) >= 32

    # Persisted, not regenerated — a new secret would orphan the GitHub hook.
    assert run_module.main(["--print-webhook-secret"]) == 0
    assert capsys.readouterr().out.strip() == first


# ── the way the timer actually invokes it ────────────────────────────────────


def test_module_help_smoke_from_repo_root():
    """`python -m plugins.pr_intent_watch.run --help` must work from the repo
    root — the systemd unit runs exactly this, with no pytest imports
    around to mask a broken bootstrap."""
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, "-m", "plugins.pr_intent_watch.run", "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "usage" in proc.stdout.lower()
    assert "--dry-run" in proc.stdout
