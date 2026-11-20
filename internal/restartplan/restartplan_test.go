package restartplan

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// sharedCase is one record of testdata/restart_cases.json — the same table
// the Python contract test (tests/gateway/test_restart_plan_contract.py)
// runs against gateway.restart.restart_transport_plan and the built
// cmd/restartplan binary.
type sharedCase struct {
	Name string `json:"name"`
	Op   string `json:"op"`
	// decide inputs
	Trigger    string `json:"trigger"`
	Supervised bool   `json:"supervised"`
	Container  bool   `json:"container"`
	ExpectPlan *Plan  `json:"expect"`
	// exit_code inputs (Supervised is shared; Trigger/Container unused)
	RestartRequested    bool `json:"restart_requested"`
	ViaService          bool `json:"via_service"`
	SuccessorStarted    bool `json:"successor_started"`
	NewerLiveGeneration bool `json:"newer_live_generation"`
	ExpectExitCode      *int `json:"expect_exit_code"`
	// probe_failed rows: the newer-generation probe raised instead of
	// answering; expect_probe_error marks a row whose decision is an error.
	ProbeFailed      bool `json:"probe_failed"`
	ExpectProbeError bool `json:"expect_probe_error"`
	// hard_exit inputs (the watchdog-exit decision): the successor's
	// /proc identity, whether the registry mirror shows a newer live
	// generation, and the systemd MAINPID handover state.
	SuccessorIdentity   string `json:"successor_identity"`
	SystemdManaged      bool   `json:"systemd_managed"`
	MainpidHandoverDone bool   `json:"mainpid_handover_done"`
	ExpectAction        string `json:"expect_action"`
	// hard_exit_runner inputs (the resolver's evidence selection):
	// SuccessorStarted/NewerLiveGeneration/SystemdManaged are shared; the
	// transfer is the resolver-layer input whose combined outcome feeds
	// the hard_exit rows' mainpid_handover_done.
	MainpidTransferSucceeds bool `json:"mainpid_transfer_succeeds"`
	// hard_exit_code inputs: the resolved action plus the code the backstop
	// was armed with, and the expected exit (expect_exited=false is PARK).
	Action        string `json:"action"`
	ArmedExitCode int    `json:"armed_exit_code"`
	ExpectCode    *int   `json:"expect_code"`
	ExpectExited  bool   `json:"expect_exited"`
}

func loadSharedCases(t *testing.T) []sharedCase {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "restart_cases.json"))
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		Cases []json.RawMessage `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared table has no cases")
	}
	var cases []sharedCase
	for _, encoded := range table.Cases {
		var tc sharedCase
		if err := json.Unmarshal(encoded, &tc); err != nil {
			t.Fatalf("parse case %s: %v", encoded, err)
		}
		cases = append(cases, tc)
	}
	return cases
}

// TestSharedTable pins all five decisions against the shared table: the
// transport rows (op "decide"), the planned-exit rows (op "exit_code"), the
// watchdog hard-exit rows (op "hard_exit"), the resolver's evidence-selection
// rows (op "hard_exit_runner"), and the action→exit-code rows
// (op "hard_exit_code").
func TestSharedTable(t *testing.T) {
	for _, tc := range loadSharedCases(t) {
		t.Run(tc.Name, func(t *testing.T) {
			switch tc.Op {
			case "decide":
				trigger, err := ParseTrigger(tc.Trigger)
				if err != nil {
					t.Fatalf("trigger: %v", err)
				}
				got, err := Decide(trigger, tc.Supervised, tc.Container)
				if err != nil {
					t.Fatalf("Decide: %v", err)
				}
				if tc.ExpectPlan == nil {
					t.Fatalf("decide case %q has no plan expectation", tc.Name)
				}
				if got != *tc.ExpectPlan {
					t.Fatalf("Decide(%s, supervised=%t, container=%t) = %+v, want %+v",
						tc.Trigger, tc.Supervised, tc.Container, got, *tc.ExpectPlan)
				}
			case "exit_code":
				checkExitCodeCase(t, tc)
			case "hard_exit":
				if tc.ExpectAction == "" {
					t.Fatalf("hard_exit case %q has no action expectation", tc.Name)
				}
				got := ResolveHardExitAction(tc.SuccessorIdentity, tc.NewerLiveGeneration, tc.SystemdManaged, tc.MainpidHandoverDone)
				if string(got) != tc.ExpectAction {
					t.Fatalf("ResolveHardExitAction(%q, newer_live=%t, managed=%t, handover=%t) = %q, want %q",
						tc.SuccessorIdentity, tc.NewerLiveGeneration, tc.SystemdManaged, tc.MainpidHandoverDone, got, tc.ExpectAction)
				}
			case "hard_exit_runner":
				if tc.ExpectAction == "" {
					t.Fatalf("hard_exit_runner case %q has no action expectation", tc.Name)
				}
				got := ResolveRunnerHardExit(tc.SuccessorStarted, tc.SuccessorIdentity, tc.NewerLiveGeneration, tc.SystemdManaged, tc.MainpidTransferSucceeds)
				if string(got) != tc.ExpectAction {
					t.Fatalf("ResolveRunnerHardExit(started=%t, %q, mirror_newer=%t, managed=%t, transfer=%t) = %q, want %q",
						tc.SuccessorStarted, tc.SuccessorIdentity, tc.NewerLiveGeneration, tc.SystemdManaged, tc.MainpidTransferSucceeds, got, tc.ExpectAction)
				}
			case "hard_exit_code":
				code, exited := HardExitCode(HardExitAction(tc.Action), tc.ArmedExitCode)
				if exited != tc.ExpectExited || (tc.ExpectExited && code != *tc.ExpectCode) {
					t.Fatalf("HardExitCode(%q, armed=%d) = (%d, exited=%t); want exited=%t code=%d",
						tc.Action, tc.ArmedExitCode, code, exited, tc.ExpectExited, deref(tc.ExpectCode))
				}
			default:
				t.Fatalf("unknown op %q", tc.Op)
			}
		})
	}
}

func checkExitCodeCase(t *testing.T, tc sharedCase) {
	t.Helper()
	if tc.ProbeFailed {
		checkProbeFailedCase(t, tc)
		return
	}
	got := ResolveExitCode(tc.RestartRequested, tc.ViaService, tc.SuccessorStarted, tc.Supervised, tc.NewerLiveGeneration)
	special := tc.ExpectExitCode != nil
	if got.Special != special || (special && got.Code != *tc.ExpectExitCode) {
		t.Fatalf("ResolveExitCode(restart=%t, via_service=%t, successor=%t, supervised=%t, newer_live=%t) = %+v, want exit %d special=%t",
			tc.RestartRequested, tc.ViaService, tc.SuccessorStarted, tc.Supervised, tc.NewerLiveGeneration, got, deref(tc.ExpectExitCode), special)
	}
}

func checkProbeFailedCase(t *testing.T, tc sharedCase) {
	t.Helper()
	got, err := ResolveExitCodeProbeFailed(tc.RestartRequested, tc.ViaService, tc.SuccessorStarted, tc.Supervised)
	if tc.ExpectProbeError {
		if !errors.Is(err, ErrProbeUnanswered) {
			t.Fatalf("ResolveExitCodeProbeFailed(%+v) = %+v, %v; want ErrProbeUnanswered", tc, got, err)
		}
		return
	}
	if err != nil {
		t.Fatalf("ResolveExitCodeProbeFailed(%+v): unexpected error %v", tc, err)
	}
	special := tc.ExpectExitCode != nil
	if got.Special != special || (special && got.Code != *tc.ExpectExitCode) {
		t.Fatalf("ResolveExitCodeProbeFailed(%+v) = %+v, want exit %d special=%t",
			tc, got, deref(tc.ExpectExitCode), special)
	}
}

// TestProbeFailureNeverKillsStartedSuccessor is the bug this path exists
// for: a broken newer-generation probe must never turn a started successor's
// handover into the service restart (75) that kills it with the unit.
func TestProbeFailureNeverKillsStartedSuccessor(t *testing.T) {
	for _, viaService := range []bool{false, true} {
		for _, supervised := range []bool{false, true} {
			if !viaService && !supervised {
				continue // unsupervised manual restart: the successor owns the home, exit is clean
			}
			got, err := ResolveExitCodeProbeFailed(true, viaService, true, supervised)
			if err != nil || got.Code != OverlapRestartExitCode || !got.Special {
				t.Fatalf("via_service=%t supervised=%t: got %+v, %v; want exit %d",
					viaService, supervised, got, err, OverlapRestartExitCode)
			}
		}
	}
}

// TestProbeFailureFailsOnlyWhereProbeDecides pins the probe-failed path
// against ResolveExitCode over the full input space: it fails exactly where
// the two probe answers disagree, and it never invents a code that neither
// answer would give.
func TestProbeFailureFailsOnlyWhereProbeDecides(t *testing.T) {
	for _, restartRequested := range []bool{false, true} {
		for _, viaService := range []bool{false, true} {
			for _, successorStarted := range []bool{false, true} {
				for _, supervised := range []bool{false, true} {
					absent := ResolveExitCode(restartRequested, viaService, successorStarted, supervised, false)
					present := ResolveExitCode(restartRequested, viaService, successorStarted, supervised, true)
					got, err := ResolveExitCodeProbeFailed(restartRequested, viaService, successorStarted, supervised)
					if (absent != present) != errors.Is(err, ErrProbeUnanswered) {
						t.Fatalf("restart=%t via_service=%t successor=%t supervised=%t: err=%v, answers %+v vs %+v",
							restartRequested, viaService, successorStarted, supervised, err, absent, present)
					}
					if err == nil && got != absent {
						t.Fatalf("probe-irrelevant decision diverged: got %+v, want %+v", got, absent)
					}
				}
			}
		}
	}
}

func deref(p *int) int {
	if p == nil {
		return 0
	}
	return *p
}

// TestTriggerNeverChangesTransport is the core invariant: the update signal
// must ride the same transport as a user restart under identical host
// conditions. The update path regressed on exactly this — a hardcoded
// via_service left manual hosts draining successor-less.
func TestTriggerNeverChangesTransport(t *testing.T) {
	for _, supervised := range []bool{false, true} {
		for _, container := range []bool{false, true} {
			userPlan, err := Decide(TriggerUser, supervised, container)
			if err != nil {
				t.Fatalf("Decide(user): %v", err)
			}
			signalPlan, err := Decide(TriggerUpdateSignal, supervised, container)
			if err != nil {
				t.Fatalf("Decide(update_signal): %v", err)
			}
			if userPlan != signalPlan {
				t.Fatalf("transport drifted across triggers at supervised=%t container=%t: user=%+v update_signal=%+v",
					supervised, container, userPlan, signalPlan)
			}
		}
	}
}

// TestPlanInternalConsistency pins the structural relations between the
// flags rather than each value in isolation: detached is via_service's
// complement (exactly one replacement owner), and a successor is expected
// everywhere except the container-without-service-manager exit.
func TestPlanInternalConsistency(t *testing.T) {
	for _, supervised := range []bool{false, true} {
		for _, container := range []bool{false, true} {
			plan, err := Decide(TriggerUser, supervised, container)
			if err != nil {
				t.Fatalf("Decide: %v", err)
			}
			if plan.Detached == plan.ViaService {
				t.Fatalf("detached must complement via_service, got %+v", plan)
			}
			if want := supervised || !plan.ViaService; plan.SpawnsSuccessor != want {
				t.Fatalf("spawns_successor = %t, want %t (supervised=%t container=%t)",
					plan.SpawnsSuccessor, want, supervised, container)
			}
		}
	}
}

// TestExitCodeExactlyOneReplacement pins the teardown invariant: a special
// exit code is emitted only when a manager must act (relaunch us, or stand
// down for our successor); every unsupervised outcome exits cleanly because
// the successor or detached replay already owns the home.
func TestExitCodeExactlyOneReplacement(t *testing.T) {
	for _, restartRequested := range []bool{false, true} {
		for _, viaService := range []bool{false, true} {
			for _, successorStarted := range []bool{false, true} {
				for _, supervised := range []bool{false, true} {
					for _, newerLive := range []bool{false, true} {
						got := ResolveExitCode(restartRequested, viaService, successorStarted, supervised, newerLive)
						switch {
						case !restartRequested || (!viaService && !(successorStarted && supervised)):
							if got.Special {
								t.Fatalf("unsupervised outcome must exit cleanly, got %+v (via_service=%t successor=%t supervised=%t)",
									got, viaService, successorStarted, supervised)
							}
						case successorStarted:
							if !got.Special || got.Code != OverlapRestartExitCode {
								t.Fatalf("successor handover must exit %d, got %+v", OverlapRestartExitCode, got)
							}
						case viaService && newerLive:
							if !got.Special || got.Code != OverlapRestartExitCode {
								t.Fatalf("newer live generation must keep the overlap exit %d, got %+v", OverlapRestartExitCode, got)
							}
						default:
							if !got.Special || got.Code != ServiceRestartExitCode {
								t.Fatalf("service restart must exit %d, got %+v", ServiceRestartExitCode, got)
							}
						}
					}
				}
			}
		}
	}
}

// TestUnknownTriggerFails pins boundary validation: an unrecognized door is
// a caller bug, never a guessed transport.
func TestUnknownTriggerFails(t *testing.T) {
	for _, raw := range []string{"", "pause_for_update", "cron", "USER"} {
		if _, err := ParseTrigger(raw); err == nil {
			t.Fatalf("ParseTrigger(%q) unexpectedly succeeded", raw)
		}
		if _, err := Decide(Trigger(raw), false, false); err == nil {
			t.Fatalf("Decide(%q) unexpectedly succeeded", raw)
		}
	}
}

// hardExitRow is one named cell of the exhaustive hard-exit decision space.
// The shared table (testdata/restart_cases.json) carries the representative
// rows both twins run; this table walks the FULL space — every identity ×
// newer-live × managed × handover combination — because the failure this
// decision guards against (a backstop exit that kills a live successor's
// cgroup) is one wrong cell away, and cells the shared table happens not to
// list drift silently.
type hardExitRow struct {
	identity     string
	newerLive    bool
	managed      bool
	handoverDone bool
	want         HardExitAction
	why          string
}

var hardExitSpace = []hardExitRow{
	// LIVE successor: the line is carried; the only question is whether the
	// supervisor can be told about it safely.
	{identity: "live", newerLive: false, managed: false, handoverDone: false, want: HardExitOverlap,
		why: "unmanaged (hub-spawn) successor needs no MAINPID transfer: 76 is safe at once"},
	{identity: "live", newerLive: false, managed: false, handoverDone: true, want: HardExitOverlap,
		why: "handover flag is irrelevant for an unmanaged successor"},
	{identity: "live", newerLive: true, managed: false, handoverDone: false, want: HardExitOverlap,
		why: "own live successor decides; a newer generation changes nothing"},
	{identity: "live", newerLive: true, managed: false, handoverDone: true, want: HardExitOverlap,
		why: "own live successor decides; a newer generation changes nothing"},
	{identity: "live", newerLive: false, managed: true, handoverDone: false, want: HardExitPark,
		why: "the incident cell: armed codes kill the cgroup, 76 without transfer strands an untracked successor — park and retry"},
	{identity: "live", newerLive: true, managed: true, handoverDone: false, want: HardExitPark,
		why: "a newer generation does not make an un-transferred unit successor safe to abandon"},
	{identity: "live", newerLive: false, managed: true, handoverDone: true, want: HardExitOverlap,
		why: "transfer landed and identity re-verified: the supervisor stands down for the successor"},
	{identity: "live", newerLive: true, managed: true, handoverDone: true, want: HardExitOverlap,
		why: "transfer landed and identity re-verified: the supervisor stands down for the successor"},
	// UNKNOWN successor: never guessed about in either direction.
	{identity: "unknown", newerLive: false, managed: false, handoverDone: false, want: HardExitPark,
		why: "unfingerprintable PID: exiting risks killing (or duplicating) a successor we cannot name"},
	{identity: "unknown", newerLive: false, managed: false, handoverDone: true, want: HardExitPark,
		why: "a handover receipt for a PID we cannot fingerprint proves nothing"},
	{identity: "unknown", newerLive: false, managed: true, handoverDone: false, want: HardExitPark,
		why: "management status of an unverifiable successor is not evidence"},
	{identity: "unknown", newerLive: false, managed: true, handoverDone: true, want: HardExitPark,
		why: "management status of an unverifiable successor is not evidence"},
	{identity: "unknown", newerLive: true, managed: false, handoverDone: false, want: HardExitPark,
		why: "park is the only action that cannot kill the possibly-live successor"},
	{identity: "unknown", newerLive: true, managed: false, handoverDone: true, want: HardExitPark,
		why: "park is the only action that cannot kill the possibly-live successor"},
	{identity: "unknown", newerLive: true, managed: true, handoverDone: false, want: HardExitPark,
		why: "park is the only action that cannot kill the possibly-live successor"},
	{identity: "unknown", newerLive: true, managed: true, handoverDone: true, want: HardExitPark,
		why: "park is the only action that cannot kill the possibly-live successor"},
	// DEAD or NONE: our line ended; the registry mirror decides whether a
	// NEWER generation forbids the supervisor-restart exit.
	{identity: "dead", newerLive: false, managed: false, handoverDone: false, want: HardExitRequested,
		why: "ordinary serial replacement: the supervisor's restart is wanted"},
	{identity: "dead", newerLive: false, managed: false, handoverDone: true, want: HardExitRequested,
		why: "a stale handover receipt cannot revive a dead successor"},
	{identity: "dead", newerLive: false, managed: true, handoverDone: false, want: HardExitRequested,
		why: "ordinary serial replacement: the supervisor's restart is wanted"},
	{identity: "dead", newerLive: false, managed: true, handoverDone: true, want: HardExitRequested,
		why: "ordinary serial replacement: the supervisor's restart is wanted"},
	{identity: "dead", newerLive: true, managed: false, handoverDone: false, want: HardExitOverlap,
		why: "newer generation lives: a unit restart would start a duplicate beside it"},
	{identity: "dead", newerLive: true, managed: false, handoverDone: true, want: HardExitOverlap,
		why: "newer generation lives: a unit restart would start a duplicate beside it"},
	{identity: "dead", newerLive: true, managed: true, handoverDone: false, want: HardExitOverlap,
		why: "newer generation lives: a unit restart would start a duplicate beside it"},
	{identity: "dead", newerLive: true, managed: true, handoverDone: true, want: HardExitOverlap,
		why: "newer generation lives: a unit restart would start a duplicate beside it"},
	{identity: "none", newerLive: false, managed: false, handoverDone: false, want: HardExitRequested,
		why: "no successor ever started: the armed code stands"},
	{identity: "none", newerLive: false, managed: false, handoverDone: true, want: HardExitRequested,
		why: "no successor ever started: a handover receipt is impossible garbage"},
	{identity: "none", newerLive: false, managed: true, handoverDone: false, want: HardExitRequested,
		why: "no successor ever started: the armed code stands"},
	{identity: "none", newerLive: false, managed: true, handoverDone: true, want: HardExitRequested,
		why: "no successor ever started: the armed code stands"},
	{identity: "none", newerLive: true, managed: false, handoverDone: false, want: HardExitOverlap,
		why: "no own successor but a newer generation lives: refuse the restart exit"},
	{identity: "none", newerLive: true, managed: false, handoverDone: true, want: HardExitOverlap,
		why: "no own successor but a newer generation lives: refuse the restart exit"},
	{identity: "none", newerLive: true, managed: true, handoverDone: false, want: HardExitOverlap,
		why: "no own successor but a newer generation lives: refuse the restart exit"},
	{identity: "none", newerLive: true, managed: true, handoverDone: true, want: HardExitOverlap,
		why: "no own successor but a newer generation lives: refuse the restart exit"},
}

// TestHardExitFullSpace walks every combination of the decision inputs and
// pins the action for each, with the failure mode the cell guards against.
func TestHardExitFullSpace(t *testing.T) {
	seen := map[string]bool{}
	for _, row := range hardExitSpace {
		label := row.identity
		key := label + "|" + fmt.Sprint(row.newerLive, row.managed, row.handoverDone)
		if seen[key] {
			t.Fatalf("duplicate space cell %s", key)
		}
		seen[key] = true
		got := ResolveHardExitAction(row.identity, row.newerLive, row.managed, row.handoverDone)
		if got != row.want {
			t.Fatalf("ResolveHardExitAction(%s, newer=%t, managed=%t, handover=%t) = %s, want %s (%s)",
				row.identity, row.newerLive, row.managed, row.handoverDone, got, row.want, row.why)
		}
	}
	// 4 identities × 2 × 2 × 2 = the complete space, no holes.
	if len(seen) != 32 {
		t.Fatalf("space table has %d cells, want 32", len(seen))
	}
}

// TestHardExitNeverKillsALiveSuccessor is the incident invariant stated
// directly: for NO combination of the other inputs may a LIVE successor (or
// a live newer generation with our successor gone) produce "requested" — the
// action that lets the armed code order the cgroup-killing restart.
func TestHardExitNeverKillsALiveSuccessor(t *testing.T) {
	for _, row := range hardExitSpace {
		liveLine := row.identity == "live" || (row.newerLive && (row.identity == "dead" || row.identity == "none"))
		if !liveLine {
			continue
		}
		if row.want == HardExitRequested {
			t.Fatalf("live line (%s newer=%t) must never answer requested — %s",
				row.identity, row.newerLive, row.why)
		}
	}
}

// TestHardExitUnknownIdentityStringParks: identity strings come over the
// wire from the Python twin; anything unrecognized must fall to park, never
// to an exit-permitting guess.
func TestHardExitUnknownIdentityStringParks(t *testing.T) {
	for _, raw := range []string{"", "Live", "LIVE", "alive", "reused", "0"} {
		if got := ResolveHardExitAction(raw, false, false, false); got != HardExitPark {
			t.Fatalf("ResolveHardExitAction(%q) = %s, want park", raw, got)
		}
	}
}

// TestHardExitCodeNeverArmsAKillAgainstALiveSuccessor pins the mapping half
// of the invariant: whatever the backstop was armed with (1 from the
// shutdown watchdog, 75 from the loop-liveness watchdog — both order the
// cgroup-killing restart), an "overlap" action must translate to the
// handover code, and a "park" action must not exit at all. Only "requested"
// lets the armed code through.
func TestHardExitCodeNeverArmsAKillAgainstALiveSuccessor(t *testing.T) {
	for _, armed := range []int{0, 1, 75, 78, 255} {
		if code, exited := HardExitCode(HardExitOverlap, armed); !exited || code != OverlapRestartExitCode {
			t.Fatalf("overlap with armed=%d = (%d, exited=%t); want (%d, true)",
				armed, code, exited, OverlapRestartExitCode)
		}
		if _, exited := HardExitCode(HardExitPark, armed); exited {
			t.Fatalf("park with armed=%d must not exit", armed)
		}
		if code, exited := HardExitCode(HardExitRequested, armed); !exited || code != armed {
			t.Fatalf("requested with armed=%d = (%d, exited=%t); want (%d, true)",
				armed, code, exited, armed)
		}
	}
}

// TestHardExitCodeUnknownActionParks: an unrecognized action string must
// fall to park, mirroring ResolveHardExitAction's treatment of unknown
// identities — the cost of parking is a stuck backstop, the cost of a wrong
// exit is a killed successor.
func TestHardExitCodeUnknownActionParks(t *testing.T) {
	for _, raw := range []string{"", "OVERLAP", "restart", "exit"} {
		if _, exited := HardExitCode(HardExitAction(raw), 1); exited {
			t.Fatalf("HardExitCode(%q, 1) must not exit", raw)
		}
	}
}

// TestHardExitAgreesWithPlannedExit is the two-doors invariant: the
// planned-exit decision (what a RETURNING teardown exits with) and the
// hard-exit decision (what a firing backstop does instead) read the same
// runner state, so wherever the planned exit protects a live successor
// (overlap 76), the hard exit must protect it too (overlap or park — never
// "requested"). A divergence would mean one of the two doors can still kill
// the successor the other door promised to keep.
func TestHardExitAgreesWithPlannedExit(t *testing.T) {
	for _, row := range hardExitSpace {
		// Reconstruct the planned-exit inputs from the row's world:
		// a restart was requested on the service route, a successor was
		// started iff we have an identity answer, supervision is implied
		// by systemdManaged, and the mirror probe answers newerLive.
		successorStarted := row.identity == "live" || row.identity == "unknown"
		planned := ResolveExitCode(true, true, successorStarted, true, row.newerLive)
		protected := planned.Special && planned.Code == OverlapRestartExitCode
		if protected && row.want == HardExitRequested {
			t.Fatalf("planned exit protects (%s newer=%t → 76) but hard exit answers requested — %s",
				row.identity, row.newerLive, row.why)
		}
	}
}

// TestSharedTableNamesEveryOp guards the parity document itself: every op
// the twins and cmd/restartplan dispatch on must carry rows in the shared
// table, and no row may name an op outside that set. The Python contract
// test only runs table rows — an op without rows would ship invisible to
// the parity net, and a typo'd op field would silently exercise nothing.
func TestSharedTableNamesEveryOp(t *testing.T) {
	ops := map[string]bool{
		"decide": false, "exit_code": false,
		"hard_exit": false, "hard_exit_runner": false, "hard_exit_code": false,
	}
	for _, tc := range loadSharedCases(t) {
		if _, known := ops[tc.Op]; !known {
			t.Fatalf("case %s names op %q, which no twin dispatches on", tc.Name, tc.Op)
		}
		ops[tc.Op] = true
	}
	for op, seen := range ops {
		if !seen {
			t.Fatalf("shared table has no %q rows — the op ships without parity coverage", op)
		}
	}
}

// TestSharedTableCoversEveryHardExitInput guards the hard-exit rows the
// same way: every documented successor identity and every hard-exit action
// must appear in at least one row, so no input class can lose its
// Python-parity pin by a row being dropped (the identities are the
// /proc answers, the actions are what a firing backstop may do).
func TestSharedTableCoversEveryHardExitInput(t *testing.T) {
	identities := map[string]bool{
		string(IdentityLive): false, string(IdentityDead): false,
		string(IdentityUnknown): false, string(IdentityNone): false,
	}
	actions := map[string]bool{
		string(HardExitRequested): false,
		string(HardExitOverlap):   false,
		string(HardExitPark):      false,
	}
	for _, tc := range loadSharedCases(t) {
		if tc.Op == "hard_exit" || tc.Op == "hard_exit_runner" {
			identities[tc.SuccessorIdentity] = true
		}
		if tc.Op == "hard_exit_code" {
			actions[tc.Action] = true
		}
	}
	for identity, seen := range identities {
		if !seen {
			t.Fatalf("shared table has no hard_exit row for identity %q", identity)
		}
	}
	for action, seen := range actions {
		if !seen {
			t.Fatalf("shared table has no hard_exit_code row for action %q", action)
		}
	}
}
