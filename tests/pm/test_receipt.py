"""pm.receipt: the universal machine-readable surface for venv ops.

Every pm sync writes one; same schema/dir as update receipts; the
updater embeds the sync sections via snapshot().
"""

from __future__ import annotations

import json

import pytest

import pm.receipt as receipt


@pytest.fixture(autouse=True)
def _isolated_receipt_context():
    """The ContextVar is module state — tests must not see each other's
    in-flight receipt (a leaked begin would corrupt the next test)."""
    receipt._current.set(None)
    yield
    receipt._current.set(None)


@pytest.fixture
def homed(tmp_path, monkeypatch):
    """Receipt dir inside a temp hermes home."""
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    return tmp_path


def test_begin_record_finalize_roundtrip(homed):
    receipt.begin("sync")
    receipt.record_step("uv-lock", True)
    receipt.record_venv_rebuild(True)
    receipt.record_bisect(
        [{"plugin": "bad", "action": "disabled", "reason": "conflict"}]
    )
    receipt.record_feature_list(["web", "acp"])
    path = receipt.finalize("bisected")
    assert path is not None and path.is_file()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == "sync"
    assert data["outcome"] == "bisected"
    assert data["venv_rebuild"] == {"ok": True, "reason": ""}
    assert data["plugin_bisect"][0]["plugin"] == "bad"
    assert data["feature_list"] == ["web", "acp"]


def test_latest_points_at_newest(homed):
    receipt.begin("sync")
    receipt.finalize("ok")
    latest = receipt.latest()
    assert latest is not None
    assert latest["outcome"] == "ok"

    receipt.begin("sync")
    receipt.record_venv_rebuild(False, "uv sync exited 1")
    receipt.finalize("failed", 1)
    latest = receipt.latest()
    assert latest["outcome"] == "failed"
    assert latest["venv_rebuild"]["reason"] == "uv sync exited 1"


def test_finalize_without_begin_is_none(homed):
    assert receipt.finalize("ok") is None


def test_snapshot_returns_inflight(homed):
    receipt.begin("sync")
    snap = receipt.snapshot()
    assert snap is not None and snap["kind"] == "sync"
    receipt.finalize("ok")
    assert receipt.snapshot() is None


def test_latest_none_when_empty(homed):
    assert receipt.latest() is None


def test_latest_does_not_create_dirs(homed):
    """Reading a receipt must be side-effect free — no logs/ mkdir."""
    import shutil

    logs = homed / "logs"
    if logs.exists():
        shutil.rmtree(logs)
    assert receipt.latest() is None
    assert not logs.exists()


def test_concurrent_finalize_writes_unique_names(homed):
    """Overlapping syncs (threaded cadence/ensure) must each get their own
    receipt file — no stamp collision overwrites."""
    import threading

    paths: list = []
    errors: list = []

    def run(i):
        try:
            receipt.begin("sync")
            receipt.record_step(f"step-{i}", True)
            paths.append(receipt.finalize("ok"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(p is not None and p.is_file() for p in paths)
    assert len({p.name for p in paths}) == 8


def test_rotation_only_removes_pm_receipts(homed):
    """The shared receipts dir also holds the updater's update_*.json —
    pm rotation must never delete those."""
    d = homed / "logs" / "update_receipts"
    d.mkdir(parents=True)
    updater = d / "update_20260101_000000_123.json"
    updater.write_text('{"kind": "update"}\n', encoding="utf-8")
    for i in range(25):
        (d / f"pm_20260101T0000{i:02d}Z-sync-1-0001.json").write_text(
            '{"kind": "sync"}\n', encoding="utf-8"
        )
    receipt._rotate(d)
    assert updater.is_file()
    kept = sorted(p.name for p in d.glob("pm_*.json"))
    assert len(kept) == 20  # oldest pm receipts rotated, updater receipt untouched


def test_receipt_state_is_thread_scoped(homed):
    """begin/record must not leak across threads: an overlapping sync in
    another thread neither sees nor clobbers this one's in-flight receipt."""
    import threading

    receipt.begin("sync")
    receipt.record_step("mine", True)
    seen: dict = {}

    def other():
        seen["snapshot_from_other"] = receipt.snapshot()
        receipt.begin("update")
        receipt.record_step("theirs", False)
        receipt.finalize("failed")
        seen["after_other"] = receipt.snapshot()

    t = threading.Thread(target=other)
    t.start()
    t.join()
    # the other thread never saw our in-flight receipt
    assert seen["snapshot_from_other"] is None
    # its begin/finalize did not clobber ours
    snap = receipt.snapshot()
    assert snap is not None and snap["kind"] == "sync"
    assert [s["name"] for s in snap["steps"]] == ["mine"]
    path = receipt.finalize("ok")
    assert path is not None and path.is_file()


def test_copied_context_does_not_corrupt_parent_receipt():
    """A copied context (asyncio.to_thread / task group pattern) inherits
    the SAME ContextVar dict — record_step must copy-on-write, never
    mutate the parent's in-flight receipt in place."""
    import contextvars

    receipt.begin("sync")
    receipt.record_step("parent", True)
    parent_before = receipt.snapshot()

    def child():
        receipt.record_step("child", False)
        return receipt.snapshot()

    ctx = contextvars.copy_context()
    child_seen = ctx.run(child)

    # the child saw the parent's receipt (ambient inheritance) and
    # appended its own step — in ITS copy only
    assert [s["name"] for s in child_seen["steps"]] == ["parent", "child"]
    # the parent's receipt is untouched by the child's record
    parent_after = receipt.snapshot()
    assert [s["name"] for s in parent_after["steps"]] == ["parent"]
    assert parent_after == parent_before
    receipt.finalize("ok")


def test_copied_context_finalize_does_not_finish_parent(homed):
    import contextvars

    receipt.begin("sync")
    contextvars.copy_context().run(receipt.finalize, "failed", 1)
    assert receipt.snapshot()["outcome"] is None


def test_recorded_values_are_not_mutable_through_the_input():
    receipt.begin("sync")
    decisions = [{"plugin": "a", "action": "kept"}]
    receipt.record_bisect(decisions)
    decisions[0]["action"] = "disabled"
    assert receipt.snapshot()["plugin_bisect"][0]["action"] == "kept"


def test_snapshot_returns_a_copy():
    """Mutating the snapshot must not touch the authoritative receipt."""
    receipt.begin("sync")
    receipt.record_step("a", True)
    snap = receipt.snapshot()
    snap["steps"].append({"name": "injected", "ok": True})
    snap["kind"] = "hijacked"
    live = receipt.snapshot()
    assert [s["name"] for s in live["steps"]] == ["a"]
    assert live["kind"] == "sync"


def test_nested_begin_with_token_restores_outer():
    """A nested begin (same context) with the token from finalize must
    restore the OUTER receipt instead of discarding it."""
    outer = receipt.begin("sync")
    receipt.record_step("outer-step", True)
    inner = receipt.begin("update")
    receipt.record_step("inner-step", True)
    receipt.finalize("ok", token=inner)
    # outer receipt survives the inner finalize
    live = receipt.snapshot()
    assert live is not None and live["kind"] == "sync"
    assert [s["name"] for s in live["steps"]] == ["outer-step"]
    path = receipt.finalize("ok", token=outer)
    assert path is not None and path.is_file()
    assert receipt.snapshot() is None


def test_finalize_without_token_pops_receipt():
    """Ambient (no token) finalize still pops the receipt — the existing
    linear begin→finalize consumers keep working."""
    receipt.begin("sync")
    assert receipt.finalize("ok") is not None
    assert receipt.snapshot() is None
    assert receipt.finalize("ok") is None  # nothing begun
