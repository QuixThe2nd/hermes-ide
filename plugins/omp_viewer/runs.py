"""Per-home registry of OMP delegate-lane run workdirs.

The lane launcher (``tools/delegate_tool_omp.py``) records each run's
workdir here the moment its ``.hermes-lane`` live session is written, so
the bundled viewer discovers every run on the machine — wherever its
workdir lives — without scanning the filesystem or carrying a
machine-specific default scan root. ``scan_roots`` stays available as an
additive discovery option so runs that predate the registry remain
findable.

Layout: ``<HERMES_HOME>/omp-lane-runs/<sha256(workdir)>.json`` — one small
entry per workdir, atomically replaced, mode 0600. One file per workdir
(rather than one shared index) keeps concurrent lane starts race-free
without locking. The viewer server (``viewer/server.py``) is a stdlib-only
standalone process and re-reads this directory directly; the entry
contract below is shared with it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RUNS_DIR_NAME = "omp-lane-runs"
# One entry is a handful of short paths; anything past this bound is not an
# entry this module wrote, and the fail-closed reader refuses to parse it.
MAX_RUN_ENTRY_BYTES = 4096
MAX_FIELD_CHARS = 1024


def lane_runs_dir(home: Optional[os.PathLike] = None) -> Path:
    """The registry directory: ``<home>/omp-lane-runs``."""
    root = Path(home) if home is not None else get_hermes_home()
    return root / RUNS_DIR_NAME


def entry_path(workdir: str, home: Optional[os.PathLike] = None) -> Path:
    """The one registry path a workdir maps to — deterministic, no scanning."""
    digest = hashlib.sha256(str(workdir).encode("utf-8")).hexdigest()
    return lane_runs_dir(home) / (digest + ".json")


def _clamp(value: object) -> str:
    return str(value or "")[:MAX_FIELD_CHARS]


def record_lane_run(
    workdir: os.PathLike | str,
    session_dir: Optional[os.PathLike | str] = None,
    model: str = "",
    *,
    home: Optional[os.PathLike] = None,
) -> Optional[Path]:
    """Persist one run's workdir; best-effort, never raises.

    Returns the entry path on success, ``None`` when the workdir is not
    registry-shaped (absolute, traversal-free) or the write failed — a
    broken registry must never take a delegation lane down with it.
    """
    try:
        raw = str(workdir or "")
        candidate = Path(raw)
        if not raw or "\x00" in raw or not candidate.is_absolute():
            return None
        if ".." in candidate.parts:
            return None
        payload = {
            "version": SCHEMA_VERSION,
            "workdir": _clamp(raw),
            "session_dir": _clamp(session_dir),
            "model": _clamp(model),
            "recorded_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }
        path = entry_path(raw, home)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / (path.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        return path
    except Exception:
        logger.debug("omp lane run registry record failed", exc_info=True)
        return None
