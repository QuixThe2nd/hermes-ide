// Package restartplan is the Go twin of the gateway restart-transport
// decision in gateway/restart.py (restart_transport_plan): given a restart
// trigger and the live host conditions, every restart door — the /restart
// slash command, the agent restart tool, the control-socket restart verb,
// and the SIGUSR1 signal entry `hermes update` and `hermes gateway restart`
// use — must derive the SAME transport flags. This is the canonical
// statement of the contract; the Python function is what the runtime
// executes (deployments carry no Go toolchain, the same arrangement as
// every other internal/* twin). cmd/restartplan is the executable form: the
// Python contract test (tests/gateway/test_restart_plan_contract.py) builds
// it and asserts identical decisions for every shared-table record in
// testdata/restart_cases.json, and the Go tests run the same table as a
// second cross-check. Production Python never shells out to it.
//
// Contract (shared with the Python twin):
//
//   - Triggers, exactly: "user" (a requester-driven restart: /restart, the
//     restart tool, the control-socket verb) and "update_signal" (SIGUSR1
//     from `hermes update` after the code swap, or `hermes gateway restart`'s
//     signal fallback). The pause-for-update pre-swap drain is NOT a trigger
//     here: it is deliberately overlap-less (a successor spawned before the
//     swap would run the old tree) and lives outside this decision.
//   - The trigger NEVER changes the transport decision. Only host conditions
//     do. This is the invariant the update path regressed on: a hardcoded
//     via_service=true for SIGUSR1 left every manually-launched gateway
//     draining successor-less — new events were refused with the
//     "not accepting new work" notice and the gateway exited expecting a
//     supervisor that did not exist.
//   - via_service is true when a manager with a restart policy owns this
//     gateway (systemd INVOCATION_ID, launchd XPC_SERVICE_NAME, the s6
//     sentinel, the explicit --external-supervisor opt-in — the Python twin's
//     is_gateway_supervisor_process) or the gateway runs inside a container
//     with a restart policy (is_container_restart_context). The process then
//     refuses new turns, drains, exits 75, and lets the supervisor/policy
//     relaunch it; a detached child would die with the cgroup instead.
//   - detached is the complement of via_service: on an unsupervised host the
//     dying process is the only one that can launch its own replacement, so
//     it arms the detached replay (`hermes gateway restart` after this PID
//     exits) when no overlap successor took over first.
//   - spawns_successor states whether an overlap successor is expected to
//     take over the platform transports during the drain. True on a
//     supervised host: the successor is spawned INSIDE the service unit so
//     the supervisor's MAINPID can move to it (the adapter keys on the same
//     systemd marker is_gateway_supervisor_process reads). True on an
//     unsupervised, non-container host: the hub adapters spawn the successor
//     over their hub RPC and the predecessor exits 0 behind it. False only in
//     a container without a service manager: the restart policy owns the
//     relaunch and a hub child would belong to the wrong cgroup.
//   - An unknown trigger is a caller bug, not a fallback: Decide reports it
//     as an error and the Python twin raises, rather than guessing a
//     transport for a door that does not exist.
package restartplan

import (
	"errors"
	"fmt"
)

// Trigger names the door a restart request arrived through.
type Trigger string

const (
	// TriggerUser is a requester-driven restart: /restart, the agent
	// restart tool, or the control-socket restart verb.
	TriggerUser Trigger = "user"
	// TriggerUpdateSignal is the SIGUSR1 entry: `hermes update` after the
	// code swap, or `hermes gateway restart`'s signal fallback.
	TriggerUpdateSignal Trigger = "update_signal"
)

// Plan is the transport decision shared by every restart door.
type Plan struct {
	// ViaService exits to the supervisor/container restart policy (75).
	ViaService bool `json:"via_service"`
	// Detached arms this process's own detached replacement replay.
	Detached bool `json:"detached"`
	// SpawnsSuccessor expects an overlap successor during the drain.
	SpawnsSuccessor bool `json:"spawns_successor"`
}

// ParseTrigger validates a wire trigger string.
func ParseTrigger(raw string) (Trigger, error) {
	switch Trigger(raw) {
	case TriggerUser, TriggerUpdateSignal:
		return Trigger(raw), nil
	default:
		return "", fmt.Errorf("restartplan: unknown trigger %q (want %q or %q)",
			raw, TriggerUser, TriggerUpdateSignal)
	}
}

// Exit codes shared with the Python twin (gateway/restart.py).
const (
	// ServiceRestartExitCode (75) asks the supervisor or container
	// restart policy to relaunch this gateway.
	ServiceRestartExitCode = 75
	// OverlapRestartExitCode (76) tells the supervisor a live overlap
	// successor already owns the unit (after a MAINPID handover).
	OverlapRestartExitCode = 76
)

// ExitCode is the teardown half of the plan: what this dying process exits
// with so exactly one replacement gateway exists afterwards.
type ExitCode struct {
	// Code is 75 (service restart), 76 (overlap handover), or 0 for an
	// ordinary clean exit.
	Code int `json:"code"`
	// Special is false for the ordinary clean exit — Code 0 then carries
	// no supervisor meaning.
	Special bool `json:"special"`
}

// ResolveExitCode decides the planned exit code at teardown.
//
// restartRequested is this process's committed restart; viaService and
// successorStarted come from the plan's execution (a successor may have
// been spawned even when the request said via_service=false); supervised
// re-checks live supervision, because a hub successor started by a
// detached-flagged request still needs 76 under systemd — exiting 0 there
// would make the supervisor start another gateway beside the successor,
// and exiting 75 without a successor would kill the fleet unit while the
// detached replay races it. An unsupervised, non-service restart exits
// cleanly: the successor or the detached replay already owns the home.
//
// newerLiveGeneration is generation-array awareness (#844): a live NEWER
// generation (hub registry) keeps the overlap exit code even when this
// process's own successor died — the lineage continues in the newer
// process and the supervisor must not start a second gateway. It matters
// only on the service route; a manual host's exit stays clean either way.
func ResolveExitCode(restartRequested, viaService, successorStarted, supervised, newerLiveGeneration bool) ExitCode {
	if !restartRequested {
		return ExitCode{}
	}
	if !viaService && !(successorStarted && supervised) {
		return ExitCode{}
	}
	if successorStarted {
		return ExitCode{Code: OverlapRestartExitCode, Special: true}
	}
	if viaService && newerLiveGeneration {
		return ExitCode{Code: OverlapRestartExitCode, Special: true}
	}
	return ExitCode{Code: ServiceRestartExitCode, Special: true}
}

// ErrProbeUnanswered reports that the newer-live-generation probe failed
// where its answer would decide the exit code.
var ErrProbeUnanswered = errors.New("restartplan: newer-generation probe failed where its answer decides the exit")

// ResolveExitCodeProbeFailed decides the planned exit code when the
// newer-live-generation probe (the hub mirror read) raised instead of
// answering. The Python twin is resolve_planned_restart_exit_code's
// probe-error path.
//
// The probe's answer matters only when it would flip a service restart
// (75) into the overlap exit (76). Everywhere else both answers give the
// same code, and that code stands; the caller logs the failure. The key
// case is a started successor: it already decides 76, so a broken probe
// must not abort teardown into the service-restart fallback. That
// fallback exits 75, and systemd (KillMode=mixed) then kills the live
// successor and its in-flight turns along with the unit. Where the answer
// does decide the code, guessing either way risks a duplicate or a dead
// fleet, so the failure is returned to the caller.
func ResolveExitCodeProbeFailed(restartRequested, viaService, successorStarted, supervised bool) (ExitCode, error) {
	absent := ResolveExitCode(restartRequested, viaService, successorStarted, supervised, false)
	present := ResolveExitCode(restartRequested, viaService, successorStarted, supervised, true)
	if absent != present {
		return ExitCode{}, ErrProbeUnanswered
	}
	return absent, nil
}

// Decide derives the transport plan for one restart request.
//
// supervised mirrors is_gateway_supervisor_process (a manager with a
// restart policy owns this gateway); container mirrors
// is_container_restart_context. The trigger is accepted for contract
// completeness and MUST NOT influence the outcome: parity across triggers
// is asserted by both twins' tests from the shared table.
func Decide(trigger Trigger, supervised, container bool) (Plan, error) {
	if _, err := ParseTrigger(string(trigger)); err != nil {
		return Plan{}, err
	}
	viaService := supervised || container
	return Plan{
		ViaService:      viaService,
		Detached:        !viaService,
		SpawnsSuccessor: supervised || !viaService,
	}, nil
}
