package retainbacklog

import "errors"

// SettleAction is what a drain does for one server-side operation status —
// the reconcile table mirroring the Python provider's
// _settle_backlog_entry:
//
//	pending / processing / completed -> SettleNone         (server owns the memory)
//	failed  / cancelled             -> SettleDeleteResubmit (row stored nothing)
//	not_found / anything unknown    -> SettleSubmit         (idempotent replay key)
type SettleAction int

const (
	// SettleNone means the operation row (or its server-side retry
	// machinery — quota deferrals park server-side as pending ops) owns
	// the memory: the entry is settled without any submission.
	SettleNone SettleAction = iota
	// SettleDeleteResubmit means the operation is terminal and stored
	// nothing: the row must be deleted before the id can be reused
	// (retry_operation refuses batch_retain parents), then the SAME id is
	// resubmitted — single-item operations commit nothing, so the resubmit
	// cannot duplicate.
	SettleDeleteResubmit
	// SettleSubmit means no operation (or an uninterpretable status)
	// exists server-side: submit async with the entry id as the
	// operation_id. A repeated id replays as the original operation, so
	// this branch is safe even when the status probe raced a just-created
	// op — the crash-window guarantee.
	SettleSubmit
)

// settledStatuses prove the memory is durably the server's.
var settledStatuses = map[string]bool{
	"pending":    true,
	"processing": true,
	"completed":  true,
}

// terminalUnstoredStatuses ended without committing the (single) item.
var terminalUnstoredStatuses = map[string]bool{
	"failed":    true,
	"cancelled": true,
}

// SettleActionForStatus maps a lowercase server-side operation status to
// the drain action. Unknown statuses map to SettleSubmit: an
// uninterpretable state cannot prove the server owns the memory, and a
// replay submission is idempotent, so submitting is the safe resolution.
func SettleActionForStatus(status string) SettleAction {
	switch {
	case settledStatuses[status]:
		return SettleNone
	case terminalUnstoredStatuses[status]:
		return SettleDeleteResubmit
	default:
		return SettleSubmit
	}
}

// ErrOperationNotFound is what Operations.OperationStatus returns (or
// wraps) when the server has no row for an id — the fresh-submit path.
var ErrOperationNotFound = errors.New("operation not found")

// Operations is the server port the reconcile needs. The Python provider
// implements it over the Hindsight SDK (get_operation_status /
// delete_operation / aretain_batch async with operation_id); a Go consumer
// would implement it over the same HTTP surface.
type Operations interface {
	// OperationStatus returns the lowercase status of one operation id, or
	// an error wrapping ErrOperationNotFound when no such row exists.
	// Transport errors are returned verbatim (the drain keeps the entry).
	OperationStatus(id string) (string, error)
	// DeleteOperation removes a terminal operation row so its id can be
	// reused by a fresh submission.
	DeleteOperation(id string) error
	// SubmitAsync submits one item as an async retain whose idempotency key
	// is the entry id; a repeated id MUST replay as the original operation
	// (no new work) — that contract is what makes reconcile safe.
	SubmitAsync(bankID, id string, item map[string]any) error
}

// Reconciler drives entry settle passes per the table above, mirroring the
// Python provider's _settle_backlog_entry: reconcile against server-side
// operation state, and only submit when the memory is not provably the
// server's.
type Reconciler struct {
	// Ops is the server port; required.
	Ops Operations
}

// Settle reconciles one queue entry. It returns nil when the entry may be
// removed: the operation is settled server-side, or a submission carrying
// the entry id was accepted (the id replays idempotently, so an
// acknowledge is ownership). Errors propagate to Drain, which keeps the
// entry and stops the pass.
func (r *Reconciler) Settle(entry Entry) error {
	status, err := r.Ops.OperationStatus(entry.ID)
	if err != nil {
		if !errors.Is(err, ErrOperationNotFound) {
			return err
		}
		status = "not_found"
	}
	switch SettleActionForStatus(status) {
	case SettleNone:
		return nil
	case SettleDeleteResubmit:
		if err := r.Ops.DeleteOperation(entry.ID); err != nil {
			return err
		}
	}
	return r.Ops.SubmitAsync(entry.BankID, entry.ID, entry.Item)
}
