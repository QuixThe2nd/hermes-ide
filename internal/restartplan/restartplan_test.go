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
	Name       string `json:"name"`
	Trigger    string `json:"trigger"`
	Supervised bool   `json:"supervised"`
	Container  bool   `json:"container"`
	Expect     Plan   `json:"expect"`
}

func loadSharedCases(t *testing.T) []sharedCase {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "restart_cases.json"))
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		Cases []sharedCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared table has no cases")
	}
	return table.Cases
}

// TestDecideSharedTable pins Decide against the shared decision table.
func TestDecideSharedTable(t *testing.T) {
	for _, tc := range loadSharedCases(t) {
		t.Run(tc.Name, func(t *testing.T) {
			trigger, err := ParseTrigger(tc.Trigger)
			if err != nil {
				t.Fatalf("trigger: %v", err)
			}
			got, err := Decide(trigger, tc.Supervised, tc.Container)
			if err != nil {
				t.Fatalf("Decide: %v", err)
			}
			if got != tc.Expect {
				t.Fatalf("Decide(%s, supervised=%t, container=%t) = %+v, want %+v",
					tc.Trigger, tc.Supervised, tc.Container, got, tc.Expect)
			}
		})
	}
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
