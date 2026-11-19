"""Generation array for overlapping gateway restarts (Python twin).

``internal/generations`` (Go) is the canonical statement of the decision
core that keeps an arbitrary number of overlapping gateway generations
straight; THIS module is the twin the runtime actually calls — deployments
(pip, docker) carry no Go toolchain. ``tests/gateway/test_generations_contract.py``
builds ``cmd/generations`` when a toolchain exists and pins this twin
against the RUNNING Go implementation over the shared table
(``internal/generations/testdata/cases.json``).

Model: every restart appends a generation. The LIVE generation with the
highest spawn sequence is authoritative (runtime lock, PID file,
gateway_state.json, cron dispatch, singleton listeners). Older generations
drain only the sessions they already own, deliver finals through the
transport hub, and exit independently in any order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "Generation",
    "GenerationArray",
    "AttachedGeneration",
    "RoutingState",
    "array_from_registry",
    "handoff_eligible",
    "register_decision",
    "route_choice",
    "takeover_allowed",
]


@dataclass(frozen=True)
class Generation:
    """One gateway process in the restart array."""

    pid: int
    start_time: int = 0
    id: str = ""
    predecessor: int = 0
    spawn_seq: int = 0
    attached: bool = False
    draining: bool = False
    live: bool = True


def _precedence(gen: Generation) -> tuple:
    # Spawn sequence first; PID breaks the tie between unrecorded
    # originals that both carry sequence 0.
    return (gen.spawn_seq, gen.pid)


class GenerationArray:
    """Array geometry questions over one snapshot of generations."""

    def __init__(self, generations: Iterable[Generation]) -> None:
        self._generations: List[Generation] = list(generations)

    def __iter__(self):
        return iter(self._generations)

    def __len__(self) -> int:
        return len(self._generations)

    def by_pid(self, pid: int) -> Optional[Generation]:
        for gen in self._generations:
            if gen.pid == pid:
                return gen
        return None

    def newest_live(self) -> Optional[Generation]:
        """The live generation with the highest precedence, or None.

        A successor whose process died is never authoritative even if the
        hub still lists it; among live generations the latest spawn wins.
        """
        best: Optional[Generation] = None
        for gen in self._generations:
            if not gen.live:
                continue
            if best is None or _precedence(best) < _precedence(gen):
                best = gen
        return best

    def newest_live_other(self, me: int) -> Optional[Generation]:
        """Newest live generation other than me: both the restart forward
        target and the supervisor handover target."""
        best = self.newest_live()
        if best is not None and best.pid == me:
            return None
        return best

    def ancestors_of(self, me: int) -> List[int]:
        """Transitive predecessor PIDs of me, nearest first.

        A predecessor with no record of its own (the original gateway
        appears only as somebody's predecessor) still appears.
        """
        chain: List[int] = []
        seen = {me}
        current = me
        while True:
            gen = self.by_pid(current)
            if gen is None or gen.predecessor <= 0 or gen.predecessor in seen:
                break
            chain.append(gen.predecessor)
            seen.add(gen.predecessor)
            current = gen.predecessor
        return chain

    def may_claim_authority(self, me: int) -> bool:
        """Only the newest live generation may take the runtime lock / PID
        file. A generation superseded while pending never claims."""
        gen = self.by_pid(me)
        if gen is None or not gen.live:
            return False
        newest = self.newest_live()
        return newest is None or newest.pid == me

    def may_start_shared_adapters(self, me: int) -> bool:
        """Shared-credential transports (no hub) may start only once no
        live ancestor keeps holding their platform locks.

        INPUT CONTRACT: the array must be complete — ``array_from_registry``
        synthesizes records for record-less predecessors with OS liveness.
        A record-less ancestor still inside the array fails closed.
        """
        gen = self.by_pid(me)
        if gen is None or not gen.live:
            return False
        for pid in self.ancestors_of(me):
            ancestor = self.by_pid(pid)
            if ancestor is None or ancestor.live:
                return False
        return True

    def describe(self) -> List[str]:
        """Diagnostics: live generations first (newest first), then dead."""
        live = sorted((g for g in self._generations if g.live),
                      key=_precedence, reverse=True)
        dead = sorted((g for g in self._generations if not g.live),
                      key=_precedence, reverse=True)
        newest = self.newest_live()
        roles: List[str] = []
        for gen in live + dead:
            if not gen.live:
                role = "gone"
            elif newest is not None and newest.pid == gen.pid:
                role = "serving"
            elif gen.draining:
                role = "draining"
            else:
                role = "starting"
            roles.append(f"{gen.pid}: {role}")
        return roles


def takeover_allowed(recorded_pid: int, recorded_live: bool,
                     ancestors_of_me: List[int]) -> bool:
    """May the claiming generation rewrite a PID file naming recorded_pid?

    Allowed when the file is absent, names a provably dead holder, or names
    one of me's ancestors. A live unrelated gateway must never be displaced
    — that is a duplicate, not a handover.
    """
    if recorded_pid <= 0 or not recorded_live:
        return True
    return recorded_pid in ancestors_of_me


def _pid_alive(pid: int) -> bool:
    """Conservative liveness for a PID with no start fingerprint: alive
    blocks, because a reused PID must not read as a free slot."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def array_from_registry(rows: Iterable[dict], *, me_pid: int,
                        me_start_time: int = 0, me_predecessor: int = 0,
                        me_draining: bool = False,
                        me_attached: bool = True) -> GenerationArray:
    """Build the complete array the twin needs from a hub registry
    snapshot plus the asking process's own record.

    ``rows`` are ``overlap_successors`` registry snapshots: live
    fingerprint-verified records in spawn order. The registry holds ONLY
    spawned successors, so this glue must (a) add or merge the asking
    generation and (b) synthesize a record for every predecessor that
    appears as somebody's predecessor without one of its own (the original
    gateway), with conservative OS liveness.

    The asking process's own generation id/attach state is self-reported;
    its liveness is trivially true (we are it).
    """
    records: Dict[int, Generation] = {}
    for seq, row in enumerate(rows, start=1):
        pid = int(row.get("pid") or 0)
        if pid <= 0:
            continue
        records[pid] = Generation(
            pid=pid,
            start_time=int(row.get("start_time") or 0),
            predecessor=int(row.get("predecessor_pid") or 0),
            spawn_seq=seq,
            attached=str(row.get("state") or "") == "attached",
            draining=False,  # derived below from successor links
            live=True,  # registry rows are live-fingerprinted or pruned
        )
    spawned_predecessors = {g.predecessor for g in records.values()
                            if g.predecessor > 0}
    for pid, gen in list(records.items()):
        if pid in spawned_predecessors:
            records[pid] = replace(gen, draining=True)
    mine = records.get(me_pid)
    if mine is not None:
        records[me_pid] = replace(mine, attached=mine.attached or me_attached,
                                  draining=me_draining, live=True)
    else:
        # The registry records only spawned successors. An asking process
        # missing from it is either the original (no known predecessor —
        # sequence 0 keeps it OLDER than every spawned successor, so an
        # original asking with a live successor never mistakes itself for
        # the newest) or a successor that never registered (hub-less
        # deployment, records lost). A successor knows its predecessor:
        # place it strictly after that predecessor, and strictly before
        # the earliest row that names IT as predecessor (its own
        # successor), so ordering stays consistent with every recorded
        # chain. No consistent window (degenerate history) keeps sequence
        # 0 — a lost-history ordering error can only delay a claim (lock
        # + PID-file takeover fail closed), never duplicate one.
        lo = 0
        if me_predecessor > 0 and me_predecessor in records:
            lo = records[me_predecessor].spawn_seq
        hi = None
        for gen in records.values():
            if gen.predecessor == me_pid:
                bound = gen.spawn_seq - 1
                hi = bound if hi is None else min(hi, bound)
        if me_predecessor > 0:
            seq = lo + 1 if (hi is None or lo + 1 <= hi) else 0
        else:
            seq = 0
        records[me_pid] = Generation(
            pid=me_pid, start_time=me_start_time,
            predecessor=me_predecessor, spawn_seq=seq,
            attached=me_attached, draining=me_draining, live=True,
        )
    # Complete the array: every record's predecessor without a record gets
    # a synthetic one (sequence 0 — older than every spawned successor).
    for gen in list(records.values()):
        pred = gen.predecessor
        if pred > 0 and pred not in records:
            records[pred] = Generation(
                pid=pred, predecessor=0, spawn_seq=0,
                attached=False, draining=True, live=_pid_alive(pred),
            )
    return GenerationArray(records.values())

@dataclass(frozen=True)
class AttachedGeneration:
    """One generation currently attached to the transport hub."""

    generation_id: str
    pid: int = 0


@dataclass(frozen=True)
class RoutingState:
    """Hub router state snapshot the routing decisions run on.

    ``attached`` is in attach order, OLDEST first: re-attaching moves a
    generation to the end, so the LAST entry is the newest attached
    generation — the one that takes new work.
    """

    attached: Tuple[AttachedGeneration, ...] = ()
    owners: Dict[str, str] = field(default_factory=dict)


def route_choice(state: RoutingState, session: str,
                 excluded: Iterable[str] = (),
                 target_pid: Optional[int] = None) -> Optional[str]:
    """Pick the generation that must serve one inbound hub event.

    ``excluded`` names generations that already refused or received the
    event (overlap handoff trail); a ``target_pid`` pins the event to the
    generation whose gateway process owns it. None means HOLD: no eligible
    generation is attached — the caller retains the event for replay,
    never drops or duplicates it.
    """
    skip = {str(item) for item in excluded if item}
    if target_pid is not None and target_pid > 0:
        for gen in state.attached:
            if (gen.pid == target_pid and gen.generation_id
                    and gen.generation_id not in skip):
                return gen.generation_id
        return None
    owner = state.owners.get(session)
    if (owner and owner not in skip
            and any(gen.generation_id == owner for gen in state.attached)):
        return owner
    for gen in reversed(state.attached):
        if gen.generation_id and gen.generation_id not in skip:
            return gen.generation_id
    return None


def handoff_eligible(state: RoutingState, visited: Iterable[str] = (),
                     target_pid: Optional[int] = None) -> bool:
    """May the hub return an event to the generation owning target_pid
    without revisiting any generation in ``visited`` (the event's trail)?"""
    if not target_pid or target_pid <= 0:
        return False
    skip = {str(item) for item in visited if item}
    return any(
        gen.pid == target_pid and gen.generation_id
        and gen.generation_id not in skip
        for gen in state.attached
    )


def register_decision(state: RoutingState, session: str, me: str) -> bool:
    """May generation ``me`` claim a locally-started turn on ``session``?

    False means the session already has a live owner that is not me — the
    existing owner keeps the session (a busy session never moves).
    """
    owner = state.owners.get(session)
    if not owner or owner == me:
        return True
    return not any(gen.generation_id == owner for gen in state.attached)
