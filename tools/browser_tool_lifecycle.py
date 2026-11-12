"""Camofox session lifecycle: inactivity cleanup and per-profile teardown."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from tools.browser_tool_origin import origin as _bt


@contextlib.contextmanager
def _session_owner_scope(task_id: str):
    """Run teardown in the session owner's Hermes home and secret scope."""
    owner_home = _bt._session_owner_homes.get(task_id)
    if owner_home is None:
        yield
        return

    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources

    home_token = set_hermes_home_override(owner_home)
    try:
        hydrate_profile_secret_sources(Path(owner_home))
        secret_token = set_secret_scope(build_profile_secret_scope(Path(owner_home)))
        try:
            yield
        finally:
            reset_secret_scope(secret_token)
    finally:
        reset_hermes_home_override(home_token)


def _update_session_activity(task_id: str) -> None:
    with _bt._cleanup_lock:
        _bt._session_last_activity[task_id] = time.time()
        _bt._session_owner_homes.setdefault(task_id, str(get_hermes_home()))


def _forget_session_tracking(task_id: str) -> None:
    with _bt._cleanup_lock:
        _bt._active_sessions.pop(task_id, None)
        _bt._session_last_activity.pop(task_id, None)
        _bt._session_owner_homes.pop(task_id, None)
        _bt._cleanup_failures.pop(task_id, None)


def _cleanup_single_browser_session(task_id: str) -> None:
    from tools.browser_camofox import camofox_close, camofox_soft_cleanup

    try:
        if not camofox_soft_cleanup(task_id):
            camofox_close(task_id)
    except Exception as exc:
        _bt.logger.warning("Camofox cleanup failed for %s: %s", task_id, exc)
    finally:
        _forget_session_tracking(task_id)


def cleanup_browser(task_id: Optional[str] = None) -> None:
    """Close an ephemeral Camofox session; managed profiles are only untracked."""
    _cleanup_single_browser_session(task_id or "default")


def _cleanup_inactive_browser_sessions() -> None:
    now = time.time()
    with _bt._cleanup_lock:
        stale = [
            task_id for task_id, last_activity in list(_bt._session_last_activity.items())
            if now - last_activity > _bt.BROWSER_SESSION_INACTIVITY_TIMEOUT
        ]
    for task_id in stale:
        try:
            with _session_owner_scope(task_id):
                _cleanup_single_browser_session(task_id)
        except Exception as exc:
            with _bt._cleanup_lock:
                failures = _bt._cleanup_failures[task_id] = _bt._cleanup_failures.get(task_id, 0) + 1
                if failures >= _bt.MAX_INACTIVITY_CLEANUP_FAILURES:
                    _bt._active_sessions.pop(task_id, None)
                    _bt._session_last_activity.pop(task_id, None)
                    _bt._session_owner_homes.pop(task_id, None)
                    _bt._cleanup_failures.pop(task_id, None)
            _bt.logger.warning("Inactive Camofox cleanup failed for %s: %s", task_id, exc)


def _start_browser_cleanup_thread() -> None:
    with _bt._cleanup_lock:
        if _bt._cleanup_thread is None or not _bt._cleanup_thread.is_alive():
            _bt._cleanup_running = True
            _bt._cleanup_thread = threading.Thread(
                target=_browser_cleanup_thread_worker, daemon=True, name="camofox-cleanup"
            )
            _bt._cleanup_thread.start()


def _stop_browser_cleanup_thread() -> None:
    try:
        _bt._cleanup_running = False
        thread = _bt._cleanup_thread
    except Exception:
        return
    if thread is not None:
        try:
            thread.join(timeout=5)
        except (SystemExit, KeyboardInterrupt):
            pass


def _browser_cleanup_thread_worker() -> None:
    while _bt._cleanup_running:
        try:
            _cleanup_inactive_browser_sessions()
        except Exception as exc:
            _bt.logger.warning("Camofox cleanup thread error: %s", exc)
        for _ in range(30):
            if not _bt._cleanup_running:
                break
            time.sleep(1)


def _emergency_cleanup_all_sessions() -> None:
    try:
        if _bt._cleanup_done:
            return
        _bt._cleanup_done = True
    except Exception:
        return
    try:
        cleanup_all_browsers()
    except Exception as exc:
        _bt.logger.error("Emergency Camofox cleanup error: %s", exc)


def cleanup_all_browsers() -> None:
    """Tear down facade-tracked and direct Camofox sessions, then reset config caches."""
    from tools import browser_camofox as camofox

    with _bt._cleanup_lock:
        task_ids = set(_bt._active_sessions) | set(_bt._session_last_activity)
    with camofox._sessions_lock:
        task_ids.update(camofox._sessions)
    for task_id in sorted(task_ids):
        cleanup_browser(task_id)
    with _bt._cleanup_lock:
        _bt._active_sessions.clear()
        _bt._session_last_activity.clear()
        _bt._session_owner_homes.clear()
        _bt._cleanup_failures.clear()

    _bt._snapshot_threshold_resolved = False
    _bt._cached_snapshot_threshold = None
    camofox._cmd_timeout_resolved = False
    camofox._cached_cmd_timeout = None


def _unlink_older_than(directory: Path, pattern: str, max_age_hours: float, label: str) -> None:
    try:
        cutoff = time.time() - max_age_hours * 3600
        for path in directory.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                _bt.logger.debug("Failed to clean old %s %s: %s", label, path, exc)
    except OSError as exc:
        _bt.logger.debug("%s cleanup error: %s", label.capitalize(), exc)


def _cleanup_old_screenshots(screenshots_dir, max_age_hours: float = 24) -> None:
    key = os.path.realpath(os.fspath(screenshots_dir))
    now = time.time()
    if now - _bt._last_screenshot_cleanup_by_dir.get(key, 0.0) < 3600:
        return
    _bt._last_screenshot_cleanup_by_dir[key] = now
    _unlink_older_than(Path(screenshots_dir), "browser_screenshot_*.png", max_age_hours, "screenshot")
