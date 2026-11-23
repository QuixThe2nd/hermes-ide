"""Shared-contract parity for the progress-pump hold/flush marker protocol.

``internal/progresshold`` (Go twin) is the canonical statement of the marker
contract the pump in ``gateway/run_turn_runner.py`` executes: the marker
vocabulary (hold ``__gate__``/``__hold__``, flush ``__flush__``, reset
``__reset__``), the (tag, event) arity rule, and the hold-wait servicing
rule — while the pump waits on a narration hold it must service every flush
marker parked in the queue, in FIFO order, without moving any row.

The shared scenario table (``internal/progresshold/testdata/hold_scenarios.json``)
is asserted from BOTH sides: the Go module's own tests and this test read the
same bytes, so the twins cannot drift. Production Python never runs the Go
binary (deployments carry no Go toolchain), the same arrangement as every
other internal/* twin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.run import _CONTEXT_FLUSH_QUEUE_MARKER
from gateway.run_turn_runner import TurnRunner

_TABLE = (
    Path(__file__).resolve().parents[2]
    / "internal"
    / "progresshold"
    / "testdata"
    / "hold_scenarios.json"
)


def _load_table():
    return json.loads(_TABLE.read_text(encoding="utf-8"))


def _scenarios():
    return {scenario["name"]: scenario for scenario in _load_table()["scenarios"]}


def _build_item(spec, events):
    """Reduce a scenario spec to the runtime queue shape. Mirrors buildItem
    in internal/progresshold/progresshold_test.go: both sides feed the twins
    identical inputs from the same table."""
    kind = spec["kind"]
    if kind == "row":
        return spec["id"]  # plain string row
    if kind == "hold":
        return (spec.get("tag") or "__gate__", events.setdefault(spec["id"], object()))
    if kind == "flush":
        return ("__flush__", events.setdefault(spec["id"], object()))
    if kind == "reset":
        return ("__reset__",)
    if kind == "tuple":
        return tuple([spec["tag"]] + [object()] * (spec["arity"] - 1))
    raise AssertionError(f"unknown scenario item kind: {kind!r}")


def test_marker_vocabulary_matches_the_shared_table():
    """The tags the Python producers/predicates speak equal the table's
    contract section (the Go test asserts the same bytes against its constants)."""
    contract = _load_table()["contract"]
    assert _CONTEXT_FLUSH_QUEUE_MARKER == contract["flush_tag"]
    event = object()
    assert TurnRunner._is_hold_marker((contract["gate_tag"], event))
    assert TurnRunner._is_hold_marker((contract["hold_tag"], event))
    assert TurnRunner._is_flush_marker((contract["flush_tag"], event))
    assert TurnRunner._is_reset_marker((contract["reset_tag"],))
    # The arity rule: a marker tag at the wrong tuple shape is content, not
    # a signal, on the Python side exactly as on the Go side.
    assert contract["marker_arity"] == 2
    assert not TurnRunner._is_flush_marker((contract["flush_tag"],))
    assert not TurnRunner._is_hold_marker((contract["gate_tag"], event, "extra"))


@pytest.mark.parametrize("name", list(_scenarios()))
def test_hold_service_partition_matches_the_shared_scenario(name):
    """The pump's hold-wait partition services exactly the table's flush
    markers, keeps exactly the table's remaining queue (FIFO), and agrees
    with the table on whether parking would have starved a waiter."""
    scenario = _scenarios()[name]
    events = {}
    items = [_build_item(spec, events) for spec in scenario["queue"]]
    item_ids = {id(item): spec["id"] for item, spec in zip(items, scenario["queue"])}

    markers, kept = TurnRunner._partition_parked_flush_markers(items)

    assert [item_ids[id(m)] for m in markers] == scenario["service"]
    assert [item_ids[id(k)] for k in kept] == scenario["kept"]
    # ParkableForHold on the Go side == "no flush marker parked" here.
    assert (not markers) == scenario["parkable"]
    # The load-bearing invariant: only markers move, never rows — the kept
    # queue is the input with flush markers removed, in order.
    assert [item_ids[id(k)] for k in kept] == [
        spec["id"] for spec in scenario["queue"] if spec["kind"] != "flush"
    ]
