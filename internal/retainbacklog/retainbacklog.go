// Package retainbacklog is the runtime implementation of the Hindsight memory
// provider's provider-rate-limit retain backlog. The Python provider keeps
// only a thin bridge (exception classification, network settle, worker
// threads); queue state, persistence, and reconciliation live HERE and run as
// the cmd/retainbacklog binary invoked by that bridge.
//
// The incident this runtime owns: Hindsight's retain path can fail with an
// HTTP 500 whose body wraps the upstream model provider's 429 —
// {"detail": "Fact extraction deferred by provider quota: ...
// ProviderRateLimitResetError: ... retry at 2026-11-17T05:36:33.557843+00:00:
// HTTP 429: ..."}. Dropping the turn loses a memory permanently; retrying
// immediately just burns the same quota. The already-built retain item is
// therefore persisted to a durable per-home JSONL backlog and drained (FIFO)
// once the provider's own reset hint passes.
//
// An entry's ID is a UUID minted at Add time, persisted before any remote
// submission, and doubles as the Hindsight server-side operation_id. Replay
// semantics are the SERVER's, verified against the live hindsight container
// (see .review-evidence/server-semantics.md in the PR checkout):
//
//   - submit_async_retain with a caller-supplied operation_id is idempotent:
//     a repeated id returns the original operation and creates no new work.
//   - retry_operation atomically re-queues a failed OR cancelled row.
//   - a streaming retain replays resumably: already-committed chunks (same
//     document_id + content hash) are skipped, so a replay after a partial
//     commit never duplicates committed chunks.
//   - a sync retain ignores operation_id and may commit earlier mini-batches
//     before the quota raise, which is why the entry — not the ack — is the
//     durable record.
//
// Consequence: a drain NEVER deletes an operation row to retry it, and an
// entry is removed only after the server-side status is observed "completed".
// Submission acks keep the entry (submitted=true) for a later poll.
//
// Only provider rate limits / quota deferrals queue. Every other failure —
// other 500s, 4xx, network errors — propagates unchanged on the Python side
// and queues nothing.
package retainbacklog

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"
)

// PollInterval is how long a drain waits before re-checking an entry the
// server owns (pending/processing, or freshly (re-)queued). The server's own
// worker retry window (default 3 x 60s) runs underneath; this poll only
// observes the outcome and re-queues on terminal failure.
const PollInterval = 60 * time.Second

// BackoffFirst is the first non-rate settle failure's retry delay; it doubles
// per consecutive failure up to BackoffMax. Without it a persistently failing
// settle (e.g. network down) re-fires every wakeup floor — a hot loop.
const (
	BackoffFirst = 60 * time.Second
	BackoffMax   = time.Hour
)

// CorruptSuffix names the quarantine sidecar next to the backlog file:
// lines that are not valid entries are appended there (never silently
// dropped) and excluded from the live queue.
const CorruptSuffix = ".corrupt"

// BacklogFilename is the JSONL file name inside <home>/hindsight/.
const BacklogFilename = "retain-backlog.jsonl"

// uuidRe accepts the two shapes real producers emit for an entry id — the
// canonical dashed UUID4 and the compact 32-hex form (uuid.UUID parses both,
// so legacy compact ids remain replayable operation keys).
var uuidRe = regexp.MustCompile(`^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$`)

// Entry is one queued retain: the already-built aretain_batch item (opaque
// JSON, replayed verbatim) plus the call-level arguments needed to replay it.
// ID is the server-side operation_id — see the package doc.
type Entry struct {
	ID     string          `json:"id"`
	BankID string          `json:"bank_id"`
	Item   json.RawMessage `json:"item"`
	// DocumentID keys server-side chunk-hash recovery: a replay must carry
	// the SAME document id as the original attempt or committed chunks
	// duplicate (the server mints a fresh one per request when omitted).
	DocumentID string    `json:"document_id,omitempty"`
	QueuedAt   time.Time `json:"queued_at"`
	RetryAfter time.Time `json:"retry_after"`
	// Submitted is true once a submission with this id has been acked (or the
	// row was re-queued via retry_operation). Until the server reports
	// "completed" the entry stays queued and is polled, never dropped.
	Submitted bool `json:"submitted,omitempty"`
	// Attempts counts consecutive non-rate settle failures of this entry; it
	// drives the exponential backoff and resets on any progress.
	Attempts int `json:"attempts,omitempty"`
}

// Clone returns a deep copy safe for handoff to a Remote.
func (e Entry) Clone() Entry {
	item := make(json.RawMessage, len(e.Item))
	copy(item, e.Item)
	e.Item = item
	return e
}

// Queue is the JSONL-backed backlog: one JSON Entry per line in FIFO file
// order, rewrites atomic (tmp + rename + fsync + dir fsync). All cross-process
// serialization is the caller's flock on <path>.lock, held for the whole
// command; a process that cannot take that lock must not mutate the file.
type Queue struct {
	mu          sync.Mutex
	path        string
	entries     []Entry
	quarantined map[string]bool
}

// BacklogPath returns the backlog file path for a home directory.
func BacklogPath(home string) string {
	return filepath.Join(home, "hindsight", BacklogFilename)
}

// Open loads the backlog at path. A missing file yields an empty queue. Lines
// that are not valid entries — unparseable JSON, a missing or non-UUID id, a
// missing bank or item — are quarantined to <path>.corrupt, never silently
// dropped; the live queue keeps only what a drain could replay. A UTF-8 BOM
// (the Windows notepad shape) is tolerated on the first line.
func Open(path string) (*Queue, error) {
	q := &Queue{path: path, quarantined: map[string]bool{}}
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return q, nil
		}
		return nil, err
	}
	raw = bytes.TrimPrefix(raw, []byte{0xEF, 0xBB, 0xBF})
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	scanner.Buffer(make([]byte, 0, 64*1024), 64*1024*1024)
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		var entry Entry
		if json.Unmarshal(line, &entry) == nil && entryValid(entry) {
			q.entries = append(q.entries, entry)
		} else if qerr := q.quarantine(string(line)); qerr != nil {
			return nil, qerr
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return q, nil
}

// Entries returns a copy of the live entries in FIFO order.
func (q *Queue) Entries() []Entry {
	q.mu.Lock()
	defer q.mu.Unlock()
	out := make([]Entry, len(q.entries))
	for i, e := range q.entries {
		out[i] = e.Clone()
	}
	return out
}

// Len is the number of live entries.
func (q *Queue) Len() int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.entries)
}

// NextRetry is the earliest retry_after among queued entries — the instant a
// later drain may make progress. Zero when the queue is empty.
func (q *Queue) NextRetry() time.Time {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.earliestRetry()
}

// Add appends one entry (FIFO tail) and persists, returning the entry's
// final id. An empty ID is minted as a UUID4 — the operation key must exist
// before any remote submission. Dedupe by ID: re-adding an id already
// present is a no-op (added false), so a crashed enqueuer that retries
// persists exactly one entry.
func (q *Queue) Add(entry Entry) (id string, added bool, err error) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if entry.ID == "" {
		minted, merr := newOperationID()
		if merr != nil {
			return "", false, merr
		}
		entry.ID = minted
	}
	if entry.QueuedAt.IsZero() {
		entry.QueuedAt = time.Now().UTC()
	}
	for _, existing := range q.entries {
		if existing.ID == entry.ID {
			return entry.ID, false, nil
		}
	}
	q.entries = append(q.entries, entry)
	return entry.ID, true, q.save()
}

func (q *Queue) earliestRetry() time.Time {
	if len(q.entries) == 0 {
		return time.Time{}
	}
	earliest := q.entries[0].RetryAfter
	for _, e := range q.entries[1:] {
		if e.RetryAfter.Before(earliest) {
			earliest = e.RetryAfter
		}
	}
	return earliest
}

// entryValid reports whether an entry is replayable: a usable operation-id
// UUID, a bank, and an item payload. Anything else is quarantined.
func entryValid(e Entry) bool {
	return uuidRe.MatchString(e.ID) && e.BankID != "" && len(e.Item) > 0 && json.Valid(e.Item)
}

// quarantine appends one invalid line to <path>.corrupt at most once per
// process (line-guarded); recoverable by an operator, never replayed.
func (q *Queue) quarantine(line string) error {
	if q.quarantined[line] {
		return nil
	}
	q.quarantined[line] = true
	return appendQuarantine(q.path+CorruptSuffix, line)
}

// save persists the entries atomically: tmp file in the same directory (0600,
// fsync) then rename over path, then a directory fsync so the rename itself
// survives a crash. An empty queue removes the file and fsyncs the directory
// too — an unlinked name must not resurrect after a crash.
// The caller holds q.mu.
func (q *Queue) save() error {
	dir := filepath.Dir(q.path)
	if len(q.entries) == 0 {
		if err := os.Remove(q.path); err != nil && !os.IsNotExist(err) {
			return err
		}
		return fsyncDir(dir)
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp := q.path + ".tmp"
	payload := make([]byte, 0, 4096)
	for _, e := range q.entries {
		line, err := json.Marshal(e)
		if err != nil {
			return err
		}
		payload = append(payload, line...)
		payload = append(payload, '\n')
	}
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, werr := f.Write(payload); werr != nil {
		f.Close()
		os.Remove(tmp)
		return werr
	}
	if werr := f.Sync(); werr != nil {
		f.Close()
		os.Remove(tmp)
		return werr
	}
	if werr := f.Close(); werr != nil {
		os.Remove(tmp)
		return werr
	}
	if err := os.Rename(tmp, q.path); err != nil {
		os.Remove(tmp)
		return err
	}
	return fsyncDir(dir)
}

// newOperationID mints a random UUID4 string.
func newOperationID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("mint operation id: %w", err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}
