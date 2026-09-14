"""``hermes update --defer-restart``: full preparation, zero activation.

The deferred flag runs the whole update transaction through pull, dependency
sync, builds, migrations and skill sync, then stops — leaving
``fleet_restart_pending`` for a later activation (the auto-update plugin's
idle-gated ``activate`` subcommand, or the next plain ``hermes update``).

Covered:

- parser exposes the flag, default off
- deferred run on a moved HEAD: marker left behind, no restart/cleanup path
  touched, receipt records the deferred restart as a skip
- deferred run on an up-to-date checkout: no pending catch-up, marker kept
- default (non-deferred) behavior unchanged

No live gateway, no network. Git and restart are mocked.
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd
from hermes_constants import get_hermes_home

#: Full 40-hex git object names — the only SHAs the strict schema binds.
PRE_SHA = "1" * 40
POST_SHA = "2" * 40


def _make_head_moved_side_effect(pre_sha=PRE_SHA, post_sha=POST_SHA):
    """Simulate git commands where HEAD advances from pre_sha to post_sha."""
    calls = {"n": 0}

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")

        if joined.endswith("rev-parse HEAD"):
            if calls["n"] == 0:
                calls["n"] += 1
                return SimpleNamespace(returncode=0, stdout=f"{pre_sha}\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=f"{post_sha}\n", stderr="")

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _make_up_to_date_side_effect(sha=PRE_SHA):
    """Simulate git commands where origin is already at HEAD."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")

        if "rev-list" in joined:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")

        if joined.endswith("rev-parse HEAD"):
            return SimpleNamespace(returncode=0, stdout=f"{sha}\n", stderr="")

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return side_effect


def _patch_update_deps(monkeypatch, tmp_path, run_side_effect):
    """Patch ``_cmd_update_impl`` helpers. Mirrors test_update_fleet_restart_pending."""
    monkeypatch.setattr(hermes_main.subprocess, "run", run_side_effect)
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(hermes_main, "_resolve_update_branch", lambda args: "main")
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: False)
    monkeypatch.setattr(
        hermes_main,
        "_get_origin_url",
        lambda *a, **k: "https://github.com/NousResearch/hermes-agent.git",
    )
    monkeypatch.setattr(hermes_main, "_is_fork", lambda *a, **k: False)
    monkeypatch.setattr(
        hermes_main, "_stash_local_changes_if_needed", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", lambda *a, **k: 0)
    monkeypatch.setattr(
        hermes_main, "_record_bytecode_fingerprint", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_run_pre_update_backup", lambda *a, **k: None)
    monkeypatch.setattr(
        hermes_main, "_pause_windows_gateways_for_update", lambda: None
    )
    monkeypatch.setattr(
        hermes_main, "_resume_windows_gateways_after_update", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(hermes_main, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(
        hermes_main, "_finish_dashboard_update_cleanup", lambda *a, **k: None
    )
    monkeypatch.setattr(hermes_main, "_build_web_ui", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_venv_core_imports_healthy", lambda: (True, ""))
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])
    # Current main's import-health probe speaks a per-probe marker protocol;
    # the generic git-mock fallback above returns empty stdout, which that
    # protocol reads as a terminated probe and the deferred path correctly
    # treats as a failed required step. Patch the validator healthy directly
    # (the same patch point _REQUIRED_STEP_FAILURES uses to fail it), so only
    # a deliberately-failed import check can withhold the prepared stamp.
    monkeypatch.setattr(
        update_cmd, "_validate_critical_modules_import", lambda _root: (True, None, None)
    )

    import hermes_cli.gateway as hermes_gateway

    monkeypatch.setattr(
        hermes_gateway, "find_gateway_pids", lambda all_profiles=False: []
    )
    monkeypatch.setattr(hermes_gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(
        hermes_gateway, "find_profile_gateway_processes", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions",
        lambda **k: [],
    )
    monkeypatch.setattr(
        "hermes_cli.update_inventory.collect_runtime_inventory",
        lambda: SimpleNamespace(runtimes=[], to_dict=lambda: {}),
    )


def _update_args(**overrides):
    base = dict(branch=None, yes=False, force=False, force_venv=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def _forbid_restart_paths(monkeypatch) -> dict:
    """Spies that must stay at zero: every activation-phase entry point."""
    calls = {
        "restart": 0,
        "catchup": 0,
        "dashboard_cleanup": 0,
        "fleet_probe": 0,
        "gateway_probe": 0,
    }

    def _spy(key, result=True):
        def _record(*_args, **_kwargs):
            calls[key] += 1
            return result

        return _record

    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", _spy("restart"))
    monkeypatch.setattr(
        update_cmd, "_apply_pending_fleet_restart_catchup", _spy("catchup", None)
    )
    monkeypatch.setattr(
        hermes_main, "_finish_dashboard_update_cleanup", _spy("dashboard_cleanup")
    )
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions", _spy("fleet_probe", [])
    )

    import hermes_cli.gateway as hermes_gateway

    monkeypatch.setattr(
        hermes_gateway, "find_gateway_pids", _spy("gateway_probe", [])
    )
    return calls


def _latest_receipt() -> dict:
    path = get_hermes_home() / "logs" / "update_receipts" / "latest.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_update_parser_exposes_defer_restart():
    import argparse

    from hermes_cli.subcommands.update import build_update_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_update_parser(subparsers, cmd_update=lambda args: None)

    args = parser.parse_args(["update", "--defer-restart"])
    assert args.defer_restart is True
    assert parser.parse_args(["update"]).defer_restart is False
    assert "--defer-restart" in subparsers.choices["update"].format_help()


# ---------------------------------------------------------------------------
# Deferred run that pulled new code
# ---------------------------------------------------------------------------


def test_deferred_prepare_leaves_marker_and_skips_every_restart(
    monkeypatch, tmp_path, capsys
):
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    calls = _forbid_restart_paths(monkeypatch)

    hermes_main.cmd_update(_update_args(defer_restart=True))

    marker = update_cmd._fleet_restart_pending_marker_path()
    assert marker.is_file(), "deferred prepare must leave the restart obligation"
    assert f"expected_sha={POST_SHA}" in marker.read_text(encoding="utf-8")
    assert calls == {
        "restart": 0,
        "catchup": 0,
        "dashboard_cleanup": 0,
        "fleet_probe": 0,
        "gateway_probe": 0,
    }
    out = capsys.readouterr().out
    assert "✓ Code updated!" in out
    assert "restart deferred" in out
    assert "Restart pending" in out


def test_deferred_prepare_receipt_records_deferred_restart(
    monkeypatch, tmp_path
):
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    _forbid_restart_paths(monkeypatch)

    hermes_main.cmd_update(_update_args(defer_restart=True))

    receipt = _latest_receipt()
    assert receipt["outcome"] == "success"
    skips = {s["name"]: s["reason"] for s in receipt["skips"]}
    assert "deferred by --defer-restart" in skips["fleet_restart"]
    assert any(s["name"] == "defer_restart" for s in receipt["steps"])


def test_deferred_prepare_failure_stays_nonzero(monkeypatch, tmp_path):
    """A preparation failure must exit nonzero — never presented as ready."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    calls = _forbid_restart_paths(monkeypatch)

    def _interrupt(*_a, **_k):
        raise KeyboardInterrupt()

    # The pull advanced HEAD (marker written), then preparation died.
    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert update_cmd._fleet_restart_pending_marker_path().is_file()
    assert calls["restart"] == 0
    assert calls["catchup"] == 0


# ---------------------------------------------------------------------------
# Prepared state: durable proof the preparation finished
# ---------------------------------------------------------------------------


def test_deferred_prepare_success_stamps_prepared_marker(monkeypatch, tmp_path):
    """Readiness is written only after the whole preparation transaction."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    _forbid_restart_paths(monkeypatch)

    hermes_main.cmd_update(_update_args(defer_restart=True))

    marker = update_cmd._fleet_restart_pending_marker_path()
    record = update_cmd._fleet_restart_prepared_path()
    assert marker.is_file()
    assert update_cmd._fleet_restart_pending_prepared() is True
    generation = update_cmd._parse_prepared_generation()
    assert generation is not None
    assert generation.expected_sha == POST_SHA
    # The generic marker keeps the obligation only; the strict fields live
    # in the authoritative prepared record.
    marker_body = marker.read_text(encoding="utf-8")
    assert "prepared=" not in marker_body
    assert f"expected_sha={POST_SHA}" in marker_body
    body = record.read_text(encoding="utf-8")
    assert "prepared=yes" in body
    assert f"expected_sha={POST_SHA}" in body
    # The receipt the record is bound to is durable and agrees.
    receipt = update_cmd._read_prepared_generation_receipt(generation)
    assert receipt is not None
    assert receipt["prepared_generation"]["generation"] == generation.generation
    # The staged siblings of the atomic swaps never survive the writes.
    assert not list(marker.parent.glob("fleet_restart_pending.tmp*"))
    assert not list(record.parent.glob("fleet_restart_prepared.tmp*"))


def test_failed_reprepare_preserves_the_prior_prepared_record_byte_identical(
    monkeypatch, tmp_path
):
    """A re-prepare that fails after the pull-time marker write cannot
    destroy the generation a previous COMPLETED preparation published."""
    THIRD_SHA = "3" * 40
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    _forbid_restart_paths(monkeypatch)

    # Tick 1: a fully successful deferred prepare publishes a generation.
    hermes_main.cmd_update(_update_args(defer_restart=True))
    record = update_cmd._fleet_restart_prepared_path()
    before = record.read_bytes()
    generation_before = update_cmd._parse_prepared_generation()
    assert generation_before is not None
    # Tick 2 shares this pid; keep its failed receipt from landing on the
    # same second-stamped file name as the published one.
    time.sleep(1.1)

    # Tick 2: the pull advances HEAD again — the generic marker is rewritten
    # with the new target — and then a required step fails.
    monkeypatch.setattr(
        hermes_main.subprocess,
        "run",
        _make_head_moved_side_effect(POST_SHA, THIRD_SHA),
    )
    monkeypatch.setattr(hermes_main, "_build_web_ui", lambda *a, **k: False)

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert excinfo.value.code == 1
    assert record.read_bytes() == before, "the prior record is byte-identical"
    assert update_cmd._parse_prepared_generation() == generation_before
    assert update_cmd._fleet_restart_pending_prepared() is True
    # The generic obligation now names the new HEAD — the two are separate.
    marker = update_cmd._fleet_restart_pending_marker_path()
    assert f"expected_sha={THIRD_SHA}" in marker.read_text(encoding="utf-8")


def test_deferred_prepare_late_failure_leaves_marker_unprepared(
    monkeypatch, tmp_path
):
    """A failure after the pull must not look activatable on a later tick."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    calls = _forbid_restart_paths(monkeypatch)

    def _node_deps_fail():
        raise subprocess.CalledProcessError(2, ["npm", "ci"])

    # The pull advanced HEAD (marker written), the Python dependency sync
    # finished, and the Node dependency stage then failed.
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", _node_deps_fail)

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert excinfo.value.code == 1
    marker = update_cmd._fleet_restart_pending_marker_path()
    assert marker.is_file(), "the pull's restart obligation survives"
    assert "prepared=" not in marker.read_text(encoding="utf-8")
    assert update_cmd._fleet_restart_pending_prepared() is False
    assert calls["restart"] == 0
    assert calls["catchup"] == 0


def test_deferred_up_to_date_run_never_stamps_readiness(monkeypatch, tmp_path):
    """`--check` reporting up to date is not proof a pending prep finished."""
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    _forbid_restart_paths(monkeypatch)
    update_cmd._write_fleet_restart_pending_marker(expected_sha=POST_SHA)

    hermes_main.cmd_update(_update_args(defer_restart=True))

    # A run that prepared nothing may not make an older, unproven obligation
    # activatable.
    assert update_cmd._fleet_restart_pending_prepared() is False
    assert update_cmd._fleet_restart_pending_marker_path().is_file()


# ---------------------------------------------------------------------------
# Every attempted-but-failed required step withholds the prepared stamp
# ---------------------------------------------------------------------------

#: name → patch applied AFTER the pull, each making one *required* step of the
#: selected install report failure or a partial result. Optional work that was
#: never selected is not here — see the companion test below.
_REQUIRED_STEP_FAILURES = {
    "lazy_refresh": lambda mp: mp.setattr(
        hermes_main, "_refresh_active_lazy_features", lambda *a, **k: False
    ),
    "critical_import": lambda mp: mp.setattr(
        update_cmd,
        "_validate_critical_modules_import",
        lambda _root: (False, "hermes_cli.gateway", "ImportError: nope"),
    ),
    "node_partial": lambda mp: mp.setattr(
        update_cmd, "_update_node_dependencies", lambda: ["ui-tui, web workspaces"]
    ),
    "web_build": lambda mp: mp.setattr(
        hermes_main, "_build_web_ui", lambda *a, **k: False
    ),
    "desktop_build": lambda mp: mp.setattr(
        update_cmd, "_rebuild_desktop_after_update", lambda *a, **k: False
    ),
    "skill_sync": lambda mp: mp.setattr(
        "tools.skills_sync.sync_skills",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("disk full")),
    ),
}


@pytest.mark.parametrize("step", sorted(_REQUIRED_STEP_FAILURES))
def test_deferred_required_step_failure_withholds_prepared_stamp(
    step, monkeypatch, tmp_path, capsys
):
    """Tried-and-failed required work means an incomplete preparation."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    calls = _forbid_restart_paths(monkeypatch)
    _REQUIRED_STEP_FAILURES[step](monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert excinfo.value.code == 1
    marker = update_cmd._fleet_restart_pending_marker_path()
    assert marker.is_file(), "the pull's restart obligation survives"
    assert update_cmd._fleet_restart_pending_prepared() is False
    out = capsys.readouterr().out
    assert "NOT prepared" in out
    assert calls["restart"] == 0
    assert calls["catchup"] == 0


def test_deferred_unselected_optional_work_still_prepares(monkeypatch, tmp_path):
    """Work that was never selected cannot fail the preparation.

    ``_build_web_ui`` returning None (nothing to build / not installed) and
    an empty Node refresh are the "unselected optional component" shapes:
    they are not failures, and a deferred run over them still publishes.
    """
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    _forbid_restart_paths(monkeypatch)
    monkeypatch.setattr(hermes_main, "_build_web_ui", lambda *a, **k: None)
    monkeypatch.setattr(
        update_cmd, "_rebuild_desktop_after_update", lambda *a, **k: True
    )

    hermes_main.cmd_update(_update_args(defer_restart=True))

    assert update_cmd._fleet_restart_pending_prepared() is True


def test_deferred_prepare_marker_write_failure_exits_nonzero(
    monkeypatch, tmp_path, capsys
):
    """An undurable obligation breadcrumb can never be reported as prepared."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    calls = _forbid_restart_paths(monkeypatch)

    def _boom(_path, _text):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(update_cmd, "_atomic_write_text", _boom)

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "Could not record the pending fleet restart durably" in out
    assert "✓ Update prepared" not in out
    assert calls["restart"] == 0
    assert calls["catchup"] == 0


# ---------------------------------------------------------------------------
# No-update repair: the obligation a repair creates is a real generation
# ---------------------------------------------------------------------------


def _patch_no_update_repair(monkeypatch):
    """Make the up-to-date path take the venv-repair branch, then succeed."""
    attempts = {"n": 0}

    def _health():
        attempts["n"] += 1
        # Unhealthy on the pre-repair probe, healthy after the repair.
        return (False, "ImportError: hermes_cli.gateway") if attempts["n"] == 1 else (
            True,
            "",
        )

    monkeypatch.setattr(update_cmd, "_venv_core_imports_healthy", _health)
    monkeypatch.setattr(hermes_main, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(
        hermes_main, "_install_python_dependencies_with_optional_fallback", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_refresh_active_lazy_features", lambda *a, **k: True
    )
    monkeypatch.setattr(
        hermes_main, "_restore_active_tool_dependencies", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_abort_dependency_sync_if_self_locked", lambda *a, **k: None
    )
    monkeypatch.setattr(
        update_cmd, "_check_and_apply_config_migration", lambda **k: None
    )
    monkeypatch.setattr(update_cmd, "_print_update_completion", lambda *a, **k: None)


def test_deferred_no_update_repair_publishes_a_generation(monkeypatch, tmp_path, capsys):
    """A repair that rewrote the venv owes a SHA-bound restart obligation."""
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    calls = _forbid_restart_paths(monkeypatch)
    _patch_no_update_repair(monkeypatch)

    hermes_main.cmd_update(_update_args(defer_restart=True))

    generation = update_cmd._parse_prepared_generation()
    assert generation is not None, "the repair must leave a validated generation"
    assert generation.expected_sha == PRE_SHA, "bound to the CURRENT head"
    assert update_cmd._read_prepared_generation_receipt(generation) is not None
    assert update_cmd._fleet_restart_pending_prepared() is True
    out = capsys.readouterr().out
    assert "Dependencies repaired — fleet restart deferred" in out
    assert calls["restart"] == 0
    assert calls["catchup"] == 0


def test_deferred_no_update_repair_failure_withholds_the_stamp(
    monkeypatch, tmp_path, capsys
):
    """A repair that did not converge is not a completed preparation."""
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    _forbid_restart_paths(monkeypatch)
    monkeypatch.setattr(
        update_cmd, "_venv_core_imports_healthy", lambda: (False, "still broken")
    )
    monkeypatch.setattr(hermes_main, "_clear_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(
        hermes_main,
        "_install_python_dependencies_with_optional_fallback",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        hermes_main, "_refresh_active_lazy_features", lambda *a, **k: True
    )
    monkeypatch.setattr(
        hermes_main, "_restore_active_tool_dependencies", lambda *a, **k: None
    )
    monkeypatch.setattr(
        hermes_main, "_abort_dependency_sync_if_self_locked", lambda *a, **k: None
    )
    monkeypatch.setattr(
        update_cmd, "_check_and_apply_config_migration", lambda **k: None
    )
    monkeypatch.setattr(
        update_cmd, "_print_update_completion", lambda *a, **k: None
    )

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert excinfo.value.code == 1
    assert update_cmd._fleet_restart_pending_prepared() is False
    assert "NOT prepared" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Codex P1 C: failed completion verification blocks prepared publication
# ---------------------------------------------------------------------------


def _force_partial_completion(monkeypatch):
    """Make the real completion verification withhold success.

    A POSITIVE WAL-vulnerable SQLite probe is the canonical partial shape:
    the update/repair itself finished, but the runtime it leaves behind
    still carries the corruption bug, so ``_print_update_summary`` /
    ``_print_verified_update_completion`` demote the outcome and return
    False without a single other stage failing.
    """
    monkeypatch.setattr(
        update_cmd,
        "_post_update_sqlite_runtime_status",
        lambda: (False, SimpleNamespace(sqlite_version_string="3.46.1")),
    )


def test_deferred_partial_completion_never_publishes_a_generation(
    monkeypatch, tmp_path, capsys
):
    """A HEAD-advancing run that ends partially complete is not prepared.

    The WAL-vulnerable probe used to be invisible to the deferred path:
    ``_print_update_summary`` returned False into a variable nobody read,
    so ``_finish_deferred_restart`` still published a prepared generation
    for a checkout the run itself called only partially complete.
    """
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    calls = _forbid_restart_paths(monkeypatch)
    _force_partial_completion(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert excinfo.value.code == 1
    marker = update_cmd._fleet_restart_pending_marker_path()
    assert marker.is_file(), "the pull's restart obligation survives"
    assert update_cmd._fleet_restart_pending_prepared() is False
    assert not update_cmd._fleet_restart_prepared_path().exists()
    out = capsys.readouterr().out
    assert "Update partially complete" in out
    assert "NOT prepared" in out
    assert "completion verification failed" in out
    assert calls["restart"] == 0
    assert calls["catchup"] == 0


def test_deferred_no_update_repair_partial_completion_never_publishes(
    monkeypatch, tmp_path, capsys
):
    """The no-update repair path holds the same publication gate.

    A venv repair that rewrote the dependencies but ended on a positive
    WAL-vulnerable probe used to ignore ``current_checkout_complete`` and
    publish a prepared generation anyway.
    """
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    calls = _forbid_restart_paths(monkeypatch)
    _patch_no_update_repair(monkeypatch)
    _force_partial_completion(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert excinfo.value.code == 1
    assert update_cmd._fleet_restart_pending_prepared() is False
    assert not update_cmd._fleet_restart_prepared_path().exists()
    assert not update_cmd._fleet_restart_pending_marker_path().exists()
    out = capsys.readouterr().out
    assert "NOT prepared" in out
    assert "completion verification failed" in out
    assert calls["restart"] == 0
    assert calls["catchup"] == 0


def test_deferred_update_partial_completion_still_restarts_by_default(
    monkeypatch, tmp_path, capsys
):
    """Control: without --defer-restart, partial completion keeps the stock
    restart phase — only the deferred publication is blocked (Codex P1 C
    explicitly preserves the non-deferred behavior)."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    _forbid_restart_paths(monkeypatch)
    _force_partial_completion(monkeypatch)

    # No preparation-failure exit: the stock run reports the partial outcome
    # through its own banner and completes the restart phase regardless.
    hermes_main.cmd_update(_update_args())

    out = capsys.readouterr().out
    assert "Update partially complete" in out
    assert "NOT prepared" not in out, "the stock path has no preparation gate"
    assert not update_cmd._fleet_restart_pending_marker_path().exists(), (
        "the stock restart phase still consumed the obligation"
    )


def test_deferred_true_noop_preserves_an_existing_generation(monkeypatch, tmp_path):
    """A no-op run invents no obligation and does not disturb a valid one."""
    from hermes_cli import update_receipt as ur

    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    calls = _forbid_restart_paths(monkeypatch)
    monkeypatch.setattr(update_cmd, "_current_checkout_head", lambda: PRE_SHA)
    update_cmd._write_fleet_restart_pending_marker(expected_sha=PRE_SHA)
    ur.begin_update_receipt()
    assert update_cmd._publish_prepared_generation() == (True, "")
    before = update_cmd._parse_prepared_generation()
    # The update run about to happen shares this pid; keep its receipt file
    # from landing on the same second-stamped name as the published one.
    time.sleep(1.1)

    hermes_main.cmd_update(_update_args(defer_restart=True))

    after = update_cmd._parse_prepared_generation()
    assert after == before, "an existing valid generation is preserved verbatim"
    assert update_cmd._fleet_restart_pending_prepared() is True
    assert calls["restart"] == 0
    assert calls["catchup"] == 0


def test_deferred_true_noop_with_no_obligation_creates_none(monkeypatch, tmp_path):
    """Nothing pulled, nothing repaired → no restart obligation at all."""
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    _forbid_restart_paths(monkeypatch)

    hermes_main.cmd_update(_update_args(defer_restart=True))

    assert not update_cmd._fleet_restart_pending_marker_path().exists()


# ---------------------------------------------------------------------------
# ZIP fallback: never under --defer-restart
# ---------------------------------------------------------------------------


def _patch_windows_zip_shape(monkeypatch):
    """Windows + a git-shaped failure after the pull = the ZIP fallback case."""
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: True)
    monkeypatch.setattr(hermes_main, "_venv_scripts_dir", lambda: None)
    monkeypatch.setattr(
        hermes_main, "_detect_venv_python_processes", lambda: []
    )
    zips = {"n": 0}
    monkeypatch.setattr(
        update_cmd,
        "_update_via_zip",
        lambda *a, **k: zips.__setitem__("n", zips["n"] + 1) or True,
    )
    return zips


def test_deferred_never_falls_back_to_zip(monkeypatch, tmp_path, capsys):
    """The ZIP fallback restarts the fleet — a deferred run keeps the failure."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    calls = _forbid_restart_paths(monkeypatch)
    zips = _patch_windows_zip_shape(monkeypatch)

    def _git_pull_dies(*_a, **_k):
        raise subprocess.CalledProcessError(1, ["git", "pull"], stderr="fatal")

    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", _git_pull_dies)

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert excinfo.value.code == 1
    assert zips["n"] == 0, "--defer-restart must not take the ZIP fallback"
    assert calls["restart"] == 0
    assert calls["catchup"] == 0
    out = capsys.readouterr().out
    assert "--defer-restart: ZIP fallback skipped" in out


def test_default_update_still_falls_back_to_zip(monkeypatch, tmp_path):
    """Control: without --defer-restart the git-failure fallback still runs."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    _forbid_restart_paths(monkeypatch)
    zips = _patch_windows_zip_shape(monkeypatch)

    def _git_pull_dies(*_a, **_k):
        raise subprocess.CalledProcessError(1, ["git", "pull"], stderr="fatal")

    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", _git_pull_dies)

    hermes_main.cmd_update(_update_args())

    assert zips["n"] == 1


# ---------------------------------------------------------------------------
# Deferred run on an already-current checkout
# ---------------------------------------------------------------------------


def test_deferred_up_to_date_never_catches_up_pending_restart(
    monkeypatch, tmp_path, capsys
):
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    calls = _forbid_restart_paths(monkeypatch)
    update_cmd._write_fleet_restart_pending_marker(expected_sha="def456")

    hermes_main.cmd_update(_update_args(defer_restart=True))

    # The marker is a pending obligation, and this run deliberately declined
    # to fulfill it — activation is a later, idle decision.
    assert update_cmd._fleet_restart_pending_marker_path().is_file()
    assert calls["catchup"] == 0
    assert calls["restart"] == 0
    out = capsys.readouterr().out
    assert "Restart pending" in out


def test_default_up_to_date_still_catches_up_pending_restart(
    monkeypatch, tmp_path
):
    """Control: without --defer-restart the pending catch-up still runs."""
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    catchups = {"n": 0}

    def _catchup():
        catchups["n"] += 1

    monkeypatch.setattr(
        update_cmd, "_apply_pending_fleet_restart_catchup", _catchup
    )
    update_cmd._write_fleet_restart_pending_marker(expected_sha="def456")

    hermes_main.cmd_update(_update_args())

    assert catchups["n"] == 1
    update_cmd._clear_fleet_restart_pending_marker()


# ---------------------------------------------------------------------------
# Windows: a deferred run refuses rather than stop/kill a venv holder
# ---------------------------------------------------------------------------

# One fake holder: a network-bound `serve` backend, the shape every rung of
# the venv-holder sweep exists to reap.
_HOLDER_CMDLINE = "venv\\Scripts\\python.exe -m hermes serve --host 10.0.0.4"
_HOLDERS = [(4242, "python.exe", _HOLDER_CMDLINE)]

# rung name → (classifier the rung consults, shape it must claim). Each rung
# reaps holders through a different stop/kill entry point; the classifiers of
# the other rungs are neutralised so exactly one rung can fire.
_VENV_STOP_RUNGS = {
    "pausable_gateway": "_leftover_pausable_gateway_pids",
    "ledger_orphan": "_ledger_reapable_backend_pids",
    "desktop_orphan": "_orphaned_desktop_backend_pids",
    "manual_serve": "_ledger_manual_serve_holders",
}


def _claim_pids(matches):
    return [pid for pid, _name, _cmdline in matches]


def _claim_serve_entries(matches):
    return [{"pid": pid, "purpose": "serve"} for pid, _name, _cmdline in matches]


def _claim_nothing(_matches):
    return None


def _claim_no_serves(_matches):
    return []


def _claim_only(monkeypatch, rung: str) -> None:
    """Make exactly one sweep rung claim the fake holders."""
    for name, classifier in _VENV_STOP_RUNGS.items():
        if name == rung:
            # The claiming answer: PIDs for the per-PID kill rungs, ledger
            # entries for the manual serve/dashboard rung.
            claim = _claim_serve_entries if name == "manual_serve" else _claim_pids
        else:
            # The "not mine" answer that moves the sweep on to the next rung.
            claim = _claim_no_serves if name == "manual_serve" else _claim_nothing
        monkeypatch.setattr(hermes_main, classifier, claim)


def _patch_windows_venv_guard(monkeypatch) -> dict:
    """Wire the Windows venv-holder guard to a fake process table.

    The re-scan after a rung reports an empty table, which is what lets the
    DEFAULT (non-deferred) path proceed past the guard. Returns the spies on
    every stop/kill entry point the sweep can reach.
    """
    monkeypatch.setattr(hermes_main, "_is_windows", lambda: True)
    # None skips the concurrent-hermes.exe shim guard above the sweep.
    monkeypatch.setattr(hermes_main, "_venv_scripts_dir", lambda: None)
    scans = {"n": 0}

    def _scan():
        scans["n"] += 1
        return [] if scans["n"] > 1 else list(_HOLDERS)

    monkeypatch.setattr(hermes_main, "_detect_venv_python_processes", _scan)
    stops = {"terminate": 0, "trees": 0, "relaunch": 0}

    def _count(key):
        def _record(*_args, **_kwargs):
            stops[key] += 1

        return _record

    monkeypatch.setattr("gateway.status.terminate_pid", _count("terminate"))
    monkeypatch.setattr(hermes_main, "_stop_process_trees", _count("trees"))
    monkeypatch.setattr(hermes_main, "_relaunch_stopped_serves", _count("relaunch"))
    return stops


@pytest.mark.parametrize("rung", sorted(_VENV_STOP_RUNGS))
def test_deferred_never_stops_windows_venv_holders(
    rung, monkeypatch, tmp_path, capsys
):
    """Every stop/kill rung refuses under --defer-restart instead of reaping."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    calls = _forbid_restart_paths(monkeypatch)
    stops = _patch_windows_venv_guard(monkeypatch)
    _claim_only(monkeypatch, rung)

    with pytest.raises(SystemExit) as excinfo:
        hermes_main.cmd_update(_update_args(defer_restart=True))

    assert excinfo.value.code == 2, "a deferred run refuses, never reaps"
    assert stops == {"terminate": 0, "trees": 0, "relaunch": 0}
    assert calls["restart"] == 0
    assert calls["catchup"] == 0
    out = capsys.readouterr().out
    assert "--defer-restart never stops a running Hermes runtime" in out
    # The refusal precedes the pull, so nothing was prepared either.
    assert not update_cmd._fleet_restart_pending_marker_path().exists()


@pytest.mark.parametrize("rung", sorted(_VENV_STOP_RUNGS))
def test_default_update_still_reaps_windows_venv_holders(rung, monkeypatch, tmp_path):
    """Control: without --defer-restart each rung still stops its holders."""
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())
    _forbid_restart_paths(monkeypatch)
    stops = _patch_windows_venv_guard(monkeypatch)
    _claim_only(monkeypatch, rung)

    hermes_main.cmd_update(_update_args())

    # Exactly one stop/kill entry point fired — the pausable-gateway rung
    # terminates per PID, the others stop one process tree.
    assert stops["terminate"] + stops["trees"] == 1
