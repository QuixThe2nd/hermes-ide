"""Foreground ``delegate_assistant`` waits — the blocking half of the lifecycle.

``background`` omitted/false must BLOCK the calling tool thread until the
mission is terminal and return the final outcome INLINE, with exactly zero
completion-queue events; ``background=true`` must return the shared
acceptance envelope immediately and deliver exactly one terminal event on the
rail. These tests pin the whole matrix around that split:

- inline wake (no rail event, record + durable row retired);
- /stop-style interrupt: the WAIT is abandoned, the mission stays active, and
  delivery flips back to the event rail (or republishes an already-parked
  outcome — never both, never neither);
- the ``missions.max_foreground_waits`` bound refusing BEFORE any mission
  exists;
- restart losing the wait but neither the mission nor its result;
- cross-process terminalization (the disk re-check fallback);
- origin vs assistant session non-interference;
- the legacy ``dispatch_assistant`` name staying dispatchable while never
  appearing in advertised definitions.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from tools import async_delegation as ad
from tools.interrupt import is_interrupted, set_interrupt
from tools.process_registry import process_registry
from tools.registry import registry

ORIGIN_KEY = "agent:main:discord:thread:abc:abc"
CHAT = "61400000000@s.whatsapp.net"
OTHER_CHAT = "61499999999@s.whatsapp.net"
# Scoped registration keeps the plugin's tools out of the global registry so
# this file cannot perturb any other test's tool resolution.
SCOPE = "test-missions-fg"


class _RegistrationCtx:
    """The slice of the plugin registration context the missions plugin uses."""

    def register_tool(self, **kw):
        registry.register(scope=SCOPE, **kw)


def _drain() -> list:
    events = []
    while not process_registry.completion_queue.empty():
        events.append(process_registry.completion_queue.get_nowait())
    return events


@pytest.fixture()
def missions_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    import plugins.missions as pm

    ad._reset_for_tests()
    _drain()
    # A failed/hung wait must not poison the foreground-slot counter for the
    # next test in this process.
    monkeypatch.setattr(pm, "_foreground_wait_count", 0)
    yield pm
    ad._reset_for_tests()
    _drain()
    with registry._lock:  # private, but the scoped slot is test-only state
        registry._scoped_tools.pop(SCOPE, None)


def _start_mission(pm, chat_id=CHAT, **extra):
    """Start a mission through the public dispatch_agent action surface."""
    return json.loads(
        pm.handle_dispatch_agent(
            {"action": "start", "chat_id": chat_id, "goal": "Agree picnic time", **extra},
            session_key=ORIGIN_KEY,
            session_id="sess-fg",
        )
    )


def _wait_in_thread(pm, chat_id=CHAT, **extra):
    """Run a FOREGROUND delegate_assistant call on its own tool thread.

    Returns ``(thread, box)`` once the mission exists and its inline claim is
    live — i.e. exactly the state a real origin turn is in while it waits.
    """
    box = {}

    def _run():
        box["out"] = pm.handle_delegate_assistant(
            {"chat_id": chat_id, "goal": "Agree picnic time", **extra},
            session_key=ORIGIN_KEY,
            session_id="sess-fg",
        )

    thread = threading.Thread(target=_run, name="fg-delegate-wait", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        mission = pm.find_active_mission_for_chat(chat_id)
        if mission and ad.inline_wait_pending(f"mission-{mission['mission_id']}"):
            return thread, box
        assert thread.is_alive(), box.get("out")
        time.sleep(0.02)
    pytest.fail(f"foreground wait never started: {box.get('out')!r}")


def _join(thread, box, timeout=15):
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "foreground delegate_assistant never returned"
    return json.loads(box["out"])


class TestForegroundInlineResult:
    def test_foreground_waits_then_returns_final_outcome_inline(self, missions_env):
        missions_env.register(_RegistrationCtx())
        thread, box = _wait_in_thread(missions_env)
        mission = missions_env.find_active_mission_for_chat(CHAT)
        assert ad.active_count() == 1  # the wait holds one live delegation unit

        # The assistant side finishes the mission from its own thread.
        closed = json.loads(
            missions_env.handle_dispatch_agent(
                {"action": "complete", "mission_id": mission["mission_id"],
                 "outcome": "Saturday 11am"}
            )
        )
        assert closed["ok"] is True

        out = _join(thread, box)
        assert out["ok"] is True
        assert out["status"] == "completed"
        assert out["delivery_mode"] == "inline"
        assert out["mission_id"] == mission["mission_id"]
        assert out["goal"] == "Agree picnic time"
        assert out["outcome"] == "Saturday 11am"
        assert out["completed_at"]
        assert out["duration_seconds"] is not None

    def test_inline_wake_emits_no_completion_event(self, missions_env):
        missions_env.register(_RegistrationCtx())
        thread, box = _wait_in_thread(missions_env)
        mission = missions_env.find_active_mission_for_chat(CHAT)
        missions_env.handle_dispatch_agent(
            {"action": "complete", "mission_id": mission["mission_id"], "outcome": "done"}
        )
        _join(thread, box)

        # Exactly one delivery channel: the inline return, so nothing on the
        # rail for this delegation.
        assert _drain() == []
        assert process_registry.completion_queue.empty()

    def test_inline_claim_retires_record_and_durable_row(self, missions_env):
        missions_env.register(_RegistrationCtx())
        thread, box = _wait_in_thread(missions_env)
        mission = missions_env.find_active_mission_for_chat(CHAT)
        missions_env.handle_dispatch_agent(
            {"action": "complete", "mission_id": mission["mission_id"], "outcome": "done"}
        )
        _join(thread, box)

        # No phantom running unit (capacity accounting / scale-to-zero)…
        assert ad.active_count() == 0
        assert ad.inline_wait_pending(f"mission-{mission['mission_id']}") is False
        # …and no replay/recovery bait left in the durable store.
        assert ad.recover_abandoned_delegations() == 0
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 0
        assert replay.empty()

    def test_cancelled_mission_returns_inline_error_outcome(self, missions_env):
        missions_env.register(_RegistrationCtx())
        thread, box = _wait_in_thread(missions_env)
        mission = missions_env.find_active_mission_for_chat(CHAT)
        missions_env.handle_dispatch_agent(
            {"action": "cancel", "mission_id": mission["mission_id"]}
        )
        out = _join(thread, box)
        assert out["ok"] is False
        assert out["status"] == "cancelled"
        assert out["delivery_mode"] == "inline"
        assert _drain() == []


class TestForegroundAbandon:
    def test_interrupt_abandons_only_the_wait(self, missions_env):
        missions_env.register(_RegistrationCtx())
        thread, box = _wait_in_thread(missions_env)
        mission = missions_env.find_active_mission_for_chat(CHAT)

        # /stop: the origin turn's interrupt bit is thread-scoped.
        set_interrupt(True, thread_id=thread.ident)
        out = _join(thread, box)
        set_interrupt(False, thread_id=thread.ident)

        assert out["ok"] is True
        assert out["status"] == "wait_abandoned"
        assert out["mission_state"] == "active"
        assert out["mission_id"] == mission["mission_id"]
        # The result names the mission and says how to steer it.
        assert "STILL ACTIVE" in out["note"]
        assert f"dispatch_agent(action='cancel', mission_id='{mission['mission_id']}')" in out["note"]
        # The mission itself is untouched.
        still = missions_env.find_active_mission_for_chat(CHAT)
        assert still is not None and still["status"] == "active"
        # Nothing was delivered while abandoning.
        assert _drain() == []

    def test_abandoned_wait_delivers_later_on_the_rail(self, missions_env):
        missions_env.register(_RegistrationCtx())
        thread, box = _wait_in_thread(missions_env)
        mission = missions_env.find_active_mission_for_chat(CHAT)
        set_interrupt(True, thread_id=thread.ident)
        _join(thread, box)
        set_interrupt(False, thread_id=thread.ident)

        # The inline claim was released and flipped back to event mode, so the
        # mission's own terminalization is what delivers — exactly one event.
        assert ad.inline_wait_pending(f"mission-{mission['mission_id']}") is False
        missions_env.handle_dispatch_agent(
            {"action": "complete", "mission_id": mission["mission_id"],
             "outcome": "Sunday 9am"}
        )
        events = _drain()
        assert len(events) == 1
        evt = events[0]
        assert evt["type"] == "async_delegation"
        assert evt["delegation_id"] == f"mission-{mission['mission_id']}"
        assert evt["status"] == "completed"
        assert "Sunday 9am" in evt["summary"]
        assert evt["tool"] == "delegate_assistant"
        assert evt["result_kind"] == "mission"

    def test_abandon_with_parked_result_publishes_it_on_the_rail(self, missions_env):
        """Interrupt racing the completion: the parked outcome is not lost.

        The waiter's claim is staged exactly as a live foreground call stages
        it, the terminal side parks the outcome into that claim (nothing on
        the rail), and THEN the wait unwinds — the window
        ``_abandon_foreground_wait``'s parked branch exists for.
        """
        missions_env.register(_RegistrationCtx())
        started = _start_mission(missions_env)
        delegation_id = f"mission-{started['mission_id']}"
        ad.register_inline_wait(
            delegation_id,
            session_key=ORIGIN_KEY,
            origin_session_id="sess-fg",
            tool="delegate_assistant",
            result_kind=ad.RESULT_KIND_MISSION,
        )

        # Terminal side parks the outcome for the waiter (publish returns
        # False, nothing on the rail)…
        missions_env.handle_dispatch_agent(
            {"action": "complete", "mission_id": started["mission_id"],
             "outcome": "Saturday 11am"}
        )
        assert _drain() == []
        assert ad.inline_wait_pending(delegation_id) is True

        # …and the wait unwinds before the waiter claims it.
        out = json.loads(missions_env._abandon_foreground_wait(started["mission_id"]))

        assert out["status"] == "wait_abandoned"
        assert out["mission_state"] == "completed"
        # The parked outcome moved to the rail: still exactly one delivery.
        events = _drain()
        assert len(events) == 1
        assert events[0]["delegation_id"] == delegation_id
        assert events[0]["status"] == "completed"
        assert "Saturday 11am" in events[0]["summary"]
        # And it stays exactly one: the durable row is claimable once.
        assert ad.claim_completion_delivery(delegation_id, "claim-a") is True
        assert ad.claim_completion_delivery(delegation_id, "claim-b") is False
        assert ad.inline_wait_pending(delegation_id) is False

    def test_origin_interrupt_does_not_touch_the_assistant_side(self, missions_env):
        """Origin and assistant sessions are separate interruption domains."""
        missions_env.register(_RegistrationCtx())
        thread, box = _wait_in_thread(missions_env)
        mission = missions_env.find_active_mission_for_chat(CHAT)
        set_interrupt(True, thread_id=thread.ident)
        _join(thread, box)

        # THIS thread stands in for the assistant profile's own turn: the
        # origin's /stop must not have signalled it.
        assert is_interrupted() is False
        closed = json.loads(
            missions_env.handle_dispatch_agent(
                {"action": "cancel", "mission_id": mission["mission_id"]}
            )
        )
        assert closed["ok"] is True
        events = _drain()
        assert len(events) == 1
        assert events[0]["status"] == "error"
        set_interrupt(False, thread_id=thread.ident)


class TestForegroundCapacity:
    def test_bound_refuses_before_creating_any_mission(self, missions_env, monkeypatch):
        missions_env.register(_RegistrationCtx())
        monkeypatch.setattr(missions_env, "_max_foreground_waits", lambda: 1)
        thread, box = _wait_in_thread(missions_env)

        refused = json.loads(
            missions_env.handle_delegate_assistant(
                {"chat_id": OTHER_CHAT, "goal": "second mission"},
                session_key=ORIGIN_KEY,
                session_id="sess-fg-2",
            )
        )
        assert refused["ok"] is False
        assert refused["error"] == "foreground_wait_capacity"
        assert "NO mission was created" in refused["message"]
        assert "background=true" in refused["message"]
        # Refused BEFORE the side effect: no mission, no message sent, nothing
        # queued.
        assert missions_env.find_active_mission_for_chat(OTHER_CHAT) is None
        assert _drain() == []

        # The held wait is unaffected and still delivers inline.
        mission = missions_env.find_active_mission_for_chat(CHAT)
        missions_env.handle_dispatch_agent(
            {"action": "complete", "mission_id": mission["mission_id"], "outcome": "done"}
        )
        out = _join(thread, box)
        assert out["status"] == "completed"

    def test_slot_is_released_when_a_wait_ends(self, missions_env, monkeypatch):
        missions_env.register(_RegistrationCtx())
        monkeypatch.setattr(missions_env, "_max_foreground_waits", lambda: 1)
        thread, box = _wait_in_thread(missions_env)
        mission = missions_env.find_active_mission_for_chat(CHAT)
        missions_env.handle_dispatch_agent(
            {"action": "complete", "mission_id": mission["mission_id"], "outcome": "done"}
        )
        assert _join(thread, box)["status"] == "completed"

        # The bound is free again: a second foreground wait starts fine.
        thread2, box2 = _wait_in_thread(missions_env, chat_id=OTHER_CHAT)
        mission2 = missions_env.find_active_mission_for_chat(OTHER_CHAT)
        missions_env.handle_dispatch_agent(
            {"action": "complete", "mission_id": mission2["mission_id"], "outcome": "done"}
        )
        assert _join(thread2, box2)["status"] == "completed"
        assert _drain() == []

    def test_bound_reads_missions_config(self, missions_env, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "missions:\n  max_foreground_waits: 2\n", encoding="utf-8"
        )
        assert missions_env._max_foreground_waits() == 2

    def test_bound_defaults_to_three(self, missions_env):
        assert missions_env._max_foreground_waits() == 3


class TestForegroundRestart:
    def test_restart_loses_the_wait_not_the_mission_or_result(self, missions_env):
        missions_env.register(_RegistrationCtx())
        started = _start_mission(missions_env)
        delegation_id = f"mission-{started['mission_id']}"
        # The claim a live foreground call holds, registered exactly the way
        # the tool does it.
        ad.register_inline_wait(
            delegation_id,
            session_key=ORIGIN_KEY,
            origin_session_id="sess-fg",
            tool="delegate_assistant",
            result_kind=ad.RESULT_KIND_MISSION,
        )

        # "Restart": the in-memory registry state dies with the process; the
        # mission store and durable store do not.
        ad._reset_for_tests()

        still = missions_env.find_active_mission_for_chat(CHAT)
        assert still is not None and still["status"] == "active"
        # No spurious "outcome unknown" turn, and nothing to replay yet.
        assert ad.recover_abandoned_delegations() == 0
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 0

        # The mission's own terminalization still delivers exactly once.
        missions_env.handle_dispatch_agent(
            {"action": "complete", "mission_id": started["mission_id"],
             "outcome": "Saturday 11am"}
        )
        events = _drain()
        assert len(events) == 1
        assert events[0]["delegation_id"] == delegation_id
        assert "Saturday 11am" in events[0]["summary"]

    def test_running_background_mission_row_is_never_recovered(self, missions_env):
        """A gateway restart must not fire on a still-active background mission."""
        missions_env.register(_RegistrationCtx())
        out = json.loads(
            missions_env.handle_dispatch_assistant(
                {"chat_id": CHAT, "goal": "Agree picnic time", "background": True},
                session_key=ORIGIN_KEY,
                session_id="sess-fg",
            )
        )
        assert out["status"] == "dispatched"
        delegation_id = out["delegation_id"]

        ad._reset_for_tests()  # "restart": owner pid is gone
        assert ad.recover_abandoned_delegations() == 0
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 0

        # …and its eventual outcome still arrives, exactly once.
        mission = missions_env.find_active_mission_for_chat(CHAT)
        missions_env.handle_dispatch_agent(
            {"action": "cancel", "mission_id": mission["mission_id"]}
        )
        events = _drain()
        assert len(events) == 1
        assert events[0]["delegation_id"] == delegation_id
        assert events[0]["status"] == "error"


class TestCrossProcessTerminalization:
    def test_other_process_close_returns_final_state_inline(self, missions_env, monkeypatch):
        """The 30 s disk re-check: closed elsewhere, result returned inline."""
        monkeypatch.setattr(missions_env, "_FOREGROUND_DISK_RECHECK_SECONDS", 0.2)
        missions_env.register(_RegistrationCtx())
        started = _start_mission(missions_env)
        delegation_id = f"mission-{started['mission_id']}"
        ad.register_inline_wait(
            delegation_id,
            session_key=ORIGIN_KEY,
            origin_session_id="sess-fg",
            tool="delegate_assistant",
            result_kind=ad.RESULT_KIND_MISSION,
        )

        # "Another process" closes the mission on disk without publishing
        # into THIS process (its rail is its own completion queue).
        mission = missions_env.find_active_mission_for_chat(CHAT)
        closed = missions_env._terminalize_mission(mission, "completed", "Saturday 11am")
        assert closed is not None

        out = json.loads(missions_env._wait_for_mission_terminal(started["mission_id"]))
        assert out["ok"] is True
        assert out["status"] == "completed"
        # The wait's own durable registration row is what the takeover
        # retires atomically: THIS return is the one delivery.
        assert out["delivery_mode"] == "inline"
        assert out["outcome"] == "Saturday 11am"
        # The stale inline claim was dropped, not left pinning a phantom unit.
        assert ad.inline_wait_pending(delegation_id) is False
        assert ad.active_count() == 0
        assert _drain() == []

    @staticmethod
    def _rail_publish_from_other_process(mission, outcome="Saturday 11am"):
        """Do what the CLOSING process's rail publish does, from outside.

        ``_notify_origin_session`` in that process runs
        ``ensure_durable_delegation`` (the terminal-side durable row) and
        ``publish_terminal_event`` — which cannot see THIS process's inline
        record, so it persists the terminal row ``pending`` and puts the
        event on ITS completion queue. The durable write is shared; the
        queue here stands in for the other process's consumer view.
        """
        from tools.process_registry import process_registry as _pr

        delegation_id = f"mission-{mission['mission_id']}"
        ad.ensure_durable_delegation(
            delegation_id=delegation_id,
            session_key=ORIGIN_KEY,
            origin_session_id="sess-fg",
            tool="delegate_assistant",
            result_kind=ad.RESULT_KIND_MISSION,
        )
        evt = {
            "type": "async_delegation",
            "delegation_id": delegation_id,
            "session_key": ORIGIN_KEY,
            "status": "completed",
            "summary": (
                f"WhatsApp assistant mission {mission['mission_id']} completed."
                f"\nOutcome: {outcome}"
            ),
            "tool": "delegate_assistant",
            "result_kind": "mission",
        }
        ad._persist_completion(evt, {"status": "completed", "summary": evt["summary"]})
        _pr.completion_queue.put(evt)
        return delegation_id

    def test_rail_row_is_taken_over_not_replayed(self, missions_env, monkeypatch):
        """Cross-process close: inline wins ownership; rail + replay cannot.

        The disk re-check sees a mission terminalized elsewhere whose rail
        delivery is still pending. The waiter must atomically TAKE OVER the
        durable delivery (returning the outcome inline) so that the closing
        process's queued event fails its consumer claim and a restart replay
        finds nothing pending — exactly one delivery, ever.
        """
        monkeypatch.setattr(missions_env, "_FOREGROUND_DISK_RECHECK_SECONDS", 0.2)
        monkeypatch.setattr(missions_env, "_FOREGROUND_POLL_SECONDS", 0.05)
        missions_env.register(_RegistrationCtx())
        started = _start_mission(missions_env)
        delegation_id = f"mission-{started['mission_id']}"
        ad.register_inline_wait(
            delegation_id,
            session_key=ORIGIN_KEY,
            origin_session_id="sess-fg",
            tool="delegate_assistant",
            result_kind=ad.RESULT_KIND_MISSION,
        )

        mission = missions_env.find_active_mission_for_chat(CHAT)
        closed = missions_env._terminalize_mission(mission, "completed", "Saturday 11am")
        assert closed is not None
        delegation_id = self._rail_publish_from_other_process(mission)

        out = json.loads(
            missions_env._wait_for_mission_terminal(started["mission_id"])
        )
        assert out["ok"] is True
        assert out["status"] == "completed"
        # The takeover won: this return IS the delivery.
        assert out["delivery_mode"] == "inline"
        assert out["outcome"] == "Saturday 11am"

        # The closing process's queued copy is still physically on the rail,
        # but no consumer can claim it — the row is already delivered, so
        # every gateway/cli/TUI consumer skips it (never both channels).
        assert ad.claim_completion_delivery(delegation_id, "rail-consumer") is False
        # And durable replay cannot deliver a second copy after a restart.
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 0
        assert replay.empty()
        assert ad.inline_wait_pending(delegation_id) is False
        assert ad.active_count() == 0

    def test_rail_claim_beats_disk_recheck_inline_returns_state_only(
        self, missions_env, monkeypatch
    ):
        """The rail already claimed the row: inline never repeats the body."""
        monkeypatch.setattr(missions_env, "_FOREGROUND_DISK_RECHECK_SECONDS", 0.2)
        monkeypatch.setattr(missions_env, "_FOREGROUND_POLL_SECONDS", 0.05)
        missions_env.register(_RegistrationCtx())
        started = _start_mission(missions_env)
        delegation_id = f"mission-{started['mission_id']}"
        ad.register_inline_wait(
            delegation_id,
            session_key=ORIGIN_KEY,
            origin_session_id="sess-fg",
            tool="delegate_assistant",
            result_kind=ad.RESULT_KIND_MISSION,
        )

        mission = missions_env.find_active_mission_for_chat(CHAT)
        missions_env._terminalize_mission(mission, "completed", "Saturday 11am")
        delegation_id = self._rail_publish_from_other_process(mission)
        # The closing process's consumer claimed the pending row FIRST —
        # it is mid-injection as a new-message turn.
        assert ad.claim_completion_delivery(delegation_id, "rail-consumer") is True

        out = json.loads(
            missions_env._wait_for_mission_terminal(started["mission_id"])
        )
        assert out["ok"] is True
        assert out["status"] == "completed"
        assert out["delivery_mode"] == "delivered_elsewhere"
        # STATE only — the outcome body belongs to the rail turn, so the
        # model must not be handed the same result twice.
        assert out["outcome"] == ""
        assert "new message" in out["message"]
        # Ownership stays with the rail: no later consumer can steal it
        # inside the claim window, and the takeover cannot re-win it.
        assert ad.claim_completion_delivery(delegation_id, "late-consumer") is False
        assert ad.claim_inline_takeover(delegation_id, "completed") is False

    def test_terminalization_racing_wait_registration(self, missions_env, monkeypatch):
        """Published to the rail BEFORE the wait registered: still exactly one.

        The closing publish cannot see an inline claim that does not exist
        yet, so the outcome rail-publishes (durable pending row + one queued
        event) and the waiter then registers on the already-terminal mission.
        The immediate first-pass disk check must take ownership away from
        the pending rail delivery: the wait returns the outcome inline, the
        queued event becomes unclaimable, and replay finds nothing.
        """
        monkeypatch.setattr(missions_env, "_FOREGROUND_DISK_RECHECK_SECONDS", 0.2)
        monkeypatch.setattr(missions_env, "_FOREGROUND_POLL_SECONDS", 0.05)
        missions_env.register(_RegistrationCtx())
        started = _start_mission(missions_env)
        delegation_id = f"mission-{started['mission_id']}"

        # Terminalize + rail-publish with NO inline claim registered yet —
        # exactly the registration race window.
        mission = missions_env.find_active_mission_for_chat(CHAT)
        closed = missions_env._terminalize_mission(mission, "completed", "Sunday 9am")
        assert closed is not None
        delegation_id = self._rail_publish_from_other_process(
            mission, outcome="Sunday 9am"
        )

        out = json.loads(
            missions_env._wait_for_mission_terminal(started["mission_id"])
        )
        assert out["ok"] is True
        assert out["status"] == "completed"
        assert out["delivery_mode"] == "inline"
        assert out["outcome"] == "Sunday 9am"
        # The rail copy is unclaimable and replay is empty: one delivery.
        assert ad.claim_completion_delivery(delegation_id, "rail-consumer") is False
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 0
        assert replay.empty()


    def test_takeover_beats_late_publisher_after_pre_persist_pause(
        self, missions_env, monkeypatch
    ):
        """The pre-persist window: mission file terminal, publisher paused.

        The closing process writes the terminal mission file BEFORE its
        durable registration / completion persist runs. A foreground waiter
        that notices the file inside that window must win ownership
        DURABLY: when the paused publisher then resumes, its
        ``ensure_durable_delegation`` cannot resurrect a pending row, its
        queued event is unclaimable by any consumer, and a restart replay
        finds nothing — the inline return was the one and only delivery.
        """
        monkeypatch.setattr(missions_env, "_FOREGROUND_DISK_RECHECK_SECONDS", 0.2)
        monkeypatch.setattr(missions_env, "_FOREGROUND_POLL_SECONDS", 0.05)
        missions_env.register(_RegistrationCtx())
        started = _start_mission(missions_env)
        delegation_id = f"mission-{started['mission_id']}"
        ad.register_inline_wait(
            delegation_id,
            session_key=ORIGIN_KEY,
            origin_session_id="sess-fg",
            tool="delegate_assistant",
            result_kind=ad.RESULT_KIND_MISSION,
        )

        # PAUSE the closing process exactly after the mission-file write:
        # terminal on disk, durable registration/persist NOT yet run.
        mission = missions_env.find_active_mission_for_chat(CHAT)
        closed = missions_env._terminalize_mission(
            mission, "completed", "Saturday 11am"
        )
        assert closed is not None

        # The foreground waiter returns inline — and its win is durable.
        out = json.loads(
            missions_env._wait_for_mission_terminal(started["mission_id"])
        )
        assert out["ok"] is True
        assert out["status"] == "completed"
        assert out["delivery_mode"] == "inline"
        assert out["outcome"] == "Saturday 11am"
        durable = ad.get_durable_delegation(delegation_id)
        assert durable is not None
        assert durable["state"] == "completed"
        assert durable["delivery_state"] == "delivered"

        # The paused late publisher now resumes (durable registration +
        # completion persist + its own queue put)…
        self._rail_publish_from_other_process(closed)

        # …and cannot deliver a second copy: no consumer claim, no replay.
        assert ad.claim_completion_delivery(delegation_id, "rail-consumer") is False
        replay = queue.Queue()
        assert ad.restore_undelivered_completions(replay) == 0
        assert replay.empty()
        assert ad.inline_wait_pending(delegation_id) is False
        assert ad.active_count() == 0


class TestLegacyAliasVisibility:
    def test_old_name_hidden_from_definitions_but_dispatchable(self, missions_env):
        scope = registry.current_scope_key()

        class _Ctx:
            def register_tool(self, **kw):
                registry.register(scope=scope, **kw)

        missions_env.register(_Ctx())
        try:
            # Advertised definitions carry only the new name.
            names = {
                d["function"]["name"]
                for d in registry.get_definitions(
                    {"delegate_assistant", "dispatch_assistant"}
                )
            }
            assert "delegate_assistant" in names
            assert "dispatch_assistant" not in names
            # The alias entry exists, is hidden, and shares the handler.
            entry = registry.get_entry("dispatch_assistant", scope=scope)
            assert entry is not None and entry.advertise is False
            assert entry.handler is registry.get_entry(
                "delegate_assistant", scope=scope
            ).handler

            # Both names dispatch, and both forward `background` unchanged.
            via_new = json.loads(
                registry.dispatch(
                    "delegate_assistant",
                    {"chat_id": CHAT, "goal": "g", "background": True},
                    scope=scope,
                    session_key=ORIGIN_KEY,
                    session_id="sess-alias-1",
                )
            )
            assert via_new["status"] == "dispatched"
            assert via_new["tool"] == "delegate_assistant"
            via_old = json.loads(
                registry.dispatch(
                    "dispatch_assistant",
                    {"chat_id": OTHER_CHAT, "goal": "g", "background": True},
                    scope=scope,
                    session_key=ORIGIN_KEY,
                    session_id="sess-alias-2",
                )
            )
            assert via_old["status"] == "dispatched"
            assert via_old["tool"] == "delegate_assistant"
            assert via_old["result_kind"] == "mission"
        finally:
            with registry._lock:
                registry._scoped_tools.pop(scope, None)
