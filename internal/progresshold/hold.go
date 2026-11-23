// Hold-wait servicing: the anti-deadlock rule the pump applies while it
// waits on a narration hold marker.
//
// The two ordering gates share one FIFO queue. A narration hold parks the
// pump; the context-card gate parks the narration send on a flush marker.
// When the flush marker sits BEHIND the hold marker, each side waits for
// the other: the pump never reaches the flush, the send never releases the
// hold, and only the timeout guard breaks the cycle a full hold-timeout
// later — visibly, the narration lands late and BELOW the tool box it was
// meant to precede. The rule in this file is the fix: a parked pump keeps
// servicing flush markers, and nothing else moves.

package progresshold

// PartitionParkedFlushMarkers splits a parked queue snapshot into the flush
// markers to service (in FIFO order) and everything else (in FIFO order).
// It mirrors the Python twin's TurnRunner._partition_parked_flush_markers.
//
// Only flush markers are extracted. Hold markers stay put: a second hold
// queued behind the first is the NEXT narration's gate and must still be
// handled in order once the current wait ends. Reset markers stay put for
// the same reason — they close boxes, and box order is output order.
func PartitionParkedFlushMarkers(items []Item) (markers, kept []Item) {
	for _, item := range items {
		if Classify(item) == KindFlush {
			markers = append(markers, item)
		} else {
			kept = append(kept, item)
		}
	}
	return markers, kept
}

// ParkableForHold reports whether the pump may park on a hold marker
// without starving a waiter. A queue carrying no flush marker is safe to
// hold against; one carrying a flush marker is not, because the send that
// releases the hold is gated on that flush. This predicate is the incident
// stated as a rule: parking with ParkableForHold(items) == false is the
// deadlock.
func ParkableForHold(items []Item) bool {
	for _, item := range items {
		if Classify(item) == KindFlush {
			return false
		}
	}
	return true
}

// PreservesRowOrder verifies the partition's load-bearing invariant: the
// kept items are exactly the input with flush markers removed — no row and
// no non-flush marker changed position relative to another. "Service the
// marker early" must never become "reorder visible output", so the tests
// assert this on every shared scenario, and any future partition rule that
// extracts more than flush markers fails loudly here.
func PreservesRowOrder(input, kept []Item) bool {
	j := 0
	for _, item := range input {
		if Classify(item) == KindFlush {
			continue
		}
		if j >= len(kept) || kept[j].ID != item.ID {
			return false
		}
		j++
	}
	return j == len(kept)
}

// ServiceOrder is the full hold-wait decision in one call: which parked
// markers the pump services now (flush markers, FIFO), what the queue
// looks like afterwards, and whether the pump may then park. It exists so
// both twins' tests assert one composed decision per scenario instead of
// re-deriving the parts.
func ServiceOrder(items []Item) (markers, kept []Item, parkable bool) {
	markers, kept = PartitionParkedFlushMarkers(items)
	return markers, kept, ParkableForHold(items)
}
