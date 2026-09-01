"""Interrupted-update fleet-restart obligation (#95294 parts 1+2).

A ``hermes update`` killed after git pull advanced HEAD but before the
fleet restart left running gateways on stale code. The next update said
"Already up to date" and skipped restart. These tests cover:

- ``fleet_restart_pending`` marker written after HEAD advances, cleared
  after a successful (or no-op) fleet restart
- interrupt between pull and restart leaves the marker
- next ``hermes update`` with git already up to date still runs the
  pending restart when the marker OR a skewed unfinished latest.json is
  present

No live gateway, no network. Git and restart are mocked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import main as hermes_main
from hermes_cli import update_cmd
from hermes_constants import get_hermes_home

#: A full 40-hex git object name — what a real HEAD looks like.
FULL_SHA = "a" * 40


def _make_head_moved_side_effect(pre_sha="abc123", post_sha="def456"):
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


def _make_up_to_date_side_effect(sha="abc123"):
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
    """Patch ``_cmd_update_impl`` helpers. Mirrors test_update_head_moved_gate."""
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
    monkeypatch.setattr(
        update_cmd, "_venv_core_imports_healthy", lambda: (True, "")
    )
    monkeypatch.setattr(update_cmd, "_update_node_dependencies", lambda: [])

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


def _update_args():
    return SimpleNamespace(branch=None, yes=False, force=False, force_venv=False)


# ---------------------------------------------------------------------------
# Manual-restart double bounce: probe the live fleet before restarting
# ---------------------------------------------------------------------------

_CURRENT_SHA = "n" * 40
_STALE_SHA = "o" * 40


def _fleet_row(profile="default", state="current", sha=None):
    if sha is None:
        sha = _CURRENT_SHA if state == "current" else _STALE_SHA
    return {
        "profile": profile,
        "pid": 4242,
        "code_sha": sha,
        "code_version": "v1",
        "state": state,
    }


def _write_latest_receipt(payload: dict) -> None:
    receipt_dir = get_hermes_home() / "logs" / "update_receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / "latest.json").write_text(json.dumps(payload), encoding="utf-8")


def _plan_receipt(*profiles: str, expected_sha: str = _CURRENT_SHA) -> dict:
    return {
        "exit_code": 0,
        "outcome": "success",
        "plan": {
            "expected_sha": expected_sha,
            "runtimes": [
                {
                    "kind": "gateway",
                    "profile": profile,
                    "pid": 100 + i,
                    "code_sha": _STALE_SHA,
                }
                for i, profile in enumerate(profiles)
            ],
        },
    }


def test_live_fleet_matrix_decides_current(monkeypatch):
    """Only an all-`current`, non-empty snapshot may skip the restart."""
    monkeypatch.setattr(
        update_cmd, "_expected_gateway_profiles_from_receipt", lambda: {"default"}
    )
    already = update_cmd._live_fleet_already_serves_checkout
    assert already([]) is False  # nothing verifiably live is not "all fine"
    assert already([_fleet_row()]) is True
    assert already([_fleet_row(state="stale")]) is False
    assert already([_fleet_row(state="unknown", sha=None)]) is False
    assert already([_fleet_row(state="down", sha=None)]) is False
    assert already([_fleet_row(), _fleet_row(state="stale", profile="work")]) is False
    assert already(["not-a-dict"]) is False


def test_live_fleet_fails_closed_without_expected_profile_evidence(monkeypatch):
    """A missing/malformed receipt cannot prove no required profile is gone."""
    monkeypatch.setattr(
        update_cmd, "_expected_gateway_profiles_from_receipt", lambda: None
    )
    already = update_cmd._live_fleet_already_serves_checkout
    # An all-current observed fleet still cannot prove a required profile is
    # not missing — clearing the marker needs positive expected-profile
    # evidence, so this keeps the normal pending-restart path.
    assert already([_fleet_row()]) is False
    assert already([_fleet_row("default"), _fleet_row("work")]) is False


def test_live_fleet_requires_every_planned_gateway_profile(monkeypatch):
    monkeypatch.setattr(
        update_cmd,
        "_expected_gateway_profiles_from_receipt",
        lambda: {"default", "work"},
    )
    already = update_cmd._live_fleet_already_serves_checkout
    # The `work` gateway the plan saw is gone from the probe — fail closed.
    assert already([_fleet_row("default")]) is False
    assert already([_fleet_row("default"), _fleet_row("work")]) is True


def test_expected_gateway_profiles_read_from_latest_receipt():
    _write_latest_receipt(_plan_receipt("default", "work"))
    assert update_cmd._expected_gateway_profiles_from_receipt() == {"default", "work"}


def test_expected_gateway_profiles_none_without_usable_plan():
    _write_latest_receipt({"outcome": "success"})
    assert update_cmd._expected_gateway_profiles_from_receipt() is None


def test_expected_gateway_profiles_none_when_receipt_is_malformed():
    receipt_dir = get_hermes_home() / "logs" / "update_receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / "latest.json").write_text("{not json", encoding="utf-8")
    assert update_cmd._expected_gateway_profiles_from_receipt() is None


def test_catchup_clears_marker_without_restart_when_fleet_current(
    monkeypatch, capsys
):
    """The manual-`/restart` shape: fleet already serves disk HEAD."""
    update_cmd._write_fleet_restart_pending_marker()
    _write_latest_receipt(_plan_receipt("default"))
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions",
        lambda **k: [_fleet_row()],
    )
    restarts = {"n": 0}
    monkeypatch.setattr(
        update_cmd,
        "_run_pending_fleet_restart",
        lambda: restarts.__setitem__("n", restarts["n"] + 1) or True,
    )

    update_cmd._apply_pending_fleet_restart_catchup()

    assert restarts["n"] == 0
    assert not update_cmd._fleet_restart_pending_marker_path().exists()
    assert "clearing the pending restart" in capsys.readouterr().out


def test_catchup_restarts_when_expected_fleet_evidence_is_missing(monkeypatch):
    """No usable receipt/plan: an all-current fleet still owes the restart."""
    update_cmd._write_fleet_restart_pending_marker()
    _write_latest_receipt({"outcome": "success"})  # no plan → no evidence
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions",
        lambda **k: [_fleet_row()],
    )
    restarts = {"n": 0}
    monkeypatch.setattr(
        update_cmd,
        "_run_pending_fleet_restart",
        lambda: restarts.__setitem__("n", restarts["n"] + 1) or True,
    )

    update_cmd._apply_pending_fleet_restart_catchup()

    assert restarts["n"] == 1
    assert not update_cmd._fleet_restart_pending_marker_path().exists()


def test_catchup_restarts_when_fleet_stale(monkeypatch):
    update_cmd._write_fleet_restart_pending_marker()
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions",
        lambda **k: [_fleet_row(state="stale")],
    )
    restarts = {"n": 0}
    monkeypatch.setattr(
        update_cmd,
        "_run_pending_fleet_restart",
        lambda: restarts.__setitem__("n", restarts["n"] + 1) or True,
    )

    update_cmd._apply_pending_fleet_restart_catchup()

    assert restarts["n"] == 1
    assert not update_cmd._fleet_restart_pending_marker_path().exists()


def test_catchup_restarts_when_planned_profile_missing(monkeypatch):
    """A planned gateway the probe can no longer see keeps the restart."""
    update_cmd._write_fleet_restart_pending_marker()
    _write_latest_receipt(_plan_receipt("default", "work"))
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions",
        lambda **k: [_fleet_row("default")],
    )
    restarts = {"n": 0}
    monkeypatch.setattr(
        update_cmd,
        "_run_pending_fleet_restart",
        lambda: restarts.__setitem__("n", restarts["n"] + 1) or True,
    )

    update_cmd._apply_pending_fleet_restart_catchup()

    assert restarts["n"] == 1


def test_catchup_fails_closed_when_fleet_probe_raises(monkeypatch):
    """A failed inspection must never read as 'already current'."""
    update_cmd._write_fleet_restart_pending_marker()

    def _probe_broken(**_kwargs):
        raise RuntimeError("probe broken")

    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions", _probe_broken
    )
    restarts = {"n": 0}
    monkeypatch.setattr(
        update_cmd,
        "_run_pending_fleet_restart",
        lambda: restarts.__setitem__("n", restarts["n"] + 1) or True,
    )

    update_cmd._apply_pending_fleet_restart_catchup()

    assert restarts["n"] == 1
    assert not update_cmd._fleet_restart_pending_marker_path().exists()


def test_catchup_keeps_marker_when_restart_incomplete(monkeypatch):
    update_cmd._write_fleet_restart_pending_marker()
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions",
        lambda **k: [_fleet_row(state="stale")],
    )
    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", lambda: False)

    with pytest.raises(SystemExit) as excinfo:
        update_cmd._apply_pending_fleet_restart_catchup()

    assert excinfo.value.code == 1
    assert update_cmd._fleet_restart_pending_marker_path().is_file()


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------


def test_marker_round_trip_under_hermes_home():
    path = update_cmd._fleet_restart_pending_marker_path()
    assert path.parent == get_hermes_home()
    assert path.name == "fleet_restart_pending"
    assert not path.exists()

    update_cmd._write_fleet_restart_pending_marker(expected_sha="abc123")
    assert path.is_file()
    body = path.read_text(encoding="utf-8")
    assert "started=" in body
    assert "pid=" in body
    assert "expected_sha=abc123" in body

    update_cmd._clear_fleet_restart_pending_marker()
    assert not path.exists()


def test_pending_marker_records_prepared_state(monkeypatch):
    """Publication is the only writer of the activatable state, atomically.

    The strict record lives in ``fleet_restart_prepared`` — the generic
    pull-time marker only ever carries the obligation.
    """
    from hermes_cli import update_receipt as ur

    path = update_cmd._fleet_restart_pending_marker_path()
    record = update_cmd._fleet_restart_prepared_path()
    update_cmd._write_fleet_restart_pending_marker(expected_sha=FULL_SHA)
    # A pull-time marker is an obligation only: preparation state unknown.
    assert update_cmd._fleet_restart_pending_prepared() is False
    assert not record.exists()

    # The real publication path: receipt finalized first, record swapped in
    # only when the checkout still is the SHA the pull recorded.
    monkeypatch.setattr(update_cmd, "_current_checkout_head", lambda: FULL_SHA)
    ur.begin_update_receipt()
    assert update_cmd._publish_prepared_generation() == (True, "")

    generation = update_cmd._parse_prepared_generation()
    assert generation is not None
    assert update_cmd._fleet_restart_pending_prepared() is True
    # The generic marker keeps only the obligation — never the strict fields.
    marker_body = path.read_text(encoding="utf-8")
    assert "prepared=" not in marker_body
    assert f"expected_sha={FULL_SHA}" in marker_body, "the obligation's target survives"
    body = record.read_text(encoding="utf-8")
    assert "prepared=yes" in body
    assert f"expected_sha={FULL_SHA}" in body
    assert not list(path.parent.glob("fleet_restart_pending.tmp*"))
    assert not list(record.parent.glob("fleet_restart_prepared.tmp*"))
    # The receipt the record points at carries the same generation identity.
    receipt = ur.read_named_receipt(generation.receipt)
    assert receipt["prepared_generation"]["generation"] == generation.generation
    assert receipt["prepared_generation"]["expected_sha"] == FULL_SHA

    update_cmd._clear_fleet_restart_pending_marker()
    assert update_cmd._fleet_restart_pending_prepared() is False


def test_publication_refuses_a_moved_head(monkeypatch):
    """A checkout that moved since the pull must not be stamped prepared."""
    from hermes_cli import update_receipt as ur

    update_cmd._write_fleet_restart_pending_marker(expected_sha=FULL_SHA)
    monkeypatch.setattr(update_cmd, "_current_checkout_head", lambda: "b" * 40)
    ur.begin_update_receipt()

    ok, reason = update_cmd._publish_prepared_generation()

    assert ok is False
    assert "expected" in reason
    assert update_cmd._fleet_restart_pending_prepared() is False


def test_publication_fails_hard_when_the_marker_cannot_be_written(monkeypatch):
    """A marker write OSError is a failed preparation, never a silent stamp."""
    from hermes_cli import update_receipt as ur

    update_cmd._write_fleet_restart_pending_marker(expected_sha=FULL_SHA)
    monkeypatch.setattr(update_cmd, "_current_checkout_head", lambda: FULL_SHA)

    def _boom(_path, _text):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(update_cmd, "_atomic_write_text", _boom)
    ur.begin_update_receipt()
    ok, reason = update_cmd._publish_prepared_generation()

    assert ok is False
    assert reason
    assert update_cmd._fleet_restart_pending_prepared() is False
    # The receipt that WAS made durable can no longer claim a published
    # generation — it is amended to failed.
    latest = ur.read_latest_receipt()
    assert latest["outcome"] == "failed"


def test_publication_fails_hard_when_read_back_differs(monkeypatch):
    """A read-back that does not re-parse is not a published generation."""
    from hermes_cli import update_receipt as ur

    update_cmd._write_fleet_restart_pending_marker(expected_sha=FULL_SHA)
    monkeypatch.setattr(update_cmd, "_current_checkout_head", lambda: FULL_SHA)
    monkeypatch.setattr(
        update_cmd, "_parse_prepared_generation", lambda: None
    )
    ur.begin_update_receipt()

    ok, reason = update_cmd._publish_prepared_generation()

    assert ok is False
    assert "read-back" in reason
    assert ur.read_latest_receipt()["outcome"] == "failed"


def test_prepared_gate_is_strict_about_the_recorded_state():
    """A torn or hand-written prepared record must not read as prepared."""
    path = update_cmd._fleet_restart_prepared_path()

    def _writes_as(body: str) -> None:
        path.write_text(body, encoding="utf-8")
        assert update_cmd._parse_prepared_generation() is None
        assert update_cmd._fleet_restart_pending_prepared() is False

    _writes_as("prepared=y\n")
    _writes_as("started=1\npid=2\n")
    # ``prepared=yes`` alone — the exact stamp the old gate trusted — proves
    # nothing without the schema, generation, SHA and receipt binding.
    _writes_as("started=1\npid=2\nexpected_sha=x\nprepared=yes\n")
    # Truncated mid-line.
    _writes_as(f"schema=1\ngeneration={'c' * 32}\nexpected_sha={FULL_SHA[:20]}")
    # Unknown future schema.
    _writes_as(
        f"schema=2\ngeneration={'c' * 32}\nexpected_sha={FULL_SHA}\n"
        "receipt=update_x.json\nprepared=yes\nrestart=pending\n"
    )
    # Complete fields, but the SHA is short and the receipt name escapes the
    # receipt directory.
    _writes_as(
        f"schema=1\ngeneration={'c' * 32}\nexpected_sha=abc123\n"
        "receipt=../latest.json\nprepared=yes\nrestart=pending\n"
    )
    # Non-hex generation id.
    _writes_as(
        f"schema=1\ngeneration=not-a-generation-id\nexpected_sha={FULL_SHA}\n"
        "receipt=update_x.json\nprepared=yes\nrestart=pending\n"
    )
    # Restart obligation already consumed.
    _writes_as(
        f"schema=1\ngeneration={'c' * 32}\nexpected_sha={FULL_SHA}\n"
        "receipt=update_x.json\nprepared=yes\nrestart=done\n"
    )
    # A duplicate key — the lenient generic-marker reader would silently
    # keep the last one; the strict record refuses the whole file.
    _writes_as(
        f"schema=1\ngeneration={'c' * 32}\nexpected_sha={FULL_SHA}\n"
        "receipt=update_x.json\nprepared=yes\nprepared=no\nrestart=pending\n"
    )
    # An unknown key — a field this schema never writes could be an
    # injected security-relevant claim.
    _writes_as(
        f"schema=1\ngeneration={'c' * 32}\nexpected_sha={FULL_SHA}\n"
        "receipt=update_x.json\nprepared=yes\nrestart=pending\n"
        "activate_without_restart=yes\n"
    )
    # No trailing newline (a torn write).
    _writes_as(
        f"schema=1\ngeneration={'c' * 32}\nexpected_sha={FULL_SHA}\n"
        "receipt=update_x.json\nprepared=yes\nrestart=pending"
    )
    # Oversize.
    _writes_as(
        f"schema=1\ngeneration={'c' * 32}\nexpected_sha={FULL_SHA}\n"
        "receipt=update_x.json\nprepared=yes\nrestart=pending\n"
        + f"pid={'1' * (update_cmd._PREPARED_RECORD_MAX_BYTES)}\n"
    )
    # A complete record whose receipt does not exist parses but is still
    # unprepared — the receipt agreement gate rejects it one layer up.
    path.write_text(
        f"schema=1\ngeneration={'c' * 32}\nexpected_sha={FULL_SHA}\n"
        "receipt=update_missing.json\nprepared=yes\nrestart=pending\n",
        encoding="utf-8",
    )
    assert update_cmd._parse_prepared_generation() is not None
    assert update_cmd._fleet_restart_pending_prepared() is False
    update_cmd._clear_fleet_restart_pending_marker()


def test_prepared_gate_requires_the_bound_receipt_to_agree(monkeypatch):
    """Marker and receipt must independently claim the same generation."""
    from hermes_cli import update_receipt as ur

    update_cmd._write_fleet_restart_pending_marker(expected_sha=FULL_SHA)
    monkeypatch.setattr(update_cmd, "_current_checkout_head", lambda: FULL_SHA)
    ur.begin_update_receipt()
    assert update_cmd._publish_prepared_generation() == (True, "")
    generation = update_cmd._parse_prepared_generation()
    receipt_path = get_hermes_home() / "logs" / "update_receipts" / generation.receipt

    original = json.loads(receipt_path.read_text(encoding="utf-8"))

    def _rewrite(**changes):
        receipt = dict(original)
        bound = dict(receipt["prepared_generation"])
        bound.update(changes)
        receipt["prepared_generation"] = bound
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        parsed = update_cmd._parse_prepared_generation()
        assert parsed is not None, "the marker itself still parses"
        assert update_cmd._read_prepared_generation_receipt(parsed) is None

    _rewrite(generation="d" * 32)  # different generation id
    _rewrite(expected_sha="b" * 40)  # different target SHA
    _rewrite(restart="done")  # obligation already consumed
    receipt = dict(original)
    receipt["outcome"] = "failed"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert (
        update_cmd._read_prepared_generation_receipt(
            update_cmd._parse_prepared_generation()
        )
        is None
    )
    receipt = dict(original)
    receipt["stop_reason"] = "sys.exit(1)"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert (
        update_cmd._read_prepared_generation_receipt(
            update_cmd._parse_prepared_generation()
        )
        is None
    )


def test_pending_needed_when_marker_exists():
    update_cmd._write_fleet_restart_pending_marker()
    assert update_cmd._pending_fleet_restart_needed() is True
    update_cmd._clear_fleet_restart_pending_marker()
    assert update_cmd._pending_fleet_restart_needed() is False


# ---------------------------------------------------------------------------
# Exact-SHA gate: a full git object id is exactly 40 or exactly 64 hex
# ---------------------------------------------------------------------------


def test_prepared_sha_gate_accepts_only_exact_full_object_ids():
    """41/63-hex strings are not git object ids and must never parse."""
    cases = [
        ("a" * 39, False),
        ("a" * 40, True),
        ("a" * 41, False),
        ("a" * 63, False),
        ("a" * 64, True),
        ("a" * 65, False),
        ("g" * 40, False),  # non-hex
        ("a" * 40 + " ", False),  # trailing whitespace
        (" " + "a" * 40, False),
        ("a" * 40 + "\n", False),
        ("a" * 39 + "A", True),  # mixed case is still hex — normalized
        ("3e3ec776a6306374d691f5383897ce73af6efcd8", True),  # exact commit
        ("3e3ec776a6", False),  # abbreviation, never a prefix match
        ("refs/heads/" + "a" * 40, False),  # suffix/prefix decoration
    ]
    for value, expected in cases:
        assert update_cmd._normalize_prepared_sha(value) == (
            value.lower() if expected else ""
        ), value
        body = (
            f"schema=1\ngeneration={'c' * 32}\nexpected_sha={value}\n"
            "receipt=update_x.json\nprepared=yes\nrestart=pending\n"
        )
        update_cmd._fleet_restart_prepared_path().write_text(
            body, encoding="utf-8"
        )
        parsed = update_cmd._parse_prepared_generation()
        assert (parsed is not None) == expected, value
        if parsed is not None:
            assert parsed.expected_sha == value.lower()
    update_cmd._clear_fleet_restart_pending_marker()


# ---------------------------------------------------------------------------
# Durable publication: truncation and fsync failures are hard failures
# ---------------------------------------------------------------------------


def _publish_setup(monkeypatch):
    from hermes_cli import update_receipt as ur

    update_cmd._write_fleet_restart_pending_marker(expected_sha=FULL_SHA)
    monkeypatch.setattr(update_cmd, "_current_checkout_head", lambda: FULL_SHA)
    ur.begin_update_receipt()


def test_publication_fails_when_the_named_receipt_is_silently_truncated(
    monkeypatch,
):
    """A bound receipt that lands truncated must fail the whole publication.

    Fault injection: the rename succeeds but the canonical path ends up
    holding only half the payload — the read-back must catch it, the
    publication must fail, and no prepared state may be accepted.
    """
    import os

    _publish_setup(monkeypatch)
    real_replace = os.replace

    def _torn_replace(src, dst):
        real_replace(src, dst)
        path = Path(dst)
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])

    monkeypatch.setattr(os, "replace", _torn_replace)

    ok, reason = update_cmd._publish_prepared_generation()

    assert ok is False
    assert reason
    assert update_cmd._parse_prepared_generation() is None
    assert update_cmd._fleet_restart_pending_prepared() is False
    assert not update_cmd._fleet_restart_prepared_path().exists()
    receipt_dir = get_hermes_home() / "logs" / "update_receipts"
    assert not list(receipt_dir.glob("*.tmp*"))


def test_publication_fails_when_the_prepared_record_is_silently_truncated(
    monkeypatch,
):
    """Same torn rename, one step later: the record read-back catches it and
    the already-durable receipt is amended to failed."""
    import os

    _publish_setup(monkeypatch)
    real_replace = os.replace
    receipt_dir = get_hermes_home() / "logs" / "update_receipts"

    def _torn_replace(src, dst):
        real_replace(src, dst)
        if Path(dst).parent != receipt_dir:  # the prepared record, not receipts
            path = Path(dst)
            data = path.read_bytes()
            path.write_bytes(data[: len(data) // 2])

    monkeypatch.setattr(os, "replace", _torn_replace)

    ok, reason = update_cmd._publish_prepared_generation()

    assert ok is False
    assert reason
    assert update_cmd._parse_prepared_generation() is None
    assert update_cmd._fleet_restart_pending_prepared() is False
    from hermes_cli import update_receipt as ur

    assert ur.read_latest_receipt()["outcome"] == "failed"
    assert not list(update_cmd._fleet_restart_prepared_path().parent.glob("*.tmp*"))


def _fsync_bomb(*, on_directory: bool):
    """An os.fsync that raises OSError for exactly one fd class."""
    import os
    import stat as stat_mod

    real_fsync = os.fsync

    def _fake(fd):
        is_dir = stat_mod.S_ISDIR(os.fstat(fd).st_mode)
        if is_dir == on_directory:
            raise OSError(5, "Input/output error")
        return real_fsync(fd)

    return _fake


def test_atomic_write_propagates_file_fsync_failure(monkeypatch, tmp_path):
    """A temp-file fsync OSError is a hard publication failure, not a log."""
    import os

    monkeypatch.setattr(os, "fsync", _fsync_bomb(on_directory=False))
    target = tmp_path / "fleet_restart_prepared"

    with pytest.raises(OSError):
        update_cmd._atomic_write_text(target, "schema=1\n")

    assert not target.exists(), "no canonical file may appear"
    assert not list(tmp_path.glob("*.tmp*")), "no staging litter survives"


def test_atomic_write_propagates_directory_fsync_failure(monkeypatch, tmp_path):
    """On POSIX a parent-directory fsync OSError fails the publication."""
    import os

    monkeypatch.setattr(os, "fsync", _fsync_bomb(on_directory=True))
    target = tmp_path / "fleet_restart_prepared"

    with pytest.raises(OSError):
        update_cmd._atomic_write_text(target, "schema=1\n")

    assert not list(tmp_path.glob("*.tmp*"))


def test_atomic_write_dir_fsync_failure_fails_publication_end_to_end(monkeypatch):
    """The same injection through the real publish path: no false success."""
    import os

    _publish_setup(monkeypatch)
    monkeypatch.setattr(os, "fsync", _fsync_bomb(on_directory=True))

    ok, reason = update_cmd._publish_prepared_generation()

    assert ok is False
    assert reason
    assert update_cmd._fleet_restart_pending_prepared() is False
    home = get_hermes_home()
    assert not list(home.glob("*.tmp*"))
    receipt_dir = home / "logs" / "update_receipts"
    assert not list(receipt_dir.glob("*.tmp*"))


def test_atomic_write_skips_directory_fsync_only_off_posix(monkeypatch, tmp_path):
    """The Windows branch is explicit: directory fsync is skipped, errors on
    the FILE fsync still propagate there."""
    import os

    # Patch the flag through the exact function object update_cmd calls:
    # a cmd_update run in another test may have purged and re-imported
    # ``hermes_cli.durable_state``, leaving two module objects alive.
    _publish = update_cmd._atomic_write_text.__globals__["durable_publish_bytes"]
    monkeypatch.setitem(_publish.__globals__, "_SUPPORTS_DIR_FSYNC", False)
    monkeypatch.setattr(os, "fsync", _fsync_bomb(on_directory=True))
    update_cmd._atomic_write_text(tmp_path / "record", "schema=1\n")
    assert (tmp_path / "record").read_text(encoding="utf-8") == "schema=1\n"

    monkeypatch.setattr(os, "fsync", _fsync_bomb(on_directory=False))
    with pytest.raises(OSError):
        update_cmd._atomic_write_text(tmp_path / "record2", "schema=1\n")


def test_atomic_write_survives_a_short_writing_filesystem(monkeypatch, tmp_path):
    """Short writes loop to completion instead of silently truncating."""
    import os

    real_write = os.write
    state = {"n": 0}

    def _short_write(fd, buf):
        state["n"] += 1
        return real_write(fd, bytes(buf[: max(1, len(buf) // 3)]))

    monkeypatch.setattr(os, "write", _short_write)
    payload = "schema=1\ngeneration=abc\n" * 40
    target = tmp_path / "record"
    update_cmd._atomic_write_text(target, payload)
    assert target.read_text(encoding="utf-8") == payload


def test_atomic_write_refuses_oversize_and_empty_payloads(tmp_path):
    with pytest.raises(OSError):
        update_cmd._atomic_write_text(tmp_path / "big", "x" * (16 * 1024 * 1024))
    with pytest.raises(OSError):
        update_cmd._atomic_write_text(tmp_path / "empty", "")
    assert not (tmp_path / "big").exists()
    assert not (tmp_path / "empty").exists()


def test_pending_needed_when_unfinished_receipt_runtime_sha_skews(monkeypatch):
    disk_sha = "e" * 40
    old_sha = "7" * 40
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: disk_sha)

    receipt_dir = get_hermes_home() / "logs" / "update_receipts"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "latest.json").write_text(
        json.dumps(
            {
                "exit_code": 1,
                "stop_reason": "KeyboardInterrupt: ",
                "outcome": "failed",
                "plan": {
                    "expected_sha": disk_sha,
                    "runtimes": [
                        {
                            "kind": "gateway",
                            "profile": "default",
                            "pid": 2111768,
                            "supervisor": "systemd",
                            "code_sha": old_sha,
                            "restart_via": "systemd",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    assert update_cmd._pending_fleet_restart_needed() is True


def test_successful_receipt_with_pre_update_plan_shas_does_not_retrigger(
    monkeypatch,
):
    """A completed update's plan.runtimes are pre-pull SHAs — not a catch-up."""
    disk_sha = "n" * 40
    old_sha = "o" * 40
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: disk_sha)

    receipt_dir = get_hermes_home() / "logs" / "update_receipts"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "latest.json").write_text(
        json.dumps(
            {
                "exit_code": 0,
                "outcome": "success",
                "plan": {
                    "expected_sha": old_sha,
                    "runtimes": [
                        {
                            "kind": "gateway",
                            "profile": "default",
                            "pid": 1,
                            "code_sha": old_sha,
                        }
                    ],
                },
                "fleet": [
                    {
                        "profile": "default",
                        "pid": 2,
                        "code_sha": disk_sha,
                        "state": "current",
                    }
                ],
                "gateway_restart": {"incomplete": False},
            }
        ),
        encoding="utf-8",
    )

    assert update_cmd._pending_fleet_restart_needed() is False


def test_stale_fleet_matrix_on_latest_receipt_is_pending(monkeypatch):
    disk_sha = "n" * 40
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: disk_sha)

    receipt_dir = get_hermes_home() / "logs" / "update_receipts"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "latest.json").write_text(
        json.dumps(
            {
                "outcome": "partial",
                "exit_code": 1,
                "fleet": [
                    {
                        "profile": "default",
                        "pid": 9,
                        "code_sha": "s" * 40,
                        "state": "stale",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert update_cmd._pending_fleet_restart_needed() is True


def test_run_pending_restart_true_when_no_gateways(monkeypatch, capsys):
    # No systemd host (and no gateways): genuinely nothing to restart. The
    # systemd mock matters as much as the gateway one now — a systemd host
    # with managed serve units owes those a restart (Codex P1 B below).
    monkeypatch.setattr(
        "hermes_cli.gateway.find_gateway_pids", lambda **k: []
    )
    monkeypatch.setattr(hermes_main, "_purge_stale_hermes_modules", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.gateway.supports_systemd_services", lambda: False
    )

    assert update_cmd._run_pending_fleet_restart() is True
    assert "nothing to restart" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Serve-only fleets: no gateway PIDs, but systemd-managed serve/dashboard
# units still run pre-update code (Codex P1 B)
# ---------------------------------------------------------------------------


def _patch_no_gateway_systemd_fleet(
    monkeypatch, list_units_stdout: str, restart_handler=None
):
    """No gateway PIDs on a systemd host whose system scope answers.

    Returns the list of systemctl command lines executed, for assertions on
    exactly which units a restart was attempted for. *restart_handler*, when
    given, replaces the default succeeding restart. Nothing here can reach a
    real systemctl — ``subprocess.run`` itself is the mock.
    """
    monkeypatch.setattr(
        "hermes_cli.gateway.find_gateway_pids", lambda **k: []
    )
    monkeypatch.setattr(hermes_main, "_purge_stale_hermes_modules", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.gateway.supports_systemd_services", lambda: True
    )
    commands: list[list[str]] = []

    def _systemctl(cmd, **_kwargs):
        commands.append([str(part) for part in cmd])
        if "list-units" in cmd:
            if "--user" in cmd:
                # No user manager on this shape of host — system scope only.
                return SimpleNamespace(returncode=1, stdout="", stderr="no bus")
            return SimpleNamespace(
                returncode=0, stdout=list_units_stdout, stderr=""
            )
        if restart_handler is not None:
            restart_handler(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", _systemctl)
    return commands


def test_no_gateway_fleet_still_restarts_systemd_serve_units(monkeypatch, capsys):
    """A serve-only fleet must not read as "nothing to restart".

    ``find_gateway_pids() == []`` used to short-circuit to success before
    the systemd sweep, so a systemd-managed ``hermes serve``/dashboard kept
    its pre-update PID and activation could never verify a replacement.
    """
    commands = _patch_no_gateway_systemd_fleet(
        monkeypatch,
        "hermes-serve.service loaded active running\n"
        "hermes-dashboard.service loaded active running\n",
    )

    assert update_cmd._run_pending_fleet_restart() is True
    restarts = [c for c in commands if "restart" in c]
    assert {c[-1] for c in restarts} == {"hermes-serve", "hermes-dashboard"}
    out = capsys.readouterr().out
    assert "restarted 2 systemd serve/dashboard unit(s)" in out
    assert "nothing to restart" not in out


def test_no_gateway_fleet_with_no_systemd_units_still_true(monkeypatch, capsys):
    """No gateways AND no managed units anywhere → success, no attempts."""
    commands = _patch_no_gateway_systemd_fleet(monkeypatch, "")

    assert update_cmd._run_pending_fleet_restart() is True
    assert not [c for c in commands if "restart" in c]
    assert "nothing to restart" in capsys.readouterr().out


def test_no_gateway_fleet_systemd_unit_restart_failure_returns_false(
    monkeypatch, capsys
):
    """A managed serve unit that cannot restart fails the catch-up."""

    def _wedged_restart(cmd):
        raise subprocess.TimeoutExpired(cmd, timeout=30)

    commands = _patch_no_gateway_systemd_fleet(
        monkeypatch,
        "hermes-serve.service loaded active running\n",
        restart_handler=_wedged_restart,
    )

    assert update_cmd._run_pending_fleet_restart() is False
    assert any("restart" in c and c[-1] == "hermes-serve" for c in commands)
    out = capsys.readouterr().out
    assert "were not restarted" in out
    assert "hermes-serve" in out


# ---------------------------------------------------------------------------
# cmd_update integration (mocked git / restart)
# ---------------------------------------------------------------------------


def test_marker_written_after_pull_cleared_after_successful_restart(
    monkeypatch, tmp_path, capsys
):
    args = _update_args()
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())

    wrote = []
    orig = update_cmd._write_fleet_restart_pending_marker

    def _spy(*, expected_sha=""):
        orig(expected_sha=expected_sha)
        wrote.append(update_cmd._fleet_restart_pending_marker_path().is_file())

    monkeypatch.setattr(update_cmd, "_write_fleet_restart_pending_marker", _spy)

    hermes_main.cmd_update(args)

    assert wrote == [True], "marker must exist immediately after HEAD advances"
    assert not update_cmd._fleet_restart_pending_marker_path().exists()
    out = capsys.readouterr().out
    assert "✓ Code updated!" in out


def test_interrupt_between_pull_and_restart_leaves_marker(
    monkeypatch, tmp_path
):
    args = _update_args()
    _patch_update_deps(monkeypatch, tmp_path, _make_head_moved_side_effect())

    def _interrupt(*_a, **_k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(hermes_main, "_clear_bytecode_cache", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        hermes_main.cmd_update(args)

    marker = update_cmd._fleet_restart_pending_marker_path()
    assert marker.is_file()
    assert "expected_sha=def456" in marker.read_text(encoding="utf-8")
    # An interrupted plain update leaves a generic obligation: preparation
    # state unknown, so the auto-updater must never activate it. Only the
    # stock catch-up below may consume it.
    assert "prepared=" not in marker.read_text(encoding="utf-8")


def test_already_up_to_date_runs_pending_restart_when_marker_present(
    monkeypatch, tmp_path, capsys
):
    args = _update_args()
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())
    update_cmd._write_fleet_restart_pending_marker(expected_sha="def456")

    seen = {"ran": False}

    def _restart():
        seen["ran"] = True
        return True

    monkeypatch.setattr(update_cmd, "_run_pending_fleet_restart", _restart)

    hermes_main.cmd_update(args)

    assert seen["ran"] is True
    assert not update_cmd._fleet_restart_pending_marker_path().exists()
    out = capsys.readouterr().out
    assert "did not restart running gateways" in out


def test_already_up_to_date_runs_pending_restart_when_receipt_skewed(
    monkeypatch, tmp_path, capsys
):
    args = _update_args()
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())

    disk_sha = "e" * 40
    monkeypatch.setattr(update_cmd, "_current_checkout_sha", lambda: disk_sha)
    receipt_dir = get_hermes_home() / "logs" / "update_receipts"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "latest.json").write_text(
        json.dumps(
            {
                "exit_code": 1,
                "stop_reason": "KeyboardInterrupt: ",
                "outcome": "failed",
                "plan": {
                    "expected_sha": disk_sha,
                    "runtimes": [
                        {
                            "kind": "gateway",
                            "profile": "default",
                            "pid": 42,
                            "code_sha": "7" * 40,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    seen = {"ran": False}
    monkeypatch.setattr(
        update_cmd,
        "_run_pending_fleet_restart",
        lambda: seen.__setitem__("ran", True) or True,
    )

    hermes_main.cmd_update(args)

    assert seen["ran"] is True
    out = capsys.readouterr().out
    assert "did not restart running gateways" in out


def test_already_up_to_date_skips_restart_when_nothing_pending(
    monkeypatch, tmp_path, capsys
):
    args = _update_args()
    _patch_update_deps(monkeypatch, tmp_path, _make_up_to_date_side_effect())

    seen = {"ran": False}
    monkeypatch.setattr(
        update_cmd,
        "_run_pending_fleet_restart",
        lambda: seen.__setitem__("ran", True) or True,
    )

    hermes_main.cmd_update(args)

    assert seen["ran"] is False
    assert "did not restart running gateways" not in capsys.readouterr().out


def test_startup_warn_prints_when_marker_present(capsys):
    update_cmd._write_fleet_restart_pending_marker()
    update_cmd._warn_pending_fleet_restart_on_startup()
    err = capsys.readouterr().err
    assert "did not restart running gateways" in err
    assert "hermes gateway restart" in err


def test_startup_warn_silent_when_nothing_pending(capsys):
    update_cmd._warn_pending_fleet_restart_on_startup()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
