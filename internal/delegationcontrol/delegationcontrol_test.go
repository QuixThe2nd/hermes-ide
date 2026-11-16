package delegationcontrol

import (
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Test scaffolding mirroring the Python pin fixtures.
// ---------------------------------------------------------------------------

// spyInterrupt mirrors _interrupt_spy: records invocations.
func spyInterrupt() (func(), *int32) {
	var calls int32
	return func() { atomic.AddInt32(&calls, 1) }, &calls
}

// cliRecord mirrors _seed_cli_record: a live coding-run record shaped exactly
// as dispatch_background_delegation writes it (no runner tid, unmonitored).
func cliRecord(id, tool, kind, parentSessionID string, mutators ...func(*Record)) *Record {
	rec := &Record{
		DelegationID:    id,
		Goal:            "Fix the flaky integration test",
		SessionKey:      "",
		ParentSessionID: parentSessionID,
		Tool:            tool,
		ResultKind:      kind,
		Status:          StatusRunning,
		DispatchedAt:    float64(time.Now().UnixNano()) / 1e9,
	}
	for _, m := range mutators {
		m(rec)
	}
	return rec
}

func withStatus(status string) func(*Record) { return func(r *Record) { r.Status = status } }
func withSessionKey(key string) func(*Record) {
	return func(r *Record) { r.SessionKey = key }
}
func withKind(kind string) func(*Record) { return func(r *Record) { r.ResultKind = kind } }
func withGoal(goal string) func(*Record) { return func(r *Record) { r.Goal = goal } }

// lineageResolver mirrors _StubLineageDB: pre-compression id -> lineage tip.
func lineageResolver(lineage map[string]string) func(string) string {
	return func(sid string) string {
		if tip, ok := lineage[sid]; ok {
			return tip
		}
		return sid
	}
}

// ---------------------------------------------------------------------------
// Steer: owned live coding runs answer not_steerable, never signal.
// ---------------------------------------------------------------------------

func TestSteerOwnedLiveClaudeRunIsNotSteerable(t *testing.T) {
	fn, calls := spyInterrupt()
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_5c686284", "delegate_claude_agent", KindCLIAgent, "sess-cli-steer-1",
		func(r *Record) { r.InterruptFn = fn }))
	caller := &Caller{SessionID: "sess-cli-steer-1"}

	reply, err := reg.Steer("deleg_5c686284", "Prefer the streaming parser", caller)
	if err != nil {
		t.Fatalf("steer of an owned live run must not error: %v", err)
	}
	if reply.Action != "steer" || reply.SubagentID != "deleg_5c686284" {
		t.Fatalf("unexpected reply identity: %+v", reply)
	}
	if reply.Status != StatusNotSteerable {
		t.Fatalf("status = %q, want %q", reply.Status, StatusNotSteerable)
	}
	low := strings.ToLower(reply.Note)
	for _, want := range []string{"mid-run", "stop", "same id", "re-delegate", "deleg_5c686284"} {
		if !strings.Contains(low, want) {
			t.Fatalf("note %q missing %q", reply.Note, want)
		}
	}
	if n := atomic.LoadInt32(calls); n != 0 {
		t.Fatalf("steer must never fire the interrupt channel; fired %d time(s)", n)
	}
}

func TestSteerOwnedLiveCursorRunIsNotSteerable(t *testing.T) {
	fn, calls := spyInterrupt()
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_9d13aa02", "delegate_cursor_agent", KindCloudAgent, "sess-cli-steer-2",
		func(r *Record) { r.InterruptFn = fn }))
	caller := &Caller{SessionID: "sess-cli-steer-2"}

	reply, err := reg.Steer("deleg_9d13aa02", "Use the staging project", caller)
	if err != nil {
		t.Fatalf("steer of an owned live cloud run must not error: %v", err)
	}
	if reply.Status != StatusNotSteerable {
		t.Fatalf("status = %q, want %q", reply.Status, StatusNotSteerable)
	}
	if !strings.Contains(strings.ToLower(reply.Note), "cloud run") {
		t.Fatalf("note must name the cloud-run constraint: %q", reply.Note)
	}
	if n := atomic.LoadInt32(calls); n != 0 {
		t.Fatalf("steer must never fire the interrupt channel; fired %d time(s)", n)
	}
}

func TestSteerEmptyMessageIsRejectedBeforeOutcome(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_steer-empty", "delegate_claude_agent", KindCLIAgent, "sess-a"))
	caller := &Caller{SessionID: "sess-a"}

	if _, err := reg.Steer("deleg_steer-empty", "   ", caller); !errors.Is(err, ErrSteerMessageNeeded) {
		t.Fatalf("err = %v, want ErrSteerMessageNeeded", err)
	}
}

func TestSteerEmptyIDIsRejected(t *testing.T) {
	if _, err := NewRegistry().Steer("  ", "text", &Caller{}); !errors.Is(err, ErrSubagentIDRequired) {
		t.Fatalf("err = %v, want ErrSubagentIDRequired", err)
	}
}

// ---------------------------------------------------------------------------
// list: exactly the owned live coding runs.
// ---------------------------------------------------------------------------

func TestListShowsOwnedLiveCodingRunsAndExcludesTheRest(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_3aa10001", "delegate_claude_agent", KindCLIAgent, "sess-cli-list-1",
		withGoal("Claude: refactor auth module")))
	reg.Put(cliRecord("deleg_3aa10002", "delegate_cursor_agent", KindCloudAgent, "sess-cli-list-1",
		withGoal("Cursor: migrate the endpoint")))
	reg.Put(cliRecord("deleg_3aa10003", "delegate_claude_agent", KindCLIAgent, "sess-someone-else",
		withGoal("Someone else's claude run")))
	reg.Put(cliRecord("deleg_3aa10004", "delegate_cursor_agent", KindCloudAgent, "sess-cli-list-1",
		withStatus(StatusCompleted), withGoal("Already finished")))
	reg.Put(cliRecord("deleg_3aa10005", "delegate_agent", KindSubagentBatch, "sess-cli-list-1",
		withGoal("Native batch unit")))
	reg.Put(cliRecord("deleg_3aa10006", "delegate_claude_agent", KindCLIAgent, "sess-cli-list-1",
		withStatus(StatusFinalize), withGoal("Delivering already")))
	caller := &Caller{SessionID: "sess-cli-list-1"}

	payload := BuildListPayload(nil, reg, caller)
	wantIDs := []string{"deleg_3aa10001", "deleg_3aa10002"}
	if payload.Count != len(wantIDs) || len(payload.Subagents) != len(wantIDs) {
		t.Fatalf("count = %d (%d entries), want %d; entries=%+v",
			payload.Count, len(payload.Subagents), len(wantIDs), payload.Subagents)
	}
	got := map[string]ListEntry{}
	for _, e := range payload.Subagents {
		got[e.SubagentID] = e
	}
	for _, id := range wantIDs {
		if _, ok := got[id]; !ok {
			t.Fatalf("missing owned live coding run %s in %+v", id, payload.Subagents)
		}
	}
	claude := got["deleg_3aa10001"]
	if claude.Tool != "delegate_claude_agent" || claude.AcceptingSteer || claude.Status != StatusRunning || claude.Goal == "" {
		t.Fatalf("claude entry wrong: %+v", claude)
	}
	cursor := got["deleg_3aa10002"]
	if cursor.Tool != "delegate_cursor_agent" || cursor.AcceptingSteer || cursor.Goal == "" || cursor.Status == "" {
		t.Fatalf("cursor entry wrong: %+v", cursor)
	}
	if payload.Note != "" {
		t.Fatalf("empty-registry note must be suppressed when a coding run is listed: %q", payload.Note)
	}
}

func TestListStallingRunsAreLive(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_stall-1", "delegate_claude_agent", KindCLIAgent, "sess-stall",
		withStatus(StatusStalling)))
	payload := BuildListPayload(nil, reg, &Caller{SessionID: "sess-stall"})
	if payload.Count != 1 || payload.Subagents[0].Status != StatusStalling {
		t.Fatalf("stalling is live and must be listed: %+v", payload)
	}
}

func TestListEmptyRegistryKeepsTheNote(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_other-1", "delegate_claude_agent", KindCLIAgent, "sess-other"))
	payload := BuildListPayload(nil, reg, &Caller{SessionID: "sess-mine"})
	if payload.Count != 0 {
		t.Fatalf("foreign runs must not be listed: %+v", payload.Subagents)
	}
	if !strings.Contains(payload.Note, "No live subagents right now") {
		t.Fatalf("empty listing must keep the note: %q", payload.Note)
	}
}

func TestListMergesNativeEntriesWithCodingRuns(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_mix-1", "delegate_cursor_agent", KindCloudAgent, "sess-mix"))
	native := []ListEntry{{
		SubagentID: "sid-native-1", Tool: "delegate_agent", Goal: "native goal",
		Status: StatusRunning, AcceptingSteer: true,
	}}
	payload := BuildListPayload(native, reg, &Caller{SessionID: "sess-mix"})
	if payload.Count != 2 {
		t.Fatalf("count = %d, want 2 (native + coding run)", payload.Count)
	}
	if payload.Note != "" {
		t.Fatalf("note must be suppressed: %q", payload.Note)
	}
	byID := map[string]ListEntry{}
	for _, e := range payload.Subagents {
		byID[e.SubagentID] = e
	}
	if !byID["sid-native-1"].AcceptingSteer {
		t.Fatalf("native entries keep accepting_steer=true: %+v", byID["sid-native-1"])
	}
	if byID["deleg_mix-1"].AcceptingSteer {
		t.Fatalf("coding runs never accept steer: %+v", byID["deleg_mix-1"])
	}
}

// ---------------------------------------------------------------------------
// stop: interrupts exactly once; foreign / spineless fail closed.
// ---------------------------------------------------------------------------

func TestStopOwnedLiveCodingRunInterruptsExactlyOnce(t *testing.T) {
	fn, calls := spyInterrupt()
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_4bb10001", "delegate_claude_agent", KindCLIAgent, "sess-cli-stop-1",
		func(r *Record) { r.InterruptFn = fn }))
	caller := &Caller{SessionID: "sess-cli-stop-1"}

	reply, err := reg.Stop("deleg_4bb10001", caller)
	if err != nil {
		t.Fatalf("stop of an owned live run must succeed: %v", err)
	}
	if reply.Action != "stop" || reply.SubagentID != "deleg_4bb10001" || reply.Status != StatusInterruptRequested {
		t.Fatalf("unexpected stop reply: %+v", reply)
	}
	if n := atomic.LoadInt32(calls); n != 1 {
		t.Fatalf("interrupt fired %d time(s), want exactly 1", n)
	}
}

func TestStopForeignCodingRunIsRefusedWithoutInterrupt(t *testing.T) {
	fn, calls := spyInterrupt()
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_4bb10002", "delegate_cursor_agent", KindCloudAgent, "sess-someone-else-2",
		func(r *Record) { r.InterruptFn = fn }))
	caller := &Caller{SessionID: "sess-cli-stop-2"}

	_, err := reg.Stop("deleg_4bb10002", caller)
	if !errors.Is(err, ErrNoLiveSubagent) {
		t.Fatalf("err = %v, want ErrNoLiveSubagent", err)
	}
	if !strings.Contains(err.Error(), "No live subagent 'deleg_4bb10002'") {
		t.Fatalf("refusal must keep the model-facing text: %v", err)
	}
	if n := atomic.LoadInt32(calls); n != 0 {
		t.Fatalf("foreign stop must not interrupt; fired %d time(s)", n)
	}
}

func TestStopFailsClosedWithoutAnySessionSpine(t *testing.T) {
	fn, calls := spyInterrupt()
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_4bb10003", "delegate_claude_agent", KindCLIAgent, "",
		withSessionKey("telegram:999:guard"), func(r *Record) { r.InterruptFn = fn }))
	caller := &Caller{SessionID: "", CurrentSessionKey: ""}

	if _, err := reg.Stop("deleg_4bb10003", caller); !errors.Is(err, ErrNoLiveSubagent) {
		t.Fatalf("err = %v, want ErrNoLiveSubagent (empty spine never claims)", err)
	}
	if n := atomic.LoadInt32(calls); n != 0 {
		t.Fatalf("spineless stop must not interrupt; fired %d time(s)", n)
	}
}

func TestStopWithNoLiveChannelAnswersInterruptFailed(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_stop-dead", "delegate_claude_agent", KindCLIAgent, "sess-dead"))
	_, err := reg.Stop("deleg_stop-dead", &Caller{SessionID: "sess-dead"})
	if !errors.Is(err, ErrInterruptFailed) {
		t.Fatalf("err = %v, want ErrInterruptFailed (never lie about signalling)", err)
	}
}

func TestStopWithOnlyThreadBitChannelStillSignals(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_stop-tid", "delegate_claude_agent", KindCLIAgent, "sess-tid",
		func(r *Record) { r.RunnerTID = 42 }))
	reply, err := reg.Stop("deleg_stop-tid", &Caller{SessionID: "sess-tid"})
	if err != nil || reply.Status != StatusInterruptRequested {
		t.Fatalf("reply=%+v err=%v; the worker-thread bit alone is a live channel", reply, err)
	}
}

func TestStopCompletedRunIsRefused(t *testing.T) {
	fn, calls := spyInterrupt()
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_stop-done", "delegate_cursor_agent", KindCloudAgent, "sess-done",
		withStatus(StatusCompleted), func(r *Record) { r.InterruptFn = fn }))
	if _, err := reg.Stop("deleg_stop-done", &Caller{SessionID: "sess-done"}); !errors.Is(err, ErrNoLiveSubagent) {
		t.Fatalf("err = %v, want ErrNoLiveSubagent (completed runs are not live)", err)
	}
	if n := atomic.LoadInt32(calls); n != 0 {
		t.Fatalf("completed stop must not interrupt; fired %d time(s)", n)
	}
}

// An interrupt_fn that re-enters the registry must not deadlock: the record is
// snapshotted under the lock and signalled outside it.
func TestStopSignalsOutsideTheRegistryLock(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_stop-reent", "delegate_claude_agent", KindCLIAgent, "sess-reent",
		func(r *Record) {
			r.InterruptFn = func() {
				// Re-enter the locked registry from inside the interrupt —
				// a lock held across signalling would deadlock here.
				reg.Put(cliRecord("deleg_stop-reent-inner", "delegate_claude_agent", KindCLIAgent, "sess-reent"))
			}
		}))
	done := make(chan struct{})
	go func() {
		defer close(done)
		if _, err := reg.Stop("deleg_stop-reent", &Caller{SessionID: "sess-reent"}); err != nil {
			t.Errorf("stop failed: %v", err)
		}
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("stop deadlocked: interrupt_fn signalled while holding the registry lock")
	}
}

// ---------------------------------------------------------------------------
// Ownership tiers: compression lineage and session-key fallback.
// ---------------------------------------------------------------------------

func TestStopOwnershipResolvesCompressionLineage(t *testing.T) {
	lineage := map[string]string{
		"sess-pre-rotation": "sess-tip-5c",
		"sess-rotated-now":  "sess-tip-5c",
	}
	fn, calls := spyInterrupt()
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_5cc10001", "delegate_claude_agent", KindCLIAgent, "sess-pre-rotation",
		func(r *Record) { r.InterruptFn = fn }))
	caller := &Caller{SessionID: "sess-rotated-now", ResolveLineage: lineageResolver(lineage)}

	reply, err := reg.Stop("deleg_5cc10001", caller)
	if err != nil || reply.Status != StatusInterruptRequested {
		t.Fatalf("reply=%+v err=%v; lineage tips match so the run is owned", reply, err)
	}
	if n := atomic.LoadInt32(calls); n != 1 {
		t.Fatalf("interrupt fired %d time(s), want 1", n)
	}
}

func TestSteerOwnershipViaCurrentSessionKey(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_5dd10002", "delegate_cursor_agent", KindCloudAgent, "",
		withSessionKey("telegram:4242:curator")))
	caller := &Caller{SessionID: "", CurrentSessionKey: "telegram:4242:curator"}

	reply, err := reg.Steer("deleg_5dd10002", "Tighten the diff scope", caller)
	if err != nil || reply.Status != StatusNotSteerable {
		t.Fatalf("reply=%+v err=%v; the routing session key is a valid spine", reply, err)
	}
}

func TestSessionKeyMismatchIsRefused(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_key-miss", "delegate_claude_agent", KindCLIAgent, "",
		withSessionKey("telegram:4242:curator")))
	caller := &Caller{SessionID: "", CurrentSessionKey: "telegram:777:other"}
	if _, err := reg.Steer("deleg_key-miss", "text", caller); !errors.Is(err, ErrNoLiveSubagent) {
		t.Fatalf("err = %v, want ErrNoLiveSubagent", err)
	}
}

// The sharpest edge: a record bound to a FOREIGN session id must stay refused
// even when its routing session key would have matched the caller's.
func TestForeignSessionIDStaysRefusedDespiteMatchingSessionKey(t *testing.T) {
	fn, calls := spyInterrupt()
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_foreign-key", "delegate_claude_agent", KindCLIAgent, "sess-someone-else",
		withSessionKey("telegram:4242:curator"), func(r *Record) { r.InterruptFn = fn }))
	caller := &Caller{SessionID: "", CurrentSessionKey: "telegram:4242:curator"}

	if _, err := reg.Stop("deleg_foreign-key", caller); !errors.Is(err, ErrNoLiveSubagent) {
		t.Fatalf("err = %v, want ErrNoLiveSubagent (foreign session id never falls through to a key match)", err)
	}
	if n := atomic.LoadInt32(calls); n != 0 {
		t.Fatalf("must not interrupt; fired %d time(s)", n)
	}
}

func TestParentSessionWithoutRecordSessionIsRefused(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_half-spine", "delegate_cursor_agent", KindCloudAgent, "",
		withSessionKey("telegram:1:x")))
	caller := &Caller{SessionID: "sess-parent", CurrentSessionKey: "telegram:1:x"}
	if _, err := reg.Stop("deleg_half-spine", caller); !errors.Is(err, ErrNoLiveSubagent) {
		t.Fatalf("err = %v, want ErrNoLiveSubagent (no session-key fallback beside a caller session id)", err)
	}
}

func TestEmptySelectorsNeverClaim(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_empty-1", "delegate_claude_agent", KindCLIAgent, "sess-x"))
	// Caller session id equal to the record's session KEY (not session id)
	// must not create cross-spine ownership.
	caller := &Caller{SessionID: "", CurrentSessionKey: "sess-x"}
	if caller.OwnsCodingRun(nil) {
		t.Fatal("nil record is never owned")
	}
	if _, err := reg.Stop("deleg_empty-1", caller); !errors.Is(err, ErrNoLiveSubagent) {
		t.Fatalf("err = %v, want ErrNoLiveSubagent", err)
	}
}

func TestUnknownIDIsRefusedWithCompletionGuidance(t *testing.T) {
	err := func() error {
		_, err := NewRegistry().Stop("sid-gone", &Caller{SessionID: "sess"})
		return err
	}()
	if !errors.Is(err, ErrNoLiveSubagent) {
		t.Fatalf("err = %v, want ErrNoLiveSubagent", err)
	}
	if !strings.Contains(err.Error(), "completion message") {
		t.Fatalf("refusal must mention the completion path: %v", err)
	}
}

// ---------------------------------------------------------------------------
// Ownership / kind / status classification table.
// ---------------------------------------------------------------------------

func TestOwnershipMatrix(t *testing.T) {
	lineage := map[string]string{"old": "tip", "now": "tip"}
	cases := []struct {
		name   string
		record *Record
		caller *Caller
		owned  bool
	}{
		{"direct session match", &Record{ParentSessionID: "s1"}, &Caller{SessionID: "s1"}, true},
		{"lineage tips match", &Record{ParentSessionID: "old"}, &Caller{SessionID: "now", ResolveLineage: lineageResolver(lineage)}, true},
		{"lineage mismatch", &Record{ParentSessionID: "old"}, &Caller{SessionID: "unrelated", ResolveLineage: lineageResolver(lineage)}, false},
		{"session key match when both spineless", &Record{SessionKey: "k"}, &Caller{CurrentSessionKey: "k"}, true},
		{"session key mismatch", &Record{SessionKey: "k"}, &Caller{CurrentSessionKey: "j"}, false},
		{"empty session key never claims", &Record{SessionKey: ""}, &Caller{CurrentSessionKey: ""}, false},
		{"record session id vs caller key only", &Record{ParentSessionID: "s1", SessionKey: "k"}, &Caller{CurrentSessionKey: "k"}, false},
		{"caller session id vs record key only", &Record{SessionKey: "k"}, &Caller{SessionID: "k"}, false},
		{"empty strings on both sides without keys", &Record{}, &Caller{}, false},
	}
	for _, tc := range cases {
		if got := tc.caller.OwnsCodingRun(tc.record); got != tc.owned {
			t.Errorf("%s: owned = %v, want %v", tc.name, got, tc.owned)
		}
	}
}

func TestKindAndStatusClassification(t *testing.T) {
	for kind, coding := range map[string]bool{
		KindCLIAgent: true, KindCloudAgent: true,
		KindSubagentBatch: false, KindMission: false, "": false, "cli": false,
	} {
		if IsCodingKind(kind) != coding {
			t.Errorf("IsCodingKind(%q) = %v, want %v", kind, IsCodingKind(kind), coding)
		}
	}
	for status, live := range map[string]bool{
		StatusRunning: true, StatusStalling: true,
		StatusFinalize: false, StatusCompleted: false, "": false, "error": false,
	} {
		if IsLiveStatus(status) != live {
			t.Errorf("IsLiveStatus(%q) = %v, want %v", status, IsLiveStatus(status), live)
		}
	}
}

// cli_agent and cloud_agent are listed ONLY when owned AND live — the full
// inclusion matrix over kind x status x ownership.
func TestListInclusionMatrix(t *testing.T) {
	ownedCaller := &Caller{SessionID: "sess-matrix"}
	foreignCaller := &Caller{SessionID: "sess-foreign"}
	cases := []struct {
		id     string
		kind   string
		status string
	}{
		{"deleg_m1", KindCLIAgent, StatusRunning},
		{"deleg_m2", KindCLIAgent, StatusStalling},
		{"deleg_m3", KindCloudAgent, StatusRunning},
		{"deleg_m4", KindCloudAgent, StatusStalling},
		{"deleg_m5", KindCLIAgent, StatusFinalize},
		{"deleg_m6", KindCLIAgent, StatusCompleted},
		{"deleg_m7", KindCloudAgent, StatusCompleted},
		{"deleg_m8", KindSubagentBatch, StatusRunning},
		{"deleg_m9", KindMission, StatusRunning},
	}
	reg := NewRegistry()
	for _, tc := range cases {
		reg.Put(cliRecord(tc.id, "delegate_claude_agent", tc.kind, "sess-matrix", withStatus(tc.status)))
	}

	owned := map[string]bool{}
	for _, e := range reg.ListCodingRuns(ownedCaller) {
		owned[e.SubagentID] = true
	}
	for _, want := range []string{"deleg_m1", "deleg_m2", "deleg_m3", "deleg_m4"} {
		if !owned[want] {
			t.Errorf("%s (owned, live, coding kind) missing from list: %v", want, owned)
		}
	}
	for _, banned := range []string{"deleg_m5", "deleg_m6", "deleg_m7", "deleg_m8", "deleg_m9"} {
		if owned[banned] {
			t.Errorf("%s must not be listed", banned)
		}
	}
	if entries := reg.ListCodingRuns(foreignCaller); len(entries) != 0 {
		t.Fatalf("foreign caller listed %d entries, want 0", len(entries))
	}
}

// ---------------------------------------------------------------------------
// Acceptance envelope control handle.
// ---------------------------------------------------------------------------

func TestEnvelopeControlHandleForClaudeRun(t *testing.T) {
	payload := AttachControlHandle(map[string]any{
		"status": "dispatched", "tool": "delegate_claude_agent",
		"result_kind": KindCLIAgent, "delegation_id": "deleg_5c686284",
	}, "delegate_claude_agent", KindCLIAgent, "deleg_5c686284", "")

	handle, ok := payload["control"].(ControlHandle)
	if !ok {
		t.Fatalf("control block missing: %+v", payload)
	}
	if handle.Tool != ControlTool || handle.DelegationID != "deleg_5c686284" ||
		handle.SubagentID != "deleg_5c686284" || handle.AcceptingSteer {
		t.Fatalf("handle wrong: %+v", handle)
	}
	if len(handle.Actions) != 2 || handle.Actions[0] != "list" || handle.Actions[1] != "stop" {
		t.Fatalf("actions = %v, want [list stop]", handle.Actions)
	}
	hint := str(payload["control_hint"])
	low := strings.ToLower(hint)
	for _, want := range []string{"delegate_agent", "deleg_5c686284", "stop", "cannot be steered", "re-delegate"} {
		if !strings.Contains(low, want) {
			t.Fatalf("control_hint %q missing %q", hint, want)
		}
	}
}

func TestEnvelopeControlHandleForCursorRunKeepsCallerHint(t *testing.T) {
	callerHint := "The run's live progress page URL is delivered with the result."
	payload := AttachControlHandle(nil, "delegate_cursor_agent", KindCloudAgent, "deleg_9d13aa02", callerHint)

	handle, ok := payload["control"].(ControlHandle)
	if !ok || handle.SubagentID != "deleg_9d13aa02" {
		t.Fatalf("control block wrong: %+v", payload["control"])
	}
	hint := str(payload["control_hint"])
	if !strings.Contains(hint, "live progress page URL") {
		t.Fatalf("caller-supplied hint text was dropped: %q", hint)
	}
	low := strings.ToLower(hint)
	for _, want := range []string{"delegate_agent", "deleg_9d13aa02", "stop", "re-delegate"} {
		if !strings.Contains(low, want) {
			t.Fatalf("control_hint %q missing %q", hint, want)
		}
	}
}

func TestEnvelopeSubagentBatchGainsNoControlHandle(t *testing.T) {
	payload := AttachControlHandle(map[string]any{"result_kind": KindSubagentBatch},
		"delegate_agent", KindSubagentBatch, "deleg_7cc20003", "native hint stays")
	if _, has := payload["control"]; has {
		t.Fatalf("subagent_batch must not gain a control block: %+v", payload)
	}
	if got := str(payload["control_hint"]); got != "native hint stays" {
		t.Fatalf("non-coding control_hint must pass through verbatim, got %q", got)
	}
}

func TestEnvelopeMissionKindGainsNoControlHandle(t *testing.T) {
	payload := AttachControlHandle(nil, "delegate_assistant", KindMission, "deleg_8dd30004", "")
	if _, has := payload["control"]; has {
		t.Fatalf("mission must not gain a control block: %+v", payload)
	}
	if _, has := payload["control_hint"]; has {
		t.Fatalf("no caller hint and no handle: control_hint must stay absent: %+v", payload)
	}
}

func TestSignalRunnerInterruptFiresEachChannelOnce(t *testing.T) {
	fn, calls := spyInterrupt()
	fired := SignalRunnerInterrupt(&Record{RunnerTID: 7, InterruptFn: fn}, "test")
	if !fired || atomic.LoadInt32(calls) != 1 {
		t.Fatalf("fired=%v calls=%d; both channels must fire, each once", fired, atomic.LoadInt32(calls))
	}
	if SignalRunnerInterrupt(&Record{}, "test") {
		t.Fatal("record with no channel must report not signalled")
	}
}

// ---------------------------------------------------------------------------
// Concurrency: snapshot-under-lock keeps list stable against writers.
// ---------------------------------------------------------------------------

func TestRegistryConcurrentPutAndList(t *testing.T) {
	reg := NewRegistry()
	reg.Put(cliRecord("deleg_conc-0", "delegate_claude_agent", KindCLIAgent, "sess-conc"))
	var wg sync.WaitGroup
	for i := 1; i <= 32; i++ {
		wg.Add(2)
		id := "deleg_conc-" + string(rune('0'+i%10)) + string(rune('0'+i/10))
		go func(id string) {
			defer wg.Done()
			reg.Put(cliRecord(id, "delegate_claude_agent", KindCLIAgent, "sess-conc"))
		}(id)
		go func() {
			defer wg.Done()
			for _, e := range reg.ListCodingRuns(&Caller{SessionID: "sess-conc"}) {
				if e.AcceptingSteer {
					t.Error("coding run listed as accepting steer")
				}
			}
		}()
	}
	wg.Wait()
	if entries := reg.ListCodingRuns(&Caller{SessionID: "sess-conc"}); len(entries) != 33 {
		t.Fatalf("listed %d entries, want 33", len(entries))
	}
}

// ---------------------------------------------------------------------------
// Small helpers (kept out of the fixtures so tests read like the Python pins).
// ---------------------------------------------------------------------------

func str(v any) string {
	s, _ := v.(string)
	return s
}
