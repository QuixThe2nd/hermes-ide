package servingidentity

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"
)

// The shared contract table (testdata/serving_cases.json) is generated from
// the Python twin's real output, so these tests and the Python contract test
// (tests/gateway/test_serving_identity_contract.py) assert the SAME bytes:
// the Go twin must render byte-identical notes and status lines, and must
// never claim more than the Python implementation proves.

type sharedCase struct {
	Name        string   `json:"name"`
	Op          string   `json:"op"`
	Input       any      `json:"input"`
	Expect      string   `json:"expect"`
	ExpectLines []string `json:"expect_lines"`
}

func loadSharedCases(t *testing.T) []sharedCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/serving_cases.json")
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
		t.Fatal("shared table is empty")
	}
	return table.Cases
}

func TestRuntimeNoteMatchesSharedTable(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		if c.Op != "note" {
			continue
		}
		input, _ := c.Input.(map[string]any)
		if got := RuntimeNote(snapshotFromTable(input)); got != c.Expect {
			t.Errorf("%s:\n got: %q\nwant: %q", c.Name, got, c.Expect)
		}
	}
}

func TestStatusLinesMatchSharedTable(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		if c.Op != "status" {
			continue
		}
		input, _ := c.Input.(map[string]any)
		if got := StatusLines(identifyFromTable(input), stateFromTable(input),
			chainedFromTable(input), probeFromTable(input)); !reflect.DeepEqual(got, c.ExpectLines) {
			t.Errorf("%s:\n got: %q\nwant: %q", c.Name, got, c.ExpectLines)
		}
	}
}

// snapshotFromTable maps a shared-table note input onto Snapshot.
func snapshotFromTable(input map[string]any) Snapshot {
	identity, _ := input["identity"].(map[string]any)
	overlap, _ := input["overlap"].(map[string]any)
	pick := func(key string) any {
		if v, ok := input[key]; ok && v != nil {
			return v
		}
		return overlap[key]
	}
	return Snapshot{
		PID:       tableInt(identity["pid"]),
		StartTime: tableInt64(identity["start_time"]),
		BuildSHA:  tableString(identity["code_sha"]),
		Draining:  tableBool(input["draining"]),

		RestartRequested:        tableBool(pick("restart_requested")),
		OverlapSuccessorPending: tableBool(pick("overlap_successor_pending")),
		PredecessorPID:          tableInt(pick("predecessor_pid")),
		SuccessorPID:            tableInt(pick("successor_pid")),
		SuccessorStartTime:      tableInt64(pick("successor_start_time")),
		SuccessorReady:          tableBool(pick("successor_ready")),
	}
}

func identifyFromTable(input map[string]any) *Identify {
	raw, ok := input["identify"].(map[string]any)
	if !ok || tableInt(raw["pid"]) <= 0 {
		return nil
	}
	overlap, _ := raw["overlap"].(map[string]any)
	return &Identify{
		PID:                tableInt(raw["pid"]),
		StartTime:          tableInt64(raw["start_time"]),
		Role:               Role(tableString(overlap["role"])),
		PredecessorPID:     tableInt(overlap["predecessor_pid"]),
		SuccessorPID:       tableInt(overlap["successor_pid"]),
		SuccessorStartTime: tableInt64(overlap["successor_start_time"]),
		SuccessorReady:     tableBool(overlap["successor_ready"]),
	}
}

func stateFromTable(input map[string]any) *StateFile {
	raw, ok := input["state"].(map[string]any)
	if !ok || tableInt(raw["pid"]) <= 0 {
		return nil
	}
	return &StateFile{
		PID:              tableInt(raw["pid"]),
		GatewayState:     tableString(raw["gateway_state"]),
		PIDIsLive:        tableBool(raw["pid_is_live"]),
		IsStale:          tableBool(raw["is_stale"]),
		RestartRequested: tableBool(raw["restart_requested"]),
	}
}

func chainedFromTable(input map[string]any) string {
	source := tableString(input["chained_source"])
	if source == "" {
		return ""
	}
	return ChainedSource(source)
}

func probeFromTable(input map[string]any) SuccessorIdentity {
	v := tableString(input["successor_identity"])
	if v == "" {
		return nil
	}
	return func(int, int64) string { return v }
}

func tableString(v any) string {
	s, _ := v.(string)
	return s
}

func tableInt(v any) int {
	f, _ := v.(float64)
	return int(f)
}

func tableInt64(v any) int64 {
	f, _ := v.(float64)
	return int64(f)
}

func tableBool(v any) bool {
	b, _ := v.(bool)
	return b
}

func TestClassifyRoles(t *testing.T) {
	cases := []struct {
		name string
		snap Snapshot
		want Role
	}{
		{"ordinary", Snapshot{}, RoleServing},
		{"pure drain without restart is not a handoff",
			Snapshot{Draining: true}, RoleServing},
		{"restart requested drains", Snapshot{RestartRequested: true},
			RoleDrainingPredecessor},
		{"pending successor serves", Snapshot{OverlapSuccessorPending: true},
			RoleOverlapSuccessor},
	}
	for _, c := range cases {
		if got := Classify(c.snap); got != c.want {
			t.Errorf("%s: got %q want %q", c.name, got, c.want)
		}
	}
}

func TestOverlapFieldsOmitUnknowns(t *testing.T) {
	if got := OverlapFields(Snapshot{}); !reflect.DeepEqual(got,
		map[string]any{"role": "serving"}) {
		t.Errorf("serving snapshot: got %v", got)
	}
	got := OverlapFields(Snapshot{
		RestartRequested:   true,
		SuccessorPID:       709505,
		SuccessorStartTime: 3001234,
		SuccessorReady:     true,
	})
	want := map[string]any{
		"role":                 "draining-predecessor",
		"successor_pid":        709505,
		"successor_start_time": int64(3001234),
		"successor_ready":      true,
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("draining snapshot:\n got: %v\nwant: %v", got, want)
	}
	// A successor pid without a fingerprint still reports readiness — the
	// consumer's probe decides what that is worth.
	got = OverlapFields(Snapshot{RestartRequested: true, SuccessorPID: 709505})
	if _, hasStart := got["successor_start_time"]; hasStart {
		t.Errorf("unknown start time must be omitted: %v", got)
	}
	if got["successor_ready"] != false {
		t.Errorf("unattested successor must report ready=false: %v", got)
	}
}

func TestStatusLinesNeverGreenWithoutProof(t *testing.T) {
	// A bare spawned PID — even alive — is not a serving claim; only the
	// hub-attested readiness ack plus a matching fingerprint is.
	draining := &Identify{
		PID: 1438, Role: RoleDrainingPredecessor,
		SuccessorPID: 709505, SuccessorStartTime: 3001234,
	}
	lines := StatusLines(draining, nil, "", func(int, int64) string { return "live" })
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⏳") {
		t.Errorf("unattested successor must be starting, got %q", lines)
	}
	// A reused PID (fingerprint mismatch) is dead even though the pid exists.
	reused := &Identify{
		PID: 1438, Role: RoleDrainingPredecessor,
		SuccessorPID: 709505, SuccessorStartTime: 3001234, SuccessorReady: true,
	}
	lines = StatusLines(reused, nil, "", func(int, int64) string { return "dead" })
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠") {
		t.Errorf("reused successor pid must warn, got %q", lines)
	}
	// A fresh live draining state record never labels its pid the server.
	state := &StateFile{PID: 1438, GatewayState: "draining", PIDIsLive: true, RestartRequested: true}
	lines = StatusLines(nil, state, "", nil)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠") {
		t.Errorf("draining state record must stay unknown, got %q", lines)
	}
}

func TestChainedSourceFallback(t *testing.T) {
	if got := ChainedSource("   "); got != "an earlier restart request" {
		t.Errorf("blank source: got %q", got)
	}
	if got := ChainedSource("discord user 42 (restart)"); got != "discord user 42 (restart)" {
		t.Errorf("recorded source: got %q", got)
	}
}
