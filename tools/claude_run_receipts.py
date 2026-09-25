"""Durable correlation receipts for ``delegate_claude_agent`` runs.

Mission Control renders one inline dispatch card per spawn-shaped
``delegate_claude_agent`` call, and a card should open that exact run in
the Claude live viewer — while the run is still going and after it
finishes. The tool already holds the full correlation at spawn time: the
parent session id + tool call id pair the gateway passes to every tool
handler, and the run log path
(``<HERMES_HOME>/claude-runs/<YYYYMMDD>-<HHMMSS>-<pid>.jsonl``) that
``run_agent_cli`` resolves right after the subprocess exists. This
module persists that pair, plus the viewer URL the tool itself computes
through ``tools.claude_viewer_url`` (the single host resolver), as one
small receipt the moment the child spawns — for synchronous and
background runs alike.

Security shape (mirrors ``tools/cursor_run_receipts.py``):

* deterministic, hash-derived file name under the SAME Hermes home the
  run logs into — a reader never scans, and never touches another
  profile's tree;
* atomic replacement (temp file in the same directory + fsync +
  ``os.replace``), mode 0600, so a concurrent reader never sees a
  half-written receipt and a re-dispatch under the same call id replaces
  the old receipt whole;
* bounded, fixed-shape JSON: format version, the exact parent session
  and tool call ids, the run stem, the viewer URL, workdir, timestamp.
  NEVER the task/prompt, credentials, tool results, or CLI output;
* the writer is best-effort: missing correlation ids or a failed write
  degrade to no receipt — the delegated coding task is never affected;
* the reader is fail-closed: size bound, exact JSON shape, exact
  embedded ids, stem/fragment agreement, an http(s)-only URL shape, and
  the run log named by the stem actually existing in that home —
  anything else yields ``None`` so a card falls back to its static form.

Pure stdlib plus ``hermes_constants``/``tools.claude_viewer_url``;
importing this module never touches the network and never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from hermes_constants import get_hermes_home
from tools.claude_viewer_url import RUN_STEM_RE, watch_url

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RUNS_DIR_NAME = "claude-runs"
RECEIPTS_DIR_NAME = "receipts"
# One receipt is a handful of short strings; anything past this bound is
# not a receipt this module wrote, and the fail-closed reader refuses to
# even parse it.
MAX_RECEIPT_BYTES = 4096
# Correlation ids are session/tool-call identifiers (short UUID-ish
# strings); a longer value is not one of ours.
MAX_ID_CHARS = 256
# Workdir is display metadata only; clamped on write and on read.
MAX_WORKDIR_CHARS = 512
# A viewer URL is host:port plus a stem fragment — well under this.
MAX_URL_CHARS = 2048

# The exact key set a receipt carries. Unknown keys fail validation: the
# shape is fixed on purpose, so nothing rides along that the writer did
# not put there.
RECEIPT_KEYS = frozenset((
    "schema_version", "session_id", "tool_call_id", "run_stem",
    "viewer_url", "workdir", "created_at",
))


def claude_runs_dir(home: Optional[os.PathLike] = None) -> Path:
    """The run-log tree receipts live under: ``<home>/claude-runs``."""
    root = Path(home) if home is not None else get_hermes_home()
    return root / RUNS_DIR_NAME


def receipts_dir(home: Optional[os.PathLike] = None) -> Path:
    """``<home>/claude-runs/receipts`` — the receipt tree proper.

    A subdirectory, not loose files: the run viewer globs
    ``claude-runs/*.jsonl`` and must stay unaware receipts exist."""
    return claude_runs_dir(home) / RECEIPTS_DIR_NAME


def binding_hash(session_id: str, tool_call_id: str) -> str:
    """Deterministic file-name hash for one exact (session, call) pair."""
    key = "%s\0%s" % (str(session_id or ""), str(tool_call_id or ""))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def receipt_path(
    session_id: str,
    tool_call_id: str,
    home: Optional[os.PathLike] = None,
) -> Path:
    """The one receipt path a (session, call) pair maps to — no scanning."""
    return receipts_dir(home) / (
        binding_hash(session_id, tool_call_id) + ".json")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Durably replace *path* with *payload*, mode 0600 (never partial)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def write_spawn_receipt(
    session_id: Optional[str],
    tool_call_id: Optional[str],
    log_path: os.PathLike | str,
    workdir: Optional[str] = None,
) -> Optional[Path]:
    """Persist the spawn correlation receipt; best-effort, never raises.

    Called the moment the Claude subprocess exists (``on_spawn``), for
    synchronous and background runs alike, so a card can link to the
    viewer before any tool result exists. Missing ids, a non-run log
    stem, or any write failure return None — the delegation itself is
    never affected.
    """
    sid = str(session_id or "").strip()
    tcid = str(tool_call_id or "").strip()
    if not sid or len(sid) > MAX_ID_CHARS \
            or not tcid or len(tcid) > MAX_ID_CHARS:
        return None
    stem = Path(log_path).stem
    if not RUN_STEM_RE.fullmatch(stem):
        return None
    payload = {
        "schema_version": SCHEMA_VERSION,
        "session_id": sid,
        "tool_call_id": tcid,
        "run_stem": stem,
        "viewer_url": watch_url(stem),
        "workdir": str(workdir or "")[:MAX_WORKDIR_CHARS],
        "created_at": _utc_now_iso(),
    }
    try:
        path = receipt_path(sid, tcid)
        _atomic_write_json(path, payload)
        return path
    except Exception:
        logger.debug("claude run receipt write failed", exc_info=True)
        return None


def _viewer_url_matches(url: object, stem: str) -> bool:
    """Fail-closed shape check for a receipt's viewer URL.

    http/https only, no userinfo, no query, an empty-or-/ path, and a
    fragment that is exactly the receipt's run stem. The host itself was
    produced by this machine's resolver at spawn time; only its shape is
    re-checked here.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        return False
    if any(ch.isspace() for ch in url):
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return False
    if parsed.path not in ("", "/"):
        return False
    if parsed.query or parsed.params:
        return False
    return parsed.fragment == stem and RUN_STEM_RE.fullmatch(stem) is not None


def load_validated_receipt(
    home: os.PathLike | str,
    session_id: Optional[str],
    tool_call_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """The validated receipt for one exact (home, session, call) triple.

    Fail-closed on every axis: the deterministic path must exist as a
    regular file inside that home's receipt tree, sized under the bound,
    owned-and-permissioned 0600; the JSON must carry exactly the fixed
    key set with the exact requested ids embedded; the viewer URL must
    be a well-formed http(s) URL whose fragment is the run stem; and the
    run log named by that stem must exist as a file in the same home's
    claude-runs tree (the run identity — a receipt naming a log that
    never existed is not proof of anything). Any failure returns None;
    this function never raises and never reads outside *home*.
    """
    sid = str(session_id or "").strip()
    tcid = str(tool_call_id or "").strip()
    if not sid or len(sid) > MAX_ID_CHARS \
            or not tcid or len(tcid) > MAX_ID_CHARS:
        return None
    try:
        root = receipts_dir(home).resolve()
        path = receipt_path(sid, tcid, home)
        resolved = path.resolve()
        if resolved.parent != root:
            return None
        if path.is_symlink() or not path.is_file():
            return None
        stat = os.lstat(path)
        if stat.st_mode & 0o077 != 0:
            return None
        if stat.st_size > MAX_RECEIPT_BYTES:
            return None
        if hasattr(os, "getuid") and stat.st_uid != os.getuid():
            # windows-footgun: ok — hasattr-gated POSIX uid check
            return None
        raw = path.read_text(encoding="utf-8")
        if len(raw) > MAX_RECEIPT_BYTES:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data.keys()) != RECEIPT_KEYS:
            return None
        if data.get("schema_version") != SCHEMA_VERSION:
            return None
        if data.get("session_id") != sid or data.get("tool_call_id") != tcid:
            return None
        stem = data.get("run_stem")
        if not isinstance(stem, str) or RUN_STEM_RE.fullmatch(stem) is None:
            return None
        if not _viewer_url_matches(data.get("viewer_url"), stem):
            return None
        workdir = data.get("workdir")
        if not isinstance(workdir, str) or len(workdir) > MAX_WORKDIR_CHARS:
            return None
        # The run identity: the JSONL log this stem names in THIS home.
        if not (claude_runs_dir(home) / (stem + ".jsonl")).is_file():
            return None
        return data
    except Exception:
        logger.debug("claude run receipt load failed", exc_info=True)
        return None


def resolve_watch_url(
    home: os.PathLike | str,
    session_id: Optional[str],
    tool_call_id: Optional[str],
) -> Optional[str]:
    """The validated viewer URL for one dispatch, or None (fail closed)."""
    receipt = load_validated_receipt(home, session_id, tool_call_id)
    if receipt is None:
        return None
    url = receipt.get("viewer_url")
    return url if isinstance(url, str) and url else None
