package servingidentity

import (
	"errors"
	"strings"
	"testing"
)

// mapStore is the injected persistence for ApplyServedBy tests: a real
// get/set pair over a map, optionally failing to simulate an unreachable
// session database.
type mapStore struct {
	records  map[string]string
	getErr   error
	setErr   error
	setCalls int
}

func newMapStore(records map[string]string) *mapStore {
	if records == nil {
		records = map[string]string{}
	}
	return &mapStore{records: records}
}

func (m *mapStore) get(key string) string {
	if m.getErr != nil {
		return ""
	}
	return m.records[key]
}

func (m *mapStore) set(key, value string) error {
	m.setCalls++
	if m.setErr != nil {
		return m.setErr
	}
	m.records[key] = value
	return nil
}

// TestTurnNotesComposition pins the note pair the turn wiring ships: the
// runtime identity note always rides; the transition note appears only when
// the DURABLE record proves a different process served the previous turns.
func TestTurnNotesComposition(t *testing.T) {
	current := Snapshot{PID: 1531038, StartTime: 3005678}
	// First turn after handoff: record names the predecessor → both notes.
	notes := TurnNotes(current, EncodeRecord(Snapshot{PID: 709505, StartTime: 3001234}))
	if len(notes) != 2 {
		t.Fatalf("transition turn: got %d notes, want 2 (%q)", len(notes), notes)
	}
	if !strings.Contains(notes[0], "pid 1531038") {
		t.Errorf("runtime note must name the serving process: %q", notes[0])
	}
	if !strings.Contains(notes[1], "replaced pid 709505, start 3001234") {
		t.Errorf("transition note must name the predecessor: %q", notes[1])
	}
	// Ordinary same-process turn: identity note only, no transition claim.
	notes = TurnNotes(current, EncodeRecord(current))
	if len(notes) != 1 {
		t.Fatalf("same-process turn: got %d notes, want 1 (%q)", len(notes), notes)
	}
	// First-ever turn (no record): identity note only — absence is not a
	// transition.
	notes = TurnNotes(current, "")
	if len(notes) != 1 {
		t.Fatalf("first turn: got %d notes, want 1 (%q)", len(notes), notes)
	}
	// Corrupt record: no claim rather than a guess.
	notes = TurnNotes(current, "{corrupt")
	if len(notes) != 1 {
		t.Fatalf("corrupt record: got %d notes, want 1 (%q)", len(notes), notes)
	}
}

// TestSessionStampParsing pins the serving-stamp contract shared with the
// restart tool: a stamp is provable only when BOTH halves are positive
// integers; anything else keeps today's behavior instead of refusing.
func TestSessionStampParsing(t *testing.T) {
	if snap, ok := SessionStamp("1531038", "3005678"); !ok ||
		snap.PID != 1531038 || snap.StartTime != 3005678 {
		t.Errorf("valid stamp: got %v ok=%v", snap, ok)
	}
	for _, c := range []struct{ pid, start string }{
		{"", "3005678"}, {"1531038", ""}, {"not-a-pid", "3005678"},
		{"1531038", "not-a-start"}, {"0", "3005678"}, {"1531038", "0"},
		{"-5", "3005678"}, {"1531038", "-5"},
	} {
		if _, ok := SessionStamp(c.pid, c.start); ok {
			t.Errorf("stamp (%q,%q) must not parse", c.pid, c.start)
		}
	}
}

// TestApplyServedByFirstTurnAfterHandoff pins the full decision flow of the
// durable transition: the predecessor's record yields the transition note,
// and this process's record replaces it (write-on-change).
func TestApplyServedByFirstTurnAfterHandoff(t *testing.T) {
	store := newMapStore(map[string]string{
		MetaKey("telegram:42"): EncodeRecord(Snapshot{PID: 709505, StartTime: 3001234}),
	})
	current := Snapshot{PID: 1531038, StartTime: 3005678}
	notes := ApplyServedBy("telegram:42", current, store.get, store.set)
	if len(notes) != 2 {
		t.Fatalf("handoff turn: got %d notes, want 2 (%q)", len(notes), notes)
	}
	if stored, ok := ParseRecord(store.records[MetaKey("telegram:42")]); !ok ||
		stored.PID != current.PID || stored.StartTime != current.StartTime {
		t.Errorf("record must be rewritten to the serving process, got %v", stored)
	}
	if store.setCalls != 1 {
		t.Errorf("write-on-change: set called %d times, want 1", store.setCalls)
	}
}

// TestApplyServedBySameProcessTurnSkipsWrite pins the point-read discipline:
// an ordinary same-process turn renders the identity note and writes
// nothing.
func TestApplyServedBySameProcessTurnSkipsWrite(t *testing.T) {
	current := Snapshot{PID: 1531038, StartTime: 3005678}
	store := newMapStore(map[string]string{MetaKey("s"): EncodeRecord(current)})
	notes := ApplyServedBy("s", current, store.get, store.set)
	if len(notes) != 1 || store.setCalls != 0 {
		t.Fatalf("same-process turn: %d notes, %d writes; want 1 note, 0 writes",
			len(notes), store.setCalls)
	}
}

// TestApplyServedByDegradesGracefully pins the never-break-a-turn rule: an
// unreadable store, a failing write, a missing set hook, or an invalid
// session id all still render the identity note and claim no transition.
func TestApplyServedByDegradesGracefully(t *testing.T) {
	current := Snapshot{PID: 1531038, StartTime: 3005678}
	// Unreadable store: no record → no transition claim.
	store := newMapStore(nil)
	store.getErr = errors.New("db locked")
	if notes := ApplyServedBy("s", current, store.get, store.set); len(notes) != 1 {
		t.Errorf("unreadable store: got %q", notes)
	}
	// Failing write: notes render, record stays absent, no panic.
	store = newMapStore(nil)
	store.setErr = errors.New("disk full")
	if notes := ApplyServedBy("s", current, store.get, store.set); len(notes) != 1 {
		t.Errorf("failing write: got %q", notes)
	}
	if _, ok := ParseRecord(store.records[MetaKey("s")]); ok {
		t.Error("failing write must not leave a record")
	}
	// No set hook: read-only mode renders and skips persistence.
	if notes := ApplyServedBy("s", current, store.get, nil); len(notes) != 1 {
		t.Errorf("nil set hook: got %q", notes)
	}
	// Invalid session id: no persistence attempted, note still renders.
	blank := newMapStore(nil)
	ApplyServedBy("   ", current, blank.get, blank.set)
	if blank.setCalls != 0 {
		t.Errorf("invalid session id: %d writes attempted", blank.setCalls)
	}
}

// TestStatusLinesAmbiguityBranches pins each new never-green branch of the
// status ladder as a unit (the shared table drives the same branches through
// both twins; these characterize the inputs that produce them):
//
//   - legacy identify (no overlap block) with multiple live gateway pids
//     names every candidate and refuses a serving claim;
//   - a legacy answerer whose own state record says draining is unknown;
//   - a state-file fallback with multiple live pids is ambiguous (a status
//     file names ONE process, never THE server among many);
//   - a draining state record with multiple live pids appends them;
//   - no sources with multiple live pids is ambiguous;
//   - a PROVEN serving role stays green beside a second live pid — that
//     overlap window is exactly when two gateways legitimately coexist.
func TestStatusLinesAmbiguityBranches(t *testing.T) {
	legacy := &Identify{PID: 709505, StartTime: 3001234}
	lines := StatusLines(legacy, nil, nil, nil, []int{709505, 1531038})
	if len(lines) != 1 || !strings.Contains(lines[0], "ambiguous") ||
		!strings.Contains(lines[0], "pid 709505, pid 1531038") {
		t.Errorf("legacy multi-pid: %q", lines)
	}
	drainingState := &StateFile{
		PID: 709505, GatewayState: "draining", PIDIsLive: true, RestartRequested: true,
	}
	lines = StatusLines(legacy, drainingState, nil, nil, nil)
	if len(lines) != 1 || !strings.Contains(lines[0], "unknown") ||
		!strings.Contains(lines[0], "draining") {
		t.Errorf("legacy draining state: %q", lines)
	}
	stateOnly := &StateFile{PID: 709505, GatewayState: "running", PIDIsLive: true}
	lines = StatusLines(nil, stateOnly, nil, nil, []int{709505, 1531038})
	if len(lines) != 1 || !strings.Contains(lines[0], "ambiguous") ||
		!strings.Contains(lines[0], "pid 709505, pid 1531038") {
		t.Errorf("state multi-pid: %q", lines)
	}
	drainingMulti := &StateFile{
		PID: 1438, GatewayState: "draining", PIDIsLive: true, RestartRequested: true,
	}
	lines = StatusLines(nil, drainingMulti, nil, nil, []int{1438, 1531038})
	if len(lines) != 1 || !strings.Contains(lines[0], "unknown") ||
		!strings.Contains(lines[0], "live gateway processes: pid 1438, pid 1531038") {
		t.Errorf("draining multi-pid: %q", lines)
	}
	lines = StatusLines(nil, nil, nil, nil, []int{709505, 1531038})
	if len(lines) != 1 || !strings.Contains(lines[0], "ambiguous") {
		t.Errorf("no sources multi-pid: %q", lines)
	}
	serving := &Identify{PID: 1531038, StartTime: 3005678, Role: RoleServing}
	lines = StatusLines(serving, nil, nil, nil, []int{709505, 1531038})
	if len(lines) != 1 || !strings.Contains(lines[0], "✓ Serving gateway: pid 1531038") {
		t.Errorf("proven serving beside second pid: %q", lines)
	}
}

// TestChainRewriteNamesServingProcess pins the chained-across-overlap
// rewrite: the ack's pid names the socket owner (the draining predecessor);
// the rewrite points the agent at the serving process that will actually
// restart, and leaves every other ack shape untouched.
func TestChainRewriteNamesServingProcess(t *testing.T) {
	predecessor := &Identify{PID: 709505, Role: RoleDrainingPredecessor,
		SuccessorPID: 1531038, SuccessorReady: true}
	ack := map[string]any{"queued": true, "pid": 709505, "status": "chained",
		"active_agents": 1}
	rewritten := ChainRewrite(ack, 1531038, predecessor)
	if rewritten["pid"] != 1531038 {
		t.Errorf("rewritten pid: got %v", rewritten["pid"])
	}
	note, _ := rewritten["note"].(string)
	if !strings.Contains(note, "pid 1531038") || !strings.Contains(note, "pid 709505") {
		t.Errorf("rewrite note must name both processes: %q", note)
	}
	if ack["pid"] != 709505 {
		t.Error("source ack must not be mutated")
	}
	// Owner is the server: untouched.
	if same := ChainRewrite(map[string]any{"status": "chained", "pid": 1531038},
		1531038, &Identify{PID: 1531038}); same["note"] != nil {
		t.Errorf("same-owner chained ack must stay untouched: %v", same)
	}
	if plain := ChainRewrite(map[string]any{"status": "restarting", "pid": 709505},
		1531038, predecessor); plain["pid"] != 709505 {
		t.Errorf("non-chained ack must stay untouched: %v", plain)
	}
}

// TestApplyServedBySuccessiveTransitions pins the A→B→A pattern: each
// process change renders a transition note and rewrites the record; a
// return to a previously-seen process is still a transition (the record
// names the immediately-preceding server, not history).
func TestApplyServedBySuccessiveTransitions(t *testing.T) {
	store := newMapStore(nil)
	processA := Snapshot{PID: 709505, StartTime: 3001234}
	processB := Snapshot{PID: 1531038, StartTime: 3005678}
	if notes := ApplyServedBy("s", processA, store.get, store.set); len(notes) != 1 {
		t.Fatalf("first turn of A: got %q", notes)
	}
	if notes := ApplyServedBy("s", processB, store.get, store.set); len(notes) != 2 {
		t.Fatalf("A→B transition: got %q", notes)
	}
	if notes := ApplyServedBy("s", processB, store.get, store.set); len(notes) != 1 {
		t.Fatalf("B steady state: got %q", notes)
	}
	back := ApplyServedBy("s", processA, store.get, store.set)
	if len(back) != 2 || !strings.Contains(back[1], "replaced pid 1531038") {
		t.Fatalf("B→A transition must name B as predecessor: %q", back)
	}
}

// TestOwnerRefusalWording pins both refusal clauses: a draining answerer
// (successor not confirmed) and a legacy answerer (no restart-handoff
// state) each name both pids and the operator alternative.
func TestOwnerRefusalWording(t *testing.T) {
	legacy := OwnerRefusal(1531038, &Identify{PID: 709505})
	if !strings.Contains(legacy, "pid 709505") || !strings.Contains(legacy, "pid 1531038") {
		t.Errorf("refusal must name both pids: %q", legacy)
	}
	if !strings.Contains(legacy, "build predates overlap reporting") {
		t.Errorf("legacy clause missing: %q", legacy)
	}
	if !strings.Contains(legacy, "hermes gateway restart") {
		t.Errorf("operator alternative missing: %q", legacy)
	}
	draining := OwnerRefusal(1531038, &Identify{PID: 709505, Role: RoleDrainingPredecessor})
	if !strings.Contains(draining, "successor is not confirmed") {
		t.Errorf("draining clause missing: %q", draining)
	}
}

// TestCandidatePIDsDedupeOrder pins the evidence-set builder: positive pids
// only, first-seen order, duplicates collapsed (the answering pid and the
// scan may name the same process).
func TestCandidatePIDsDedupeOrder(t *testing.T) {
	if got := candidatePIDs(709505, 0, -3, 1531038, 709505); len(got) != 2 ||
		got[0] != 709505 || got[1] != 1531038 {
		t.Errorf("dedupe/order: got %v", got)
	}
	if got := candidatePIDs(); len(got) != 0 {
		t.Errorf("empty input: got %v", got)
	}
	if got := pidList([]int{709505, 1531038}); got != "pid 709505, pid 1531038" {
		t.Errorf("pidList: got %q", got)
	}
}

// TestTurnNotesIncidentTurn pins the live-incident turn shape: a draining
// predecessor's record + the successor serving now → the runtime note names
// the serving process AND the transition note explains the handoff.
func TestTurnNotesIncidentTurn(t *testing.T) {
	successor := Snapshot{
		PID: 1531038, StartTime: 3005678,
		OverlapSuccessorPending: true, PredecessorPID: 709505,
	}
	notes := TurnNotes(successor, EncodeRecord(Snapshot{PID: 709505, StartTime: 3001234}))
	if len(notes) != 2 {
		t.Fatalf("incident turn: got %d notes, want 2 (%q)", len(notes), notes)
	}
	if !strings.Contains(notes[0], "pid 1531038") {
		t.Errorf("runtime note must name the successor: %q", notes[0])
	}
	if !strings.Contains(notes[1], "replaced pid 709505, start 3001234") {
		t.Errorf("transition note must name the predecessor: %q", notes[1])
	}
}

// TestApplyServedByRecordShape pins the durable record's wire shape on
// write: exactly pid + start_time (a process fingerprint — records never
// carry a build sha).
func TestApplyServedByRecordShape(t *testing.T) {
	store := newMapStore(nil)
	current := Snapshot{PID: 1531038, StartTime: 3005678, BuildSHA: "should-not-persist"}
	ApplyServedBy("s", current, store.get, store.set)
	written := store.records[MetaKey("s")]
	if !strings.Contains(written, `"pid":1531038`) ||
		!strings.Contains(written, `"start_time":3005678`) {
		t.Errorf("record shape: %q", written)
	}
	if strings.Contains(written, "should-not-persist") {
		t.Errorf("record must not carry a build sha: %q", written)
	}
}

// TestSharedTableCoversTheHonestyContract mirrors the Python contract
// test's coverage pin from the Go side: every proof-gating branch of the
// ladder must stay in the shared table, so neither twin can silently lose
// the cases that were once false claims.
func TestSharedTableCoversTheHonestyContract(t *testing.T) {
	names := map[string]bool{}
	for _, c := range loadSharedCases(t) {
		names[c.Name] = true
	}
	for _, required := range []string{
		"status_ready_live_successor_green",
		"status_spawned_unattested_starting",
		"status_pid_reuse_dead",
		"status_state_file_draining_never_green",
		"status_legacy_identify_no_overlap",
		"status_legacy_identify_multi_process_ambiguous",
		"status_no_sources_multi_process_ambiguous",
		"status_serving_green_beside_second_pid",
		"transition_changed_names_both_processes",
		"transition_first_turn_silent",
		"record_same_process_skip",
		"record_process_change_write",
		"owner_guard_legacy_predecessor_refuse",
		"owner_guard_attested_chain",
		"owner_refusal_legacy_answerer",
		"chain_rewrite_names_serving_process",
		"turn_notes_first_turn_after_handoff",
	} {
		if !names[required] {
			t.Errorf("shared table lost the %s case", required)
		}
	}
}

// TestStatusLinesScanEvidenceMatrix pins the scan's role: evidence, never
// a claim. A confirmed serving process stays green with two live pids
// (that IS the overlap window); unproven rungs get demoted to ambiguous
// only when the scan shows a second process.
func TestStatusLinesScanEvidenceMatrix(t *testing.T) {
	green := &Identify{PID: 1531038, StartTime: 3005678, Role: RoleServing,
		SuccessorPID: 1531038, SuccessorReady: true}
	// Proven serving role + second live pid → still green (overlap window).
	lines := StatusLines(green, nil, nil, nil, []int{709505, 1531038})
	if !anyLineContains(lines, "✓ Serving gateway: pid 1531038") {
		t.Fatalf("proven role must stay green beside a second pid: %q", lines)
	}
	// Legacy answerer (no overlap block) + second live pid → ambiguous.
	legacy := &Identify{PID: 1531038, StartTime: 3005678}
	lines = StatusLines(legacy, nil, nil, nil, []int{709505, 1531038})
	if !anyLineContains(lines, "ambiguous") {
		t.Fatalf("legacy answerer with a second live pid must be ambiguous: %q", lines)
	}
	// Same legacy answerer as the only gateway process → still never green
	// (the serving role is unprovable without restart-handoff state).
	lines = StatusLines(legacy, nil, nil, nil, []int{1531038})
	if !anyLineContains(lines, "⚠") || anyLineContains(lines, "✓") {
		t.Fatalf("legacy identify must never be green, even alone: %q", lines)
	}
	// Answering pid absent from the scan → dead-process rung.
	lines = StatusLines(legacy, nil, nil, nil, []int{709505})
	if !anyLineContains(lines, "⚠") || anyLineContains(lines, "✓") {
		t.Fatalf("absent pid must not stay green: %q", lines)
	}
}

// TestTransitionNoteSuccessiveProcessNames pins the wording across an
// A→B→C chain: every note names the exact predecessor fingerprint, so a
// carried-over conversation never reads as a new task.
func TestTransitionNoteSuccessiveProcessNames(t *testing.T) {
	processA := Snapshot{PID: 709505, StartTime: 3001234, BuildSHA: "aaaaaaaa"}
	processB := Snapshot{PID: 1531038, StartTime: 3005678, BuildSHA: "bbbbbbbb"}
	processC := Snapshot{PID: 2048123, StartTime: 3009999, BuildSHA: "cccccccc"}
	prevA, okA := ParseRecord(EncodeRecord(processA))
	prevB, okB := ParseRecord(EncodeRecord(processB))
	if !okA || !okB {
		t.Fatalf("record roundtrip failed: %v %v", okA, okB)
	}
	first := TransitionNote(prevA, processB)
	second := TransitionNote(prevB, processC)
	if !strings.Contains(first, "replaced pid 709505, start 3001234") {
		t.Errorf("A→B note: %q", first)
	}
	if !strings.Contains(second, "replaced pid 1531038, start 3005678") {
		t.Errorf("B→C note: %q", second)
	}
	if strings.Contains(first, "2048123") || strings.Contains(second, "709505, start 3001234") {
		t.Errorf("notes must name only the immediate predecessor: %q / %q", first, second)
	}
}

// TestRecordWriteNeededBoundaries pins the write-on-change predicate: a
// matching fingerprint, a fresh session, and each individually-changed
// field.
func TestRecordWriteNeededBoundaries(t *testing.T) {
	current := Snapshot{PID: 1531038, StartTime: 3005678}
	same := EncodeRecord(current)
	if RecordWriteNeeded(same, current) {
		t.Error("identical fingerprint must skip the write")
	}
	if !RecordWriteNeeded("", current) {
		t.Error("empty stored record must write")
	}
	if !RecordWriteNeeded("{not json", current) {
		t.Error("corrupt stored record must write")
	}
	reusedPID := EncodeRecord(Snapshot{PID: 1531038, StartTime: 1111111})
	if !RecordWriteNeeded(reusedPID, current) {
		t.Error("same pid with a different start time is a NEW process — must write")
	}
	replacedPID := EncodeRecord(Snapshot{PID: 709505, StartTime: 3005678})
	if !RecordWriteNeeded(replacedPID, current) {
		t.Error("different pid must write")
	}
}

// TestValidSessionIDForms pins the session-id gate for the durable record:
// it rejects absent/blank ids (never non-blank ones — session keys are
// opaque strings owned by the store layer, not filesystem paths).
func TestValidSessionIDForms(t *testing.T) {
	for _, bad := range []string{"", "   ", "\t\n"} {
		if ValidSessionID(bad) {
			t.Errorf("must reject %q", bad)
		}
	}
	for _, good := range []string{"s", "abc123", "session-1_2", "A.b:c", "../opaque-key"} {
		if !ValidSessionID(good) {
			t.Errorf("must accept %q", good)
		}
	}
}

// TestTurnNotesSilenceForms pins normalization: an absent record and an
// empty-string record (the wire form of silence) are the same no-note.
func TestTurnNotesSilenceForms(t *testing.T) {
	current := Snapshot{PID: 1531038, StartTime: 3005678, BuildSHA: "abc12345"}
	if got := TurnNotes(current, ""); len(got) != 1 {
		t.Errorf("empty record: %q", got)
	}
	if got := TurnNotes(current, "null"); len(got) != 1 {
		t.Errorf("null record: %q", got)
	}
	same := TurnNotes(current, EncodeRecord(current))
	if len(same) != 1 || strings.Contains(same[0], "was served by") {
		t.Errorf("same-process record must not emit a transition note: %q", same)
	}
}

// TestBuildClauseStability pins the short-sha rendering used in notes and
// status lines (8 chars, prefixed) — the record deliberately has none.
func TestBuildClauseStability(t *testing.T) {
	if got := buildClause(1531038, 3005678, "0123456789abcdef"); got != "pid 1531038, start 3005678, build 01234567" {
		t.Errorf("long sha: %q", got)
	}
	if got := buildClause(1531038, 0, "abcd"); got != "pid 1531038, build abcd" {
		t.Errorf("short sha, no start: %q", got)
	}
	if got := buildClause(1531038, 3005678, ""); got != "pid 1531038, start 3005678" {
		t.Errorf("absent sha must stay silent: %q", got)
	}
}

func anyLineContains(lines []string, needle string) bool {
	for _, line := range lines {
		if strings.Contains(line, needle) {
			return true
		}
	}
	return false
}
