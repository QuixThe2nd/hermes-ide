"""Stage lifecycle for papercuts: queued → triage → fixing → verifying → closed.

The driver is ``hermes papercuts tick`` — one bounded pass that fills free
triage slots, launches at most one work order per due stage, and exits. All
LLM work happens in detached ``hermes -z`` runs whose prompts end by calling
the ``advance`` tool action, so no stage work happens in-process.

Durability rule: the stage event is appended to the journal BEFORE any Discord
post or subprocess launch. A killed process loses at most one step and the next
tick re-derives it from the journal.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STAGES = ("queued", "triage", "fixing", "verifying", "closed", "needs_human")
ACTIVE_STAGES = ("triage", "fixing", "verifying")
WORK_STAGES = ("triage", "fixing", "verifying")

_DEFAULT_THREADS_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "max_active": 3,
    "stage_timeout_minutes": 120,
    "max_attempts": 3,
}

_TEMPLATE_PATH = Path(__file__).resolve().parent / "stage_prompts.md"
_TEMPLATE_RE = re.compile(r"<!-- template: (\w+(?:-+\w+)*) -->")


def threads_settings() -> Dict[str, Any]:
    """Threads config: ``papercuts.threads`` in config.yaml wins over the
    plugin-entry ``settings.threads`` block; defaults ship dark (enabled=false)."""
    settings = dict(_DEFAULT_THREADS_SETTINGS)
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return settings
    entry = (((cfg.get("plugins") or {}).get("entries") or {}).get("papercuts") or {})
    for source in ((entry.get("settings") or {}).get("threads"), (cfg.get("papercuts") or {}).get("threads")):
        if isinstance(source, dict):
            for key in _DEFAULT_THREADS_SETTINGS:
                if key in source:
                    settings[key] = source[key]
    for key in ("max_active", "stage_timeout_minutes", "max_attempts"):
        try:
            settings[key] = max(1, int(settings[key]))
        except (TypeError, ValueError):
            settings[key] = _DEFAULT_THREADS_SETTINGS[key]
    settings["enabled"] = bool(settings["enabled"])
    return settings


def threads_enabled() -> bool:
    return bool(threads_settings()["enabled"])


# -- journal helpers (late import: the package __init__ owns the store) ------


def _append_stage_event(item_id: str, stage: str, *, locked: bool = False, **fields: Any) -> None:
    """Append a stage event. ``locked=True`` when the caller already holds
    ``_store_lock`` — flock is not re-entrant across file descriptions, so a
    nested acquisition would deadlock."""
    from plugins.papercuts import _append_event_unlocked, _now, _store_lock

    epoch, iso = _now()
    event: Dict[str, Any] = {"kind": "stage", "id": item_id, "stage": stage, "ts": iso, "ts_epoch": epoch}
    for key, value in fields.items():
        if value is not None:
            event[key] = value
    if locked:
        _append_event_unlocked(event)
    else:
        with _store_lock():
            _append_event_unlocked(event)


# -- work orders --------------------------------------------------------------


def work_dir() -> Path:
    from plugins.papercuts import _store_dir

    return _store_dir() / "work"


def _pidfile(item_id: str, stage: str) -> Path:
    return work_dir() / f"{item_id}-{stage}.pid"


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # permission = alive but not ours
        return True
    return True


def _run_alive(item: Dict[str, Any]) -> bool:
    pid_path = _pidfile(str(item.get("id") or ""), str(item.get("stage") or ""))
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _pid_exists(pid)


def _launch_run(item: Dict[str, Any], stage: str, prompt_text: str) -> Optional[int]:
    """Detached ``hermes -z`` run: new session on POSIX so a gateway restart
    cannot kill it. Writes the pidfile only after a successful spawn."""
    item_id = str(item.get("id") or "")
    work = work_dir()
    work.mkdir(parents=True, exist_ok=True)
    prompt_path = work / f"{item_id}-{stage}.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    log_path = work / f"{item_id}-{stage}.log"
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "cwd": str(work),
    }
    with log_path.open("a", encoding="utf-8") as log_fh:
        kwargs["stdout"] = log_fh
        kwargs["stderr"] = subprocess.STDOUT
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:  # pragma: no cover - Windows
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(["hermes", "-z", prompt_text], **kwargs)
        except OSError:
            return None
    _pidfile(item_id, stage).write_text(f"{proc.pid}\n", encoding="utf-8")
    return proc.pid


def _render_template(name: str) -> str:
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    markers = list(_TEMPLATE_RE.finditer(text))
    for marker, nxt in zip(markers, markers[1:] + [None]):
        if marker.group(1) == name:
            end = nxt.start() if nxt else len(text)
            return text[marker.end():end].strip("\n")
    raise ValueError(f"stage template '{name}' not found")


def render_stage_prompt(item: Dict[str, Any], stage: Optional[str] = None) -> str:
    from plugins.papercuts.threads import steer_notes_block

    if stage is None:
        stage = str(item.get("stage") or "triage")
    if stage not in WORK_STAGES:  # queued/closed/needs_human have no template
        stage = "triage"

    values = {
        "id": item.get("id", ""),
        "summary": item.get("summary", ""),
        "observed": item.get("observed", ""),
        "workaround": item.get("workaround", "") or "(none)",
        "suggested_fix": item.get("suggested_fix", ""),
        "component": item.get("component", ""),
        "severity": item.get("severity", "minor"),
        "occurrences": int(item.get("occurrences") or 1),
        "category": item.get("category", ""),
        "session_id": item.get("session_id", ""),
        "pr_url": item.get("pr_url", "") or "(no PR yet)",
        "steer_notes": steer_notes_block(item.get("steer_notes")),
    }
    rendered = _render_template("shared-header") + "\n\n" + _render_template(stage)
    return rendered.format(**values)


def _launch_stage(item: Dict[str, Any], stage: str) -> Tuple[Optional[int], Optional[str]]:
    prompt = render_stage_prompt(item, stage)
    pid = _launch_run(item, stage, prompt)
    return pid, None if pid is not None else "failed to launch detached run (is `hermes` on PATH?)"


# -- tick ---------------------------------------------------------------------


def _priority_key(item: Dict[str, Any]) -> Tuple[int, int, float]:
    return (
        -int(item.get("occurrences") or 1),
        0 if str(item.get("severity")) == "major" else 1,
        float(item.get("ts_epoch") or 0),
    )


def _stage_age(item: Dict[str, Any], now: float) -> float:
    """Seconds since the last stage event; a non-numeric stage_since (iso
    string from a hand-written event) reads as stale so the tick re-checks."""
    try:
        return max(0.0, now - float(item.get("stage_since") or 0))
    except (TypeError, ValueError):
        return float("inf")


def tick(dry_run: bool = False, now: Optional[float] = None) -> int:
    settings = threads_settings()
    if not settings["enabled"]:
        print(
            "papercuts threads are disabled (papercuts.threads.enabled=false); "
            "tick is a no-op. log/resolve keep working unchanged."
        )
        return 0

    from plugins.papercuts import _events_path, _fold, _read_events_unlocked, _store_lock

    if dry_run and not _events_path().exists():
        # Zero side effects means not even bringing the store into existence.
        print("dry run: nothing to do this tick.")
        return 0
    if now is None:
        now = time.time()

    max_active = settings["max_active"]
    timeout = settings["stage_timeout_minutes"] * 60
    max_attempts = settings["max_attempts"]

    plans: List[Tuple[str, Any, Tuple, Dict[str, Any]]] = []  # (description, fn, args, kwargs)
    with _store_lock():
        items = list(_fold(_read_events_unlocked()).values())

        active = [
            item
            for item in items
            if item.get("stage") in ACTIVE_STAGES and item.get("status") == "open"
        ]
        free = max_active - len(active)
        queue = sorted(
            (
                item
                for item in items
                if item.get("stage") == "queued"
                and item.get("status") == "open"
                # A queued id that already carries a thread (chase-class reuse)
                # must not open a second one on a later tick.
                and not item.get("thread_id")
            ),
            key=_priority_key,
        )
        for item in queue[: max(0, free)]:
            plans.append(
                (
                    f"open inbox thread + start triage for {item['id']} ({item.get('summary', '')})",
                    _enter_triage,
                    (dict(item),),
                    {},
                )
            )

        for item in active:
            item_id = str(item["id"])
            stage = str(item.get("stage") or "")
            if _run_alive(item):
                continue
                continue
            attempts = int(item.get("attempts") or 0)
            if attempts >= max_attempts:
                reason = f"gave up after {attempts} attempt(s) at stage {stage} without progress"
                plans.append(
                    (
                        f"move {item_id} to needs_human ({reason})",
                        _to_needs_human,
                        (dict(item),),
                        {"reason": reason},
                    )
                )
            elif attempts == 0 or _stage_age(item, now) >= timeout:
                plans.append(
                    (
                        f"emit {stage} work order for {item_id} (attempt {attempts + 1})",
                        _emit_work_order,
                        (dict(item),),
                        {},
                    )
                )

    if dry_run:
        if not plans:
            print("dry run: nothing to do this tick.")
        for description, _fn, _args, _kwargs in plans:
            print(f"dry run: would {description}")
        return 0

    if not plans:
        print("tick: nothing to do.")
        return 0
    for description, fn, args, kwargs in plans:
        warning = fn(*args, **kwargs)
        line = f"tick: {description}"
        if warning:
            line += f" — warning: {warning}"
            print(line)
        else:
            print(line)
    return 0


def _is_chase_summary(summary: Any) -> bool:
    """Chase summaries form one conversation class: the excerpt after the
    colon is per-message detail, not a different papercut."""
    from plugins.papercuts import _CHASE_RE

    return bool(_CHASE_RE.match(str(summary or "").strip()))


def _chase_thread_for(items: Dict[str, Dict[str, Any]], exclude_id: str) -> str:
    """Best existing thread for the chase class: prefer an open active-stage
    thread, otherwise the most recently touched one. Empty when none exists."""
    best_rank: Optional[Tuple[int, float]] = None
    best_thread = ""
    for other in items.values():
        other_id = str(other.get("id") or "")
        if other_id == exclude_id or not other.get("thread_id"):
            continue
        if not _is_chase_summary(other.get("summary")):
            continue
        active = other.get("stage") in ACTIVE_STAGES and other.get("status") == "open"
        try:
            recency = float(other.get("stage_since") or other.get("ts_epoch") or 0)
        except (TypeError, ValueError):
            recency = 0.0
        rank = (0 if active else 1, -recency)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_thread = str(other["thread_id"])
    return best_thread


def _enter_triage(item: Dict[str, Any]) -> Optional[str]:
    """queued → triage: persist, open the thread (once per id), launch work."""
    from plugins.papercuts import _fold, _read_events_unlocked, _store_lock
    from plugins.papercuts import threads as pc_threads

    item_id = str(item["id"])
    if _is_chase_summary(item.get("summary")):
        # One chase class = one inbox thread. A duplicate chase id merges
        # into the class's existing thread (no open, no post, no run) and
        # closes out of the queue so no later tick retries the open.
        with _store_lock():
            reused = _chase_thread_for(_fold(_read_events_unlocked()), item_id)
        if reused:
            _append_stage_event(
                item_id, "closed", thread_id=reused, note="merged into the chase-class thread"
            )
            return None
    _append_stage_event(item_id, "triage", attempts=0)
    thread_id = str(item.get("thread_id") or "")
    if not thread_id:
        thread_id = pc_threads.open_thread(item) or ""
        if thread_id:
            # The thread exists only after the post; record it durably so a
            # crash here cannot orphan it or open a second one next tick.
            _append_stage_event(item_id, "triage", attempts=0, thread_id=thread_id)
        else:
            return "could not open inbox thread (hermes_starts unavailable or Discord failed); will retry next tick"
    refreshed = dict(item)
    refreshed["stage"] = "triage"
    refreshed["thread_id"] = thread_id
    refreshed["attempts"] = 1
    _append_stage_event(item_id, "triage", attempts=1, thread_id=thread_id or None)
    _pid, warning = _launch_stage(refreshed, "triage")
    return warning


def _emit_work_order(item: Dict[str, Any]) -> Optional[str]:
    """One work order for the current stage: persist attempts, then launch."""
    item_id = str(item["id"])
    stage = str(item.get("stage") or "")
    attempts = int(item.get("attempts") or 0) + 1
    _append_stage_event(item_id, stage, attempts=attempts)
    refreshed = dict(item)
    refreshed["attempts"] = attempts
    _pid, warning = _launch_stage(refreshed, stage)
    return warning


def _to_needs_human(item: Dict[str, Any], reason: str) -> Optional[str]:
    from plugins.papercuts import threads as pc_threads

    item_id = str(item["id"])
    _append_stage_event(item_id, "needs_human", note=reason, resumes=str(item.get("stage") or ""))
    thread_id = str(item.get("thread_id") or "")
    warning: Optional[str] = None
    if thread_id:
        if not pc_threads.post(thread_id, f"🙋 needs a human: {reason}"):
            warning = "could not post needs_human note to the thread"
        if not pc_threads.flag_needs_human(thread_id, item):
            warning = warning or "could not rename the thread with the 🙋 prefix"
    return warning


# -- agent-facing actions -----------------------------------------------------


def handle_advance(args: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
    from plugins.papercuts import _clean_text, _error, _fold, _json, _read_events_unlocked, _resolve_id, _store_lock
    from plugins.papercuts import threads as pc_threads

    to_stage = str(args.get("to_stage") or "").strip().lower()
    if to_stage not in STAGES:
        return _error("invalid_input", f"to_stage must be one of {list(STAGES)}")
    try:
        note = _clean_text("note", args.get("note"))
        pr_url = str(args.get("pr_url") or "").strip()
    except ValueError as exc:
        return _error("invalid_input", str(exc))
    if to_stage == "verifying" and not pr_url:
        return _error("invalid_input", "advance to verifying requires pr_url")

    with _store_lock():
        items = _fold(_read_events_unlocked())
        item_id, err = _resolve_id(items, str(args.get("id") or ""))
        if err:
            return _error("not_found", err)
        item = items[item_id]
        if item.get("stage") == to_stage:
            # Idempotent, journal-clean no-op — including a second advance to
            # closed: the papercut is already there, nothing to record.
            return _json({"success": True, "action": "advance", "changed": False, "item": item})
        resumes = item.get("stage") if to_stage == "needs_human" else None
        _append_stage_event(item_id, to_stage, note=note, pr_url=pr_url or None, resumes=resumes, locked=True)
        # Re-fold from the journal without re-taking the lock (caller holds it).
        from plugins.papercuts import _read_events_unlocked as _read

        item = _fold(_read())[item_id]

    warning = None
    thread_id = str(item.get("thread_id") or "")
    if thread_id and threads_enabled():
        pr_detail = f"\nPR: {pr_url}" if pr_url else ""
        if to_stage == "closed":
            # Archive owns the one closing post as well as the state change;
            # posting an arrow update here as well duplicated every close.
            posted = pc_threads.archive(thread_id, f"✅ closed: {note}{pr_detail}")
        else:
            posted = pc_threads.post(thread_id, f"→ {to_stage}: {note}{pr_detail}")
            if to_stage == "needs_human":
                if not pc_threads.flag_needs_human(thread_id, item):
                    posted = False
        if not posted:
            warning = "stage recorded, but the Discord thread post failed"
    payload = {"success": True, "action": "advance", "changed": True, "item": item}
    if warning:
        payload["warning"] = warning
    return _json(payload)


def handle_steer(args: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
    from plugins.papercuts import _clean_text, _error, _fold, _json, _metadata, _now, _read_events_unlocked, _resolve_id, _store_lock, _append_event_unlocked
    from plugins.papercuts import threads as pc_threads

    try:
        message = _clean_text("note", args.get("message"), required=True)
    except ValueError as exc:
        return _error("invalid_input", str(exc))

    with _store_lock():
        items = _fold(_read_events_unlocked())
        item_id, err = _resolve_id(items, str(args.get("id") or ""))
        if err:
            return _error("not_found", err)
        item = items[item_id]
        epoch, iso = _now()
        # "back on human steer": a parked needs_human papercut re-enters the
        # stage it parked from (recorded on the needs_human event) with the
        # fresh note attached to its stage prompt. The stage event comes FIRST —
        # the fold resets steer_notes on a stage change, so the steer event must
        # land after it to survive into the next work order.
        if item.get("stage") == "needs_human":
            resume_stage = str(item.get("resumes") or "triage")
            if resume_stage not in WORK_STAGES:
                resume_stage = "triage"
            _append_stage_event(item_id, resume_stage, attempts=0, note=f"steered: {message}", locked=True)
        _append_event_unlocked(
            {"kind": "steer", "id": item_id, "message": message, "ts": iso, "ts_epoch": epoch, **_metadata(kwargs)}
        )
        # Re-fold from the journal without re-taking the lock (caller holds it).
        from plugins.papercuts import _read_events_unlocked as _read

        item = _fold(_read())[item_id]

    warning = None
    thread_id = str(item.get("thread_id") or "")
    if thread_id and threads_enabled():
        ok = pc_threads.post(thread_id, f"🧭 human steer: {message}")
        if item.get("stage") == "triage" and not pc_threads.steer_ready(thread_id, item):
            ok = False
        if not ok:
            warning = "steer recorded, but the Discord thread post failed"
    payload = {"success": True, "action": "steer", "item": item}
    if warning:
        payload["warning"] = warning
    return _json(payload)
