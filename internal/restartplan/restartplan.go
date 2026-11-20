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

// SuccessorIdentity is the /proc-backed identity answer for the process a
// draining predecessor spawned: shared with the Python twin's
// _overlap_successor_identity_state, which compares the successor's PID
// start-time fingerprint against the one recorded at spawn.
type SuccessorIdentity string

const (
	// IdentityNone: no successor was started (or its PID is unknown to us).
	IdentityNone SuccessorIdentity = "none"
	// IdentityLive: the PID exists and the start-time fingerprint matches —
	// the successor we spawned is still that process.
	IdentityLive SuccessorIdentity = "live"
	// IdentityDead: the PID is gone, or a different incarnation now owns it
	// (PID reuse — the fingerprint mismatch proves our successor ended).
	IdentityDead SuccessorIdentity = "dead"
	// IdentityUnknown: the PID exists but cannot be fingerprinted (missing
	// start time on either side). Never guessed about.
	IdentityUnknown SuccessorIdentity = "unknown"
)

// HardExitAction is what a teardown backstop — the thread shutdown watchdog,
// the loop-liveness watchdog, any hard os._exit on the predecessor — must do
// at its exit point. The 2026-11-20 incident: a draining predecessor wedged
// in stop(), the shutdown watchdog fired after 60s and os._exit(1)-ed; under
// Restart=always + KillMode=mixed systemd read exit 1 as unit failure, killed
// the whole cgroup — including the live, authoritative successor serving
// turns — and restarted the unit beside whatever survived. The armed code of
// a backstop is a unit-level instruction to the supervisor; when a verified
// successor carries the restart line, that instruction must be the overlap
// handover (or nothing at all).
type HardExitAction string

const (
	// HardExitRequested: nothing verifiable protects the line — the armed
	// exit code stands (the supervisor may restart the unit).
	HardExitRequested HardExitAction = "requested"
	// HardExitOverlap: a verified live successor (or newer live generation)
	// owns the line — exit through the overlap code (76) so the supervisor
	// stands down for it instead of restarting the unit.
	HardExitOverlap HardExitAction = "overlap"
	// HardExitPark: do not exit. Both armed codes order a unit restart that
	// kills the successor's cgroup, so beside a successor we cannot safely
	// hand over to, staying alive is strictly better: the backstop waits and
	// re-plans until the successor dies, the MAINPID transfer lands, or
	// teardown completes.
	HardExitPark HardExitAction = "park"
)

// ResolveHardExitAction decides what a hard-exit backstop does with the
// successor evidence in hand. It is the twin of the Python resolver's pure
// core (gateway/overlap_restart.py::hard_exit_action); the imperative half —
// reading /proc, probing the registry mirror, performing the systemd MAINPID
// transfer — is what the Python runtime executes.
//
// successorIdentity comes from the identity state above. systemdManaged is
// whether the successor lives in this unit's cgroup and therefore needs the
// MAINPID handover before exit 76 is safe (an unmanaged successor — hub
// spawn on a manual host — needs nothing). mainpidHandoverDone is whether
// that transfer already succeeded.
//
// The rules, each earned by a failure mode:
//
//   - live + (unmanaged OR handover done) → overlap: the successor provably
//     owns the line and the supervisor can be told.
//   - live + managed + handover not done → park: exiting 76 without the
//     transfer leaves an untracked successor the supervisor may duplicate;
//     exiting the armed code kills it. Neither is acceptable; park and retry.
//   - unknown → park: an unfingerprintable successor is never guessed about.
//   - dead/none + newer live generation → overlap: our successor is gone but
//     a NEWER generation (a forwarded restart, a forked line) still lives —
//     serial supervisor replacement would start a duplicate beside it.
//   - dead/none + no newer generation → requested: the ordinary case; the
//     supervisor's restart is exactly what we want.
//
// An unrecognized identity string is treated as unknown (park): the cost of
// parking is a stuck process an operator can kill by PID; the cost of a
// wrong exit is a killed successor or a duplicate gateway.
func ResolveHardExitAction(
	successorIdentity string, newerLiveGeneration, systemdManaged, mainpidHandoverDone bool,
) HardExitAction {
	switch SuccessorIdentity(successorIdentity) {
	case IdentityLive:
		if !systemdManaged || mainpidHandoverDone {
			return HardExitOverlap
		}
		return HardExitPark
	case IdentityDead, IdentityNone: // the registry mirror decides
		if newerLiveGeneration {
			return HardExitOverlap
		}
		return HardExitRequested
	default: // unknown or unrecognized: never guessed about
		return HardExitPark
	}
}

// ResolveRunnerHardExit is the twin of the Python runner resolver
// (gateway/overlap_restart.py::resolve_hard_exit_action): the
// evidence-selection rules that turn a draining runner's synchronous state
// into the inputs of ResolveHardExitAction. The Python half gathers the
// evidence (reading /proc, probing the registry mirror, attempting the
// systemd MAINPID transfer — never the wedged event loop); this states
// WHICH evidence may decide:
//
//   - no started successor (or its PID unknown) → identity none; the
//     registry mirror decides alone (a newer live generation may carry the
//     line the never-spawned successor would have).
//   - dead → the mirror decides alone, for the same reason.
//   - live or unknown → the runner's own successor state is the only
//     evidence: the mirror is not even read. No mirror answer can change
//     the action (live either hands over or parks on the transfer, unknown
//     parks), so a stale mirror must not be able to flip either.
//
// transferSucceeded is the managed-live path's combined outcome: the MAINPID
// transfer landed AND a second identity read still said live (the re-check
// that keeps a successor which died during the transfer from authorizing
// exit 76). It is honored only for a live identity — the resolver attempts
// the transfer for nothing else, so no other identity can arrive with a
// handover receipt.
func ResolveRunnerHardExit(
	successorStarted bool, identity string, mirrorNewerLive, systemdManaged, transferSucceeded bool,
) HardExitAction {
	if !successorStarted {
		return ResolveHardExitAction(string(IdentityNone), mirrorNewerLive, false, false)
	}
	if SuccessorIdentity(identity) == IdentityDead {
		return ResolveHardExitAction(string(IdentityDead), mirrorNewerLive, false, false)
	}
	handover := transferSucceeded && SuccessorIdentity(identity) == IdentityLive
	return ResolveHardExitAction(identity, false, systemdManaged, handover)
}

// HardExitCode maps a resolved hard-exit action to the code the backstop
// must exit with. It is the twin of the Python watchdog helper
// (_planned_hard_exit_code in gateway/shutdown_watchdog.py): "overlap"
// carries the handover exit regardless of what the backstop was armed with
// (arming happens before the drain, when no successor state is known);
// "requested" keeps the armed code; "park" does not exit at all.
//
// The returned ok=false means PARK: the caller (a watchdog thread) must not
// exit the process — both armed codes order a unit restart that kills the
// live successor's cgroup, so the backstop waits and re-plans instead.
func HardExitCode(action HardExitAction, armedExitCode int) (code int, ok bool) {
	switch action {
	case HardExitOverlap:
		return OverlapRestartExitCode, true
	case HardExitRequested:
		return armedExitCode, true
	default: // park (and any unrecognized action: never guess an exit)
		return 0, false
	}
}

// NotifyStoppingOnAction reports whether a draining predecessor may emit
// sd_notify STOPPING=1, given the hard-exit action its successor evidence
// resolves to. It is the twin of the Python stop-notification gate
// (gateway/overlap_restart.py::stopping_notify_on_action).
//
// The 2026-11-21 incident: the predecessor's teardown stopped the systemd
// watchdog at the head of its drain, and SystemdWatchdog.stop emitted
// STOPPING=1 while the predecessor was still MAINPID. For a Type=notify
// unit that datagram is a unit-level instruction — systemd flipped the
// still-active unit to deactivating (stop-sigterm) with no stop job, and
// when the overlap successor later took MAINPID the unit STAYED
// deactivating: READY=1 is ignored in stop states, so TimeoutStopSec
// (24h10min on the incident unit) kept running against the cgroup the
// live successor serves in, ending in a SIGTERM/SIGKILL of the successor
// and its in-flight turns. An armed stop notification is the same class
// of hazard as an armed exit code, so the same action decides it:
//
//   - requested → notify: nothing verifiable carries the line (a normal
//     stop, or a non-overlap restart the supervisor will replace). This
//     is the only action that permits the datagram.
//   - overlap → silent: a verified live successor (or newer live
//     generation) owns the line.
//   - park → silent: an unfingerprintable successor may own it; silence
//     cannot order the unit torn down, STOPPING=1 can.
func NotifyStoppingOnAction(action HardExitAction) bool {
	return action == HardExitRequested
}

// ResolveRunnerNotifyStopping is the evidence-selection twin of the Python
// runner gate (gateway/overlap_restart.py::stopping_notify_permitted):
// whether THIS draining runner may tell systemd it is stopping. It derives
// the answer from the same resolver evidence that picks the hard-exit
// action (ResolveRunnerHardExit), gathered read-only — unlike the
// hard-exit resolver it performs no MAINPID transfer, because the teardown
// exit boundary does that; the stop head only needs to know whether the
// unit may be told to stop.
//
// systemdManaged and the transfer outcome are passed false deliberately:
// they only separate "overlap" from "park", and the stop notification is
// suppressed for both — a managed live successor whose handover has not
// landed yet is exactly the incident's shape.
func ResolveRunnerNotifyStopping(
	successorStarted bool, identity string, mirrorNewerLive bool,
) bool {
	return ResolveRunnerHardExit(successorStarted, identity, mirrorNewerLive, false, false) ==
		HardExitRequested
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
