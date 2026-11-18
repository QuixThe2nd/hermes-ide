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
	lines := StatusLines(legacy, nil, "", nil, []int{709505, 1531038})
	if len(lines) != 1 || !strings.Contains(lines[0], "ambiguous") ||
		!strings.Contains(lines[0], "pid 709505, pid 1531038") {
		t.Errorf("legacy multi-pid: %q", lines)
	}
	drainingState := &StateFile{
		PID: 709505, GatewayState: "draining", PIDIsLive: true, RestartRequested: true,
	}
	lines = StatusLines(legacy, drainingState, "", nil, nil)
	if len(lines) != 1 || !strings.Contains(lines[0], "unknown") ||
		!strings.Contains(lines[0], "draining") {
		t.Errorf("legacy draining state: %q", lines)
	}
	stateOnly := &StateFile{PID: 709505, GatewayState: "running", PIDIsLive: true}
	lines = StatusLines(nil, stateOnly, "", nil, []int{709505, 1531038})
	if len(lines) != 1 || !strings.Contains(lines[0], "ambiguous") ||
		!strings.Contains(lines[0], "pid 709505, pid 1531038") {
		t.Errorf("state multi-pid: %q", lines)
	}
	drainingMulti := &StateFile{
		PID: 1438, GatewayState: "draining", PIDIsLive: true, RestartRequested: true,
	}
	lines = StatusLines(nil, drainingMulti, "", nil, []int{1438, 1531038})
	if len(lines) != 1 || !strings.Contains(lines[0], "unknown") ||
		!strings.Contains(lines[0], "live gateway processes: pid 1438, pid 1531038") {
		t.Errorf("draining multi-pid: %q", lines)
	}
	lines = StatusLines(nil, nil, "", nil, []int{709505, 1531038})
	if len(lines) != 1 || !strings.Contains(lines[0], "ambiguous") {
		t.Errorf("no sources multi-pid: %q", lines)
	}
	serving := &Identify{PID: 1531038, StartTime: 3005678, Role: RoleServing}
	lines = StatusLines(serving, nil, "", nil, []int{709505, 1531038})
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
