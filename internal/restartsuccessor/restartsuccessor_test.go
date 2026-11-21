package restartsuccessor

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// The shared contract table (testdata/restart_cases.json) is the same
// record set the Python contract test
// (tests/gateway/test_restart_successor_contract.py) runs against
// gateway/overlap_restart.py::spawn_successor, so the two implementations
// are pinned to one decision table: every restart spawns exactly one
// overlap successor through the first overlap-capable adapter, and no
// config key disables the spawn.

type sharedTable struct {
	Cases []struct {
		Name   string   `json:"name"`
		Input  Scenario `json:"input"`
		Expect Outcome  `json:"expect"`
	} `json:"cases"`
}

func loadSharedTable(t *testing.T) sharedTable {
	t.Helper()
	raw, err := os.ReadFile("testdata/restart_cases.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table sharedTable
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared table has no cases")
	}
	return table
}

func TestDecideMatchesSharedTable(t *testing.T) {
	for _, c := range loadSharedTable(t).Cases {
		t.Run(c.Name, func(t *testing.T) {
			got := Decide(c.Input)
			if !reflect.DeepEqual(got, c.Expect) {
				t.Fatalf("Decide() = %+v, want %+v", got, c.Expect)
			}
		})
	}
}

// TestConfigNeverDisablesTheSpawn pins the removal itself: the decision is
// byte-identical whatever the (stale) config content — restart_platforms
// false, true, or absent.
func TestConfigNeverDisablesTheSpawn(t *testing.T) {
	for _, c := range loadSharedTable(t).Cases {
		withoutConfig := c.Input
		withoutConfig.Config = nil
		if !reflect.DeepEqual(Decide(c.Input), Decide(withoutConfig)) {
			t.Fatalf("%s: config changed the decision; config must never gate the spawn", c.Name)
		}
	}
}

// TestAlreadyStartedNeverSpawns: the latch is the exactly-one guarantee —
// no adapter spawn may be attempted once a successor is started.
func TestAlreadyStartedNeverSpawns(t *testing.T) {
	for _, c := range loadSharedTable(t).Cases {
		if c.Input.Started && len(c.Expect.SpawnAttempted) != 0 {
			t.Fatalf("%s: started runner must not attempt spawns", c.Name)
		}
	}
}

// TestTableCoversTheAlwaysSpawnContract: every branch of the policy must
// appear in the shared table, or the table stops proving the contract.
func TestTableCoversTheAlwaysSpawnContract(t *testing.T) {
	required := []string{
		"stale_restart_platforms_false_still_spawns",
		"stale_restart_platforms_true_same_decision",
		"already_started_returns_latched_pid_without_spawning",
		"non_capable_adapters_are_skipped_to_legacy_drain",
		"spawn_none_falls_through_to_next_capable_adapter",
		"readiness_marked_on_every_capable_adapter",
		"spawn_error_with_latched_child_retains_successor",
		"spawn_error_without_latched_child_raises",
	}
	seen := map[string]bool{}
	for _, c := range loadSharedTable(t).Cases {
		seen[c.Name] = true
	}
	for _, name := range required {
		if !seen[name] {
			t.Errorf("shared table lost the %s case", name)
		}
	}
}
