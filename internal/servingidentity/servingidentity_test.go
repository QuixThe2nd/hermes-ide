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
	Expect      any      `json:"expect"`
	ExpectLines []string `json:"expect_lines"`
}

// expectString renders a scalar expectation (note/transition/record/
// owner-guard/owner-refusal print one value; chain-rewrite prints a JSON
// object compared separately).
func (c sharedCase) expectString() string {
	s, _ := c.Expect.(string)
	return s
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
		if got := RuntimeNote(snapshotFromTable(input)); got != c.expectString() {
			t.Errorf("%s:\n got: %q\nwant: %q", c.Name, got, c.expectString())
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
			probeFromTable(input),
			pidsFromTable(input["gateway_pids"])); !reflect.DeepEqual(got, c.ExpectLines) {
			t.Errorf("%s:\n got: %q\nwant: %q", c.Name, got, c.ExpectLines)
		}
	}
}

// TestTransitionMatchesSharedTable drives TransitionNote through the shared
// honesty table: a proven process change renders the carried-over vs
// process-local note; same/unknown/first-turn render nothing.
func TestTransitionMatchesSharedTable(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		if c.Op != "transition" {
			continue
		}
		input, _ := c.Input.(map[string]any)
		if got := TransitionNote(snapshotFromRecord(input["previous"]),
			identityFromRecord(input["identity"])); got != c.expectString() {
			t.Errorf("%s:\n got: %q\nwant: %q", c.Name, got, c.expectString())
		}
	}
}

// TestRecordAndOwnerOpsMatchSharedTable drives the durable-record and
// restart-gate ops through the same shared table the Python contract test
// executes, so both twins are pinned on the identical inputs.
func TestRecordAndOwnerOpsMatchSharedTable(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		input, _ := c.Input.(map[string]any)
		switch c.Op {
		case "record":
			current := snapshotFromRecord(input["record"])
			got := "skip"
			if ValidSessionID(tableString(input["session_id"])) &&
				RecordWriteNeeded(tableString(input["stored"]), current,
					tableString(input["note"])) {
				got = "write"
			}
			if got != c.expectString() {
				t.Errorf("%s: got %q want %q", c.Name, got, c.expectString())
			}
		case "owner_guard":
			if got := OwnerAction(tableInt(input["serving_pid"]),
				tableIdentify(input["identify"]),
				tableRegistryRows(input["registry_rows"])); got != c.expectString() {
				t.Errorf("%s: got %q want %q", c.Name, got, c.expectString())
			}
		case "owner_refusal":
			if got := OwnerRefusal(tableInt(input["serving_pid"]),
				tableIdentify(input["identify"])); got != c.expectString() {
				t.Errorf("%s: got %q want %q", c.Name, got, c.expectString())
			}
		}
	}
}

// TestAckRewriteAndTurnNotesMatchSharedTable pins the two JSON-valued
// ops: the control-socket ack rewrite and the per-turn note pair.
func TestAckRewriteAndTurnNotesMatchSharedTable(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		input, _ := c.Input.(map[string]any)
		switch c.Op {
		case "ack_rewrite":
			ack, _ := input["ack"].(map[string]any)
			got := AckRewrite(map[string]any(ack), tableInt(input["serving_pid"]),
				tableIdentify(input["identify"]),
				tableRegistryRows(input["registry_rows"]))
			want, _ := c.Expect.(map[string]any)
			// JSON round-trip both sides: table numbers decode as float64
			// while the rewrite writes ints — marshaled bytes are the
			// contract, not the Go value types.
			gotJSON, _ := json.Marshal(got)
			wantJSON, _ := json.Marshal(want)
			if string(gotJSON) != string(wantJSON) {
				t.Errorf("%s:\n got: %s\nwant: %s", c.Name, gotJSON, wantJSON)
			}
		case "turn_notes":
			got := TurnNotes(identityFromRecord(input["identity"]),
				tableString(input["stored"]))
			if !reflect.DeepEqual(got, c.ExpectLines) {
				t.Errorf("%s:\n got: %q\nwant: %q", c.Name, got, c.ExpectLines)
			}
		}
	}
}

// TestCodeDeltaOpsMatchSharedTable drives the code-delta ops through the
// shared honesty table: the record's build-sha proof decides the verdict,
// and the note is never fabricated — a failed git render renders the
// could-not-compute form, an unprovable predecessor says which reason, and
// an unreadable current build stays quiet.
func TestCodeDeltaOpsMatchSharedTable(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		if c.Op != "code_delta_decision" && c.Op != "code_delta_note" {
			continue
		}
		input, _ := c.Input.(map[string]any)
		stored, currentSHA := tableString(input["stored"]), tableString(input["code_sha"])
		if c.Op == "code_delta_decision" {
			if got := CodeDeltaDecision(stored, currentSHA); got != c.expectString() {
				t.Errorf("%s: got %q want %q", c.Name, got, c.expectString())
			}
			continue
		}
		if got := CodeDeltaNote(stored, currentSHA, tableDeltaRender(input)); got != c.expectString() {
			t.Errorf("%s:\n got: %q\nwant: %q", c.Name, got, c.expectString())
		}
	}
}

// tableDeltaRender builds the fixed DeltaRender a code_delta_note case
// describes: a failed git renders nothing; otherwise the case's commits and
// stat render as a successful git read.
func tableDeltaRender(input map[string]any) DeltaRender {
	if tableString(input["git"]) == "failed" {
		return nil
	}
	commits, stat := tableString(input["commits"]), tableString(input["stat"])
	return func(string, string) (string, string, bool) { return commits, stat, true }
}

// tableIdentify maps a table identify answer {pid, overlap{...}} onto
// Identify (nil when absent).
func tableIdentify(raw any) *Identify {
	record, ok := raw.(map[string]any)
	if !ok || tableInt(record["pid"]) <= 0 {
		return nil
	}
	overlap, _ := record["overlap"].(map[string]any)
	return &Identify{
		PID:            tableInt(record["pid"]),
		Role:           Role(tableString(overlap["role"])),
		SuccessorPID:   tableInt(overlap["successor_pid"]),
		SuccessorReady: tableBool(overlap["successor_ready"]),
	}
}

// tableRegistryRows maps the table's registry_rows array onto
// []RegistryRow (nil when absent — no registry evidence).
func tableRegistryRows(raw any) []RegistryRow {
	items, ok := raw.([]any)
	if !ok {
		return nil
	}
	rows := make([]RegistryRow, 0, len(items))
	for _, item := range items {
		record, ok := item.(map[string]any)
		if !ok {
			continue
		}
		rows = append(rows, RegistryRow{
			PID:            tableInt(record["pid"]),
			PredecessorPID: tableInt(record["predecessor_pid"]),
		})
	}
	return rows
}

// snapshotFromRecord maps a persisted served-by record {pid,start_time}
// onto Snapshot (no sha — records never carry one).
func snapshotFromRecord(raw any) Snapshot {
	record, _ := raw.(map[string]any)
	return Snapshot{
		PID:       tableInt(record["pid"]),
		StartTime: tableInt64(record["start_time"]),
	}
}

// identityFromRecord maps the current-identity record {pid, start_time,
// code_sha} — the sha distinguishes a true successor from a rebuilt binary.
func identityFromRecord(raw any) Snapshot {
	snapshot := snapshotFromRecord(raw)
	if record, ok := raw.(map[string]any); ok {
		snapshot.BuildSHA = tableString(record["code_sha"])
	}
	return snapshot
}

// pidsFromTable maps the JSON gateway_pids array onto []int.
func pidsFromTable(raw any) []int {
	items, _ := raw.([]any)
	pids := make([]int, 0, len(items))
	for _, item := range items {
		if pid := tableInt(item); pid > 0 {
			pids = append(pids, pid)
		}
	}
	return pids
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
	lines := StatusLines(draining, nil, func(int, int64) string { return "live" }, nil)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⏳") {
		t.Errorf("unattested successor must be starting, got %q", lines)
	}
	// A reused PID (fingerprint mismatch) is dead even though the pid exists.
	reused := &Identify{
		PID: 1438, Role: RoleDrainingPredecessor,
		SuccessorPID: 709505, SuccessorStartTime: 3001234, SuccessorReady: true,
	}
	lines = StatusLines(reused, nil, func(int, int64) string { return "dead" }, nil)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠") {
		t.Errorf("reused successor pid must warn, got %q", lines)
	}
	// A fresh live draining state record never labels its pid the server.
	state := &StateFile{PID: 1438, GatewayState: "draining", PIDIsLive: true, RestartRequested: true}
	lines = StatusLines(nil, state, nil, nil)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠") {
		t.Errorf("draining state record must stay unknown, got %q", lines)
	}
	// A legacy identify answer (no overlap role) is never green — during an
	// overlap window that answerer is the draining predecessor, and its
	// serving role cannot be proven from here.
	legacy := &Identify{PID: 709505, StartTime: 3001234}
	lines = StatusLines(legacy, nil, nil, nil)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠") ||
		strings.Contains(lines[0], "✓") {
		t.Errorf("legacy identify must never be green, got %q", lines)
	}
	// A proven serving role stays green beside a second live gateway pid:
	// that overlap window is exactly when two gateways legitimately coexist.
	serving := &Identify{PID: 1531038, StartTime: 3005678, Role: RoleServing}
	lines = StatusLines(serving, nil, nil, []int{709505, 1531038})
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "✓") {
		t.Errorf("proven serving role must stay green, got %q", lines)
	}
}

// TestTransitionStateMatrix pins the comparison rules: pid inequality is a
// change; equal pids need BOTH start fingerprints (a reused pid with
// differing fingerprints is a change; missing fingerprints are unknown, not
// a guess in either direction); a missing record is unknown.
func TestTransitionStateMatrix(t *testing.T) {
	cases := []struct {
		name string
		prev Snapshot
		cur  Snapshot
		want string
	}{
		{"no previous record", Snapshot{}, Snapshot{PID: 1531038, StartTime: 3005678}, "unknown"},
		{"pid changed", Snapshot{PID: 709505, StartTime: 3001234}, Snapshot{PID: 1531038, StartTime: 3005678}, "changed"},
		{"same pid and fingerprint", Snapshot{PID: 1531038, StartTime: 3005678}, Snapshot{PID: 1531038, StartTime: 3005678}, "same"},
		{"reused pid differing fingerprint", Snapshot{PID: 709505, StartTime: 3001234}, Snapshot{PID: 709505, StartTime: 3019999}, "changed"},
		{"same pid missing current fingerprint", Snapshot{PID: 709505, StartTime: 3001234}, Snapshot{PID: 709505}, "unknown"},
		{"missing previous fingerprint", Snapshot{PID: 709505}, Snapshot{PID: 709505, StartTime: 3001234}, "unknown"},
	}
	for _, c := range cases {
		if got := TransitionState(c.prev, c.cur); got != c.want {
			t.Errorf("%s: got %q want %q", c.name, got, c.want)
		}
	}
}

// TestTransitionNoteSilence pins that the note renders ONLY on a proven
// change — never on a session's first turn, same-process turns, or
// unprovable comparisons — and names both identities when it does.
func TestTransitionNoteSilence(t *testing.T) {
	first := Snapshot{PID: 1531038, StartTime: 3005678, BuildSHA: "abc123"}
	if got := TransitionNote(Snapshot{}, first); got != "" {
		t.Errorf("first-ever turn must be silent, got %q", got)
	}
	same := Snapshot{PID: 1531038, StartTime: 3005678}
	if got := TransitionNote(same, first); got != "" {
		t.Errorf("same process must be silent, got %q", got)
	}
	unprovable := Snapshot{PID: 1531038}
	if got := TransitionNote(unprovable, first); got != "" {
		t.Errorf("unprovable comparison must be silent, got %q", got)
	}
	changed := TransitionNote(
		Snapshot{PID: 709505, StartTime: 3001234},
		Snapshot{PID: 1531038, StartTime: 3005678, BuildSHA: "abc123"})
	if !strings.Contains(changed, "pid 1531038, start 3005678, build abc123 replaced pid 709505, start 3001234") {
		t.Errorf("changed note must name both identities, got %q", changed)
	}
	if !strings.Contains(changed, "not a new task") ||
		!strings.Contains(changed, "Process-local state did NOT carry over") {
		t.Errorf("changed note must separate carried-over from process-local, got %q", changed)
	}
}

// TestMetaKeyAndSessionID pins the durable-record key namespace and the
// session-id validity gate (blank/whitespace ids claim nothing).
func TestMetaKeyAndSessionID(t *testing.T) {
	if got := MetaKey("discord:12345"); got != "served_by:discord:12345" {
		t.Errorf("meta key: got %q", got)
	}
	for _, id := range []string{"discord:12345", " spaced-but-real "} {
		if !ValidSessionID(id) {
			t.Errorf("session id %q must be valid", id)
		}
	}
	for _, id := range []string{"", "   "} {
		if ValidSessionID(id) {
			t.Errorf("session id %q must be invalid", id)
		}
	}
}

// TestRecordRoundtripAndWriteDiscipline pins the served-by record shape and
// the write-on-change rule: a matching stored record is a skip (one cheap
// point read per ordinary turn); absent, corrupt, or differing records are
// writes so the durable predecessor stays learnable.
func TestRecordRoundtripAndWriteDiscipline(t *testing.T) {
	current := Snapshot{PID: 1531038, StartTime: 3005678}
	encoded := EncodeRecord(current, RuntimeNote(current), "")
	parsed, ok := ParseRecord(encoded)
	if !ok || parsed.PID != current.PID || parsed.StartTime != current.StartTime {
		t.Fatalf("roundtrip: got %v ok=%v from %q", parsed, ok, encoded)
	}
	if !strings.Contains(encoded, `"pid":1531038`) {
		t.Errorf("record shape: %q", encoded)
	}
	if StoredNote(encoded) != RuntimeNote(current) {
		t.Errorf("record must carry the note proof: %q", encoded)
	}
	if RecordWriteNeeded(encoded, current, RuntimeNote(current)) {
		t.Error("matching stored record must be a skip")
	}
	if !RecordWriteNeeded(encoded, current, "new wording") {
		t.Error("same fingerprint with a changed note wording must be a write")
	}
	for name, stored := range map[string]string{
		"absent":        "",
		"corrupt":       "{not json",
		"non-dict":      `"pid 1531038"`,
		"different pid": `{"pid":709505,"start_time":3001234}`,
		"reused pid":    `{"pid":1531038,"start_time":3019999}`,
	} {
		if !RecordWriteNeeded(stored, current, RuntimeNote(current)) {
			t.Errorf("%s: must be a write", name)
		}
	}
	if _, ok := ParseRecord(`{"pid":-1,"start_time":3005678}`); ok {
		t.Error("non-positive pid is not a record")
	}
}

// TestOwnerActionMatrix pins the restart-tool gate: the socket's answerer
// must be the serving process, attest it as a hub-ready successor, or —
// for a serving-role answerer — be proven the NEWEST generation of an
// array that positively lists the requester (the registry-proven
// forward). Anything else is a refusal, not a queue.
func TestOwnerActionMatrix(t *testing.T) {
	array := []RegistryRow{
		{PID: 424242, PredecessorPID: 134659},
		{PID: 557728, PredecessorPID: 424242},
	}
	cases := []struct {
		name       string
		servingPID int
		id         *Identify
		rows       []RegistryRow
		want       string
	}{
		{"no stamp keeps old behavior", 0, &Identify{PID: 709505}, nil, "proceed"},
		{"no identify answer", 1531038, nil, array, "proceed"},
		{"owner is the serving process", 1531038, &Identify{PID: 1531038}, array, "proceed"},
		{"legacy predecessor answering", 1531038, &Identify{PID: 709505}, array, "refuse"},
		{"draining predecessor no successor", 1531038,
			&Identify{PID: 709505, Role: RoleDrainingPredecessor}, array, "refuse"},
		{"attests a different successor", 1531038,
			&Identify{PID: 709505, Role: RoleDrainingPredecessor, SuccessorPID: 42}, array, "refuse"},
		{"attests successor not ready", 1531038,
			&Identify{PID: 709505, Role: RoleDrainingPredecessor, SuccessorPID: 1531038}, array, "refuse"},
		{"attests this server ready", 1531038,
			&Identify{PID: 709505, Role: RoleDrainingPredecessor, SuccessorPID: 1531038,
				SuccessorReady: true}, array, "attested-chain"},
		{"serving answerer without registry rows", 134659,
			&Identify{PID: 557728, Role: RoleServing}, nil, "refuse"},
		{"serving answerer unlisted requester", 134659,
			&Identify{PID: 557728, Role: RoleServing}, []RegistryRow{
				{PID: 424242, PredecessorPID: 999001},
				{PID: 557728, PredecessorPID: 424242},
			}, "refuse"},
		{"serving answerer not the newest row", 134659,
			&Identify{PID: 557728, Role: RoleServing}, []RegistryRow{
				{PID: 557728, PredecessorPID: 134659},
				{PID: 888999, PredecessorPID: 557728},
			}, "refuse"},
		{"registry proves the array forward", 134659,
			&Identify{PID: 557728, Role: RoleServing}, array, "attested-forward"},
		{"requester listed only as predecessor", 134659,
			&Identify{PID: 424242, Role: RoleServing}, []RegistryRow{
				{PID: 424242, PredecessorPID: 134659},
			}, "attested-forward"},
		{"junk rows prove nothing", 134659,
			&Identify{PID: 557728, Role: RoleServing}, []RegistryRow{
				{PID: 0, PredecessorPID: 134659}, {PID: 557728},
			}, "refuse"},
	}
	for _, c := range cases {
		if got := OwnerAction(c.servingPID, c.id, c.rows); got != c.want {
			t.Errorf("%s: got %q want %q", c.name, got, c.want)
		}
	}
}
