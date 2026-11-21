// Package restartsuccessor is the Go twin of the always-spawn restart
// policy in gateway/overlap_restart.py::spawn_successor: every restart of a
// gateway process spawns exactly one overlap successor through the first
// overlap-capable platform adapter, and no configuration key can disable
// that hand-over. The gateway.restart_platforms option was removed
// (PR #825); stale values in user config.yaml files are inert leftovers.
//
// This is the canonical statement of the contract; the Python function is
// what the runtime executes (deployments carry no Go toolchain — the same
// arrangement as every other internal/* twin). cmd/restartsuccessor is the
// executable form: the Python contract test
// (tests/gateway/test_restart_successor_contract.py) builds it and asserts
// identical decisions for every record in testdata/restart_cases.json, and
// the Go tests run the same table as a second cross-check.
//
// Contract (shared with the Python twin):
//   - The spawn decision reads adapter capability and the runner's
//     successor latch ONLY. Config — including a stale
//     gateway.restart_platforms: false — is never consulted; Scenario
//     carries it solely so the shared table can prove its irrelevance.
//   - Exactly one successor: a runner whose latch is set returns the
//     latched pid without attempting any spawn.
//   - An adapter participates only when supports_overlap_restart is True
//     AND it exposes spawn_overlap_successor; others are skipped in
//     iteration order. The first participant yielding a pid wins and is
//     latched; a participant returning None falls through to the next.
//   - A spawn that raised but latched a child pid retains that child as
//     the successor (never a second spawn), without readiness marking.
//     A spawn that raised with no latched child is an error the caller
//     turns into the legacy drain path.
//   - On a clean spawn, EVERY overlap-capable adapter gets the readiness
//     callbacks (wait_for_overlap_successor, mark_overlap_successor_ready)
//     — the ones that did not spawn included.
//   - No capable adapter at all is the legacy-drain outcome: the
//     predecessor drains without a successor and the supervisor revives.
package restartsuccessor

// Scenario is one spawn decision's input: the runner's successor latch,
// the adapters in iteration order, and the (ignored) gateway config.
type Scenario struct {
	// Started mirrors runner._overlap_restart_successor_started; when
	// true, StartedPID mirrors runner._overlap_restart_successor_pid.
	Started    bool `json:"started"`
	StartedPID int  `json:"started_pid"`

	ViaService     bool `json:"via_service"`
	PredecessorPID int  `json:"predecessor_pid"`

	Adapters []AdapterSpec `json:"adapters"`

	// Config is the user's config.yaml content — including stale
	// gateway.restart_platforms leftovers. The decision never reads it;
	// it exists so the shared table can pin that no key disables the
	// spawn.
	Config map[string]any `json:"config"`
}

// AdapterSpec is one platform adapter as spawn_successor sees it.
type AdapterSpec struct {
	Name string `json:"name"`

	// Supports mirrors supports_overlap_restart (must be exactly True).
	Supports bool `json:"supports"`
	// HasSpawn marks a callable spawn_overlap_successor.
	HasSpawn bool `json:"has_spawn"`

	// SpawnPID is what spawn returns; nil mirrors Python None (the
	// adapter yields no successor and the scan continues).
	SpawnPID *int `json:"spawn_pid"`
	// SpawnError marks a spawn that raised after Popen; LatchedPID then
	// mirrors adapter._last_overlap_successor_pid.
	SpawnError bool `json:"spawn_error"`
	LatchedPID *int `json:"latched_pid"`

	// ServiceManaged mirrors _last_overlap_successor_service_managed.
	ServiceManaged bool `json:"service_managed"`
	// HasMarkReady marks the wait_for/mark_overlap_successor_ready
	// callables every capable adapter receives after a clean spawn.
	HasMarkReady bool `json:"has_mark_ready"`
}

// Results of Decide. Wire-stable: the shared table and cmd/restartsuccessor
// emit these names.
const (
	ResultSpawned                 = "spawned"
	ResultReadinessFailedRetained = "readiness-failed-retained"
	ResultAlreadyStarted          = "already-started"
	ResultLegacyDrain             = "legacy-drain"
	ResultError                   = "error"
)

// Outcome is the observed decision for one Scenario.
type Outcome struct {
	Result          string   `json:"result"`
	PID             *int     `json:"pid"`
	Started         bool     `json:"started"`
	SpawnAttempted  []string `json:"spawn_attempted"`
	ReadinessMarked []string `json:"readiness_marked"`
	ServiceManaged  bool     `json:"service_managed"`
}

// newOutcome pre-initializes the list fields so every return path is
// JSON-round-trip identical to the shared table ([] vs null).
func newOutcome() Outcome {
	return Outcome{SpawnAttempted: []string{}, ReadinessMarked: []string{}}
}

// Decide runs the always-spawn policy over one scenario, mirroring
// gateway.overlap_restart.spawn_successor decision for decision.
func Decide(s Scenario) Outcome {
	if s.Started {
		pid := s.StartedPID
		return Outcome{
			Result:          ResultAlreadyStarted,
			PID:             &pid,
			Started:         true,
			SpawnAttempted:  []string{},
			ReadinessMarked: []string{},
		}
	}
	out := newOutcome()
	for _, a := range s.Adapters {
		if !a.Supports || !a.HasSpawn {
			continue
		}
		out.SpawnAttempted = append(out.SpawnAttempted, a.Name)
		readinessFailed := false
		var pid int
		switch {
		case a.SpawnError:
			if a.LatchedPID == nil {
				out.Result = ResultError
				return out
			}
			pid = *a.LatchedPID
			readinessFailed = true
		case a.SpawnPID != nil:
			pid = *a.SpawnPID
		default:
			// Python None: this adapter yields no successor; scan on.
			continue
		}
		out.Started = true
		out.PID = &pid
		out.ServiceManaged = a.ServiceManaged
		if !readinessFailed {
			for _, c := range s.Adapters {
				if c.Supports && c.HasMarkReady {
					out.ReadinessMarked = append(out.ReadinessMarked, c.Name)
				}
			}
		}
		if readinessFailed {
			out.Result = ResultReadinessFailedRetained
		} else {
			out.Result = ResultSpawned
		}
		return out
	}
	out.Result = ResultLegacyDrain
	return out
}
