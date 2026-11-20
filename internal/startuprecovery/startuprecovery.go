// Package startuprecovery is the Go twin of the gateway's startup
// session-recovery verdict (gateway/lifecycle_ledger.py previous_life_unclean
// and GatewayStartupMixin._recover_previous_run_sessions). The Python
// functions are what the runtime executes; deployments carry no Go
// toolchain, the same arrangement as every other internal/* twin. Both
// twins run the shared table in testdata/cases.json.
//
// Contract (shared with the Python twin):
//
//   - Two receipts describe the previous gateway life. The .clean_shutdown
//     marker is written by whichever process finished a drained teardown.
//     The lifecycle ledger sentinel is owned by the newest generation that
//     claimed it. In an overlap restart these are different processes: the
//     predecessor writes the marker after its successor has already claimed
//     the ledger. So the marker can vouch for a life that was not the last
//     one.
//   - The ledger's unclean verdict therefore wins. A marker beside an
//     unclean ledger is a stale receipt: the interrupted turns are
//     recovered (promoted to resume_pending), not discarded.
//   - The verdict is read from this boot's own claim when it already ran
//     (record_startup stamps prior_unclean_exit on the new sentinel). Before
//     the claim (an overlap successor still pending), it falls back to
//     read-only detection: phase "running" whose owner is no longer live.
//   - A clean or absent ledger changes nothing: the marker decides exactly
//     as before.
package startuprecovery

// Sentinel is the subset of state/gateway.lifecycle.json the verdict reads.
type Sentinel struct {
	Phase            string `json:"phase"`
	PID              int    `json:"pid"`
	PriorUncleanExit bool   `json:"prior_unclean_exit"`
}

// Mode is what startup does with the previous run's in-flight sessions.
type Mode string

const (
	// ModeDiscard clears orphan active-turn markers and consumes the clean
	// marker: the previous process drained its turns.
	ModeDiscard Mode = "discard"
	// ModeRecover promotes exact active-turn markers (plus the 120 s legacy
	// fallback) to resume_pending: the previous life died mid-turn.
	ModeRecover Mode = "recover"
)

// LedgerUnclean reports whether the ledger says the previous life died
// uncleanly. sentinel is nil when the file is absent or unreadable; me is
// this process's PID; ownerLive is the sentinel owner's liveness (PID plus
// birth-time identity), consulted only before this boot's own claim.
func LedgerUnclean(sentinel *Sentinel, me int, ownerLive bool) bool {
	if sentinel == nil {
		return false
	}
	if sentinel.PID == me {
		return sentinel.PriorUncleanExit
	}
	return sentinel.Phase == "running" && !ownerLive
}

// Decide picks the recovery mode from the clean marker and the ledger.
func Decide(cleanMarker, ledgerUnclean bool) Mode {
	if cleanMarker && !ledgerUnclean {
		return ModeDiscard
	}
	return ModeRecover
}
