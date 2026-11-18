package retainbacklog

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// SettleAction is what a drain does for one observed server-side operation
// status. The table encodes the SERVER's verified retry semantics (see the
// package doc and .review-evidence/server-semantics.md):
//
//	completed                    -> SettleRemove    (memory durably stored; entry's job is done)
//	pending / processing         -> SettlePoll      (server owns it; its own retry window runs;
//	                                                 re-check after PollInterval)
//	failed / cancelled           -> SettleRetryOp   (retry_operation atomically re-queues both;
//	                                                 NEVER delete the row to retry — cancel is
//	                                                 cooperative, work in flight may commit, and
//	                                                 delete+resubmit discards the server's own
//	                                                 durable retry state)
//	not_found / anything unknown -> SettleSubmit    (fresh or pruned row; submission is idempotent
//	                                                 by operation id and resumes via chunk-hash
//	                                                 recovery when the row existed before)
type SettleAction int

const (
	SettleRemove SettleAction = iota
	SettlePoll
	SettleRetryOp
	SettleSubmit
)

// RateLimitedError reports a drain-time remote failure that is a provider
// rate limit / quota deferral; RetryAt is the earliest instant a retry may
// run, taken from the fresh failure. Every entry still queued behind the same
// provider limit is pushed forward to it.
type RateLimitedError struct {
	RetryAt time.Time
}

func (e *RateLimitedError) Error() string {
	return fmt.Sprintf("provider rate limited; retry at %s", e.RetryAt.Format(time.RFC3339))
}

// ErrGoneFromRetry is what Remote.RetryOperation returns when the server no
// longer has the row (404 — pruned): the drain falls back to a fresh submit.
var ErrGoneFromRetry = errors.New("operation gone from retry")

// ErrRetryConflict is what Remote.RetryOperation returns when the server
// refuses the retry with an AMBIGUOUS 409 (e.g. the row moved to
// pending/processing between the status read and the retry call, or any 409
// that is not the explicit permanent refusal). The entry simply polls again.
var ErrRetryConflict = errors.New("retry refused")

// ErrRetryPermanent is what Remote.RetryOperation returns when the server's
// 409 explicitly says the operation can never be re-queued — the live
// server's exact shape is the batch_retain parent "with no incomplete
// sub-batches to retry (its children have all completed, or it has none)",
// which instructs the caller to resubmit the source documents
// (memory_engine.retry_operation). The drain complies: the entry mints a NEW
// operation id (persisted atomically before any submission) and replays the
// same item + document id — the server's chunk-hash recovery skips every
// chunk the old operation already committed, so nothing duplicates. The old
// server row is never deleted; there is no delete in this protocol.
var ErrRetryPermanent = errors.New("retry permanently refused")

// Remote is the host side of the drain: everything needing the Hindsight SDK,
// the API key, and the provider's secret scope. The Go runtime owns the queue
// and the reconcile decisions; the host owns the network.
type Remote interface {
	// Status returns the lowercase server-side operation status for id
	// ("not_found" when the server has no row). Transport failures return a
	// non-nil error (the entry is kept and backed off).
	Status(ctx context.Context, bankID, opID string) (string, error)
	// RetryOperation asks the server to re-queue a failed/cancelled
	// operation. Returns ErrGoneFromRetry / ErrRetryConflict for 404/409.
	RetryOperation(ctx context.Context, bankID, opID string) error
	// Submit replays the entry as an async retain carrying its id. A provider
	// rate limit must return *RateLimitedError; other errors back the entry
	// off. A nil return is a submission ack — the entry STAYS queued
	// (submitted=true) and is polled until "completed".
	Submit(ctx context.Context, entry Entry) error
}

// SettleActionForStatus maps a lowercase server-side operation status to the
// drain action. Unknown statuses map to SettleSubmit: an uninterpretable
// state cannot prove the server owns the memory, and a replay submission is
// idempotent by operation id, so submitting is the safe resolution.
func SettleActionForStatus(status string) SettleAction {
	switch status {
	case "completed":
		return SettleRemove
	case "pending", "processing":
		return SettlePoll
	case "failed", "cancelled":
		return SettleRetryOp
	default: // not_found, unknown
		return SettleSubmit
	}
}

// ActionString names an action for protocol/test use.
func ActionString(a SettleAction) string {
	switch a {
	case SettleRemove:
		return "remove"
	case SettlePoll:
		return "poll"
	case SettleRetryOp:
		return "retry_op"
	default:
		return "submit"
	}
}

// Drain performs one FIFO settle pass over the entries that are individually
// due (retry_after <= now). The pass starts only when at least one entry is
// due — the earliest retry_after gates it — and each entry is additionally
// gated on its own retry_after, so a not-yet-due FIFO head is never settled
// early by a due tail.
//
// Per entry: reconcile against server state per SettleActionForStatus; an
// entry is removed only on observed "completed". A *RateLimitedError from the
// remote stops the pass and pushes every still-queued entry's retry_after
// forward to the fresh hint (the provider limit is server-wide). Any other
// remote error keeps the failing entry, bumps its consecutive-failure
// backoff (BackoffFirst doubling to BackoffMax), and stops the pass.
//
// The return value is the earliest retry_after among entries still queued
// after the pass (zero when empty) — the instant a later drain may make
// progress. The caller must hold the cross-process flock for the whole pass.
func (q *Queue) Drain(now time.Time, remote Remote) (time.Time, error) {
	ctx := context.Background()
	q.mu.Lock()
	defer q.mu.Unlock()
	for i := 0; i < len(q.entries); {
		entry := q.entries[i]
		if entry.RetryAfter.After(now) {
			i++ // per-entry due gate: not yet due, leave queued in place
			continue
		}
		status := "not_found"
		if entry.Submitted {
			var err error
			status, err = remote.Status(ctx, entry.BankID, entry.ID)
			if err != nil {
				var rl *RateLimitedError
				if errors.As(err, &rl) {
					q.pushAllForward(rl.RetryAt)
					return q.earliestRetry(), rl
				}
				q.backOff(i, now)
				return q.earliestRetry(), err
			}
		}
		switch SettleActionForStatus(status) {
		case SettleRemove:
			q.entries = append(q.entries[:i], q.entries[i+1:]...)
			if err := q.save(); err != nil {
				return q.earliestRetry(), err
			}
		case SettlePoll:
			q.entries[i].RetryAfter = now.Add(PollInterval)
			q.entries[i].Attempts = 0
			if err := q.save(); err != nil {
				return q.earliestRetry(), err
			}
			i++
		case SettleRetryOp:
			err := remote.RetryOperation(ctx, entry.BankID, entry.ID)
			if err != nil {
				switch {
				case errors.Is(err, ErrRetryPermanent):
					newID, nerr := newOperationID()
					if nerr != nil {
						q.backOff(i, now)
						return q.earliestRetry(), nerr
					}
					// The server will never re-run the old id (its children all
					// completed or it has none). Swap to a fresh operation id and
					// persist the swap BEFORE submitting: a crash in between
					// leaves a not-yet-submitted entry whose new id replays
					// cleanly (not_found -> submit). The old server row stays;
					// the unchanged document id + item keep chunk-hash recovery
					// intact, so committed chunks are never re-ingested.
					q.entries[i].ID = newID
					q.entries[i].Submitted = false
					q.entries[i].Attempts = 0
					q.entries[i].RetryAfter = now
					if serr := q.save(); serr != nil {
						return q.earliestRetry(), serr
					}
					entry.ID = newID
					entry.Submitted = false
					// fall through to a fresh submit below under the new id
				case errors.Is(err, ErrGoneFromRetry):
					// fall through to a fresh submit below
				case errors.Is(err, ErrRetryConflict):
					q.entries[i].RetryAfter = now.Add(PollInterval)
					q.entries[i].Attempts = 0
					if serr := q.save(); serr != nil {
						return q.earliestRetry(), serr
					}
					i++
					continue
				default:
					var rl *RateLimitedError
					if errors.As(err, &rl) {
						q.pushAllForward(rl.RetryAt)
						return q.earliestRetry(), rl
					}
					q.backOff(i, now)
					return q.earliestRetry(), err
				}
			} else {
				q.entries[i].Submitted = true
				q.entries[i].RetryAfter = now.Add(PollInterval)
				q.entries[i].Attempts = 0
				if serr := q.save(); serr != nil {
					return q.earliestRetry(), serr
				}
				i++
				continue
			}
			fallthrough
		default: // SettleSubmit
			err := remote.Submit(ctx, entry.Clone())
			if err != nil {
				var rl *RateLimitedError
				if errors.As(err, &rl) {
					q.pushAllForward(rl.RetryAt)
					return q.earliestRetry(), rl
				}
				q.backOff(i, now)
				return q.earliestRetry(), err
			}
			q.entries[i].Submitted = true
			q.entries[i].RetryAfter = now.Add(PollInterval)
			q.entries[i].Attempts = 0
			if err := q.save(); err != nil {
				return q.earliestRetry(), err
			}
			i++
		}
	}
	return q.earliestRetry(), nil
}

// pushAllForward moves every queued entry's retry_after to at least hint.
// The provider quota is server-wide, so entries queued behind the head that
// just hit the limit must not wake before it resets. The caller holds q.mu.
func (q *Queue) pushAllForward(hint time.Time) {
	if hint.IsZero() {
		return
	}
	for i := range q.entries {
		if q.entries[i].RetryAfter.Before(hint) {
			q.entries[i].RetryAfter = hint
		}
	}
	if err := q.save(); err != nil {
		// best-effort: the next pass re-reads the file and re-derives; a
		// failed push only means an earlier retry, never a lost entry.
		_ = err
	}
}

// backOff keeps entry i and schedules its next attempt with the exponential
// non-rate backoff. The caller holds q.mu.
func (q *Queue) backOff(i int, now time.Time) {
	q.entries[i].Attempts++
	delay := BackoffFirst
	for n := 1; n < q.entries[i].Attempts; n++ {
		delay *= 2
		if delay >= BackoffMax {
			delay = BackoffMax
			break
		}
	}
	q.entries[i].RetryAfter = now.Add(delay)
	if err := q.save(); err != nil {
		_ = err // same best-effort reasoning as pushAllForward
	}
}
