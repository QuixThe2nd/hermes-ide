package profileprovision

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// tableCase is one row of the shared decision table in testdata/
// provision_cases.json — the same rows the Python bridge contract test
// (tests/plugins/deep_research/test_provision_bridge.py) drives through the
// compiled binary, so the JSON contract is pinned from both sides.
type tableCase struct {
	Name   string         `json:"name"`
	Record map[string]any `json:"record"`
	Expect map[string]any `json:"expect"`
}

func loadTable(t *testing.T) []tableCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/provision_cases.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		Cases []tableCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared table is empty")
	}
	return table.Cases
}

// TestSharedTablePlansAndClassifies executes every shared-table row through
// the pure decision functions and compares the FULL decision — field-for-field
// after a JSON round-trip, which is the shape the Python bridge receives.
func TestSharedTablePlansAndClassifies(t *testing.T) {
	for _, tc := range loadTable(t) {
		t.Run(tc.Name, func(t *testing.T) {
			var got any
			switch tc.Record["op"] {
			case "plan":
				got = PlanProvision(State{
					Name:          tc.Record["name"].(string),
					Exists:        tc.Record["exists"].(bool),
					ActiveProfile: tc.Record["active_profile"].(string),
					ActiveIsNamed: tc.Record["active_is_named"].(bool),
				})
			case "classify":
				got = ClassifyFailure(Failure{
					Name:  tc.Record["name"].(string),
					Race:  tc.Record["race"].(bool),
					Error: tc.Record["error"].(string),
				})
			default:
				t.Fatalf("unknown op %v", tc.Record["op"])
			}
			encoded, err := json.Marshal(got)
			if err != nil {
				t.Fatalf("marshal decision: %v", err)
			}
			var decoded map[string]any
			if err := json.Unmarshal(encoded, &decoded); err != nil {
				t.Fatalf("unmarshal decision: %v", err)
			}
			if !reflect.DeepEqual(decoded, tc.Expect) {
				t.Errorf("decision mismatch\n got: %#v\nwant: %#v", decoded, tc.Expect)
			}
		})
	}
}

// TestSkipBeatsInvalidName pins the rule order: an existing profile is never
// recreated and never name-validated — the operator may point
// worker_profile at any live profile, including "default".
func TestSkipBeatsInvalidName(t *testing.T) {
	plan := PlanProvision(State{Name: "not a valid id!", Exists: true})
	if plan.Action != ActionSkip {
		t.Errorf("existing profile with an odd name must skip, got %q", plan.Action)
	}
}

// TestRefusalsNeverClone guards the refusal shape: no plan that refuses may
// carry clone instructions the caller might half-execute.
func TestRefusalsNeverClone(t *testing.T) {
	for _, name := range []string{"", "default", "root", "sudo", "hermes", "BAD", "a/b", ".."} {
		plan := PlanProvision(State{Name: name})
		if plan.Action != ActionRefuse {
			t.Errorf("name %q must refuse, got %q", name, plan.Action)
		}
		if plan.CloneConfig || plan.CloneFrom != "" {
			t.Errorf("refusal for %q must not carry clone instructions: %+v", name, plan)
		}
		if plan.Code != CodeInvalidProfile {
			t.Errorf("refusal for %q must carry %q, got %q", name, CodeInvalidProfile, plan.Code)
		}
	}
}

// TestCloneSourceAcceptance walks the clone-source edge cases: a named id
// with 64 chars is fine, one past the ceiling and every non-id falls back to
// "" (create_profile resolves the active home itself).
func TestCloneSourceAcceptance(t *testing.T) {
	long := string(make([]byte, 0)) +
		"p" + repeat("_", 63) // 64 chars total: the ceiling
	cases := []struct {
		active string
		named  bool
		want   string
	}{
		{"coder", true, "coder"},
		{long, true, long},
		{"", true, ""},
		{"default", true, ""},
		{"custom", false, ""},
		{"coder", false, ""},
		{"has space", true, ""},
		{"UPPER", true, ""},
	}
	for _, tc := range cases {
		got := cloneSource(State{Name: "researcher", ActiveProfile: tc.active, ActiveIsNamed: tc.named})
		if got != tc.want {
			t.Errorf("cloneSource(active=%q, named=%v) = %q, want %q", tc.active, tc.named, got, tc.want)
		}
	}
}

// TestRaceClassificationIsTerminal: a race is provisioned regardless of the
// error text, and a non-race failure always names the profile and carries
// the stable code the tool reports.
func TestRaceClassificationIsTerminal(t *testing.T) {
	if got := ClassifyFailure(Failure{Name: "x", Race: true, Error: "anything"}); got.Outcome != OutcomeProvisioned {
		t.Errorf("race must classify as provisioned, got %+v", got)
	}
	got := ClassifyFailure(Failure{Name: "researcher", Race: false, Error: "disk full"})
	if got.Outcome != OutcomeFailed || got.Code != CodeProvisionFailed {
		t.Errorf("failure misclassified: %+v", got)
	}
	want := `could not provision worker profile "researcher": disk full`
	if got.Message != want {
		t.Errorf("message = %q, want %q", got.Message, want)
	}
}

func repeat(s string, n int) string {
	out := ""
	for range n {
		out += s
	}
	return out
}
