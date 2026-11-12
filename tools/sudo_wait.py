"""Per-session accounting for time blocked on interactive sudo password entry."""

import contextlib
import threading
import time


class _HumanWaitState:
    __slots__ = ("pending", "window_started", "completed_seconds")

    def __init__(self) -> None:
        self.pending = 0
        self.window_started: float | None = None
        self.completed_seconds = 0.0


_human_wait_lock = threading.Lock()
_human_wait_states: dict[str, _HumanWaitState] = {}
_HUMAN_WAIT_MAX_SESSIONS = 256
HUMAN_WAIT_WINDOW_S = 300.0
HUMAN_WAIT_MARGIN_S = 60.0


def human_wait_ceiling() -> float:
    """Cap one sudo wait's deadline contribution at a platform-safe value."""
    from agent.deadline import MAX_SAFE_TIMEOUT_S

    return float(min(HUMAN_WAIT_WINDOW_S + HUMAN_WAIT_MARGIN_S, MAX_SAFE_TIMEOUT_S))


def _clamped_window_seconds(started: float, now: float, ceiling: float) -> float:
    return min(max(0.0, now - started), ceiling)


def _human_wait_state(session_key: str) -> _HumanWaitState:
    """Return wait state; caller holds ``_human_wait_lock``."""
    state = _human_wait_states.get(session_key)
    if state is None:
        for key in list(_human_wait_states):
            if len(_human_wait_states) < _HUMAN_WAIT_MAX_SESSIONS:
                break
            if _human_wait_states[key].pending == 0:
                del _human_wait_states[key]
        state = _human_wait_states[session_key] = _HumanWaitState()
    return state


def _resolve_key(session_key: str | None) -> str:
    if session_key is not None:
        return session_key
    from tools.session_context import get_current_session_key

    return get_current_session_key()


@contextlib.contextmanager
def human_wait_window(session_key: str | None = None):
    """Exclude a genuine interactive sudo password wait from batch deadlines."""
    key = _resolve_key(session_key)
    now = time.monotonic()
    with _human_wait_lock:
        state = _human_wait_state(key)
        if state.pending == 0:
            state.window_started = now
        state.pending += 1
    try:
        yield
    finally:
        now = time.monotonic()
        ceiling = human_wait_ceiling()
        with _human_wait_lock:
            state = _human_wait_states.get(key)
            if state is not None:
                state.pending -= 1
                if state.pending == 0:
                    if state.window_started is not None:
                        state.completed_seconds += _clamped_window_seconds(
                            state.window_started, now, ceiling
                        )
                    state.window_started = None


def human_wait_seconds(session_key: str | None = None) -> float:
    """Return completed plus currently open sudo-wait seconds for a session."""
    key = _resolve_key(session_key)
    now = time.monotonic()
    ceiling = human_wait_ceiling()
    with _human_wait_lock:
        state = _human_wait_states.get(key)
        if state is None:
            return 0.0
        total = state.completed_seconds
        if state.window_started is not None:
            total += _clamped_window_seconds(state.window_started, now, ceiling)
        return total
