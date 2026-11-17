package retainbacklog

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// signatureBody is the real failure signature from the logs: HTTP 500 whose
// detail wraps the upstream 429 and carries the provider's reset hint.
func signatureBody(retryAt string) string {
	return fmt.Sprintf(
		`{"detail":"Fact extraction deferred by provider quota: 1/1 chunks failed. First failures: `+
			`chunk 0: ProviderRateLimitResetError: Provider quota exhausted (openai/glm-5.3-flash, `+
			`scope=retain_extract_facts); retry at %s: HTTP 429: quota exceeded"}`, retryAt)
}

func mustParse(t *testing.T, layout, value string) time.Time {
	t.Helper()
	parsed, err := time.Parse(layout, value)
	if err != nil {
		t.Fatalf("parse %q: %v", value, err)
	}
	return parsed.UTC()
}

func TestClassify(t *testing.T) {
	retryExact := mustParse(t, time.RFC3339Nano, "2026-11-17T05:36:33.557843+00:00")
	utc := func(h, m int) time.Time {
		return time.Date(2026, 11, 17, h, m, 0, 0, time.UTC)
	}
	tests := []struct {
		name       string
		status     int
		body       string
		limited    bool
		wantRetry  time.Time
		fallbackOK bool // wantRetry ignored; assert now+1h within bounds
	}{
		{
			name:      "signature 500 body parses retry hint",
			status:    500,
			body:      signatureBody("2026-11-17T05:36:33.557843+00:00"),
			limited:   true,
			wantRetry: retryExact,
		},
		{
			name:      "direct 429 is rate limited with body marker",
			status:    429,
			body:      "HTTP 429: too many requests; retry at 2026-11-17T06:00:00Z",
			limited:   true,
			wantRetry: utc(6, 0),
		},
		{
			name:    "bare 429 without any body",
			status:  429,
			limited: true,
		},
		{
			name:      "hint with mixed case marker and caps Retry At",
			status:    500,
			body:      "DEFERRED BY PROVIDER QUOTA … Retry At 2026-11-17T08:05:00Z: HTTP 429",
			limited:   true,
			wantRetry: utc(8, 5),
		},
		{
			name:    "hint without quota marker or 429 is not rate limited",
			status:  500,
			body:    "retry at 2026-11-17T09:44:00",
			limited: false,
		},
		{
			name:      "naive hint after marker reads as UTC",
			status:    500,
			body:      "deferred by provider quota; retry at 2026-11-17T09:44:00",
			limited:   true,
			wantRetry: utc(9, 44),
		},
		{
			name:      "lowercase z suffix hint",
			status:    500,
			body:      "deferred by provider quota; retry at 2026-11-17T10:01:00z",
			limited:   true,
			wantRetry: utc(10, 1),
		},
		{
			name:    "garbage hint token falls back to one hour",
			status:  429,
			body:    "retry at soon-ish",
			limited: true,
		},
		{
			name:      "quota marker lowercase envelope with hint",
			status:    500,
			body:      "deferred by provider quota; retry at 2026-11-17T07:12:00Z",
			limited:   true,
			wantRetry: utc(7, 12),
		},
		{
			name:    "marker without retry hint falls back one hour",
			status:  500,
			body:    "ProviderRateLimitResetError: quota spent",
			limited: true,
		},
		{
			name:      "retry hint followed by prose colon still parses",
			status:    500,
			body:      "deferred by provider quota … retry at 2026-11-17T05:36:33Z: HTTP 429: x",
			limited:   true,
			wantRetry: utc(5, 36).Add(33 * time.Second),
		},
		{
			name:    "other 500 is not rate limited",
			status:  500,
			body:    `{"detail": "internal server error"}`,
			limited: false,
		},
		{
			name:    "client error is not rate limited",
			status:  400,
			body:    "bad request",
			limited: false,
		},
		{
			name:    "unavailable is not rate limited",
			status:  503,
			body:    "service unavailable",
			limited: false,
		},
		{
			name:    "network error shape status 0 is not rate limited",
			status:  0,
			body:    "dial tcp: connection refused",
			limited: false,
		},
		{
			name:    "marker spelled in body but wrong status still limited via needle",
			status:  502,
			body:    "PROVIDERRATELIMITRESETERROR: spent",
			limited: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			limited, retryAt := Classify(tt.status, tt.body)
			if limited != tt.limited {
				t.Fatalf("rateLimited = %v, want %v", limited, tt.limited)
			}
			if !tt.limited {
				if !retryAt.IsZero() {
					t.Fatalf("retryAt = %v, want zero when not limited", retryAt)
				}
				return
			}
			switch {
			case !tt.wantRetry.IsZero():
				if !retryAt.Equal(tt.wantRetry) {
					t.Fatalf("retryAt = %v, want %v", retryAt, tt.wantRetry)
				}
			case tt.fallbackOK || tt.wantRetry.IsZero():
				lo := time.Now().UTC().Add(DefaultRetryDelay - time.Minute)
				hi := time.Now().UTC().Add(DefaultRetryDelay + time.Minute)
				if retryAt.Before(lo) || retryAt.After(hi) {
					t.Fatalf("fallback retryAt = %v, want ~now+%v", retryAt, DefaultRetryDelay)
				}
			}
		})
	}
}

func TestClassifyFallbackDeterministic(t *testing.T) {
	// The Python twin injects `now`; the Go contract derives it internally, so
	// the fallback is asserted relative to the observed call time.
	before := time.Now().UTC()
	limited, retryAt := Classify(429, "")
	after := time.Now().UTC()
	if !limited {
		t.Fatal("429 must be rate limited")
	}
	if retryAt.Before(before.Add(DefaultRetryDelay)) || retryAt.After(after.Add(DefaultRetryDelay)) {
		t.Fatalf("fallback retryAt = %v, want between now+%v bounds around call", retryAt, DefaultRetryDelay)
	}
}

func item(content string) map[string]any {
	return map[string]any{"content": content, "timestamp": "2026-11-17T05:00:00Z"}
}

// idMemo maps a test label to one minted operation id so assertions can
// reference labels while every entry still carries a real UUID operation key
// (entry ids must be replayable operation ids, see entryValid).
var idMemo = map[string]string{}

func idFor(label string) string {
	if id, ok := idMemo[label]; ok {
		return id
	}
	id := newOperationID()
	idMemo[label] = id
	return id
}

func entry(label, content string, retryAfter time.Time) Entry {
	return Entry{ID: idFor(label), BankID: "test-bank", Item: item(content), QueuedAt: time.Now().UTC(), RetryAfter: retryAfter}
}

func openQueue(t *testing.T, path string) *Queue {
	t.Helper()
	q, err := Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	return q
}

func TestQueuePersistenceRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "hindsight", "retain-backlog.jsonl")
	past := time.Now().UTC().Add(-time.Minute)
	q := openQueue(t, path)
	if q.MayExist() {
		t.Fatal("fresh queue must not may-exist (hot path stays file-I/O free)")
	}
	if _, err := q.Add(entry("id-1", "first memory", past)); err != nil {
		t.Fatalf("Add: %v", err)
	}
	if _, err := q.Add(entry("id-2", "second memory", past)); err != nil {
		t.Fatalf("Add: %v", err)
	}
	if !q.MayExist() {
		t.Fatal("queue with entries must may-exist")
	}

	reopened := openQueue(t, path) // survives restart
	if !reopened.MayExist() {
		t.Fatal("reopened queue must may-exist")
	}
	got := reopened.Entries()
	if len(got) != 2 || got[0].ID != idFor("id-1") || got[1].ID != idFor("id-2") {
		t.Fatalf("FIFO order lost on round-trip: %+v", got)
	}
	if got[0].Item["content"] != "first memory" || got[0].BankID != "test-bank" {
		t.Fatalf("entry payload mutated on round-trip: %+v", got[0])
	}
}

func TestQueueAddDedupeByID(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	due := time.Now().UTC().Add(-time.Second)
	if added, _ := q.Add(entry("dup", "memory", due)); !added {
		t.Fatal("first add must be accepted")
	}
	if added, _ := q.Add(entry("dup", "memory", due)); added {
		t.Fatal("duplicate id must not enqueue a second copy")
	}
	if got := len(q.Entries()); got != 1 {
		t.Fatalf("entries = %d, want 1", got)
	}
}

func TestQueueFilePermissionsAndJSONLShape(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("0600 mode bits are POSIX-only")
	}
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	if _, err := q.Add(entry("id-1", "memory", time.Now().UTC())); err != nil {
		t.Fatalf("Add: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("Stat: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("file mode = %o, want 600", perm)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	lines := 0
	for _, line := range splitLines(string(raw)) {
		var probe map[string]any
		if json.Unmarshal([]byte(line), &probe) != nil {
			t.Fatalf("line %d is not a JSON object: %q", lines, line)
		}
		lines++
	}
	if lines != 1 {
		t.Fatalf("JSONL lines = %d, want 1", lines)
	}
}

func TestDrainNotDueSendsNothing(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	future := time.Now().UTC().Add(2 * time.Hour)
	if _, err := q.Add(entry("id-1", "memory", future)); err != nil {
		t.Fatalf("Add: %v", err)
	}
	now := time.Now().UTC()
	calls := 0
	if _, err := q.Drain(now, func(Entry) error { calls++; return nil }); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if calls != 0 {
		t.Fatalf("retain called %d times before the earliest retry_after", calls)
	}
	if got := len(q.Entries()); got != 1 {
		t.Fatalf("entries = %d, want 1", got)
	}
	// The gate re-opens at the earliest retry_after.
	if _, err := q.Drain(future.Add(time.Second), func(Entry) error { calls++; return nil }); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if calls != 1 {
		t.Fatalf("retain called %d times once due, want 1", calls)
	}
}

func TestDrainFIFORemoveAfterSuccess(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	due := time.Now().UTC().Add(-time.Minute)
	for _, e := range []Entry{entry("id-1", "first memory", due), entry("id-2", "second memory", due)} {
		if _, err := q.Add(e); err != nil {
			t.Fatalf("Add: %v", err)
		}
	}
	var order []string
	if _, err := q.Drain(time.Now().UTC(), func(e Entry) error {
		order = append(order, e.Item["content"].(string))
		return nil
	}); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if len(order) != 2 || order[0] != "first memory" || order[1] != "second memory" {
		t.Fatalf("drain order = %v, want FIFO [first memory second memory]", order)
	}
	if q.MayExist() {
		t.Fatal("drained queue must stop may-existing")
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("drained backlog file still present: %v", err)
	}
	// A second pass sends nothing (exactly-once).
	calls := 0
	if _, err := q.Drain(time.Now().UTC(), func(Entry) error { calls++; return nil }); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if calls != 0 {
		t.Fatalf("second drain sent %d entries, want 0", calls)
	}
}

func TestDrainRemovesOnlySucceededEntries(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	due := time.Now().UTC().Add(-time.Minute)
	for _, e := range []Entry{entry("ok-1", "first", due), entry("bad-1", "second", due), entry("ok-2", "third", due)} {
		if _, err := q.Add(e); err != nil {
			t.Fatalf("Add: %v", err)
		}
	}
	boom := errors.New("transient network blip")
	_, err := q.Drain(time.Now().UTC(), func(e Entry) error {
		if e.ID == idFor("bad-1") {
			return boom
		}
		return nil
	})
	if !errors.Is(err, boom) {
		t.Fatalf("Drain err = %v, want the non-rate-limit failure surfaced", err)
	}
	// First removed after success; the failing one and everything behind it stay.
	remaining := q.Entries()
	if len(remaining) != 2 || remaining[0].ID != idFor("bad-1") || remaining[1].ID != idFor("ok-2") {
		t.Fatalf("remaining = %+v, want [bad-1 ok-2]", remaining)
	}
	// A later pass can finish the job.
	if _, err := q.Drain(time.Now().UTC(), func(Entry) error { return nil }); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if got := len(openQueue(t, path).Entries()); got != 0 {
		t.Fatalf("entries after recovery drain = %d, want 0", got)
	}
}

func TestDrainStopsOnRateLimitAndPushesRetryForward(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	due := time.Now().UTC().Add(-time.Minute)
	for _, e := range []Entry{entry("head-1", "first", due), entry("tail-1", "second", due)} {
		if _, err := q.Add(e); err != nil {
			t.Fatalf("Add: %v", err)
		}
	}
	newRetry := time.Now().UTC().Add(3 * time.Hour)
	calls := 0
	next, err := q.Drain(time.Now().UTC(), func(e Entry) error {
		calls++
		return &RateLimitedError{RetryAt: newRetry}
	})
	if err != nil {
		t.Fatalf("rate-limited drain must stop the pass, not fail: %v", err)
	}
	if !next.Equal(newRetry) {
		t.Fatalf("drain next-retry = %v, want the pushed hint %v", next, newRetry)
	}
	if calls != 1 {
		t.Fatalf("retain called %d times, want 1 (stop on first rate-limit)", calls)
	}
	for _, e := range q.Entries() {
		if !e.RetryAfter.Equal(newRetry) {
			t.Fatalf("entry %s retry_after = %v, want %v", e.ID, e.RetryAfter, newRetry)
		}
	}
	// While the head is still limited the gate stays shut.
	calls = 0
	if _, err := q.Drain(time.Now().UTC().Add(time.Hour), func(Entry) error { calls++; return nil }); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if calls != 0 {
		t.Fatalf("retain called %d times before the pushed retry_after", calls)
	}
	// Once past it, the same FIFO order drains.
	var order []string
	if _, err := q.Drain(newRetry.Add(time.Second), func(e Entry) error {
		order = append(order, e.Item["content"].(string))
		return nil
	}); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if len(order) != 2 || order[0] != "first" || order[1] != "second" {
		t.Fatalf("post-limit drain order = %v, want FIFO", order)
	}
}

func TestDrainEarliestGateMixedEntries(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	past := time.Now().UTC().Add(-time.Minute)
	future := time.Now().UTC().Add(30 * time.Minute)
	if _, err := q.Add(entry("later-1", "queued while limited", future)); err != nil {
		t.Fatalf("Add: %v", err)
	}
	// While every entry is still limited the pass gate (min retry_after)
	// holds and nothing runs.
	if _, err := q.Add(entry("later-2", "queued later", future)); err != nil {
		t.Fatalf("Add: %v", err)
	}
	calls := 0
	if _, err := q.Drain(time.Now().UTC(), func(Entry) error { calls++; return nil }); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if calls != 0 {
		t.Fatalf("retain called %d times while the earliest retry_after had not passed", calls)
	}
	// A newly due entry opens the gate for the whole FIFO: entries whose own
	// retry_after is later still ship oldest-first, and a still-limited
	// provider re-limits them via the drain error path (TestDrainStopsOn...).
	if _, err := q.Add(entry("due-1", "queued after reset", past)); err != nil {
		t.Fatalf("Add: %v", err)
	}
	var order []string
	if _, err := q.Drain(time.Now().UTC(), func(e Entry) error {
		order = append(order, e.ID)
		return nil
	}); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if len(order) != 3 || order[0] != idFor("later-1") || order[1] != idFor("later-2") || order[2] != idFor("due-1") {
		t.Fatalf("drain order = %v, want FIFO [later-1 later-2 due-1]", order)
	}
	if got := len(openQueue(t, path).Entries()); got != 0 {
		t.Fatalf("entries after mixed drain = %d, want 0", got)
	}
}

func TestEntriesCopyAndCallArgFidelity(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	due := time.Now().UTC().Add(-time.Second)
	async := true
	src := Entry{
		ID: idFor("id-args"), BankID: "bank-x", Item: item("with args"),
		DocumentID: "doc-42", RetainAsync: &async, QueuedAt: time.Now().UTC(), RetryAfter: due,
	}
	if _, err := q.Add(src); err != nil {
		t.Fatalf("Add: %v", err)
	}

	var got Entry
	if _, err := q.Drain(time.Now().UTC(), func(e Entry) error { got = e; return nil }); err != nil {
		t.Fatalf("Drain: %v", err)
	}
	if got.BankID != "bank-x" || got.DocumentID != "doc-42" || got.RetainAsync == nil || !*got.RetainAsync {
		t.Fatalf("call-level args lost in flight: %+v", got)
	}

	// Entries() must snapshot: mutating the returned slice never reorders the queue.
	if _, err := q.Add(entry("a", "a", due)); err != nil {
		t.Fatalf("Add: %v", err)
	}
	if _, err := q.Add(entry("b", "b", due)); err != nil {
		t.Fatalf("Add: %v", err)
	}
	snapshot := q.Entries()
	snapshot[0], snapshot[1] = snapshot[1], snapshot[0]
	if ids := []string{q.Entries()[0].ID, q.Entries()[1].ID}; ids[0] != idFor("a") || ids[1] != idFor("b") {
		t.Fatalf("queue order perturbed by caller mutation: %v", ids)
	}
}

func TestDrainWrappedRateLimitedErrorUnwraps(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	if _, err := q.Add(entry("id-1", "memory", time.Now().UTC().Add(-time.Minute))); err != nil {
		t.Fatalf("Add: %v", err)
	}
	newRetry := time.Now().UTC().Add(90 * time.Minute)
	next, err := q.Drain(time.Now().UTC(), func(Entry) error {
		return fmt.Errorf("aretain_batch: %w", &RateLimitedError{RetryAt: newRetry})
	})
	if err != nil {
		t.Fatalf("wrapped rate-limit must stop the pass, not fail: %v", err)
	}
	if got := q.Entries()[0].RetryAfter; !got.Equal(newRetry) {
		t.Fatalf("retry_after = %v, want %v", got, newRetry)
	}
	if !next.Equal(newRetry) {
		t.Fatalf("drain next-retry = %v, want the pushed hint %v", next, newRetry)
	}
}

func TestOpenQuarantinesCorruptLines(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "retain-backlog.jsonl")
	good, _ := json.Marshal(entry("good-1", "kept", time.Now().UTC()))
	torn := map[string]any{"id": nil} // id null: unusable as an operation key
	bad, _ := json.Marshal(torn)
	// The identical corrupt line appears twice in one file: quarantined
	// once per load (per-instance dedupe, mirroring the Python twin's
	// in-memory line set; a restart re-appends, like Python's).
	if err := os.WriteFile(path, []byte(
		string(good)+"\n"+string(bad)+"\n{not json\n"+string(bad)+"\n"+string(good)+"\n"), 0o600); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	q := openQueue(t, path)
	entries := q.Entries()
	if len(entries) != 2 || entries[0].ID != idFor("good-1") || entries[1].ID != idFor("good-1") {
		t.Fatalf("torn-line recovery = %+v, want both good entries", entries)
	}
	// The corrupt lines survive in the sidecar, never silently dropped.
	raw, err := os.ReadFile(path + CorruptSuffix)
	if err != nil {
		t.Fatalf("corrupt sidecar missing: %v", err)
	}
	if !strings.Contains(string(raw), string(bad)) || !strings.Contains(string(raw), "{not json") {
		t.Fatalf("corrupt sidecar = %q, want both bad lines", raw)
	}
	if got := strings.Count(string(raw), string(bad)); got != 1 {
		t.Fatalf("identical corrupt line quarantined %d times in one load, want 1", got)
	}
	// Reopen keeps replaying the good entries (a restart re-quarantines the
	// still-live corrupt lines, like the Python twin).
	if entries := openQueue(t, path).Entries(); len(entries) != 2 {
		t.Fatalf("reopen entries = %d, want 2", len(entries))
	}
}

func TestDrainReturnsNextRetry(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	future := time.Now().UTC().Add(2 * time.Hour)
	if _, err := q.Add(entry("later", "memory", future)); err != nil {
		t.Fatalf("Add: %v", err)
	}
	// Gate shut: the pass returns the earliest retry so the caller can arm an
	// exact wakeup instead of polling.
	next, err := q.Drain(time.Now().UTC(), func(Entry) error { return nil })
	if err != nil || !next.Equal(future) {
		t.Fatalf("gated drain = (%v, %v), want (%v, nil)", next, err, future)
	}
	if !q.NextRetry().Equal(future) {
		t.Fatalf("NextRetry = %v, want %v", q.NextRetry(), future)
	}
	// Fully drained: zero time means no wakeup is needed.
	if next, err = q.Drain(future.Add(time.Second), func(Entry) error { return nil }); err != nil || !next.IsZero() {
		t.Fatalf("drained next-retry = (%v, %v), want (zero, nil)", next, err)
	}
	// Non-rate-limit settle failure: entry kept, its retry returned with the
	// error so a wakeup can still be armed for the retry.
	if _, err := q.Add(entry("flaky", "memory", time.Now().UTC().Add(-time.Minute))); err != nil {
		t.Fatalf("Add: %v", err)
	}
	next, err = q.Drain(time.Now().UTC(), func(Entry) error { return errors.New("probe down") })
	if err == nil || !next.Equal(q.NextRetry()) {
		t.Fatalf("failed settle = (%v, %v), want (earliest retry, error)", next, err)
	}
}

func TestAddMintsOperationID(t *testing.T) {
	path := filepath.Join(t.TempDir(), "retain-backlog.jsonl")
	q := openQueue(t, path)
	added, err := q.Add(Entry{BankID: "test-bank", Item: item("minted"), RetryAfter: time.Now().UTC()})
	if err != nil || !added {
		t.Fatalf("Add = (%v, %v)", added, err)
	}
	id := q.Entries()[0].ID
	if !uuidRe.MatchString(id) {
		t.Fatalf("Add must mint a UUID operation id when empty, got %q", id)
	}
	// The minted id is persisted — it survives a reopen, still replayable.
	if got := openQueue(t, path).Entries()[0].ID; got != id {
		t.Fatalf("minted id not durable: %q vs %q", got, id)
	}
}

func TestOpenMissingFileIsQuiet(t *testing.T) {
	q := openQueue(t, filepath.Join(t.TempDir(), "absent.jsonl"))
	if q.MayExist() {
		t.Fatal("absent backlog must not may-exist")
	}
	if _, err := q.Drain(time.Now().UTC(), func(Entry) error {
		t.Fatal("retain must not run for an absent backlog")
		return nil
	}); err != nil {
		t.Fatalf("Drain: %v", err)
	}
}

func TestOpenDirectoryError(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "isdir"), 0o750); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(filepath.Join(dir, "isdir")); err == nil {
		t.Fatal("opening a directory must error, not masquerade as missing")
	}
}

func splitLines(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			if chunk := s[start:i]; chunk != "" {
				out = append(out, chunk)
			}
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}
