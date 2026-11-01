"""pm sync receipts: the machine-readable surface for venv operations.

Every pm venv sync — startup, plugin install, update rebuild — writes a
receipt with the SAME schema the updater's receipts use
(hermes_cli.update_receipt), into the same
``<HERMES_HOME>/logs/update_receipts/`` dir with a ``kind`` field
separating kinds. One reader (``hermes pm status``, desktop IPC) serves
every surface: a failed venv rebuild is as reportable as a failed
update.

The in-flight receipt lives in a ContextVar, not a module global: the
sync cadence and an ensure bisect can overlap across threads, and a
shared global lets one run's begin/finalize clobber another's record.

Two hazards of ContextVar state are handled explicitly:
- Copied contexts (``contextvars.copy_context()``, asyncio.to_thread)
  share the SAME dict object — so every record_* COPY-ON-WRITES: it
  deep-copies, mutates the copy, and re-sets it in the current context.
  A child task records into its own copy; the parent's receipt is
  untouched.
- A nested ``begin`` in the same context would silently drop the outer
  receipt. ``begin`` therefore returns the ContextVar token; passing it
  to ``finalize(..., token=...)`` restores the OUTER receipt instead of
  discarding it. The ambient no-token begin→finalize stays as-is (the
  existing linear consumers — pm sync, the plugin-check cadence).
"""

from __future__ import annotations

import contextvars
import copy
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_RECEIPT_KEEP = 20

# Scoped current receipt — per-context (threads get their own via
# context isolation), same pattern as agent/context_compressor's pin.
_current: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "pm_receipt_current", default=None
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _receipt_dir() -> Path:
    """The receipts dir — a pure path computation, NO mkdir side
    effect: readers (latest()) must not create state."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / "update_receipts"


def begin(kind: str) -> contextvars.Token:
    """Start recording a sync. ``kind``: 'sync' | 'update' | 'plugin-check'.
    Returns the ContextVar token — pass it to ``finalize(token=...)`` when
    this begin nests inside an outer begin in the same context, so the
    outer receipt survives the inner finalize."""
    return _current.set(
        {
            "schema": 1,
            "kind": kind,
            "started_at": _utc_now_iso(),
            "steps": [],
            "venv_rebuild": None,
            "plugin_bisect": [],
            "feature_list": None,
            "platform": None,
            "outcome": None,
        }
    )


def _record(mutate) -> None:
    """Copy-on-write record: the ContextVar may be shared with copied
    contexts, so mutate a deep copy and re-set it in THIS context only."""
    current = _current.get()
    if current is None:
        return
    updated = dict(current)
    mutate(updated)
    _current.set(copy.deepcopy(updated))


def record_step(name: str, ok: bool, detail: str = "") -> None:
    _record(lambda r: r.update(steps=[*r["steps"],
        {"name": name, "ok": ok, "detail": detail, "at": _utc_now_iso()}]))


def record_venv_rebuild(ok: bool, reason: str = "") -> None:
    _record(lambda r: r.__setitem__("venv_rebuild", {"ok": ok, "reason": reason}))


def record_bisect(decisions: list[dict]) -> None:
    _record(lambda r: r.__setitem__("plugin_bisect", decisions))


def record_feature_list(extras: Optional[list[str]]) -> None:
    _record(lambda r: r.__setitem__("feature_list", extras))


def record_platform(platform_id: str) -> None:
    _record(lambda r: r.__setitem__("platform", platform_id))


def record_plugin_checks(results: list) -> None:
    """Plugin update-check results (the cadence's receipt section).
    Each item is a plugins_updates.CheckResult.to_json() dict."""
    _record(
        lambda r: r.__setitem__(
            "plugin_checks", [r.to_json() if hasattr(r, "to_json") else r for r in results]
        )
    )


def snapshot() -> Optional[dict[str, Any]]:
    """The in-flight receipt data — for the updater to EMBED its sync
    sections into its own receipt (one schema, one directory). A deep
    COPY: the authoritative in-flight dict is never exposed for the
    caller to mutate."""
    current = _current.get()
    return copy.deepcopy(current) if current is not None else None


def finalize(
    outcome: str, exit_code: int = 0, token: Optional[contextvars.Token] = None
) -> Optional[Path]:
    """Write the receipt (``outcome``: ok | refused | failed | bisected)
    and rotate. Returns its path; None when nothing was begun.

    With ``token`` (from the matching ``begin``): pops only this begin's
    receipt and restores the outer one. Without: pops whatever is
    current (the ambient linear-consumer form)."""
    current = snapshot()
    if current is None:
        return None
    if token is not None:
        _current.reset(token)
    else:
        _current.set(None)
    current["outcome"] = outcome
    current["exit_code"] = exit_code
    current["finished_at"] = _utc_now_iso()
    try:
        path = _write_rotated(current)
    except OSError:
        return None
    return path


def latest() -> Optional[dict[str, Any]]:
    """The newest receipt (any kind) — the reader surface for
    ``hermes pm status`` + the desktop. Pure read: never creates the
    receipts dir."""
    try:
        point = _receipt_dir() / "latest.json"
        if point.is_file():
            return json.loads(point.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return None


def _receipt_name(data: dict[str, Any]) -> str:
    """Unique name for concurrent writers: stamp + pid + full random
    uuid4 (same pid+random convention as the updater's receipts)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    kind = data.get("kind") or "sync"
    return f"pm_{stamp}-{kind}-{os.getpid()}-{uuid.uuid4().hex}.json"


def _write_rotated(data: dict[str, Any]) -> Path:
    """Atomic + durable publication: temp file + fsync + rename for both
    the stamped receipt and latest.json (utils.atomic_json_write)."""
    import utils

    d = _receipt_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / _receipt_name(data)
    utils.atomic_json_write(path, data)
    utils.atomic_json_write(d / "latest.json", data)
    _rotate(d)
    return path


def _rotate(d: Path) -> None:
    """Keep the newest _RECEIPT_KEEP PM receipts. The dir also holds the
    updater's ``update_*.json`` receipts — this rotates ONLY pm's own
    (``pm_*.json``); the updater rotates its own."""
    receipts = sorted(
        (p for p in d.glob("pm_*.json") if p.is_file()),
        key=lambda p: p.name,
    )
    for stale in receipts[:-_RECEIPT_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass
