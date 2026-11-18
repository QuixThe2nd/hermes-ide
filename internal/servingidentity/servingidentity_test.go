package servingidentity

import (
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// Classification (mirrors tests/gateway/test_serving_identity.py).
// ---------------------------------------------------------------------------

func TestClassifyOrdinaryProcessIsServing(t *testing.T) {
	if got := Classify(Snapshot{PID: 709505}); got != RoleServing {
		t.Fatalf("ordinary snapshot: got %q, want %q", got, RoleServing)
	}
}

func TestClassifyOverlapSuccessor(t *testing.T) {
	s := Snapshot{PID: 709505, OverlapSuccessorPending: true, PredecessorPID: 1438}
	if got := Classify(s); got != RoleOverlapSuccessor {
		t.Fatalf("pending successor: got %q, want %q", got, RoleOverlapSuccessor)
	}
}

func TestClassifyDrainingPredecessorNeedsRestartRequest(t *testing.T) {
	s := Snapshot{PID: 1438, Draining: true, RestartRequested: true, SuccessorPID: 709505}
	if got := Classify(s); got != RoleDrainingPredecessor {
		t.Fatalf("draining predecessor: got %q, want %q", got, RoleDrainingPredecessor)
	}
	// Pure shutdown/pause drain (no restart request) is not a handoff.
	shutdown := Snapshot{PID: 1438, Draining: true}
	if got := Classify(shutdown); got != RoleServing {
		t.Fatalf("shutdown drain: got %q, want %q", got, RoleServing)
	}
}

func TestClassifyRestartRequestWinsOverPending(t *testing.T) {
	// A restart-requested process is ending; the pending flag cannot turn a
	// dying predecessor back into a successor.
	s := Snapshot{PID: 1438, RestartRequested: true, OverlapSuccessorPending: true}
	if got := Classify(s); got != RoleDrainingPredecessor {
		t.Fatalf("restart request precedence: got %q, want %q", got, RoleDrainingPredecessor)
	}
}

// ---------------------------------------------------------------------------
// Runtime note wording.
// ---------------------------------------------------------------------------

func TestRuntimeNoteNamesServingPidAndExternalPidCaveat(t *testing.T) {
	note := RuntimeNote(Snapshot{PID: 709505, StartTime: 123456, BuildSHA: "abcdef1234"})
	for _, want := range []string{
		"[System note: This turn is served by Hermes gateway pid 709505, start 123456, build abcdef12.",
		"systemctl", "pid file",
		"is the process running this conversation.",
	} {
		if !strings.Contains(note, want) {
			t.Fatalf("serving note missing %q:\n%s", want, note)
		}
	}
	if strings.Contains(note, "predecessor") || strings.Contains(note, "successor") {
		t.Fatalf("serving note must not claim a transition:\n%s", note)
	}
}

func TestRuntimeNoteOverlapSuccessorExplainsDrainingPredecessor(t *testing.T) {
	note := RuntimeNote(Snapshot{
		PID: 709505, OverlapSuccessorPending: true, PredecessorPID: 1438,
	})
	for _, want := range []string{
		"pid 709505", "predecessor pid 1438", "draining",
		"gateway_state.json may still report pid 1438",
		"the restart already succeeded", "not ... incomplete restart",
	} {
		if want == "not ... incomplete restart" {
			if !strings.Contains(note, "incomplete restart") {
				t.Fatalf("overlap note missing 'incomplete restart':\n%s", note)
			}
			continue
		}
		if !strings.Contains(note, want) {
			t.Fatalf("overlap note missing %q:\n%s", want, note)
		}
	}
}

func TestRuntimeNoteDrainingPredecessorDirectsSuccessorAndFinish(t *testing.T) {
	note := RuntimeNote(Snapshot{
		PID: 1438, RestartRequested: true, Draining: true, SuccessorPID: 709505,
	})
	for _, want := range []string{
		"pid 1438", "successor pid 709505", "Finish this turn's work",
		"do not call restart again",
	} {
		if !strings.Contains(note, want) {
			t.Fatalf("draining note missing %q:\n%s", want, note)
		}
	}
}

func TestRuntimeNoteDrainingWithoutSpawnedSuccessorStaysHonest(t *testing.T) {
	note := RuntimeNote(Snapshot{PID: 1438, RestartRequested: true})
	if !strings.Contains(note, "a successor is being started") {
		t.Fatalf("spawn-pending note must not invent a successor pid:\n%s", note)
	}
	if strings.Contains(note, "pid 709505") {
		t.Fatalf("spawn-pending note invented a successor pid:\n%s", note)
	}
}

// ---------------------------------------------------------------------------
// Status lines ladder.
// ---------------------------------------------------------------------------

func TestStatusLinesDistinguishServingSuccessorFromDrainingPredecessor(t *testing.T) {
	id := &Identify{
		PID: 1438, Role: RoleDrainingPredecessor, SuccessorPID: 709505,
	}
	lines := StatusLines(id, nil, "", func(pid int) bool { return true })
	joined := strings.Join(lines, "\n")
	if !strings.Contains(joined, "✓ Serving gateway: pid 709505") {
		t.Fatalf("serving claim must name the successor:\n%s", joined)
	}
	for _, want := range []string{"pid 1438", "Draining predecessor", "not an incomplete restart"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("predecessor context missing %q:\n%s", want, joined)
		}
	}
	if len(lines) != 2 {
		t.Fatalf("expected serving + predecessor lines, got %d:\n%s", len(lines), joined)
	}
}

func TestStatusLinesWarnWhenReportedSuccessorDied(t *testing.T) {
	id := &Identify{PID: 1438, Role: RoleDrainingPredecessor, SuccessorPID: 709505}
	lines := StatusLines(id, nil, "", func(pid int) bool { return false })
	joined := strings.Join(lines, "\n")
	if !strings.HasPrefix(lines[0], "⚠ Serving gateway: unknown") {
		t.Fatalf("dead successor must not yield a serving claim:\n%s", joined)
	}
	if !strings.Contains(joined, "pid 709505") || !strings.Contains(joined, "not running") {
		t.Fatalf("dead successor line must name it:\n%s", joined)
	}
}

func TestStatusLinesPlainIdentifyNamesAnsweringPid(t *testing.T) {
	lines := StatusLines(&Identify{PID: 709505, StartTime: 42}, nil, "", nil)
	want := "✓ Serving gateway: pid 709505, start 42 — answering on the gateway control socket"
	if len(lines) != 1 || lines[0] != want {
		t.Fatalf("plain identify: got %v, want [%s]", lines, want)
	}
}

func TestStatusLinesOverlapSuccessorAuthoritative(t *testing.T) {
	lines := StatusLines(&Identify{PID: 709505, Role: RoleOverlapSuccessor}, nil, "", nil)
	if len(lines) != 1 || !strings.Contains(lines[0], "restart successor, now authoritative") {
		t.Fatalf("post-claim successor line: %v", lines)
	}
}

func TestStatusLinesStateFileFallbackWhenSocketSilent(t *testing.T) {
	st := &StateFile{PID: 1438, GatewayState: "draining", PIDIsLive: true}
	lines := StatusLines(nil, st, "", nil)
	want := "✓ Serving gateway: pid 1438 (from gateway_state.json, state 'draining')"
	if len(lines) != 1 || lines[0] != want {
		t.Fatalf("state fallback: got %v, want [%s]", lines, want)
	}
}

func TestStatusLinesUnknownWhenStateStale(t *testing.T) {
	st := &StateFile{PID: 1438, GatewayState: "draining", PIDIsLive: true, IsStale: true}
	lines := StatusLines(nil, st, "", nil)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠ Serving gateway: unknown") ||
		!strings.Contains(lines[0], "is stale") {
		t.Fatalf("stale state: %v", lines)
	}
}

func TestStatusLinesUnknownWhenStatePidDead(t *testing.T) {
	st := &StateFile{PID: 1438, GatewayState: "running"}
	lines := StatusLines(nil, st, "", nil)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠ Serving gateway: unknown") ||
		!strings.Contains(lines[0], "pid 1438 is not running") {
		t.Fatalf("dead state pid: %v", lines)
	}
}

func TestStatusLinesUnknownWhenNoSources(t *testing.T) {
	lines := StatusLines(nil, nil, "", nil)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠ Serving gateway: unknown") {
		t.Fatalf("no sources: %v", lines)
	}
}

func TestStatusLinesChainedRestartLine(t *testing.T) {
	lines := StatusLines(&Identify{PID: 709505}, nil, "telegram user 42 (restart)", nil)
	if len(lines) != 2 || !strings.Contains(lines[1], "Chained restart queued") ||
		!strings.Contains(lines[1], "telegram user 42 (restart)") {
		t.Fatalf("chained line: %v", lines)
	}
}

func TestStatusLinesNoChainedLineWithoutMarker(t *testing.T) {
	lines := StatusLines(&Identify{PID: 709505}, nil, "", nil)
	for _, ln := range lines {
		if strings.Contains(ln, "Chained restart") {
			t.Fatalf("unexpected chained line: %v", lines)
		}
	}
}

// ---------------------------------------------------------------------------
// Overlap fields + chained source.
// ---------------------------------------------------------------------------

func TestOverlapFieldsOmitUnknowns(t *testing.T) {
	serving := OverlapFields(Snapshot{PID: 709505})
	if len(serving) != 1 || serving["role"] != "serving" {
		t.Fatalf("serving overlap fields: %v", serving)
	}
	successor := OverlapFields(Snapshot{PID: 709505, OverlapSuccessorPending: true, PredecessorPID: 1438})
	if len(successor) != 2 || successor["role"] != "overlap-successor" ||
		successor["predecessor_pid"].(int) != 1438 {
		t.Fatalf("successor overlap fields: %v", successor)
	}
	draining := OverlapFields(Snapshot{PID: 1438, RestartRequested: true, SuccessorPID: 709505})
	if len(draining) != 2 || draining["role"] != "draining-predecessor" ||
		draining["successor_pid"].(int) != 709505 {
		t.Fatalf("draining overlap fields: %v", draining)
	}
}

func TestChainedSourceFallback(t *testing.T) {
	if got := ChainedSource(""); got != "an earlier restart request" {
		t.Fatalf("empty chained source: %q", got)
	}
	if got := ChainedSource("  telegram user 42 "); got != "telegram user 42" {
		t.Fatalf("chained source trim: %q", got)
	}
}

// ---------------------------------------------------------------------------
// Full contract matrix: every Python twin case, Go-side.
// ---------------------------------------------------------------------------

func TestBuildClauseVariants(t *testing.T) {
	cases := []struct {
		name string
		snap Snapshot
		want string
	}{
		{"pid only", Snapshot{PID: 7}, "pid 7"},
		{"pid and start", Snapshot{PID: 7, StartTime: 99}, "pid 7, start 99"},
		{"pid start build", Snapshot{PID: 7, StartTime: 99, BuildSHA: "ab"}, "pid 7, start 99, build ab"},
		{"build truncated to 8", Snapshot{PID: 7, BuildSHA: "abcdefghij"}, "pid 7, build abcdefgh"},
		{"exactly 8 not truncated", Snapshot{PID: 7, BuildSHA: "abcdefgh"}, "pid 7, build abcdefgh"},
	}
	for _, tc := range cases {
		if got := buildClause(tc.snap.PID, tc.snap.StartTime, tc.snap.BuildSHA); got != tc.want {
			t.Errorf("%s: got %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestClassifyEdgeMatrix(t *testing.T) {
	cases := []struct {
		name string
		snap Snapshot
		want Role
	}{
		{"zero snapshot", Snapshot{}, RoleServing},
		{"pending without predecessor pid", Snapshot{OverlapSuccessorPending: true}, RoleOverlapSuccessor},
		{"drain only", Snapshot{Draining: true}, RoleServing},
		{"restart request only", Snapshot{RestartRequested: true}, RoleDrainingPredecessor},
		{"restart with junk successor", Snapshot{RestartRequested: true, SuccessorPID: -3}, RoleDrainingPredecessor},
		{"pending with junk predecessor", Snapshot{OverlapSuccessorPending: true, PredecessorPID: -1}, RoleOverlapSuccessor},
	}
	for _, tc := range cases {
		if got := Classify(tc.snap); got != tc.want {
			t.Errorf("%s: got %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestRuntimeNoteOverlapSuccessorWithoutPredecessorFallsToBase(t *testing.T) {
	// Pending flag set but no provable predecessor pid: the note must not
	// name one; the base identity line is the honest rendering.
	note := RuntimeNote(Snapshot{PID: 709505, OverlapSuccessorPending: true})
	if strings.Contains(note, "predecessor pid") {
		t.Fatalf("note invented a predecessor:\n%s", note)
	}
	if !strings.Contains(note, "is the process running this conversation.") {
		t.Fatalf("note must keep the base caveat:\n%s", note)
	}
}

func TestStatusLinesIdentifyWithoutPidFallsToStateLadder(t *testing.T) {
	// A malformed identify answer (no pid) is not an identity source.
	st := &StateFile{PID: 1438, GatewayState: "running", PIDIsLive: true}
	lines := StatusLines(&Identify{Role: RoleServing}, st, "", nil)
	if len(lines) != 1 || !strings.Contains(lines[0], "from gateway_state.json") {
		t.Fatalf("malformed identify must fall through:\n%v", lines)
	}
}

func TestStatusLinesNegativeStatePidIsUnknown(t *testing.T) {
	lines := StatusLines(nil, &StateFile{PID: -5, PIDIsLive: true}, "", nil)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠ Serving gateway: unknown — no control socket") {
		t.Fatalf("junk state pid: %v", lines)
	}
}

func TestStatusLinesUnknownVariantsNameTheReason(t *testing.T) {
	cases := []struct {
		name string
		st   *StateFile
		want string
	}{
		{"stale", &StateFile{PID: 1, GatewayState: "draining", IsStale: true},
			"gateway_state.json is stale (state 'draining')"},
		{"dead pid", &StateFile{PID: 1438, GatewayState: "running"},
			"gateway_state.json pid 1438 is not running"},
		{"no sources", nil,
			"no control socket answer and no gateway_state.json record"},
	}
	for _, tc := range cases {
		lines := StatusLines(nil, tc.st, "", nil)
		if len(lines) != 1 || !strings.HasPrefix(lines[0], "⚠ Serving gateway: unknown") {
			t.Errorf("%s: must be an unknown line: %v", tc.name, lines)
			continue
		}
		if !strings.Contains(lines[0], tc.want) {
			t.Errorf("%s: reason %q missing from %q", tc.name, tc.want, lines[0])
		}
	}
}

func TestStatusLinesChainedRidesEveryLadderRung(t *testing.T) {
	cases := []struct {
		name string
		id   *Identify
		st   *StateFile
	}{
		{"identify", &Identify{PID: 709505}, nil},
		{"state", nil, &StateFile{PID: 1438, PIDIsLive: true}},
		{"unknown", nil, nil},
	}
	for _, tc := range cases {
		lines := StatusLines(tc.id, tc.st, "cli restart", nil)
		if len(lines) != 2 || !strings.Contains(lines[1], "Chained restart queued") ||
			!strings.Contains(lines[1], "cli restart") {
			t.Errorf("%s: chained line must append: %v", tc.name, lines)
		}
	}
}

func TestStatusLinesPIDLiveNilTrustsReportedSuccessor(t *testing.T) {
	id := &Identify{PID: 1438, Role: RoleDrainingPredecessor, SuccessorPID: 709505}
	lines := StatusLines(id, nil, "", nil)
	if len(lines) != 2 || !strings.Contains(lines[0], "pid 709505") {
		t.Fatalf("nil probe must trust the reported successor: %v", lines)
	}
}

func TestOverlapFieldsOmitJunkPids(t *testing.T) {
	fields := OverlapFields(Snapshot{RestartRequested: true, SuccessorPID: -2, PredecessorPID: 0})
	if len(fields) != 1 || fields["role"] != "draining-predecessor" {
		t.Fatalf("junk pids must be omitted: %v", fields)
	}
}

func TestRuntimeNoteNeverEmptyForAnyRole(t *testing.T) {
	for _, snap := range []Snapshot{
		{PID: 1}, {PID: 1, OverlapSuccessorPending: true, PredecessorPID: 2},
		{PID: 1, OverlapSuccessorPending: true}, {PID: 1, RestartRequested: true},
		{PID: 1, RestartRequested: true, SuccessorPID: 2},
	} {
		if note := RuntimeNote(snap); !strings.HasPrefix(note, "[System note: This turn is served by Hermes gateway pid 1") {
			t.Fatalf("note must always open with the serving identity: %q", note)
		}
	}
}

// ---------------------------------------------------------------------------
// Restart-result identity contract.
// ---------------------------------------------------------------------------

func TestRestartSuccessContentCarriesOldAndNewIdentity(t *testing.T) {
	content := RestartSuccessContent("pid 1438", "pid 709505", "")
	want := "Restarted successfully\n" +
		"Old process: pid 1438\n" +
		"New process: pid 709505\n" +
		"This is a tool result for you, not a message to send or quote " +
		"to the user. This chat is no longer running in pid 1438; " +
		"you are now running in pid 709505. Continue the interrupted " +
		"task with your tools until the original work is verified, then " +
		"reply to the user about that work."
	if content != want {
		t.Fatalf("content mismatch:\ngot:  %q\nwant: %q", content, want)
	}
}

func TestRestartSuccessContentWithoutOldProcess(t *testing.T) {
	content := RestartSuccessContent("", "pid 709505", "")
	if strings.Contains(content, "Old process:") {
		t.Fatalf("legacy notice must not print an unknown old identity:\n%s", content)
	}
	if !strings.Contains(content, "New process: pid 709505") {
		t.Fatalf("notice must name the new process:\n%s", content)
	}
}

func TestRestartSuccessContentHubLine(t *testing.T) {
	content := RestartSuccessContent("pid 1438", "pid 709505", "bounce ok")
	if !strings.HasSuffix(content, "\nHub: bounce ok") {
		t.Fatalf("hub report must be one trailing line:\n%s", content)
	}
}

func TestParseRestartSuccessContentRoundTrips(t *testing.T) {
	for _, tc := range []struct{ old, hub string }{
		{"pid 1438", ""}, {"", ""}, {"pid 1438", "bounce ok"},
	} {
		content := RestartSuccessContent(tc.old, "pid 709505", tc.hub)
		gotOld, gotNew, ok := ParseRestartSuccessContent(content)
		if !ok || gotOld != tc.old || gotNew != "pid 709505" {
			t.Errorf("round-trip (%q, %q): got (%q, %q, %v)", tc.old, tc.hub, gotOld, gotNew, ok)
		}
	}
}

func TestParseRestartSuccessContentRejectsPartialShapes(t *testing.T) {
	for name, content := range map[string]string{
		"bare first line":  NoticeFirstLine,
		"missing new":      "Restarted successfully\nOld process: pid 1438",
		"reworded trailer": "Restarted successfully\nOld process: pid 1438\nNew process: pid 709505\nGo on.",
		"prefix mismatch":  "restarted successfully\nNew process: pid 709505",
		"empty new":        "Restarted successfully\nNew process: \nThis is a tool result for you.",
		"empty hub":        "Restarted successfully\nNew process: pid 1\nx\nHub: ",
	} {
		if _, _, ok := ParseRestartSuccessContent(content); ok {
			t.Errorf("%s: must not be trusted: %q", name, content)
		}
	}
}

func TestQueuedResultSaysQueuedAndNamesOldProcess(t *testing.T) {
	payload := QueuedResult("chained", 1, true, "pid 1438")
	if payload["queued"] != true {
		t.Fatalf("committed reply must carry queued=true: %v", payload)
	}
	if payload["process"] != "pid 1438" {
		t.Fatalf("process must name the OLD gateway: %v", payload)
	}
	if note, _ := payload["note"].(string); !strings.Contains(note, "not completed") ||
		!strings.Contains(note, "OLD gateway") || !strings.Contains(note, NoticeFirstLine) {
		t.Fatalf("queued note must be self-describing: %v", payload["note"])
	}
	if payload[TurnControlField] != GatewayRestartValue {
		t.Fatalf("terminal status must stamp the control field: %v", payload)
	}
}

func TestQueuedResultControlFieldOnlyForTerminalStatuses(t *testing.T) {
	if stamp := TurnControlFieldFor("restarting"); stamp == nil {
		t.Fatalf("restarting is terminal")
	}
	if stamp := TurnControlFieldFor("refused"); stamp != nil {
		t.Fatalf("non-terminal status must not stamp: %v", stamp)
	}
	if payload := QueuedResult("refused", 0, nil, "pid 1"); payload[TurnControlField] != nil {
		t.Fatalf("non-terminal queued result must not stamp: %v", payload)
	}
}

func TestQueuedResultOmitsUnknownProcess(t *testing.T) {
	payload := QueuedResult("restarting", 0, nil, "")
	if _, present := payload["process"]; present {
		t.Fatalf("unknown process must be omitted, never faked: %v", payload)
	}
}
