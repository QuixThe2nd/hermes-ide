// Package generations is the canonical statement of the gateway restart
// generation array: the decision core that keeps an arbitrary number of
// overlapping gateway generations straight.
//
// The Python twin (gateway/generations.py) is what the runtime uses —
// deployments (pip, docker) carry no Go toolchain, the same arrangement as
// every other internal/* twin. The contract test
// (tests/gateway/test_generations_contract.py) builds cmd/generations and
// asserts the twins agree on every record of the shared table
// (testdata/cases.json); the Go tests run the same table as a second
// cross-check.
//
// Model (shared with the Python twin):
//
// A gateway restart no longer hands a single successor one slot; every
// restart appends a generation. At any moment the LIVE generation with the
// highest spawn sequence is authoritative: it owns the runtime lock, the
// PID file, gateway_state.json, cron dispatch and the singleton listeners
// (api_server, webhook, whatsapp). Older generations are draining: they
// keep ONLY the in-flight sessions they already own, deliver their finals
// through the transport hub, and exit independently in any order.
//
// Invariants this package computes:
//
//   - NewestLive: precedence is liveness first, then spawn sequence. A
//     successor whose process died is never authoritative even if the hub
//     still lists it; among live generations the latest spawn wins. Spawn
//     sequence comes from the hub's successor registry, which records
//     spawns in order; the original (unspawned) gateway is sequence 0.
//   - AncestorsOf walks predecessor links transitively and may include
//     PIDs with no record of their own (the original gateway appears only
//     as somebody's predecessor).
//   - MayClaimAuthority: only the newest live generation may take the
//     runtime lock / PID file. A generation superseded while it was still
//     waiting to claim never claims — its own successor owns the authority.
//   - MayStartSharedAdapters: shared-credential transports (everything
//     without a transport hub) may start only once no live ancestor
//     remains; listener adapters are NOT gated on this (the predecessor
//     stops its listener at handover, so the newest may bind at claim).
//   - TakeoverAllowed: a successor rewriting the PID file may displace
//     only its own ancestors and provably dead holders — never an
//     unrelated live gateway.
package generations

import "sort"

// Generation is one gateway process in the restart array.
type Generation struct {
	// PID is the process id. The zero value means "no such generation".
	PID int `json:"pid"`
	// ID is the hub generation id ("" when only the PID is known).
	ID string `json:"id,omitempty"`
	// StartTime is the process start fingerprint (jiffies/centiseconds —
	// only same-host equality is meaningful); 0 = unknown.
	StartTime int64 `json:"start_time,omitempty"`
	// Predecessor is the PID of the spawning predecessor; 0 = original.
	Predecessor int `json:"predecessor,omitempty"`
	// SpawnSeq orders hub-recorded spawns; the original gateway is 0.
	// Ties (two unrecorded originals) fall back to PID order.
	SpawnSeq int `json:"spawn_seq,omitempty"`
	// Attached: currently attached to the transport hub.
	Attached bool `json:"attached,omitempty"`
	// Draining: has spawned its own successor and begun its drain.
	Draining bool `json:"draining,omitempty"`
	// Live: process proven alive (PID plus start fingerprint check).
	// Unknown liveness counts as live: refusing to hand authority to a
	// possibly-live successor is safe; the reverse is a duplicate.
	Live bool `json:"live"`
}

// Array is one snapshot of the generation set.
type Array []Generation

// ByPID returns the generation with that PID, or nil.
func (a Array) ByPID(pid int) *Generation {
	for i := range a {
		if a[i].PID == pid {
			return &a[i]
		}
	}
	return nil
}

// less orders generations by precedence: spawn sequence, then PID for the
// unrecorded originals that share sequence 0.
func less(x, y *Generation) bool {
	if x.SpawnSeq != y.SpawnSeq {
		return x.SpawnSeq < y.SpawnSeq
	}
	return x.PID < y.PID
}

// NewestLive returns the live generation with the highest precedence, or
// nil when no generation is provably live.
func (a Array) NewestLive() *Generation {
	var best *Generation
	for i := range a {
		g := &a[i]
		if !g.Live {
			continue
		}
		if best == nil || less(best, g) {
			best = g
		}
	}
	return best
}

// NewestLiveOther returns the newest live generation other than me, or nil.
// This answers BOTH "who executes a restart forwarded from me" and "whose
// PID a supervisor handover should target": in the array model the newest
// live generation owns new work, so it is both the forward target and the
// surviving owner.
func (a Array) NewestLiveOther(me int) *Generation {
	best := a.NewestLive()
	if best != nil && best.PID == me {
		return nil
	}
	return best
}

// AncestorsOf returns the transitive predecessor PIDs of me, nearest
// first. A predecessor with no record of its own (the original gateway)
// still appears — it exists as somebody's Predecessor.
func (a Array) AncestorsOf(me int) []int {
	chain := make([]int, 0, 4) // non-nil so JSON encodes [] not null
	seen := map[int]bool{me: true}
	current := me
	for {
		g := a.ByPID(current)
		if g == nil || g.Predecessor == 0 || seen[g.Predecessor] {
			break
		}
		chain = append(chain, g.Predecessor)
		seen[g.Predecessor] = true
		current = g.Predecessor
	}
	return chain
}

// MayClaimAuthority: me may take the runtime lock / PID file only when no
// newer live generation exists. A generation superseded while pending (its
// own successor already spawned) must stay out of the authority race
// forever; it drains and exits under exit code 76.
func (a Array) MayClaimAuthority(me int) bool {
	if a.ByPID(me) == nil || !a.ByPID(me).Live {
		return false
	}
	newer := a.NewestLive()
	return newer == nil || newer.PID == me
}

// MayStartSharedAdapters: shared-credential transports (no hub: Telegram,
// Feishu, socket Slack…) may connect only once no live ancestor keeps
// holding their platform locks. Listener adapters are not gated here: the
// predecessor closes its listening socket at handover, so the newest
// generation binds at claim time.
//
// INPUT CONTRACT: the array must be complete. The hub registry records
// only spawned successors; the original gateway appears solely as
// somebody's Predecessor. Callers must therefore resolve each record-less
// predecessor into a synthetic record (spawn sequence 0, OS-verified
// liveness) BEFORE asking this question. A record-less ancestor inside the
// array still fails closed: liveness of an unknown process cannot be
// proven from geometry alone.
func (a Array) MayStartSharedAdapters(me int) bool {
	g := a.ByPID(me)
	if g == nil || !g.Live {
		return false
	}
	for _, pid := range a.AncestorsOf(me) {
		ancestor := a.ByPID(pid)
		// A record-less ancestor (the original gateway exists only as
		// somebody's predecessor) is presumed still blocking: the caller
		// cannot know it exited, and the shared-credential platform locks
		// it held are invisible from here. Fail closed.
		if ancestor == nil || ancestor.Live {
			return false
		}
	}
	return true
}

// TakeoverAllowed decides whether the generation claiming authority may
// rewrite a PID file that currently names recordedPID. Allowed when the
// file is absent (0), names a provably dead holder, or names one of me's
// ancestors (ancestorsOf — nearest first, from AncestorsOf). A live
// unrelated gateway must never be displaced: that is a duplicate, not a
// handover.
func TakeoverAllowed(recordedPID int, recordedLive bool, ancestorsOfMe []int) bool {
	if recordedPID <= 0 || !recordedLive {
		return true
	}
	for _, pid := range ancestorsOfMe {
		if pid == recordedPID {
			return true
		}
	}
	return false
}

// Describe renders the snapshot for diagnostics: live generations first
// (newest first), then dead ones, each as "pid: role".
func (a Array) Describe() []string {
	live := make([]*Generation, 0, len(a))
	dead := make([]*Generation, 0, len(a))
	for i := range a {
		if a[i].Live {
			live = append(live, &a[i])
		} else {
			dead = append(dead, &a[i])
		}
	}
	descend := func(s []*Generation) {
		sort.SliceStable(s, func(i, j int) bool { return less(s[j], s[i]) })
	}
	descend(live)
	descend(dead)
	newest := a.NewestLive()
	roles := make([]string, 0, len(a))
	for _, g := range append(live, dead...) {
		role := "gone"
		switch {
		case !g.Live:
			role = "gone"
		case newest != nil && newest.PID == g.PID:
			role = "serving"
		case g.Draining:
			role = "draining"
		default:
			role = "starting"
		}
		roles = append(roles, itoa(g.PID)+": "+role)
	}
	return roles
}

func itoa(v int) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var digits [20]byte
	i := len(digits)
	for v > 0 {
		i--
		digits[i] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		i--
		digits[i] = '-'
	}
	return string(digits[i:])
}
