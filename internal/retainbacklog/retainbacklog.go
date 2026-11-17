// Package retainbacklog is the Go contract for the Hindsight memory
// provider's provider-rate-limit retain backlog, mirroring
// plugins/memory/hindsight/retain_backlog.py in the Python tree. The Python
// provider still executes the Python copy; this package is the Go contract,
// the same arrangement as internal/cronyield — it is not imported by Python.
//
// The incident this contract encodes: Hindsight's retain path can fail with
// an HTTP 500 whose body wraps the upstream model provider's 429 —
// {"detail": "Fact extraction deferred by provider quota: ...
// ProviderRateLimitResetError: ... retry at 2026-11-17T05:36:33.557843+00:00:
// HTTP 429: ..."}. Dropping the turn loses a memory permanently; retrying
// immediately just burns the same quota. The provider therefore persists the
// already-built retain item to a durable per-profile JSONL backlog and drains
// it (FIFO, remove-after-success) once the provider's own reset hint passes.
//
// Only provider rate limits / quota deferrals queue. Every other failure —
// other 500s, 4xx, network errors — must behave exactly as before: raise, keep
// nothing, change no latency on the happy path.
package retainbacklog

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

// quotaNeedles are the body markers of a provider-quota deferral, matched
// against the case-folded body. "providerratelimitreseterror" is the folded
// exception name the Hindsight server embeds; "deferred by provider quota" is
// its envelope phrase. A direct HTTP 429 is rate-limited regardless of body.
var quotaNeedles = []string{
	"providerratelimitreseterror",
	"deferred by provider quota",
}

// retryAtRe captures the provider's reset hint. The signature body continues
// "…+00:00: HTTP 429…", so the token must be trimmed of trailing prose
// punctuation; an ISO 8601 timestamp never legitimately ends with any of it.
var retryAtRe = regexp.MustCompile(`(?i)retry at (\S+)`)

// trailingPunctuation is stripped from a captured retry-at token before
// parsing. Mirrors the Python rstrip(":,;)]}.>").
const trailingPunctuation = ":,;)]}.>"

// DefaultRetryDelay is the fallback wait when the body carries no parseable
// "retry at <ISO>" hint: queue now, retry in an hour.
const DefaultRetryDelay = time.Hour

// Classify reports whether a failed retain was rejected by a provider rate
// limit / quota and, when it was, the earliest instant a retry may run.
//
// Rate-limited iff status is 429 or the body carries a provider-quota marker
// (case-insensitive). retryAt parses a "retry at <ISO 8601>" hint from the
// body; without a parseable hint it falls back to now plus
// DefaultRetryDelay. When rateLimited is false retryAt is the zero Time and
// callers must propagate the failure unchanged.
func Classify(status int, body string) (rateLimited bool, retryAt time.Time) {
	text := strings.ToLower(body)
	limited := status == 429
	if !limited {
		for _, needle := range quotaNeedles {
			if strings.Contains(text, needle) {
				limited = true
				break
			}
		}
	}
	if !limited {
		return false, time.Time{}
	}
	return true, parseRetryAt(body, time.Now())
}

// parseRetryAt extracts the reset hint, or falls back to base + 1h. Accepts
// the layouts datetime.isoformat emits (offset or Z, optional fraction);
// a naive timestamp is read as UTC.
func parseRetryAt(body string, base time.Time) time.Time {
	match := retryAtRe.FindStringSubmatch(body)
	if match != nil {
		raw := strings.TrimRight(match[1], trailingPunctuation)
		if strings.HasSuffix(raw, "Z") || strings.HasSuffix(raw, "z") {
			raw = raw[:len(raw)-1] + "+00:00"
		}
		for _, layout := range []string{time.RFC3339Nano, time.RFC3339, "2006-01-02T15:04:05.999999999", "2006-01-02T15:04:05"} {
			if parsed, err := time.Parse(layout, raw); err == nil {
				return parsed.UTC()
			}
		}
	}
	return base.UTC().Add(DefaultRetryDelay)
}

// Entry is one queued retain: the already-built aretain_batch item plus the
// call-level arguments needed to replay it verbatim (bank_id/document_id/
// retain_async are call-level args, never item keys, on the Python side too).
type Entry struct {
	ID          string         `json:"id"`
	BankID      string         `json:"bank_id"`
	Item        map[string]any `json:"item"`
	DocumentID  string         `json:"document_id,omitempty"`
	RetainAsync *bool          `json:"retain_async,omitempty"`
	QueuedAt    time.Time      `json:"queued_at"`
	RetryAfter  time.Time      `json:"retry_after"`
}

// RateLimitedError is what a drain-time retain attempt returns when the
// provider is still rate-limited; RetryAt is the new earliest retry instant
// taken from the fresh failure.
type RateLimitedError struct {
	RetryAt time.Time
}

func (e *RateLimitedError) Error() string {
	return fmt.Sprintf("provider rate limited; retry at %s", e.RetryAt.Format(time.RFC3339))
}

// Queue is the JSONL-backed backlog contract: <path> holds one JSON Entry per
// line (FIFO file order), rewrites are atomic (tmp + rename, 0600), and every
// operation holds the queue mutex for its whole pass so concurrent enqueuers
// and drainers serialize — the Python side additionally flocks <path>.lock
// across processes. A queue whose file is missing may exist false and every
// operation is a no-op, which is what keeps the happy path free of file I/O.
type Queue struct {
	mu       sync.Mutex
	path     string
	entries  []Entry
	mayExist bool
}

// Open loads the backlog at path (one stat; a missing file yields an empty,
// not-may-exist queue). Malformed lines are skipped, never fatal: a torn
// write must not strand every later memory behind it.
func Open(path string) (*Queue, error) {
	q := &Queue{path: path}
	file, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return q, nil
		}
		return nil, err
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var entry Entry
		if json.Unmarshal([]byte(line), &entry) == nil && entry.ID != "" {
			q.entries = append(q.entries, entry)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	q.mayExist = len(q.entries) > 0
	return q, nil
}

// MayExist is the cheap hot-path gate mirroring the Python flag: True only
// when a backlog might hold entries.
func (q *Queue) MayExist() bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.mayExist
}

// Entries returns a copy of the live entries in FIFO order.
func (q *Queue) Entries() []Entry {
	q.mu.Lock()
	defer q.mu.Unlock()
	out := make([]Entry, len(q.entries))
	copy(out, q.entries)
	return out
}

// Add appends one entry (FIFO tail) and persists. Dedupe by ID: re-adding an
// id already present is a no-op (added false) — the exactly-once guard for
// re-persisting a known entry.
func (q *Queue) Add(entry Entry) (added bool, err error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for _, existing := range q.entries {
		if existing.ID == entry.ID {
			return false, q.save()
		}
	}
	q.entries = append(q.entries, entry)
	return true, q.save()
}

// Drain performs one FIFO pass, mirroring the Python writer drain:
//
//   - nothing runs while now is before the earliest retry_after;
//   - entries are attempted oldest-first;
//   - an entry is removed (and persisted) only after its retain succeeds —
//     removal happens under the lock right after success;
//   - a *RateLimitedError stops the pass and pushes the retry_after of every
//     entry still waiting behind the same provider limit forward to the new
//     hint (the Python side pushes every earlier retry_after the same way, so
//     the earliest-retry gate never re-opens while the head is still limited);
//   - any other error keeps the failing entry, stops the pass, and is
//     returned for the caller to log.
//
// retain is never called with queue state held on any other goroutine.
func (q *Queue) Drain(now time.Time, retain func(Entry) error) error {
	q.mu.Lock()
	defer q.mu.Unlock()
	if !q.mayExist || len(q.entries) == 0 {
		return nil
	}
	earliest := q.earliestRetry()
	if now.UTC().Before(earliest) {
		return nil
	}
	for i := 0; i < len(q.entries); i++ {
		entry := q.entries[i]
		err := retain(entry)
		if err == nil {
			// Remove-after-success, exactly once: the entry is gone from
			// disk before the next one is attempted.
			q.entries = append(q.entries[:i], q.entries[i+1:]...)
			i--
			if err := q.save(); err != nil {
				return err
			}
			continue
		}
		var limited *RateLimitedError
		if ok := asRateLimited(err, &limited); ok {
			for j := range q.entries {
				if q.entries[j].RetryAfter.UTC().Before(limited.RetryAt.UTC()) {
					q.entries[j].RetryAfter = limited.RetryAt.UTC()
				}
			}
			if err := q.save(); err != nil {
				return err
			}
			return nil
		}
		return err // non-rate-limit failure: entry kept, pass stops
	}
	return q.save()
}

// earliestRetry is the drain gate: min(retry_after) over all entries. The
// caller holds q.mu.
func (q *Queue) earliestRetry() time.Time {
	if len(q.entries) == 0 {
		return time.Time{}
	}
	earliest := q.entries[0].RetryAfter.UTC()
	for _, entry := range q.entries[1:] {
		if at := entry.RetryAfter.UTC(); at.Before(earliest) {
			earliest = at
		}
	}
	return earliest
}

// asRateLimited unwraps RateLimitedError (direct or wrapped).
func asRateLimited(err error, target **RateLimitedError) bool {
	if limited, ok := err.(*RateLimitedError); ok {
		*target = limited
		return true
	}
	type unwrapper interface{ Unwrap() error }
	if u, ok := err.(unwrapper); ok {
		return asRateLimited(u.Unwrap(), target)
	}
	return false
}

// save persists the entries atomically: tmp file in the same directory (0600,
// fsync) then rename over path. An empty queue removes the file entirely —
// a drained backlog must not make every later hot-path stat pay I/O forever.
// The caller holds q.mu.
func (q *Queue) save() error {
	if len(q.entries) == 0 {
		q.mayExist = false
		err := os.Remove(q.path)
		if err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	}
	q.mayExist = true
	if dir := filepath.Dir(q.path); dir != "" {
		if err := os.MkdirAll(dir, 0o750); err != nil {
			return err
		}
	}
	tmp, err := os.CreateTemp(filepath.Dir(q.path), filepath.Base(q.path)+".tmp*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name()) // no-op after a successful rename
	writer := bufio.NewWriter(tmp)
	encoder := json.NewEncoder(writer)
	for _, entry := range q.entries {
		if err := encoder.Encode(entry); err != nil {
			tmp.Close()
			return err
		}
	}
	if err := writer.Flush(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), q.path)
}
