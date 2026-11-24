package readygates

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestDecideSharedTable(t *testing.T) {
	path := filepath.Join("testdata", "cases.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []struct {
			Name                 string           `json:"name"`
			Platforms            []PlatformState  `json:"platforms"`
			CronStarted          bool             `json:"cron_started"`
			HousekeepingStarted  bool             `json:"housekeeping_started"`
			McpDiscoveryState    string           `json:"mcp_discovery_state"`
			BootPathSendsState   string           `json:"boot_path_sends_state"`
			Expect               struct {
				Ready     bool     `json:"ready"`
				BlockedBy []string `json:"blocked_by"`
			} `json:"expect"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	for _, c := range doc.Cases {
		t.Run(c.Name, func(t *testing.T) {
			res := Decide(Input{
				Platforms:           c.Platforms,
				CronStarted:         c.CronStarted,
				HousekeepingStarted: c.HousekeepingStarted,
				McpDiscoveryState:   c.McpDiscoveryState,
				BootPathSendsState:  c.BootPathSendsState,
			})
			if res.Ready != c.Expect.Ready {
				t.Fatalf("ready: got %v want %v", res.Ready, c.Expect.Ready)
			}
			if len(res.BlockedBy) != len(c.Expect.BlockedBy) {
				t.Fatalf("blocked_by len: got %v want %v", res.BlockedBy, c.Expect.BlockedBy)
			}
			for i := range res.BlockedBy {
				if res.BlockedBy[i] != c.Expect.BlockedBy[i] {
					t.Fatalf("blocked_by: got %v want %v", res.BlockedBy, c.Expect.BlockedBy)
				}
			}
		})
	}
}
