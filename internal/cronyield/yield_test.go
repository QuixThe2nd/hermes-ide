package cronyield

import "testing"

// A fresh-gateway baseline: every precondition of the original gate holds and
// the holder is an unrelated process that started later.
var freshGateway = Inputs{
	SkewPresent: true, OwnsLock: false, HolderActive: true, ProbeOK: true,
	HolderPID: 5555, PredecessorPID: 0, HolderStartTime: 3000, OwnStartTime: 1000,
}

// TestShouldYield walks the full contract: the original three-probe gate
// (skew, self-ownership, holder liveness), the fail-open rule, and the
// holder-freshness verification added after the 2026-11-17 overlap-restart
// incident — including every negative the Python copy
// (tests/cron/test_cron_tick_stale_yield.py) pins down.
func TestShouldYield(t *testing.T) {
	tests := []struct {
		name string
		in   Inputs
		want bool
	}{
		// The gate's original purpose: skew + an unrelated, strictly later
		// holder → yield, keeping stale code out of the dispatch race.
		{
			name: "fresh foreign gateway: yields",
			in:   freshGateway,
			want: true,
		},
		{
			name: "holder started only one tick later still yields",
			in:   Inputs{SkewPresent: true, HolderActive: true, ProbeOK: true, HolderPID: 9, HolderStartTime: 1001, OwnStartTime: 1000},
			want: true,
		},

		// The original three probes keep their veto.
		{
			name: "no skew (fresh code or unreadable fingerprint): proceeds",
			in:   Inputs{OwnsLock: false, HolderActive: true, ProbeOK: true, HolderPID: 5555, HolderStartTime: 3000, OwnStartTime: 1000},
			want: false,
		},
		{
			name: "this process owns the runtime lock: proceeds",
			in:   Inputs{SkewPresent: true, OwnsLock: true, HolderActive: true, ProbeOK: true, HolderPID: 5555, HolderStartTime: 3000, OwnStartTime: 1000},
			want: false,
		},
		{
			name: "no live holder (desktop-standalone): proceeds",
			in:   Inputs{SkewPresent: true, OwnsLock: false, HolderActive: false, ProbeOK: true, HolderPID: 5555, HolderStartTime: 3000, OwnStartTime: 1000},
			want: false,
		},

		// Fail-open: yielding is a certainty claim, never a guess. A probe
		// that cannot answer — lock probe error, unreadable holder record,
		// unresolvable own start time — must never withhold the tick.
		{
			name: "probe failed: proceeds",
			in:   Inputs{SkewPresent: true, OwnsLock: false, HolderActive: true, ProbeOK: false, HolderPID: 5555, HolderStartTime: 3000, OwnStartTime: 1000},
			want: false,
		},
		{
			name: "unreadable holder record (no pid): proceeds",
			in:   Inputs{SkewPresent: true, HolderActive: true, ProbeOK: true, HolderPID: 0, HolderStartTime: 3000, OwnStartTime: 1000},
			want: false,
		},
		{
			name: "holder record without start time: proceeds",
			in:   Inputs{SkewPresent: true, HolderActive: true, ProbeOK: true, HolderPID: 5555, HolderStartTime: 0, OwnStartTime: 1000},
			want: false,
		},
		{
			name: "own start time unresolvable: proceeds",
			in:   Inputs{SkewPresent: true, HolderActive: true, ProbeOK: true, HolderPID: 5555, HolderStartTime: 3000, OwnStartTime: 0},
			want: false,
		},

		// The 2026-11-17 incident: a draining overlap predecessor holds the
		// lock while its pending successor runs the ticker. The successor is
		// the one generation allowed to dispatch — it must not yield to the
		// very process whose drain suppresses dispatch.
		{
			name: "holder is this process's overlap predecessor (started earlier): proceeds",
			in:   Inputs{SkewPresent: true, HolderActive: true, ProbeOK: true, HolderPID: 4242, PredecessorPID: 4242, HolderStartTime: 100, OwnStartTime: 200},
			want: false,
		},
		{
			name: "holder is the predecessor even with a later recorded start: proceeds",
			in:   Inputs{SkewPresent: true, HolderActive: true, ProbeOK: true, HolderPID: 4242, PredecessorPID: 4242, HolderStartTime: 9999, OwnStartTime: 200},
			want: false,
		},

		// Freshness is ordinal, not assumed: an unrelated holder that did
		// NOT start later is at least as stale as this process.
		{
			name: "unrelated holder started earlier: proceeds",
			in:   Inputs{SkewPresent: true, HolderActive: true, ProbeOK: true, HolderPID: 5555, HolderStartTime: 100, OwnStartTime: 200},
			want: false,
		},
		{
			name: "equal start times are not provably fresher: proceeds",
			in:   Inputs{SkewPresent: true, HolderActive: true, ProbeOK: true, HolderPID: 5555, HolderStartTime: 200, OwnStartTime: 200},
			want: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ShouldYield(tt.in); got != tt.want {
				t.Errorf("ShouldYield(%+v) = %v, want %v", tt.in, got, tt.want)
			}
		})
	}
}

// TestHolderFreshnessRequiresEveryPrecondition proves the holder-freshness
// arm cannot be satisfied by any single condition: flipping each baseline
// input of the yielding case, one at a time, must flip the verdict to
// "proceed".
func TestHolderFreshnessRequiresEveryPrecondition(t *testing.T) {
	flips := map[string]func(in Inputs) Inputs{
		"skew absent":        func(in Inputs) Inputs { in.SkewPresent = false; return in },
		"owns lock":          func(in Inputs) Inputs { in.OwnsLock = true; return in },
		"holder inactive":    func(in Inputs) Inputs { in.HolderActive = false; return in },
		"probe failed":       func(in Inputs) Inputs { in.ProbeOK = false; return in },
		"holder pid missing": func(in Inputs) Inputs { in.HolderPID = 0; return in },
		"holder start unset": func(in Inputs) Inputs { in.HolderStartTime = 0; return in },
		"own start unset":    func(in Inputs) Inputs { in.OwnStartTime = 0; return in },
		"holder is predecessor": func(in Inputs) Inputs {
			in.PredecessorPID = in.HolderPID
			return in
		},
		"holder not later": func(in Inputs) Inputs { in.HolderStartTime = in.OwnStartTime; return in },
	}
	for name, flip := range flips {
		t.Run(name, func(t *testing.T) {
			if got := ShouldYield(flip(freshGateway)); got {
				t.Errorf("ShouldYield with %q still yields; every precondition must be required", name)
			}
		})
	}
}
