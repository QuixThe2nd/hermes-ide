"""Steer-driven force-backgrounding of blocking delegations.

A user follow-up arriving while the top-level agent is blocked inside a SYNCHRONOUS
``delegate_agent`` used to be queued behind the run (the subagent-protection demotion,
#30170) — the user waited out every child even when their message changed the plan.
Instead, the follow-up releases the blocking wait: every still-running child is adopted
onto the async-delegation rail (:mod:`tools.async_delegation`) — the SAME machinery the
timeout path uses, so exactly one completion event re-enters the conversation per child —
and the text enters the live run as a steer at the next tool boundary.

Invariants:
- The worker is NEVER stopped by the release; adoption owns its teardown.
- ``adopt`` decides once per wait: a steer listener and the timeout tail racing each
  other join the same in-flight adoption instead of double-dispatching.
- A rail refusal keeps today's blocking semantics: the steer stays stashed for the normal
  drain and only the timeout ends the wait.
- Registration is opt-out per config (``delegation.steer_force_background``) and only the
  top-level parent in a delivery-capable session registers at all.
"""

import logging
import threading
import time
import weakref
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_WAITS: "weakref.WeakKeyDictionary[Any, List['_BlockingWait']]" = weakref.WeakKeyDictionary()


class _BlockingWait:
    """One registered blocking ``_ChildRun.await_child`` wait.

    ``wake`` is set (with ``delegation_id`` populated) when a steer adopts the worker;
    ``settled`` is the wait's own settle/heartbeat event and is only ever SET here on a
    successful adoption — a refusal must keep the wait blocked.
    """

    __slots__ = ("run", "future", "settled", "wake", "delegation_id", "adopting", "adopted", "registered_at")

    def __init__(self, run: Any, future: Any, settled: threading.Event) -> None:
        self.run = run
        self.future = future
        self.settled = settled
        self.wake = threading.Event()
        self.delegation_id: Optional[str] = None
        self.adopting = False
        self.adopted = False
        self.registered_at = time.monotonic()

    def released_by_steer(self) -> bool:
        return self.wake.is_set() and self.delegation_id is not None

    def live(self) -> bool:
        return not self.future.done() and not self.adopted


def _eligible_parent(parent_agent: Any) -> bool:
    """Only the top-level parent the user is talking to, in a session that can
    receive a late completion, and only while the config switch is on."""
    from tools import async_delegation, delegate_tool_config

    if parent_agent is None:
        return False
    try:
        if not delegate_tool_config.steer_force_background_enabled():
            return False
        if int(getattr(parent_agent, "_delegate_depth", 0) or 0) != 0:
            return False
        if not async_delegation._omitted_default_delivery_supported():
            return False
    except Exception:
        logger.debug("force-background eligibility check failed", exc_info=True)
        return False
    return True


def register_blocking_wait(run: Any, future: Any, settled: threading.Event) -> Optional[_BlockingWait]:
    """Register one blocking wait for steer release. ``None`` when ineligible — the
    caller then keeps today's plain ``settled.wait`` behavior."""
    parent_agent = getattr(run, "parent_agent", None)
    if not _eligible_parent(parent_agent):
        return None
    wait = _BlockingWait(run, future, settled)
    with _LOCK:
        waits = _WAITS.setdefault(parent_agent, [])
        waits.append(wait)
    return wait


def unregister_blocking_wait(run: Any, wait: Optional[_BlockingWait]) -> None:
    if wait is None:
        return
    parent_agent = getattr(run, "parent_agent", None)
    with _LOCK:
        waits = _WAITS.get(parent_agent)
        if waits and wait in waits:
            waits.remove(wait)
        if waits is not None and not waits and parent_agent in _WAITS:
            del _WAITS[parent_agent]


def registered_wait_count(parent_agent: Any) -> int:
    """How many still-running, unadopted waits are registered (live only — a
    settled worker leaves no phantom, matching internal/forcebg LiveCount)."""
    with _LOCK:
        return sum(1 for w in _WAITS.get(parent_agent) or () if w.live())


def has_eligible_blocking_waits(parent_agent: Any) -> bool:
    """True when at least one still-running, unadopted wait is registered for the
    agent — the gateway's cheap gate before it commits to the release path."""
    with _LOCK:
        return any(w.live() for w in _WAITS.get(parent_agent) or ())


def _live_waits_for(parent_agent: Any) -> List[_BlockingWait]:
    with _LOCK:
        return [w for w in _WAITS.get(parent_agent) or () if w.live()]


def adopt(wait: _BlockingWait, join_inflight: float = 5.0) -> Optional[str]:
    """Adopt one wait's still-running worker onto the async-delegation rail.

    Idempotent per wait: the first caller dispatches (``run._adopt_running_worker``),
    later callers join the in-flight adoption and receive the same id. Returns the
    delegation id, or ``None`` when the rail refused — the caller keeps today's
    stop-and-abandon / keep-blocking semantics. The worker is never stopped here.
    """
    with _LOCK:
        if wait.adopted:
            return wait.delegation_id
        if wait.adopting:
            joining = True
        else:
            joining = False
            wait.adopting = True
    if joining:
        # The dispatcher always sets ``wake`` when it finishes (id or refusal).
        # A dispatch somehow longer than the join window reads as a refusal —
        # conservative, and never a second dispatch.
        wait.wake.wait(join_inflight)
        with _LOCK:
            return wait.delegation_id
    try:
        delegation_id: Optional[str] = None
        try:
            delegation_id = wait.run._adopt_running_worker(wait.future)
        except Exception:
            logger.warning("force-background adoption dispatch failed", exc_info=True)
            delegation_id = None
        wait.delegation_id = delegation_id
        wait.adopted = True
        if delegation_id:
            # Release the blocking wait; adoption now owns the worker's teardown.
            wait.settled.set()
        wait.wake.set()
        return delegation_id
    finally:
        with _LOCK:
            wait.adopting = False


def steer_forcing_background(agent: Any, text: str) -> Tuple[bool, List[str]]:
    """Deliver *text* as a steer and force-background every live blocking wait.

    The steer is accepted FIRST: an agent that refuses it (turn already finalizing)
    must not lose the user's message — without an accepted steer there is nothing for
    the release to carry, so nothing is adopted. On acceptance every live wait is
    adopted; ids of the adopted delegations come back for the receipt. A rail refusal
    degrades to the plain-steer path (empty ids) — the wait keeps blocking.
    """
    if not text or not text.strip():
        return False, []
    call = getattr(agent, "steer", None)
    if not callable(call):
        return False, []
    try:
        accepted = bool(call(text))
    except Exception:
        logger.warning("force-background steer call failed", exc_info=True)
        return False, []
    if not accepted:
        return False, []
    ids: List[str] = []
    for wait in _live_waits_for(agent):
        delegation_id = adopt(wait)
        if delegation_id:
            ids.append(delegation_id)
    return True, ids


def wait_until_released(wait: _BlockingWait, settled: threading.Event, timeout: Optional[float]) -> None:
    """Block until the worker settles, the configured cap elapses, or a steer
    releases the wait (adoption sets ``settled`` too, so the poll is fail-safe)."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if wait.wake.wait(0.2) and wait.delegation_id is not None:
            return
        if settled.is_set():
            return
        if deadline is not None and time.monotonic() >= deadline:
            return


def entry_notice(delegation_id: str) -> str:
    """The parent-facing tool entry text for a steer-released wait."""
    return (
        "The user sent a new message while this subagent was still running, so it was moved to the "
        f"background as delegation {delegation_id} - it was NOT stopped and its result will re-enter "
        "this conversation as a completion message when it finishes. The user's message follows as a "
        "steer; respond to it now rather than waiting for the background result."
    )


_BUSY_RECEIPT_INJECTED_CAP = 1500


def busy_receipt(delegation_ids: Tuple[str, ...], injected_text: str) -> str:
    """The user-facing busy receipt naming the adopted delegations and showing the
    exact text injected into the agent's context (capped)."""
    ids = ", ".join(delegation_ids)
    shown = injected_text.strip()
    if len(shown) > _BUSY_RECEIPT_INJECTED_CAP:
        shown = shown[:_BUSY_RECEIPT_INJECTED_CAP].rstrip() + "..."
    plural = "s" if len(delegation_ids) != 1 else ""
    return (
        f"🌀 Your message moved the running agent{plural} to the background (delegation{plural} {ids}).\n"
        f"Injected into the agent's context:\n```\n{shown}\n```"
    )


def steered_queue_receipt(status_detail: str = "") -> str:
    """Queue-mode plain-steer receipt (the rail refused the adoption race): the
    text landed inside the run, nothing is queued to replay it."""
    return (
        f"⏩ Steered into current run{status_detail}. "
        "Your message will be injected at a safe point in this run."
    )


def summary(accepted: bool, ids: List[str]) -> str:
    """One-line verdict for logs and the Go twin's shared transcript."""
    if not accepted:
        return "steer refused; nothing adopted"
    if not ids:
        return "steer accepted; adoption refused (children keep blocking)"
    return f"steer accepted; force-backgrounded {len(ids)} delegation(s): {', '.join(ids)}"


def _reset_for_tests() -> None:
    with _LOCK:
        _WAITS.clear()
