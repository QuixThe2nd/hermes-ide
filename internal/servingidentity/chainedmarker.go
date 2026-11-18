package servingidentity

import "strings"

// ChainedMarker is a peeked .restart_chain.json payload as the status
// surface consumes it: the restart source plus the generation identity the
// recording drainer stamped (itself and the pending successor it spawned).
// A legacy marker — recorded before generation stamping — carries only
// Source and can never be classified against a serving process.
//
// Zero PID fields mean "absent" on the wire (the recorder omits them).
type ChainedMarker struct {
	Source           string
	PredecessorPID   int
	PredecessorStart int64
	SuccessorPID     int
	SuccessorStart   int64
}

// Legacy reports whether the marker names no process at all (recorded by a
// pre-stamping build). Parity twin of
// gateway.restart_chain._marker_is_legacy.
func (m *ChainedMarker) Legacy() bool {
	return m == nil || (m.PredecessorPID <= 0 && m.SuccessorPID <= 0)
}

// ChainedMarkerState classifies a peeked marker against the process
// provably serving turns. Parity twin of
// gateway.restart_chain.chained_marker_state:
//
//   - "pending": the marker belongs to the serving process's own handoff —
//     it names the serving process as the draining predecessor, or as the
//     successor that predecessor is draining for;
//   - "stale": the marker carries its generation's identity and the serving
//     process is provably NEITHER member — that generation is dead and
//     nothing will ever act on the marker;
//   - "unproven": legacy marker, or a comparison missing the fingerprints
//     to prove either way — claim nothing, keep the pending rendering.
func ChainedMarkerState(m *ChainedMarker, servingPID int, servingStart int64) string {
	if m == nil || m.Legacy() {
		return "unproven"
	}
	if servingPID <= 0 {
		return "unproven"
	}
	for _, member := range [2]struct {
		pid   int
		start int64
	}{
		{m.PredecessorPID, m.PredecessorStart},
		{m.SuccessorPID, m.SuccessorStart},
	} {
		if member.pid == servingPID {
			if member.start == 0 || servingStart == 0 {
				return "unproven"
			}
			if member.start == servingStart {
				return "pending"
			}
			return "stale"
		}
	}
	// The serving process is provably not the marker's predecessor nor its
	// successor: a different live pid is a different process regardless of
	// fingerprints.
	return "stale"
}

// ChainedLine renders the status line for a peeked marker against the
// proven serving identity (pid + start fingerprint; zero when unproven).
// Wording is byte-identical to gateway.serving_identity's chained block:
// a provably dead generation is never rendered as a queued restart.
func ChainedLine(m *ChainedMarker, servingPID int, servingStart int64) string {
	if m == nil {
		return ""
	}
	source := ChainedSource(m.Source)
	if ChainedMarkerState(m, servingPID, servingStart) != "stale" {
		return "⏳ Chained restart queued: another restart will run once the current handoff " +
			"completes (requested by " + source + ")"
	}
	var generationParts []string
	if m.PredecessorPID > 0 {
		generationParts = append(generationParts, "predecessor pid "+itoa(m.PredecessorPID))
	}
	if m.SuccessorPID > 0 {
		generationParts = append(generationParts, "successor pid "+itoa(m.SuccessorPID))
	}
	generation := strings.Join(generationParts, " → ")
	if generation == "" {
		generation = "a previous process generation"
	}
	return "⚠ Chained restart marker is stale and will not run: requested by " + source +
		", recorded for " + generation + ", but the serving process is " +
		startClause(servingPID, servingStart) +
		"; nothing will act on it — the next gateway start clears it"
}
