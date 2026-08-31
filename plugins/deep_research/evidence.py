"""Evidence ledger for research jobs.

A ``post_tool_call`` hook records *fetched* sources into the job's private
``evidence.jsonl``. The hook is a strict no-op unless the runner set a private,
canonical ledger path in the environment — ordinary conversations (including
the parent that started the job) record nothing.

Only successful fetches count as evidence. ``web_search`` snippets are
discovery, never evidence. Page bodies and secrets are never written.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugins.deep_research.citations import normalize_url
from plugins.deep_research.jobs import FILE_MODE
from plugins.deep_research.jobs import evidence_path as job_evidence_path

EVIDENCE_ENV = "HERMES_RESEARCH_EVIDENCE"
LANE_ENV = "HERMES_RESEARCH_LANE"

# Tools whose successful completion means "this URL was actually opened".
FETCH_TOOLS = frozenset({"web_extract", "browser_navigate"})
# Discovery-only tools: never recorded, even on success.
DISCOVERY_TOOLS = frozenset({"web_search"})

# Guard the whole hook: a broken ledger must never break a tool call.
_RECORD_LOCK = threading.Lock()

_MAX_TITLE_CHARS = 200


def canonical_ledger_path(raw: Optional[str]) -> Optional[Path]:
    """Validate the runner-provided ledger path, or return ``None``.

    Accepts only ``…/research_jobs/<canonical job id>/evidence.jsonl`` so a
    hostile or accidental environment value can never redirect evidence writes
    at an arbitrary file.
    """
    if not raw:
        return None
    from plugins.deep_research.jobs import is_canonical_job_id

    path = Path(raw)
    if path.name != "evidence.jsonl":
        return None
    if not is_canonical_job_id(path.parent.name):
        return None
    if path.parent.parent.name != "research_jobs":
        return None
    if not path.is_absolute():
        return None
    return path


def evidence_ledger_path() -> Optional[Path]:
    """The active ledger path for this process, if any."""
    return canonical_ledger_path(os.environ.get(EVIDENCE_ENV))


def current_lane() -> int:
    try:
        return int(os.environ.get(LANE_ENV, "") or 0)
    except ValueError:
        return 0


def record_source(
    path: Path,
    *,
    url: str,
    tool: str,
    lane: int,
    title: str = "",
    status: str = "fetched",
) -> bool:
    """Append one source record. Safe under concurrent writers."""
    record = {
        "url": url,
        "normalized_url": normalize_url(url),
        "tool": tool,
        "lane": lane,
        "fetched_at": _now_iso(),
        "title": (title or "")[:_MAX_TITLE_CHARS],
        "status": status,
    }
    line = json.dumps(record, ensure_ascii=True) + "\n"
    with _RECORD_LOCK:
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not path.exists():
                # Create private-first: a plain append would honor the umask.
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
                os.close(fd)
            with open(path, "a", encoding="utf-8") as handle:
                _flock(handle)
                try:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                finally:
                    _funlock(handle)
            return True
        except OSError:
            return False


def _flock(handle: Any) -> None:
    """Advisory cross-process lock where the platform provides one."""
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # windows-footgun: ok — POSIX only path
    except (ImportError, OSError):
        pass


def _funlock(handle: Any) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # windows-footgun: ok — POSIX only path
    except (ImportError, OSError):
        pass


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Result interpretation
# ---------------------------------------------------------------------------


def _parse_result(result: Any) -> Optional[Dict[str, Any]]:
    """Best-effort JSON decode of a tool result payload."""
    if isinstance(result, dict):
        return result
    if not isinstance(result, str) or not result.strip():
        return None
    try:
        parsed = json.loads(result)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _result_indicates_error(payload: Optional[Dict[str, Any]]) -> bool:
    if not payload:
        return True
    if payload.get("success") is False:
        return True
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return True
    return False


def fetched_urls_from_result(tool_name: str, args: Dict[str, Any], result: Any) -> List[Dict[str, str]]:
    """URLs (plus titles) proven fetched by this call; empty when not provable.

    Conservative by design: a shape we do not understand yields nothing, so an
    unevidenced URL later fails citation validation rather than passing silently.
    """
    payload = _parse_result(result)
    if _result_indicates_error(payload):
        return []
    out: List[Dict[str, str]] = []

    if tool_name == "web_extract":
        results = payload.get("results") if payload else None
        if not isinstance(results, list):
            return []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            entry_error = entry.get("error")
            if isinstance(entry_error, str) and entry_error.strip():
                continue  # per-URL failure: that page was NOT fetched
            title = entry.get("title")
            out.append({"url": url.strip(), "title": title if isinstance(title, str) else ""})
        return out

    if tool_name == "browser_navigate":
        url = args.get("url") if isinstance(args, dict) else None
        if isinstance(url, str) and url.strip():
            out.append({"url": url.strip(), "title": ""})
        return out

    return out


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def handle_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, **_: Any) -> None:
    """``post_tool_call`` hook: record fetched sources for the active job.

    No-ops (recording nothing) unless the runner set a canonical evidence path.
    Never raises into the host.
    """
    try:
        if tool_name in DISCOVERY_TOOLS or tool_name not in FETCH_TOOLS:
            return
        path = evidence_ledger_path()
        if path is None:
            return
        if not isinstance(args, dict):
            return
        for source in fetched_urls_from_result(tool_name, args, result):
            record_source(
                path,
                url=source["url"],
                tool=tool_name,
                lane=current_lane(),
                title=source.get("title", ""),
            )
    except Exception:  # noqa: BLE001 — hook must never break a tool call
        return


__all__ = [
    "DISCOVERY_TOOLS",
    "EVIDENCE_ENV",
    "FETCH_TOOLS",
    "LANE_ENV",
    "canonical_ledger_path",
    "current_lane",
    "evidence_ledger_path",
    "fetched_urls_from_result",
    "handle_post_tool_call",
    "job_evidence_path",
    "record_source",
]
