package retainbacklog

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeOps is an in-memory Hindsight operations surface recording every
// submission so tests can prove exactly-once across crash windows.
type fakeOps struct {
	mu        sync.Mutex
	statuses  map[string]string // operation id -> status ("" == not found)
	submits   map[string]int    // operation id -> submit count
	deletes   map[string]int
	statusErr map[string]error // per-id probe failure injection
}

func newFakeOps() *fakeOps {
	return &fakeOps{
		statuses:  map[string]string{},
		submits:   map[string]int{},
		deletes:   map[string]int{},
		statusErr: map[string]error{},
	}
}

func (f *fakeOps) OperationStatus(id string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err, ok := f.statusErr[id]; ok {
		return "", err
	}
	status, ok := f.statuses[id]
	if !ok {
		return "", fmt.Errorf("get %s: %w", id, ErrOperationNotFound)
	}
	return status, nil
}

func (f *fakeOps) DeleteOperation(id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.statuses[id]; !ok {
		return fmt.Errorf("delete %s: %w", id, ErrOperationNotFound)
	}
	delete(f.statuses, id)
	f.deletes[id]++
	return nil
}

func (f *fakeOps) SubmitAsync(bankID, id string, item map[string]any) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.statuses[id]; !ok {
		f.statuses[id] = "pending" // replay of a live id returns the original op
	}
	f.submits[id]++
	return nil
}

func (f *fakeOps) submitCount(id string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.submits[id]
}

func (f *fakeOps) deleteCount(id string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.deletes[id]
}

// writeFileEntry persists one entry line directly, the way a crash leaves
// the backlog: submitted server-side but not yet removed locally.
func writeFileEntry(t *testing.T, path string, e Entry) {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		t.Fatalf("ReadFile: %v", err)
	}
	line, err := jsonMarshalLine(e)
	if err != nil {
		t.Fatalf("marshal entry: %v", err)
	}
	if err := os.WriteFile(path, append(raw, line...), 0o600); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
}

func TestSettleActionForStatus(t *testing.T) {
	cases := []struct {
		status string
		want   SettleAction
	}{
		{"pending", SettleNone},
		{"processing", SettleNone},
		{"completed", SettleNone},
		{"failed", SettleDeleteResubmit},
		{"cancelled", SettleDeleteResubmit},
		{"not_found", SettleSubmit},
		{"", SettleSubmit},
		{"some-future-status", SettleSubmit},
	}
	for _, tc := range cases {
		if got := SettleActionForStatus(tc.status); got != tc.want {
			t.Errorf("SettleActionForStatus(%q) = %v, want %v", tc.status, got, tc.want)
		}
	}
}

func TestReconcileSettlesLiveOperationWithoutResubmit(t *testing.T) {
	for _, status := range []string{"pending", "processing", "completed"} {
		ops := newFakeOps()
		q := openQueue(t, filepath.Join(t.TempDir(), "retain-backlog.jsonl"))
		e := entry("live-op", "server owns it", time.Now().UTC().Add(-time.Minute))
		if _, err := q.Add(e); err != nil {
			t.Fatalf("Add: %v", err)
		}
		ops.statuses[e.ID] = status
		r := &Reconciler{Ops: ops}
		if _, err := q.Drain(time.Now().UTC(), r.Settle); err != nil {
			t.Fatalf("Drain(%s): %v", status, err)
		}
		if got := ops.submitCount(e.ID); got != 0 {
			t.Fatalf("status %s: %d submissions, want 0 (server owns the op)", status, got)
		}
		if len(q.Entries()) != 0 {
			t.Fatalf("status %s: entry not removed", status)
		}
	}
}

func TestReconcileTerminalDeletesRowThenResubmitsSameID(t *testing.T) {
	for _, status := range []string{"failed", "cancelled"} {
		ops := newFakeOps()
		q := openQueue(t, filepath.Join(t.TempDir(), "retain-backlog.jsonl"))
		e := entry("terminal-op", "fresh chance", time.Now().UTC().Add(-time.Minute))
		if _, err := q.Add(e); err != nil {
			t.Fatalf("Add: %v", err)
		}
		ops.statuses[e.ID] = status
		r := &Reconciler{Ops: ops}
		if _, err := q.Drain(time.Now().UTC(), r.Settle); err != nil {
			t.Fatalf("Drain(%s): %v", status, err)
		}
		if got := ops.deleteCount(e.ID); got != 1 {
			t.Fatalf("status %s: %d deletes, want 1", status, got)
		}
		if got := ops.submitCount(e.ID); got != 1 {
			t.Fatalf("status %s: %d submissions, want 1 (same id resubmitted)", status, got)
		}
		if ops.statuses[e.ID] != "pending" {
			t.Fatalf("status %s: resubmit did not create a pending op", status)
		}
	}
}

func TestReconcileNotFoundSubmitsOnce(t *testing.T) {
	ops := newFakeOps()
	q := openQueue(t, filepath.Join(t.TempDir(), "retain-backlog.jsonl"))
	e := entry("fresh-op", "never submitted", time.Now().UTC().Add(-time.Minute))
	if _, err := q.Add(e); err != nil {
		t.Fatalf("Add: %v", err)
	}
	r := &Reconciler{Ops: ops}
	if _, err := q.Drain(time.Now().UTC(), r.Settle); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if got := ops.submitCount(e.ID); got != 1 {
		t.Fatalf("submissions = %d, want 1", got)
	}
	if len(q.Entries()) != 0 {
		t.Fatal("entry not removed after accepted submission")
	}
}

func TestReconcileProbeFailureKeepsEntry(t *testing.T) {
	ops := newFakeOps()
	q := openQueue(t, filepath.Join(t.TempDir(), "retain-backlog.jsonl"))
	e := entry("probe-op", "keep me", time.Now().UTC().Add(-time.Minute))
	if _, err := q.Add(e); err != nil {
		t.Fatalf("Add: %v", err)
	}
	ops.statusErr[e.ID] = errors.New("status endpoint down")
	r := &Reconciler{Ops: ops}
	if _, err := q.Drain(time.Now().UTC(), r.Settle); err == nil {
		t.Fatal("probe transport failure must surface, not silently settle")
	}
	if len(q.Entries()) != 1 {
		t.Fatal("entry must be kept for the next pass")
	}
}

// TestReconcileCrashWindowNoDuplicate is the whole point of the operation
// identity: a crash between the submission ack and the local removal must
// resolve by reconciling against the live operation — never by a second
// submission.
func TestReconcileCrashWindowNoDuplicate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	ops := newFakeOps()
	q := openQueue(t, path)
	e := entry("crash-op", "crash window", time.Now().UTC().Add(-time.Minute))
	if _, err := q.Add(e); err != nil {
		t.Fatalf("Add: %v", err)
	}
	r := &Reconciler{Ops: ops}
	if _, err := q.Drain(time.Now().UTC(), r.Settle); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if got := ops.submitCount(e.ID); got != 1 {
		t.Fatalf("submissions after ack = %d, want 1", got)
	}
	// The crash: the submission reached the server (op pending) but the
	// local removal never ran — the entry line is still on disk.
	writeFileEntry(t, path, e)
	restarted := openQueue(t, path)
	if _, err := restarted.Drain(time.Now().UTC(), r.Settle); err != nil {
		t.Fatalf("Drain after crash: %v", err)
	}
	if got := ops.submitCount(e.ID); got != 1 {
		t.Fatalf("submissions across crash window = %d, want exactly 1 (duplicate!)", got)
	}
	if len(restarted.Entries()) != 0 {
		t.Fatal("crash-window entry not settled on restart")
	}
}

// TestReconcileUnknownStatusSubmitsIdempotently pins the safety argument
// for unmapped statuses: the entry id is a replay key, so a submission is
// never a duplicate even when the status probe raced a just-created op.
func TestReconcileUnknownStatusSubmitsIdempotently(t *testing.T) {
	ops := newFakeOps()
	q := openQueue(t, filepath.Join(t.TempDir(), "retain-backlog.jsonl"))
	e := entry("odd-op", "unknown status", time.Now().UTC().Add(-time.Minute))
	if _, err := q.Add(e); err != nil {
		t.Fatalf("Add: %v", err)
	}
	ops.statuses[e.ID] = "some-future-status"
	r := &Reconciler{Ops: ops}
	if _, err := q.Drain(time.Now().UTC(), r.Settle); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if got := ops.submitCount(e.ID); got != 1 {
		t.Fatalf("submissions = %d, want 1 (replayed as the original op)", got)
	}
	if ops.statuses[e.ID] != "some-future-status" {
		t.Fatal("replay must not have replaced the live operation row")
	}
}

func TestConcurrentDrainsSettleEachEntryOnce(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	ops := newFakeOps()
	var ids []string
	due := time.Now().UTC().Add(-time.Minute)
	for i := range 3 {
		e := entry(fmt.Sprintf("concurrent-%d", i), fmt.Sprintf("memory %d", i), due)
		ids = append(ids, e.ID)
		if _, err := q.Add(e); err != nil {
			t.Fatalf("Add: %v", err)
		}
	}
	// A slow status probe widens the race window: three drainers in
	// parallel must still settle each entry exactly once.
	slow := &slowOps{inner: ops, delay: 25 * time.Millisecond}
	r := &Reconciler{Ops: slow}
	var wg sync.WaitGroup
	for range 3 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = q.Drain(time.Now().UTC(), r.Settle)
		}()
	}
	wg.Wait()
	for _, id := range ids {
		if got := ops.submitCount(id); got != 1 {
			t.Fatalf("entry %s settled %d times, want exactly 1", id, got)
		}
	}
	if len(q.Entries()) != 0 {
		t.Fatalf("entries left after concurrent drains: %d", len(q.Entries()))
	}
}

// slowOps delays every probe so concurrent drains genuinely overlap.
type slowOps struct {
	inner *fakeOps
	delay time.Duration
}

func (s *slowOps) OperationStatus(id string) (string, error) {
	time.Sleep(s.delay)
	return s.inner.OperationStatus(id)
}

func (s *slowOps) DeleteOperation(id string) error {
	time.Sleep(s.delay)
	return s.inner.DeleteOperation(id)
}

func (s *slowOps) SubmitAsync(bankID, id string, item map[string]any) error {
	time.Sleep(s.delay)
	return s.inner.SubmitAsync(bankID, id, item)
}

// jsonMarshalLine is the test-side JSONL encoder (one object per line).
func jsonMarshalLine(e Entry) ([]byte, error) {
	raw, err := json.Marshal(e)
	if err != nil {
		return nil, err
	}
	if !strings.HasSuffix(string(raw), "\n") {
		return append(raw, '\n'), nil
	}
	return raw, nil
}
