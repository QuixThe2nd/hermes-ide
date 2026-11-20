package restartplan

import (
	"encoding/json"
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

// TestSharedTable pins both decisions against the shared table: the
// transport rows (op "decide") and the planned-exit rows (op "exit_code").
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
				got := ResolveExitCode(tc.RestartRequested, tc.ViaService, tc.SuccessorStarted, tc.Supervised, tc.NewerLiveGeneration)
				special := tc.ExpectExitCode != nil
				if got.Special != special || (special && got.Code != *tc.ExpectExitCode) {
					t.Fatalf("ResolveExitCode(restart=%t, via_service=%t, successor=%t, supervised=%t, newer_live=%t) = %+v, want exit %d special=%t",
						tc.RestartRequested, tc.ViaService, tc.SuccessorStarted, tc.Supervised, tc.NewerLiveGeneration, got, deref(tc.ExpectExitCode), special)
				}
			default:
				t.Fatalf("unknown op %q", tc.Op)
			}
		})
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
