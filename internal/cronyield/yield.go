// Package cronyield is the Go contract for the cron tick's stale-code
// yield gate, mirroring cron/scheduler.py::_should_yield_tick_to_fresh_gateway
// and _runtime_lock_holder_is_fresher in the Python tree. The Python cron
// scheduler still executes the Python copy; this package is the Go contract,
// the same arrangement as internal/cronunreachable — it is not imported by
// Python.
//
// The gate exists because a long-lived process whose checkout was updated
// underneath it (hot git pull, interrupted `hermes update`) serves mixed
// sys.modules: when such a process races a fresh gateway for the cron tick
// lock and wins, every agent job it dispatches can die on ImportErrors whose
// real cause is staleness. A stale ticker must therefore get out of the way —
// but ONLY for a holder that is provably fresher. Yielding is a certainty
// claim, never a guess: any probe failure means "proceed" (fail-open),
// because a wrongly withheld tick silently kills the user's only ticker.
//
// Incident this contract encodes (2026-11-17, ~10h of zero cron dispatch):
// during a chained overlap restart the draining predecessor P kept the
// runtime lock while its pending successor S ran the ticker. A git pull made
// both generations stale; the gate saw "skew + someone else holds the lock"
// and yielded — to P, whose own drain gate suppresses dispatch. Holding the
// lock is not proof of freshness. The holder must not be this process's
// overlap predecessor (a draining predecessor is older by construction and
// its pending successor is the one generation allowed to dispatch) and must
// have started strictly later than this process.
package cronyield

// Inputs carries one tick's yield-gate probes. All pid/start-time values use
// the host-local units of gateway/status.py::get_process_start_time
// (/proc jiffies on Linux, psutil centiseconds elsewhere); both start times
// come from the same host, so the comparison is sound.
type Inputs struct {
	// SkewPresent: this process's boot fingerprint ≠ the on-disk revision
	// (it is provably running stale, mixed-module code).
	SkewPresent bool
	// OwnsLock: THIS process holds the gateway runtime lock — a gateway
	// never yields its own ticks.
	OwnsLock bool
	// HolderActive: some process currently holds the gateway runtime lock.
	HolderActive bool
	// ProbeOK: every probe answered. Any probe failure (skew unreadable,
	// lock probe error, unreadable holder record, unresolvable own start
	// time) must yield false — freshness cannot be proven, so the tick
	// proceeds.
	ProbeOK bool
	// HolderPID is the lock-holder pid from the lock record; 0 when the
	// record is unreadable or carries no usable pid.
	HolderPID int
	// PredecessorPID is this process's overlap predecessor
	// (gateway/overlap_restart.py::overlap_predecessor_pid); 0 when this
	// process has no overlap-predecessor identity.
	PredecessorPID int
	// HolderStartTime is the holder's process start-time fingerprint from
	// the lock record; <= 0 when absent.
	HolderStartTime int64
	// OwnStartTime is this process's own start-time fingerprint; <= 0 when
	// unresolvable.
	OwnStartTime int64
}

// ShouldYield reports whether a cron tick in this process must yield to a
// fresher gateway before entering the tick-lock race. True only when ALL
// hold:
//
//  1. this process is provably stale (skew present),
//  2. it does not own the gateway runtime lock,
//  3. another process currently holds that lock,
//  4. every probe answered (fail-open), and
//  5. the holder is provably fresher: a usable pid + start time, the pid is
//     not this process's overlap predecessor, and the holder started
//     strictly later than this process.
//
// Equal start times are NOT fresher — an unprovable ordering must not yield.
func ShouldYield(in Inputs) bool {
	if !in.ProbeOK || !in.SkewPresent || in.OwnsLock || !in.HolderActive {
		return false
	}
	if in.HolderPID <= 0 || in.HolderStartTime <= 0 || in.OwnStartTime <= 0 {
		return false
	}
	if in.PredecessorPID > 0 && in.HolderPID == in.PredecessorPID {
		return false
	}
	return in.HolderStartTime > in.OwnStartTime
}
