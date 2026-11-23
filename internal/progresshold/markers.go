// Package progresshold is the Go twin of the gateway progress-pump marker
// contract in gateway/run_turn_runner.py: the progress-queue marker protocol
// (narration hold, context-card flush, bubble reset) and the anti-deadlock
// rule that governs what the pump may do while it waits on a hold.
//
// This is the canonical statement of the contract; the Python module is what
// the runtime executes (deployments — pip, docker — carry no Go toolchain,
// the same arrangement as every other internal/* twin). The shared scenario
// table (testdata/hold_scenarios.json) is asserted from BOTH sides: the Go
// tests here and the Python contract test
// (tests/gateway/test_progress_hold_markers_contract.py) read the same
// bytes, so the twins cannot drift.
//
// Contract (shared with the Python twin):
//
//   - The progress queue is strict FIFO. Rows are never reordered; only
//     markers may be serviced out of order, and only flush markers.
//   - A hold marker ("__gate__" or "__hold__", arity 2) means a narration
//     send is in flight: the pump closes the current bubble and holds the
//     next box's first send until the marker's event is released, so the
//     narration lands ABOVE the tool box it narrates.
//   - A flush marker ("__flush__", arity 2) is the context-card ordering
//     gate: deliver what was absorbed ahead of it, then set its event.
//     Cards are queued before the first model call, so they always precede
//     any hold marker armed by that call's commentary.
//   - While the pump waits on a hold it MUST service every flush marker
//     parked in the queue: the narration send that releases the hold is
//     itself gated on that flush, so parking without service deadlocks
//     both waits until the last-resort timeout guard fires — the live
//     incident this package pins ("progress hold marker never released"
//     exactly one hold-timeout after the step).
//   - Servicing a parked flush marker early cannot violate the
//     cards-above-reply invariant: the hold handler force-delivers the
//     absorbed bubble (which carries every card, since cards precede the
//     hold marker in FIFO) before it starts waiting.
//   - A hold that is never released still ends at the pump's timeout
//     guard; the guard exists for a dead narration send, never as the
//     ordering mechanism.
package progresshold

// Marker tags carried as the first element of a queued tuple. They mirror
// the tags the Python producers emit: the narration gate
// (GatewayStreamConsumer._begin_progress_gate) and the context-card gate
// (gateway.run._CONTEXT_FLUSH_QUEUE_MARKER).
const (
	// GateTag marks a narration hold armed from the agent worker thread.
	GateTag = "__gate__"
	// HoldTag marks a narration hold armed inside the event loop.
	HoldTag = "__hold__"
	// FlushTag marks the context-card ordering gate's drain barrier.
	FlushTag = "__flush__"
	// ResetTag closes the current tool-progress bubble without a wait.
	ResetTag = "__reset__"
)

// markerArity is the tuple shape every event-carrying marker shares:
// (tag, event). An entry that carries a marker tag at any other arity is a
// row that happens to look marker-shaped; it is never serviced as a signal
// (mirrors the Python twin's len(raw) == 2 predicates).
const markerArity = 2

// Kind is the pump's classification of one queued entry.
type Kind int

const (
	// KindRow is ordinary progress content: boxed and delivered in FIFO.
	KindRow Kind = iota
	// KindReset closes the current bubble so the next box opens fresh.
	KindReset
	// KindHold parks the pump until the in-flight narration send releases.
	KindHold
	// KindFlush is the context-card gate's drain-and-signal barrier.
	KindFlush
)

// String renders the kind for test failures and debug output.
func (k Kind) String() string {
	switch k {
	case KindRow:
		return "row"
	case KindReset:
		return "reset"
	case KindHold:
		return "hold"
	case KindFlush:
		return "flush"
	}
	return "unknown"
}

// Item is one queued progress-queue entry reduced to the parts the marker
// protocol sees: a stable ID (test correlation only), the tuple tag (""
// for plain rows), and the tuple arity. Production rows are plain strings;
// they classify as KindRow through the zero Tag.
type Item struct {
	ID    string `json:"id"`
	Tag   string `json:"tag"`
	Arity int    `json:"arity"`
}

// Classify reduces an item to its marker kind. It mirrors the Python twin's
// _is_reset_marker / _is_hold_marker / _is_flush_marker predicates exactly:
// reset tolerates trailing payload (arity >= 1), while hold and flush
// require precisely the (tag, event) pair — a tag at the wrong arity is
// content, not a signal.
func Classify(item Item) Kind {
	switch item.Tag {
	case ResetTag:
		if item.Arity >= 1 {
			return KindReset
		}
	case GateTag, HoldTag:
		if item.Arity == markerArity {
			return KindHold
		}
	case FlushTag:
		if item.Arity == markerArity {
			return KindFlush
		}
	}
	return KindRow
}
