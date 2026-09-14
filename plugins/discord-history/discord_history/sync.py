"""Pure sync-window and reconciliation planning."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Any


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class SyncPlan:
    channel_id: str
    mode: str
    after: str | None
    before: str
    full_export: bool


def plan_sync(channel_id: str, mode: str, *, newest_created_at: datetime | None,
              overlap_hours: int, export_before: datetime) -> SyncPlan:
    if mode not in {"backfill", "incremental", "reconcile"}:
        raise ValueError("invalid_sync_mode")
    if overlap_hours < 0:
        raise ValueError("invalid_overlap_hours")
    after = None
    if mode == "incremental" and newest_created_at is not None:
        after = _iso(_utc(newest_created_at) - timedelta(hours=overlap_hours))
    return SyncPlan(str(channel_id), mode, after, _iso(export_before),
                    mode in {"backfill", "reconcile"})


def plan_reconciliation(expected_scope_ids: Iterable[str],
                        inventory: Mapping[str, Mapping[str, Any]],
                        exports: Mapping[str, str]) -> dict[str, Any]:
    """Derive completeness; only a complete result is safe for tombstoning."""
    expected = set(map(str, expected_scope_ids))
    inventory_complete = {str(k) for k, v in inventory.items() if v.get("state") == "complete"}
    export_complete = {str(k) for k, v in exports.items() if v in {"ok", "empty"}}
    missing_inventory = sorted(expected - inventory_complete, key=int)
    missing_exports = sorted(expected - export_complete, key=int)
    extra_inventory = sorted(inventory_complete - expected, key=int)
    extra_exports = sorted(export_complete - expected, key=int)
    complete = not (missing_inventory or missing_exports or extra_inventory or extra_exports)
    return {"complete": complete, "tombstone_safe": complete,
            "expected_scope_ids": sorted(expected, key=int),
            "missing_inventory_ids": missing_inventory,
            "missing_export_ids": missing_exports,
            "extra_inventory_ids": extra_inventory, "extra_export_ids": extra_exports,
            "termination_reason": "set_equality_complete" if complete else "scope_set_mismatch"}
