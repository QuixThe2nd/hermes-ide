package retainbacklog

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func item(t *testing.T, content string) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"content": content})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func dueQueue(t *testing.T, entries ...Entry) *Queue {
	t.Helper()
	q := &Queue{path: filepath.Join(t.TempDir(), "retain-backlog.jsonl"), quarantined: map[string]bool{}}
	for _, e := range entries {
		if e.ID == "" {
			e.ID = "11111111-2222-4333-8444-555555555555"
		}
		if len(e.Item) == 0 {
			e.Item = item(t, "x")
		}
		if e.BankID == "" {
			e.BankID = "bank"
		}
		if e.RetryAfter.IsZero() {
			e.RetryAfter = time.Now().Add(-time.Minute)
		}
		if _, _, err := q.Add(e); err != nil {
			t.Fatal(err)
		}
	}
	return q
}

// fakeRemote records every call so tests assert exactly which server actions
// a drain took (submit vs retry vs delete — there is no delete anymore).
type fakeRemote struct {
	mu          sync.Mutex
	statuses    map[string]string
	submits     []string
	retries     []string
	statusCalls int
	submitErr   error
	retryErr    error
	statusErr   error
}

func newFakeRemote() *fakeRemote {
	return &fakeRemote{statuses: map[string]string{}}
}

func (f *fakeRemote) Status(_ context.Context, _, opID string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.statusCalls++
	if f.statusErr != nil {
		return "", f.statusErr
	}
	if s, ok := f.statuses[opID]; ok {
		return s, nil
	}
	return "not_found", nil
}

func (f *fakeRemote) RetryOperation(_ context.Context, _, opID string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.retries = append(f.retries, opID)
	if f.retryErr != nil {
		return f.retryErr
	}
	f.statuses[opID] = "pending"
	return nil
}

func (f *fakeRemote) Submit(_ context.Context, e Entry) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.submits = append(f.submits, e.ID)
	if f.submitErr != nil {
		return f.submitErr
	}
	f.statuses[e.ID] = "pending"
	return nil
}

func TestDrainRemovesOnlyOnCompleted(t *testing.T) {
	q := dueQueue(t, Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"})
	remote := newFakeRemote()
	// Submission acked: entry must STAY queued for polling (durable recovery).
	next, err := q.Drain(time.Now().UTC(), remote)
	if err != nil {
		t.Fatal(err)
	}
	if q.Len() != 1 {
		t.Fatalf("after submit ack: entries=%d, want 1 (kept until completed)", q.Len())
	}
	if !q.Entries()[0].Submitted {
		t.Fatal("entry not marked submitted after ack")
	}
	if want := time.Now().Add(PollInterval); next.Sub(want) > time.Second {
		t.Fatalf("next=%v, want ~%v (poll interval)", next, want)
	}

	// pending -> kept; processing -> kept.
	for _, status := range []string{"pending", "processing"} {
		remote.statuses["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"] = status
		if _, err := q.Drain(time.Now().Add(2*PollInterval), remote); err != nil {
			t.Fatal(err)
		}
		if q.Len() != 1 {
			t.Fatalf("status %s: entries=%d, want kept 1", status, q.Len())
		}
		if len(remote.submits) != 1 {
			t.Fatalf("status %s: submits=%d, want no resubmit of a submitted op", status, len(remote.submits))
		}
	}

	// completed -> removed.
	remote.statuses["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"] = "completed"
	if next, err = q.Drain(time.Now().Add(4*PollInterval), remote); err != nil {
		t.Fatal(err)
	}
	if q.Len() != 0 || !next.IsZero() {
		t.Fatalf("after completed: entries=%d next=%v, want drained", q.Len(), next)
	}
}

// Crash window: the process dies after the submission ack but before any
// local bookkeeping. The entry is already persisted submitted=true (save
// happens under the ack), so the next drain polls instead of re-submitting —
// no duplicate submission exists in the crash window.
func TestCrashAfterAckPollsInsteadOfResubmitting(t *testing.T) {
	// submitted=true is what the pre-crash pass persisted after its submit
	// ack; the reopened queue must see exactly that on disk.
	q := dueQueue(t, Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", Submitted: true})
	if _, err := os.Stat(q.path); err != nil {
		t.Fatal(err)
	}
	// Simulate the crash: reopen from disk (nothing in memory survives).
	reopened, err := Open(q.path)
	if err != nil {
		t.Fatal(err)
	}
	remote := newFakeRemote()
	remote.statuses["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"] = "processing"
	if _, err := reopened.Drain(time.Now().Add(time.Minute), remote); err != nil {
		t.Fatal(err)
	}
	if len(remote.submits) != 0 {
		t.Fatalf("submits=%d, want 0 (the acked op is polled, never resubmitted)", len(remote.submits))
	}
	if remote.statusCalls == 0 {
		t.Fatal("reopened entry must poll, not submit")
	}
}

// failed and cancelled operations are re-queued through the server's own
// retry endpoint — the row is NEVER deleted and the entry is NEVER dropped.
func TestFailedAndCancelledGoThroughRetryOperation(t *testing.T) {
	for _, status := range []string{"failed", "cancelled"} {
		q := dueQueue(t, Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", Submitted: true})
		remote := newFakeRemote()
		remote.statuses["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"] = status
		if _, err := q.Drain(time.Now().UTC(), remote); err != nil {
			t.Fatal(err)
		}
		if len(remote.retries) != 1 {
			t.Fatalf("%s: retries=%d, want 1 retry_operation call", status, len(remote.retries))
		}
		if len(remote.submits) != 0 {
			t.Fatalf("%s: submits=%d, want 0 (no delete+resubmit)", status, len(remote.submits))
		}
		if q.Len() != 1 {
			t.Fatalf("%s: entries=%d, want kept 1 until completed", status, q.Len())
		}
	}
}

func TestRetryConflictPollsAgain(t *testing.T) {
	q := dueQueue(t, Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", Submitted: true})
	remote := newFakeRemote()
	remote.statuses["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"] = "failed"
	remote.retryErr = ErrRetryConflict
	next, err := q.Drain(time.Now().UTC(), remote)
	if err != nil {
		t.Fatal(err)
	}
	if q.Len() != 1 {
		t.Fatalf("entries=%d, want kept", q.Len())
	}
	if want := time.Now().Add(PollInterval); next.Sub(want) > time.Second {
		t.Fatalf("next=%v, want poll interval after conflict", next)
	}
}

func TestRetryGoneFallsBackToSubmit(t *testing.T) {
	q := dueQueue(t, Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", Submitted: true})
	remote := newFakeRemote()
	remote.statuses["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"] = "failed"
	remote.retryErr = ErrGoneFromRetry
	if _, err := q.Drain(time.Now().UTC(), remote); err != nil {
		t.Fatal(err)
	}
	if len(remote.submits) != 1 {
		t.Fatalf("submits=%d, want 1 after gone-from-retry", len(remote.submits))
	}
	if q.Len() != 1 {
		t.Fatalf("entries=%d, want kept until completed", q.Len())
	}
}

// The server's PERMANENT retry refusal — a batch_retain parent whose children
// all completed (or it has none) — can never be re-queued. The entry mints a
// NEW operation id, persists the swap atomically BEFORE submitting, and
// replays the same item + document id (server-side chunk-hash recovery skips
// every chunk the old operation already committed). The old server row is
// never deleted: there is no delete in the protocol at all.
func TestPermanentRetryResubmitsUnderNewOperationID(t *testing.T) {
	oldID := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	q := dueQueue(t, Entry{ID: oldID, Submitted: true, DocumentID: "doc-keep"})
	remote := newFakeRemote()
	remote.statuses[oldID] = "failed"
	remote.retryErr = ErrRetryPermanent

	if _, err := q.Drain(time.Now().UTC(), remote); err != nil {
		t.Fatal(err)
	}
	if len(remote.retries) != 1 || remote.retries[0] != oldID {
		t.Fatalf("retries=%v, want exactly one retry of the old id", remote.retries)
	}
	if len(remote.submits) != 1 {
		t.Fatalf("submits=%d, want 1 resubmission under the new id", len(remote.submits))
	}
	newID := remote.submits[0]
	if newID == oldID || !uuidRe.MatchString(newID) {
		t.Fatalf("new id %q, want a fresh uuid != %q", newID, oldID)
	}
	entries := q.Entries()
	if len(entries) != 1 || entries[0].ID != newID {
		t.Fatalf("queued id=%v, want the new id", entries)
	}
	if entries[0].DocumentID != "doc-keep" {
		t.Fatalf("document id changed: %q", entries[0].DocumentID)
	}
	if !entries[0].Submitted {
		t.Fatal("resubmitted entry must be marked submitted after the ack")
	}
	// Durable: the id swap survives reopen (it was saved before the submit).
	reopened, err := Open(q.path)
	if err != nil {
		t.Fatal(err)
	}
	if got := reopened.Entries(); len(got) != 1 || got[0].ID != newID || !got[0].Submitted {
		t.Fatalf("reopened entries=%v, want new id submitted", got)
	}
}

// Ambiguous refusals keep polling only for a bounded window; the permanent
// refusal must not be re-asked forever (the fake's retries list proves the
// retry is attempted once per pass, and the new-id submit breaks the cycle).
func TestPermanentRetryIsNotRetriedForever(t *testing.T) {
	oldID := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	q := dueQueue(t, Entry{ID: oldID, Submitted: true, DocumentID: "doc-keep"})
	remote := newFakeRemote()
	remote.statuses[oldID] = "failed"
	remote.retryErr = ErrRetryPermanent

	// First pass: retry refused permanently -> resubmit under a new id.
	if _, err := q.Drain(time.Now().UTC(), remote); err != nil {
		t.Fatal(err)
	}
	newID := q.Entries()[0].ID
	// The server finishes the resubmitted operation; the old row is gone.
	remote.retryErr = nil
	remote.statuses[newID] = "completed"
	if _, err := q.Drain(time.Now().Add(PollInterval), remote); err != nil {
		t.Fatal(err)
	}
	if q.Len() != 0 {
		t.Fatalf("entries=%d, want settled after the new op completed", q.Len())
	}
	if len(remote.retries) != 1 {
		t.Fatalf("retries=%v, want exactly one retry attempt total", remote.retries)
	}
}

// The permanent-refusal swap must be persisted BEFORE the new-id submit:
// if the pass dies between swap and submit (kill -9, crash), reopening
// from disk keeps the entry under the NEW id, submitted=false — so the
// next pass submits the new id and never touches the dead one again.
func TestPermanentSwapSurvivesAbortBetweenSwapAndSubmit(t *testing.T) {
	oldID := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	q := dueQueue(t, Entry{ID: oldID, Submitted: true, DocumentID: "doc-keep"})
	remote := newFakeRemote()
	remote.statuses[oldID] = "failed"
	remote.retryErr = ErrRetryPermanent
	remote.submitErr = errors.New("aborted after swap (crash window)")
	if _, err := q.Drain(time.Now().UTC(), remote); err == nil {
		t.Fatal("want the mid-pass abort propagated")
	}

	reopened, err := Open(q.path)
	if err != nil {
		t.Fatal(err)
	}
	entries := reopened.Entries()
	if len(entries) != 1 || entries[0].ID == oldID || entries[0].Submitted {
		t.Fatalf("entries=%+v, want the swapped id persisted with submitted=false", entries)
	}
	newID := entries[0].ID

	// A later pass — after the submit-failure backoff — with a fresh
	// remote (nothing known server-side) settles under the new id alone;
	// the refused id is never retried again.
	remote2 := newFakeRemote()
	if _, err := reopened.Drain(time.Now().UTC().Add(BackoffFirst+time.Second), remote2); err != nil {
		t.Fatal(err)
	}
	if len(remote2.retries) != 0 {
		t.Fatalf("retries=%v, want none — old id is dead", remote2.retries)
	}
	if len(remote2.submits) != 1 || remote2.submits[0] != newID {
		t.Fatalf("submits=%v, want exactly [%s]", remote2.submits, newID)
	}
}

// The pass is gated by the earliest retry_after, and each entry is
// individually gated: a not-yet-due FIFO head must not be settled early just
// because a later entry is due.
func TestNotDueHeadIsNotSettledEarly(t *testing.T) {
	headDue := time.Now().Add(2 * time.Hour)
	q := dueQueue(t,
		Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", RetryAfter: headDue},
		Entry{ID: "99999999-8888-4777-8666-555555555555", RetryAfter: time.Now().Add(-time.Minute)},
	)
	remote := newFakeRemote()
	next, err := q.Drain(time.Now().UTC(), remote)
	if err != nil {
		t.Fatal(err)
	}
	if len(remote.submits) != 1 || remote.submits[0] != "99999999-8888-4777-8666-555555555555" {
		t.Fatalf("submits=%v, want only the due tail entry", remote.submits)
	}
	// next is the earliest actionable instant among queued entries — the
	// submitted tail's poll (PollInterval out), NOT the not-due head's +2h.
	if want := time.Now().Add(PollInterval); next.Sub(want) > time.Second {
		t.Fatalf("next=%v, want ~%v (tail's poll)", next, want)
	}
	head := q.Entries()[0]
	if head.ID != "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" || !head.RetryAfter.Equal(headDue) {
		t.Fatalf("head mutated: %+v", head)
	}
}

// A non-rate settle failure backs the entry off exponentially instead of
// re-firing on every wakeup — no once-a-second hot loop.
func TestNonRateErrorBacksOffExponentially(t *testing.T) {
	q := dueQueue(t, Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"})
	remote := newFakeRemote()
	remote.submitErr = errors.New("connection refused")
	now := time.Now().UTC()
	next, err := q.Drain(now, remote)
	if err == nil || !strings.Contains(err.Error(), "connection refused") {
		t.Fatalf("err=%v, want propagated", err)
	}
	if q.Len() != 1 {
		t.Fatal("entry must be kept")
	}
	if got := next.Sub(now); got < BackoffFirst-time.Second || got > BackoffFirst+time.Second {
		t.Fatalf("first backoff=%v, want ~%v", got, BackoffFirst)
	}
	// Immediate re-drain before the backoff elapses must do NOTHING.
	remote2 := newFakeRemote()
	if next2, err := q.Drain(now.Add(10*time.Second), remote2); err != nil || len(remote2.submits) != 0 {
		t.Fatalf("within backoff: err=%v submits=%d, want none", err, len(remote2.submits))
	} else if next2.Sub(now) < 55*time.Second {
		t.Fatalf("next=%v, must stay ~60s out (no hot loop)", next2)
	}
	// Second failure doubles the backoff.
	remote.submitErr = errors.New("connection refused")
	next, err = q.Drain(now.Add(BackoffFirst+time.Second), remote)
	if err == nil {
		t.Fatal("want failure propagated")
	}
	if got := next.Sub(now); got < 2*BackoffFirst-time.Second {
		t.Fatalf("second backoff=%v, want ~%v", got, 2*BackoffFirst)
	}
}

// A fresh provider-quota failure during the drain pushes EVERY queued entry
// forward to the new hint — the limit is server-wide.
func TestRateLimitPushesAllEntriesForward(t *testing.T) {
	otherDue := time.Now().Add(-time.Hour)
	q := dueQueue(t,
		Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
		Entry{ID: "99999999-8888-4777-8666-555555555555", RetryAfter: otherDue},
	)
	hint := time.Now().Add(90 * time.Minute)
	remote := newFakeRemote()
	remote.submitErr = &RateLimitedError{RetryAt: hint}
	next, err := q.Drain(time.Now().UTC(), remote)
	var rl *RateLimitedError
	if !errors.As(err, &rl) {
		t.Fatalf("err=%v, want RateLimitedError", err)
	}
	_ = next
	reopened, rerr := Open(q.path)
	if rerr != nil {
		t.Fatal(rerr)
	}
	for _, e := range reopened.Entries() {
		if e.RetryAfter.Before(hint.Add(-time.Second)) {
			t.Fatalf("entry %s retry_after=%v, want >= hint %v", e.ID, e.RetryAfter, hint)
		}
	}
}

func TestUnknownStatusSubmitsIdempotently(t *testing.T) {
	q := dueQueue(t, Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", Submitted: true})
	remote := newFakeRemote()
	remote.statuses["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"] = "weird_new_status"
	if _, err := q.Drain(time.Now().UTC(), remote); err != nil {
		t.Fatal(err)
	}
	if len(remote.submits) != 1 {
		t.Fatalf("submits=%d, want idempotent replay submit", len(remote.submits))
	}
}

func TestAddMintsUUIDAndDedupes(t *testing.T) {
	q := dueQueue(t) // empty
	id, added, err := q.Add(Entry{BankID: "b", Item: item(t, "c"), RetryAfter: time.Now()})
	if err != nil || !added {
		t.Fatalf("added=%v err=%v", added, err)
	}
	if !uuidRe.MatchString(id) {
		t.Fatalf("minted id %q not a uuid", id)
	}
	if id2, added, err := q.Add(Entry{ID: id, BankID: "b", Item: item(t, "c"), RetryAfter: time.Now()}); added || id2 != id || err != nil {
		t.Fatalf("re-add: id2=%q added=%v err=%v, want dedupe no-op", id2, added, err)
	}
	if q.Len() != 1 {
		t.Fatalf("entries=%d, want 1", q.Len())
	}
}

func TestOpenQuarantinesCorruptLinesAndToleratesBOM(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "retain-backlog.jsonl")
	bom := []byte{0xEF, 0xBB, 0xBF}
	good := `{"id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","bank_id":"b","item":{"content":"x"},"queued_at":"2026-01-01T00:00:00Z","retry_after":"2026-01-01T00:00:00Z"}` + "\n"
	bad := "{not json"
	os.WriteFile(path, append(bom, []byte(bad+"\n"+good)...), 0o600)

	q, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if q.Len() != 1 || q.Entries()[0].ID != "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" {
		t.Fatalf("entries=%d, want the one good line", q.Len())
	}
	corrupt, err := os.ReadFile(path + CorruptSuffix)
	if err != nil || !strings.Contains(string(corrupt), bad) {
		t.Fatalf("corrupt sidecar=%q err=%v, want the bad line", corrupt, err)
	}
}

func TestSaveEmptyUnlinksFile(t *testing.T) {
	q := dueQueue(t, Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", Submitted: true})
	remote := newFakeRemote()
	remote.statuses["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"] = "completed"
	if _, err := q.Drain(time.Now().UTC(), remote); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(q.path); !os.IsNotExist(err) {
		t.Fatalf("backlog file must be unlinked when empty, stat err=%v", err)
	}
}

// Two OpenLocked callers serialize: the second blocks until the first
// releases. Independent in-process queues are not a lock.
func TestOpenLockedSerializes(t *testing.T) {
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	first, err := OpenLocked(home)
	if err != nil {
		t.Fatal(err)
	}
	acquired := make(chan *LockedQueue, 1)
	go func() {
		second, err := OpenLocked(home)
		if err != nil {
			t.Error(err)
			return
		}
		acquired <- second
	}()
	select {
	case second := <-acquired:
		second.Close()
		t.Fatal("second OpenLocked acquired while first held the lock")
	case <-time.After(200 * time.Millisecond):
		// still blocked: correct
	}
	first.Close()
	select {
	case second := <-acquired:
		second.Close()
	case <-time.After(5 * time.Second):
		t.Fatal("second OpenLocked never acquired after release")
	}
}

// The durable recovery chain end-to-end at queue level: submit-ack -> poll
// pending -> server fails it -> retry_operation -> poll -> completed -> gone.
func TestFullLifecycleKeepsEntryUntilCompleted(t *testing.T) {
	q := dueQueue(t, Entry{ID: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"})
	remote := newFakeRemote()
	id := "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

	for step, status := range []string{"pending", "failed", "pending", "processing"} {
		if status == "failed" {
			remote.statuses[id] = "pending" // RetryOperation sets pending
		} else {
			remote.statuses[id] = status
		}
		if _, err := q.Drain(time.Now().Add(time.Duration(step+1)*PollInterval), remote); err != nil {
			t.Fatalf("step %d (%s): %v", step, status, err)
		}
		if q.Len() != 1 {
			t.Fatalf("step %d (%s): entry lost", step, status)
		}
	}
	remote.statuses[id] = "completed"
	if _, err := q.Drain(time.Now().Add(5*PollInterval), remote); err != nil {
		t.Fatal(err)
	}
	if q.Len() != 0 {
		t.Fatal("entry must be removed only at completed")
	}
}
