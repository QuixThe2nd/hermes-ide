package generations

import "sort"

// Registry glue: build the complete decision Array from a transport hub's
// successor-registry snapshot plus the asking process's own record.
//
// The hub registry records ONLY spawned successors, in spawn order, each
// verified live by PID + start-time fingerprint before it reaches this
// layer (unverifiable rows are pruned upstream). Two kinds of generation
// therefore never appear in it:
//
//   - the ORIGINAL gateway, which was nobody's successor — it exists in the
//     registry only as somebody's Predecessor PID;
//   - a successor that never registered (hub-less deployment, records
//     lost) — it knows its own predecessor and must be ordered between
//     that predecessor and any recorded generation that names IT as
//     predecessor.
//
// ArrayFromRegistry closes both gaps so the Array satisfies the input
// contract of MayStartSharedAdapters: every record-less predecessor is
// resolved into a synthetic record (spawn sequence 0, liveness supplied by
// the caller's OS probe). The caller — not this package — performs the
// probe: liveness is a host syscall, decisions are not.

// RegistryRow is one live successor-registry record. Rows arrive in spawn
// order; spawn sequence is derived from that order (first row = 1).
type RegistryRow struct {
	// PID of the spawned successor. Rows with PID <= 0 are ignored.
	PID int `json:"pid"`
	// StartTime is the process start fingerprint the hub verified.
	StartTime int64 `json:"start_time"`
	// Predecessor is the PID that spawned this successor; 0 = unknown.
	Predecessor int `json:"predecessor_pid"`
	// Attached: currently attached to the transport hub.
	Attached bool `json:"attached"`
}

// Me is the asking process's own record. Liveness is trivially true (the
// process asking is alive); attach/drain state is self-reported.
type Me struct {
	// PID of the asking process.
	PID int `json:"pid"`
	// StartTime fingerprint of the asking process (0 = unknown).
	StartTime int64 `json:"start_time"`
	// Predecessor PID of the asking process (0 = it is the original).
	Predecessor int `json:"predecessor"`
	// Draining: the asking process has spawned a successor and begun its drain.
	Draining bool `json:"draining"`
	// Attached: the asking process is attached to the transport hub.
	Attached bool `json:"attached"`
}

// Probe is the caller's OS-liveness answer for a record-less predecessor.
// Only PIDs that appear as somebody's Predecessor without a row of their
// own are probed; unknown PIDs here are ignored.
type Probe struct {
	PID  int  `json:"pid"`
	Live bool `json:"live"`
}

// ArrayFromRegistry builds the complete Array the decision core needs.
//
// Construction rules, in order:
//
//  1. rows become records, spawn sequence = row order (1-based);
//  2. every record named as somebody's predecessor is draining;
//  3. the asking Me is merged (or added: after its predecessor when known,
//     before the earliest record naming it as predecessor — sequence 0
//     when no consistent window exists, which can only DELAY a claim,
//     never duplicate one: lock + PID-file takeover fail closed);
//  4. every remaining record-less predecessor gets a synthetic record with
//     spawn sequence 0 and the caller's probed liveness — unprobed PIDs
//     read as LIVE (a reused PID must never look like a free slot).
func ArrayFromRegistry(rows []RegistryRow, me Me, probes []Probe) Array {
	liveness := make(map[int]bool, len(probes))
	for _, p := range probes {
		if p.PID > 0 {
			liveness[p.PID] = p.Live
		}
	}
	records := make(map[int]Generation, len(rows)+2)
	for seq, row := range rows {
		if row.PID <= 0 {
			continue
		}
		records[row.PID] = Generation{
			PID:         row.PID,
			StartTime:   row.StartTime,
			Predecessor: row.Predecessor,
			SpawnSeq:    seq + 1,
			Attached:    row.Attached,
			Draining:    false, // derived below from successor links
			Live:        true,  // registry rows are live-fingerprinted or pruned
		}
	}
	spawnedPredecessors := make(map[int]bool, len(records))
	for _, g := range records {
		if g.Predecessor > 0 {
			spawnedPredecessors[g.Predecessor] = true
		}
	}
	for pid, g := range records {
		if spawnedPredecessors[pid] {
			g.Draining = true
			records[pid] = g
		}
	}
	if mine, ok := records[me.PID]; ok {
		mine.Attached = mine.Attached || me.Attached
		mine.Draining = me.Draining
		mine.Live = true
		records[me.PID] = mine
	} else {
		records[me.PID] = Generation{
			PID:         me.PID,
			StartTime:   me.StartTime,
			Predecessor: me.Predecessor,
			SpawnSeq:    unrecordedSeq(records, me),
			Attached:    me.Attached,
			Draining:    me.Draining,
			Live:        true,
		}
	}
	// Complete the array: every predecessor without a record of its own
	// (the original gateway) gets a synthetic record older than every
	// spawned successor. Unprobed means live: authority must fail closed.
	for _, g := range records {
		if g.Predecessor > 0 {
			if _, ok := records[g.Predecessor]; !ok {
				live, probed := liveness[g.Predecessor]
				if !probed {
					// Unprobed means unknown: unknown reads
					// as live (fail closed).
					live = true
				}
				records[g.Predecessor] = Generation{
					PID:      g.Predecessor,
					SpawnSeq: 0,
					Draining: true,
					Live:     live,
				}
			}
		}
	}
	return toArray(records)
}

// unrecordedSeq places a successor that never registered inside the
// recorded chain: strictly after its predecessor, strictly before the
// earliest record naming it as predecessor. No consistent window (a
// degenerate history) keeps sequence 0 — an ordering error in a lost
// history can only delay a claim, never duplicate one.
func unrecordedSeq(records map[int]Generation, me Me) int {
	if me.Predecessor <= 0 {
		// The original gateway: older than every spawned successor.
		// An original asking with a live successor must not mistake
		// itself for the newest.
		return 0
	}
	lo := 0
	if pred, ok := records[me.Predecessor]; ok {
		lo = pred.SpawnSeq
	}
	hi := -1
	for _, g := range records {
		if g.Predecessor == me.PID {
			bound := g.SpawnSeq - 1
			if hi < 0 || bound < hi {
				hi = bound
			}
		}
	}
	if hi >= 0 && lo+1 > hi {
		return 0
	}
	return lo + 1
}

// toArray renders the record map into decision order (spawn sequence,
// then PID for the unrecorded originals).
func toArray(records map[int]Generation) Array {
	out := make(Array, 0, len(records))
	for _, g := range records {
		out = append(out, g)
	}
	sort.Slice(out, func(i, j int) bool { return less(&out[i], &out[j]) })
	return out
}
