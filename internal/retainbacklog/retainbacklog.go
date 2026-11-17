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
// it (FIFO) once the provider's own reset hint passes.
//
// An entry's ID is a UUID minted at Add time, persisted before any remote
// submission, and doubles as the Hindsight server-side operation_id: the
// drain replays every entry async with that id, and the server treats a
// repeated id as a replay of the original operation (no new work). The drain
// is therefore reconcile-then-submit, remove-on-ack: settle(entry) checks the
// server-side operation state (pending/processing/completed = the memory is
// durably the server's; failed/cancelled = delete the row, then resubmit the
// same id; not found = submit it) and returns nil once settled. That makes
// the crash window between server ack and local removal harmless — a restart
// reconciles instead of duplicating.
//
// Only provider rate limits / quota deferrals queue. Every other failure —
// other 500s, 4xx, network errors — must behave exactly as before: raise, keep
// nothing, change no latency on the happy path.
package retainbacklog

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
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

// CorruptSuffix names the quarantine sidecar next to the backlog file:
// lines that are not valid entries are appended there (never silently
// dropped) and excluded from the live queue.
const CorruptSuffix = ".corrupt"

// uuidRe accepts the two shapes real producers emit for an entry id — the
// canonical dashed UUID4 and the compact 32-hex form (uuid.UUID parses both,
// so legacy compact ids remain replayable operation keys).
var uuidRe = regexp.MustCompile(`^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$`)

// Classify reports whether a failed retain was rejected by a provider rate
// limit / quota and, when it was, the earliest instant a retry may run.
//
// Rate-limited iff status is 429 or the body carries a provider-quota marker
// (case-insensitive). retryAt parses a "retry at <ISO 8601>" hint from the
// body; without a parseable hint it falls back to now plus
// DefaultRetryDelay. When rateLimited is false retryAt is the zero Time and
// callers must propagate the failure unchanged.
func Classify(status int, body string) (rateLimited bool, retryAt time.Time) {
	rateLimited = status == 429
	if !rateLimited {
		folded := strings.ToLower(body)
		for _, needle := range quotaNeedles {
			if strings.Contains(folded, needle) {
				rateLimited = true
				break
			}
		}
	}
	if !rateLimited {
		return false, time.Time{}
	}
	return true, parseRetryAt(body, time.Now().UTC())
}

// parseRetryAt extracts the reset hint, or falls back to base + 1h. Accepts
// the layouts datetime.isoformat emits (offset or Z, optional fraction);
// a naive timestamp is read as UTC.
func parseRetryAt(body string, base time.Time) time.Time {
	if m := retryAtRe.FindStringSubmatch(body); m != nil {
		token := strings.Trim(m[1], trailingPunctuation)
		if strings.HasSuffix(token, "z") || strings.HasSuffix(token, "Z") {
			token = token[:len(token)-1] + "+00:00"
		}
		for _, layout := range []string{time.RFC3339Nano, time.RFC3339, "2006-01-02T15:04:05.999999-0700", "2006-01-02T15:04:05"} {
			if at, err := time.Parse(layout, token); err == nil {
				return at.UTC()
			}
		}
	}
	return base.UTC().Add(DefaultRetryDelay)
}

// Entry is one queued retain: the already-built aretain_batch item plus the
// call-level arguments needed to replay it verbatim (bank_id/document_id/
// retain_async are call-level args, never item keys, on the Python side too).
// ID is the server-side operation_id — see the package doc.
type Entry struct {
	ID          string         `json:"id"`
	BankID      string         `json:"bank_id"`
	Item        map[string]any `json:"item"`
	DocumentID  string         `json:"document_id,omitempty"`
	RetainAsync *bool          `json:"retain_async,omitempty"`
	QueuedAt    time.Time      `json:"queued_at"`
	RetryAfter  time.Time      `json:"retry_after"`
}

// RateLimitedError is what a drain-time settle returns when the provider is
// still rate-limited; RetryAt is the new earliest retry instant taken from
// the fresh failure.
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
	mu          sync.Mutex
	path        string
	entries     []Entry
	mayExist    bool
	quarantined map[string]bool
}

// Open loads the backlog at path (one stat; a missing file yields an empty,
// not-may-exist queue). Lines that are not valid entries — unparseable JSON,
// a missing or non-UUID id, a missing bank — are quarantined to
// <path>.corrupt, never silently dropped; the live queue keeps only what a
// drain could replay.
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
		if json.Unmarshal([]byte(line), &entry) == nil && entryValid(entry) {
			q.entries = append(q.entries, entry)
		} else if qerr := q.quarantine(line); qerr != nil {
			return nil, qerr
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
func (q *Queue) MayExist() bool { return q.mayExist }

// Entries returns a copy of the live entries in FIFO order.
func (q *Queue) Entries() []Entry {
	q.mu.Lock()
	defer q.mu.Unlock()
	out := make([]Entry, len(q.entries))
	copy(out, q.entries)
	return out
}

// NextRetry is the earliest retry_after among queued entries — the instant a
// later drain may make progress. Zero when the queue is empty.
func (q *Queue) NextRetry() time.Time {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.earliestRetry()
}

// Add appends one entry (FIFO tail) and persists. An empty ID is minted as a
// UUID4 — the operation key must exist before any remote submission.
// Dedupe by ID: re-adding an id already present is a no-op (added false) —
// the exactly-once guard for re-persisting a known entry.
func (q *Queue) Add(entry Entry) (added bool, err error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if entry.ID == "" {
		entry.ID = newOperationID()
	}
	for _, existing := range q.entries {
		if existing.ID == entry.ID {
			return false, q.save()
		}
	}
	q.entries = append(q.entries, entry)
	return true, q.save()
}

// Drain performs one FIFO settle pass, mirroring the Python writer drain:
//
//   - nothing runs while now is before the earliest retry_after;
//   - entries are settled oldest-first; settle(entry) owns the remote side
//     (reconcile against server-side operation state, submit when unknown) —
//     nil means the operation is durably server-owned;
//   - an entry is removed (and persisted) only after settle succeeds —
//     removal happens under the lock right after the ack;
//   - a *RateLimitedError stops the pass and pushes the retry_after of every
//     entry still waiting behind the same provider limit forward to the new
//     hint (the Python side pushes every earlier retry_after the same way, so
//     the earliest-retry gate never re-opens while the head is still limited);
//   - any other error keeps the failing entry, stops the pass, and is
//     returned for the caller to log.
//
// The return is the earliest retry_after among entries still queued after the
// pass — the instant a later drain may make progress; zero when the backlog
// is empty — so the caller can arm an exact wakeup.
//
// settle is never called with queue state held on any other goroutine.
func (q *Queue) Drain(now time.Time, settle func(Entry) error) (time.Time, error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if !q.mayExist || len(q.entries) == 0 {
		return time.Time{}, nil
	}
	earliest := q.earliestRetry()
	if now.UTC().Before(earliest) {
		return earliest, nil
	}
	for i := 0; i < len(q.entries); i++ {
		entry := q.entries[i]
		err := settle(entry)
		if err == nil {
			// Remove-after-ack, exactly once: the entry is gone from
			// disk before the next one is attempted.
			q.entries = append(q.entries[:i], q.entries[i+1:]...)
			i--
			if err := q.save(); err != nil {
				return q.earliestRetry(), err
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
				return q.earliestRetry(), err
			}
			return limited.RetryAt.UTC(), nil
		}
		return q.earliestRetry(), err // non-rate-limit failure: entry kept, pass stops
	}
	if len(q.entries) == 0 {
		return time.Time{}, nil
	}
	return q.earliestRetry(), nil
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

// newOperationID mints a UUID4 string, mirroring Python's
// str(uuid.uuid4()) at enqueue time.
func newOperationID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failure is process-fatal territory; the id must still
		// be unique, so fall back to a time-derived value rather than reuse.
		now := time.Now().UTC().UnixNano()
		for i := range b {
			b[i] = byte(now >> (uint(i%8) * 8))
		}
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	dst := make([]byte, 36)
	hex.Encode(dst[0:8], b[0:4])
	dst[8] = '-'
	hex.Encode(dst[9:13], b[4:6])
	dst[13] = '-'
	hex.Encode(dst[14:18], b[6:8])
	dst[18] = '-'
	hex.Encode(dst[19:23], b[8:10])
	dst[23] = '-'
	hex.Encode(dst[24:36], b[10:16])
	return string(dst)
}

// entryValid reports whether an entry is replayable: a usable operation-id
// UUID, a bank, and an item payload. Anything else is quarantined.
func entryValid(e Entry) bool {
	return uuidRe.MatchString(e.ID) && e.BankID != "" && e.Item != nil
}

// quarantine appends one invalid line to <path>.corrupt exactly once per
// process (line-guarded); recoverable by an operator, never replayed. The
// caller holds no lock (Open-time only).
func (q *Queue) quarantine(line string) error {
	if q.quarantined == nil {
		q.quarantined = map[string]bool{}
	}
	if q.quarantined[line] {
		return nil
	}
	q.quarantined[line] = true
	corrupt := q.path + CorruptSuffix
	if dir := filepath.Dir(corrupt); dir != "" {
		if err := os.MkdirAll(dir, 0o750); err != nil {
			return err
		}
	}
	file, err := os.OpenFile(corrupt, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err := file.WriteString(line + "\n"); err != nil {
		return err
	}
	return file.Sync()
}

// save persists the entries atomically: tmp file in the same directory (0600,
// fsync) then rename over path, then a directory fsync so the rename itself
// survives a crash. An empty queue removes the file entirely — a drained
// backlog must not make every later hot-path stat pay I/O forever.
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
	if err := os.Rename(tmp.Name(), q.path); err != nil {
		return err
	}
	if dir, derr := os.Open(filepath.Dir(q.path)); derr == nil {
		dir.Sync() // best-effort: persist the rename
		dir.Close()
	}
	return nil
}
