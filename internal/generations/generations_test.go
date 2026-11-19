package generations

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

type tableCase struct {
	Name        string        `json:"name"`
	Generations Array         `json:"generations"`
	Me          int           `json:"me"`
	PIDFile     *tablePIDFile `json:"pid_file"`
	Expect      struct {
		NewestLive     int   `json:"newest_live"`
		RestartTarget  int   `json:"restart_target"`
		MayClaim       bool  `json:"may_claim"`
		MayStartShared bool  `json:"may_start_shared"`
		Ancestors      []int `json:"ancestors"`
		Takeover       *bool `json:"takeover"`
		Describe       []any `json:"describe"`
	} `json:"expect"`
}

type tablePIDFile struct {
	PID  int  `json:"pid"`
	Live bool `json:"live"`
}

func loadTable(t *testing.T) []tableCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/cases.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var doc struct {
		Cases []tableCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("shared table is empty")
	}
	return doc.Cases
}

// TestSharedTable pins the Go twin to the shared contract table — the same
// records the Python contract test (tests/gateway/test_generations_contract.py)
// runs through cmd/generations against gateway/generations.py. A case failing
// here means the canonical statement changed; a case failing only on the
// Python side means the twin drifted.
func TestSharedTable(t *testing.T) {
	for _, tc := range loadTable(t) {
		t.Run(tc.Name, func(t *testing.T) {
			ancestors := tc.Generations.AncestorsOf(tc.Me)
			if ancestors == nil {
				ancestors = []int{}
			} // JSON-expect comes back as []; keep DeepEqual symmetric
			if !reflect.DeepEqual(ancestors, tc.Expect.Ancestors) {
				t.Errorf("AncestorsOf(%d) = %v, want %v", tc.Me, ancestors, tc.Expect.Ancestors)
			}
			if got := pidOf(tc.Generations.NewestLive()); got != tc.Expect.NewestLive {
				t.Errorf("NewestLive() = %d, want %d", got, tc.Expect.NewestLive)
			}
			if got := pidOf(tc.Generations.NewestLiveOther(tc.Me)); got != tc.Expect.RestartTarget {
				t.Errorf("NewestLiveOther(%d) = %d, want %d", tc.Me, got, tc.Expect.RestartTarget)
			}
			if got := tc.Generations.MayClaimAuthority(tc.Me); got != tc.Expect.MayClaim {
				t.Errorf("MayClaimAuthority(%d) = %v, want %v", tc.Me, got, tc.Expect.MayClaim)
			}
			if got := tc.Generations.MayStartSharedAdapters(tc.Me); got != tc.Expect.MayStartShared {
				t.Errorf("MayStartSharedAdapters(%d) = %v, want %v", tc.Me, got, tc.Expect.MayStartShared)
			}
			if !reflect.DeepEqual([]string(tc.Generations.Describe()), stringSlice(tc.Expect.Describe)) {
				t.Errorf("Describe() = %v, want %v", tc.Generations.Describe(), tc.Expect.Describe)
			}
			if tc.PIDFile != nil {
				if tc.Expect.Takeover == nil {
					t.Fatalf("case has pid_file but no expected takeover verdict")
				}
				if got := TakeoverAllowed(tc.PIDFile.PID, tc.PIDFile.Live, ancestors); got != *tc.Expect.Takeover {
					t.Errorf("TakeoverAllowed(%d, live=%v) = %v, want %v",
						tc.PIDFile.PID, tc.PIDFile.Live, got, *tc.Expect.Takeover)
				}
			}
		})
	}
}

func stringSlice(values []any) []string {
	out := make([]string, 0, len(values))
	for _, v := range values {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func pidOf(g *Generation) int {
	if g == nil {
		return 0
	}
	return g.PID
}

// TestAncestorCycleTerminates: a corrupted chain (A→B→A) must not spin.
func TestAncestorCycleTerminates(t *testing.T) {
	a := Array{
		{PID: 1, Predecessor: 2, SpawnSeq: 2, Live: true},
		{PID: 2, Predecessor: 1, SpawnSeq: 1, Live: true},
	}
	if got := a.AncestorsOf(1); !reflect.DeepEqual(got, []int{2}) {
		t.Errorf("AncestorsOf on cyclic chain = %v, want [2]", got)
	}
}

// TestSelfPredecessorTerminates: a record naming itself its own predecessor
// must not spin.
func TestSelfPredecessorTerminates(t *testing.T) {
	a := Array{{PID: 7, Predecessor: 7, SpawnSeq: 1, Live: true}}
	if got := a.AncestorsOf(7); len(got) != 0 {
		t.Errorf("AncestorsOf self-predecessor = %v, want empty", got)
	}
}

// TestUnknownLiveTieBreak: two record-less originals share sequence 0; the
// higher PID must not silently lose precedence (deterministic order keeps
// snapshot re-evaluation stable).
func TestUnknownLiveTieBreak(t *testing.T) {
	a := Array{
		{PID: 11, Predecessor: 0, SpawnSeq: 0, Live: true},
		{PID: 12, Predecessor: 0, SpawnSeq: 0, Live: true},
	}
	if got := a.NewestLive(); got == nil || got.PID != 12 {
		t.Errorf("NewestLive tie-break = %+v, want pid 12", got)
	}
}
