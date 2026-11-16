// Package delegationcontrol is the Go twin of the delegate_agent control
// plane for live CLI coding runs in QuixThe2nd/hermes-ide: the ownership,
// list-inclusion, steer-outcome, and acceptance-envelope rules implemented by
// tools/delegate_tool_registry.py (_owns_coding_run_record,
// _owned_live_coding_run, _coding_run_list_entries, _coding_run_steer_reply,
// _coding_run_stop_reply) and tools/async_delegation.py
// (build_background_acceptance_envelope's control block). Same rules,
// independently tested. Production Python never imports or shells out to it;
// it exists so the control contract has a second, line-balance-accounted
// implementation.
//
// Contract (shared with the Python twin):
//   - A live coding run (result_kind cli_agent for delegate_claude_agent,
//     cloud_agent for delegate_cursor_agent) is registered only in the async
//     delegation registry under a deleg_* id, never in the live-subagent
//     registry.
//   - list merges native subagent entries with owned live coding runs; the
//     deleg_* id doubles as the subagent_id and accepting_steer is always
//     false. When anything is listed, the "No live subagents right now" note
//     is suppressed.
//   - steer of an owned live coding run answers not_steerable (never an
//     error, never "No live subagent", never fires the interrupt channels):
//     a Claude CLI run's stdin is closed and a Cursor cloud run has none. The
//     note tells the caller to stop the same id and re-delegate.
//   - stop of an owned live coding run signals the runner exactly once (both
//     the worker-thread bit and interrupt_fn channels) and answers
//     interrupt_requested. The record is snapshotted under the registry lock
//     and signalled outside it, so an interrupt_fn that re-enters the
//     registry cannot deadlock.
//   - Ownership fails closed; empty selectors never claim anything. Both
//     session ids set: equal, or equal after compression-lineage resolution.
//     A caller with no session id matches only by routing session key and
//     only when the record carries no parent session id. Every other shape
//     (including a foreign session id whose session key would have matched)
//     refuses.
package delegationcontrol

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

// Result kinds of a delegation record (mirrors tools/async_delegation.py).
const (
	KindSubagentBatch = "subagent_batch"
	KindCLIAgent      = "cli_agent"
	KindCloudAgent    = "cloud_agent"
	KindMission       = "mission"
)

// Record statuses that matter to control decisions (mirrors _ACTIVE_STATES:
// "finalizing" is already delivering its terminal result — not controllable).
const (
	StatusRunning   = "running"
	StatusStalling  = "stalling"
	StatusFinalize  = "finalizing"
	StatusCompleted = "completed"
)

// Reply statuses (mirrors _CONTROL_OUTCOMES and the coding-run steer reply).
const (
	StatusNotSteerable       = "not_steerable"
	StatusInterruptRequested = "interrupt_requested"
	StatusQueued             = "queued"
)

// Model-facing control tool every coding-run handle names.
const ControlTool = "delegate_agent"

// EmptyListNote mirrors the payload note emitted when nothing is live.
const EmptyListNote = "No live subagents right now. Children that already finished have delivered (or will deliver) their results as normal completion messages — there is nothing to steer or stop."

// Control actions a coding-run handle advertises; steer is never among them.
var CodingRunActions = []string{"list", "stop"}

// Error categories; every refusal carries the model-facing text and one of
// these sentinels so callers can branch without matching strings.
var (
	ErrSubagentIDRequired  = errors.New("subagent_id required")
	ErrSteerMessageNeeded  = errors.New("steer message required")
	ErrNoLiveSubagent      = errors.New("no live subagent")
	ErrInterruptFailed     = errors.New("interrupt failed")
	ErrUnknownAction       = errors.New("unknown action")
	ErrSteerNotAccepting   = errors.New("steer not accepting")
	ErrCodingRunNotSteered = errors.New("coding run not steerable")
)

// Record mirrors the async-delegation registry fields control reads.
type Record struct {
	DelegationID    string
	Tool            string
	ResultKind      string
	Status          string
	Goal            string
	SessionKey      string
	ParentSessionID string
	DispatchedAt    float64 // unix seconds
	// InterruptFn mirrors interrupt_fn: the tool-specific cancellation
	// (process-group kill, cloud-run cancel). Called with NO registry lock
	// held; it may re-enter the registry.
	InterruptFn func()
	// RunnerTID mirrors runner_tid: the worker-thread interrupt bit target.
	// Non-zero means the cooperative in-runner channel exists.
	RunnerTID int64
}

// Caller is the controlling conversation's spine: the durable session id, an
// optional compression-lineage resolver (pre-rotation id -> lineage tip), and
// the routing session key bound to the current context.
type Caller struct {
	SessionID         string
	CurrentSessionKey string
	ResolveLineage    func(sessionID string) string
}

func (c *Caller) lineage(sid string) string {
	if c == nil || c.ResolveLineage == nil || sid == "" {
		return sid
	}
	return c.ResolveLineage(sid)
}

// IsCodingKind reports whether a result kind is a controllable coding run.
func IsCodingKind(kind string) bool {
	return kind == KindCLIAgent || kind == KindCloudAgent
}

// IsLiveStatus reports whether a record status is live (running or stalling).
func IsLiveStatus(status string) bool {
	return status == StatusRunning || status == StatusStalling
}

// OwnsCodingRun decides ownership of one async-record, fail closed on every
// empty selector (mirrors _owns_coding_run_record):
//
//  1. Both session ids set: equal, or equal after lineage resolution on
//     either side (compression rotation).
//  2. Caller spineless (no session id) AND record carries no parent session
//     id: the routing session key must be non-empty and equal.
//  3. Everything else refuses — including a foreign parent session id whose
//     session key would have matched.
func (c *Caller) OwnsCodingRun(rec *Record) bool {
	if rec == nil {
		return false
	}
	parentSID := c.SessionID
	recordSID := rec.ParentSessionID
	if parentSID != "" && recordSID != "" {
		if parentSID == recordSID {
			return true
		}
		tip := c.lineage(recordSID)
		return tip == parentSID || tip == c.lineage(parentSID)
	}
	if parentSID == "" && recordSID == "" {
		return c.CurrentSessionKey != "" && c.CurrentSessionKey == rec.SessionKey
	}
	return false
}

// Registry mirrors async_delegation._records + _records_lock.
type Registry struct {
	mu      sync.Mutex
	records map[string]*Record
}

func NewRegistry() *Registry {
	return &Registry{records: make(map[string]*Record)}
}

func (r *Registry) Put(rec *Record) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.records[rec.DelegationID] = rec
}

func (r *Registry) Remove(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.records, id)
}

// snapshot returns a copy of the record map, taken under the lock. Callers
// resolve ownership and signal interrupts OUTSIDE any lock.
func (r *Registry) snapshot() []*Record {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]*Record, 0, len(r.records))
	for _, rec := range r.records {
		copy := *rec
		out = append(out, &copy)
	}
	return out
}

// ListEntry is one model-facing list row. The deleg_* id doubles as the
// subagent_id the model steers/stops with.
type ListEntry struct {
	SubagentID     string  `json:"subagent_id"`
	Tool           string  `json:"tool"`
	Goal           string  `json:"goal"`
	Status         string  `json:"status"`
	RunningSeconds float64 `json:"running_seconds"`
	AcceptingSteer bool    `json:"accepting_steer"`
}

// ListCodingRuns returns the owned live coding runs (mirrors
// _coding_run_list_entries): foreign sessions, completed runs, finalizing
// runs, and subagent_batch records are all excluded.
func (r *Registry) ListCodingRuns(c *Caller) []ListEntry {
	now := float64(time.Now().UnixNano()) / 1e9
	entries := []ListEntry{}
	for _, rec := range r.snapshot() {
		if !IsCodingKind(rec.ResultKind) || !IsLiveStatus(rec.Status) {
			continue
		}
		if !c.OwnsCodingRun(rec) {
			continue
		}
		entries = append(entries, ListEntry{
			SubagentID:     rec.DelegationID,
			Tool:           rec.Tool,
			Goal:           rec.Goal,
			Status:         rec.Status,
			RunningSeconds: now - rec.DispatchedAt,
			AcceptingSteer: false,
		})
	}
	return entries
}

// ListPayload mirrors the action='list' reply shape.
type ListPayload struct {
	Action    string      `json:"action"`
	Count     int         `json:"count"`
	Subagents []ListEntry `json:"subagents"`
	Note      string      `json:"note,omitempty"`
}

// BuildListPayload merges native subagent entries with owned live coding
// runs. The empty-registry note appears only when NOTHING is listed — an
// owned coding run alone is enough to suppress it.
func BuildListPayload(native []ListEntry, reg *Registry, c *Caller) ListPayload {
	entries := make([]ListEntry, 0, len(native))
	entries = append(entries, native...)
	if reg != nil {
		entries = append(entries, reg.ListCodingRuns(c)...)
	}
	payload := ListPayload{Action: "list", Count: len(entries), Subagents: entries}
	if len(entries) == 0 {
		payload.Note = EmptyListNote
	}
	return payload
}

// ControlReply mirrors the steer/stop reply shape.
type ControlReply struct {
	Action     string `json:"action"`
	SubagentID string `json:"subagent_id"`
	Status     string `json:"status"`
	Note       string `json:"note"`
}

func noLiveSubagentError(id string) error {
	return fmt.Errorf(
		"%w: No live subagent '%s' in this conversation's spawn tree. It may have already finished "+
			"(its result arrives as a normal completion message). Use action='list' to see live children.",
		ErrNoLiveSubagent, id)
}

func interruptFailedError(id string) error {
	return fmt.Errorf(
		"%w: Could not interrupt '%s' — it likely finished in the last moment. "+
			"Its result arrives as a normal completion message.",
		ErrInterruptFailed, id)
}

// NotSteerableNote is the honest steer answer for a live owned coding run
// (mirrors _coding_run_steer_reply): say why mid-run text is impossible, how
// to stop it (same id), and to re-delegate the course correction.
func NotSteerableNote(id string) string {
	return fmt.Sprintf(
		"Live coding run '%s' cannot accept mid-run text: a Claude CLI run's stdin is closed "+
			"and a Cursor cloud run has none. To change course, stop it first "+
			"(action='stop', subagent_id='%s' — the same id), then re-delegate a new run "+
			"carrying the course correction.", id, id)
}

// StopNote mirrors the shared stop success note.
const StopNote = "The subagent stops at its next iteration boundary (in-flight tool calls are asked to cancel). " +
	"Its partial result still re-enters the conversation as a completion message — do not wait or poll."

// SignalRunnerInterrupt mirrors async_delegation._signal_runner_interrupt:
// both channels fire — the worker-thread interrupt bit (cooperative hooks
// inside the runner) and interrupt_fn (the tool-specific cancellation).
// Returns true when any channel fired.
func SignalRunnerInterrupt(rec *Record, reason string) bool {
	signalled := false
	if rec.RunnerTID != 0 {
		// The Python twin sets a per-thread interrupt bit; the twin records
		// only whether the channel exists.
		signalled = true
	}
	if rec.InterruptFn != nil {
		rec.InterruptFn()
		signalled = true
	}
	return signalled
}

// ownedLiveCodingRun resolves one deleg_* id to an owned live coding run,
// snapshotted under the lock; ownership is resolved OUTSIDE the lock (it may
// walk a session-lineage resolver). nil when absent, not a coding run, no
// longer live, or not owned.
func (r *Registry) ownedLiveCodingRun(id string, c *Caller) *Record {
	for _, rec := range r.snapshot() {
		if rec.DelegationID != id {
			continue
		}
		if !IsCodingKind(rec.ResultKind) || !IsLiveStatus(rec.Status) {
			return nil
		}
		if !c.OwnsCodingRun(rec) {
			return nil
		}
		return rec
	}
	return nil
}

// Steer answers a steer action against the coding-run registry. An owned live
// coding run always answers not_steerable and NEVER signals an interrupt
// channel; an unresolvable or foreign id refuses with ErrNoLiveSubagent.
func (r *Registry) Steer(id, message string, c *Caller) (ControlReply, error) {
	sid := strings.TrimSpace(id)
	if sid == "" {
		return ControlReply{}, fmt.Errorf(
			"%w: action='steer' requires subagent_id (from the spawn dispatch response or action='list')",
			ErrSubagentIDRequired)
	}
	rec := r.ownedLiveCodingRun(sid, c)
	if rec == nil {
		return ControlReply{}, noLiveSubagentError(sid)
	}
	if strings.TrimSpace(message) == "" {
		return ControlReply{}, fmt.Errorf(
			"%w: action='steer' requires a non-empty 'message' describing the course correction",
			ErrSteerMessageNeeded)
	}
	return ControlReply{
		Action:     "steer",
		SubagentID: sid,
		Status:     StatusNotSteerable,
		Note:       NotSteerableNote(sid),
	}, nil
}

// Stop ends an owned live coding run: the record snapshot is taken under the
// registry lock, the interrupt channels fire exactly once OUTSIDE it, and the
// reply carries the shared interrupt_requested shape. A record with no live
// channel answers ErrInterruptFailed rather than lying about signalling.
func (r *Registry) Stop(id string, c *Caller) (ControlReply, error) {
	sid := strings.TrimSpace(id)
	if sid == "" {
		return ControlReply{}, fmt.Errorf(
			"%w: action='stop' requires subagent_id (from the spawn dispatch response or action='list')",
			ErrSubagentIDRequired)
	}
	rec := r.ownedLiveCodingRun(sid, c)
	if rec == nil {
		return ControlReply{}, noLiveSubagentError(sid)
	}
	if !SignalRunnerInterrupt(rec, "delegate_agent stop "+sid) {
		return ControlReply{}, interruptFailedError(sid)
	}
	return ControlReply{
		Action:     "stop",
		SubagentID: sid,
		Status:     StatusInterruptRequested,
		Note:       StopNote,
	}, nil
}

// ControlHandle mirrors the acceptance envelope's control block.
type ControlHandle struct {
	Tool           string   `json:"tool"`
	DelegationID   string   `json:"delegation_id"`
	SubagentID     string   `json:"subagent_id"`
	Actions        []string `json:"actions"`
	AcceptingSteer bool     `json:"accepting_steer"`
}

// ControlHandleSentence is the handle text appended to control_hint (mirrors
// build_background_acceptance_envelope).
func ControlHandleSentence(delegationID string) string {
	return fmt.Sprintf(
		"Control this run via delegate_agent with its id '%s': action='stop' ends it "+
			"(it cannot be steered mid-run — the Claude CLI runs with stdin closed and a "+
			"Cursor cloud run has no stdin); re-delegate a new run with the course correction.",
		delegationID)
}

// AttachControlHandle mirrors the envelope assembly: ONLY cli_agent and
// cloud_agent payloads gain a control block and a handle sentence appended to
// any caller-supplied control_hint (caller text is preserved, never dropped).
// Every other result kind — subagent_batch included — gains nothing and keeps
// an explicitly passed control_hint verbatim.
func AttachControlHandle(payload map[string]any, tool, resultKind, delegationID, controlHint string) map[string]any {
	if payload == nil {
		payload = map[string]any{}
	}
	if !IsCodingKind(resultKind) {
		if controlHint != "" {
			payload["control_hint"] = controlHint
		}
		return payload
	}
	payload["control"] = ControlHandle{
		Tool:           ControlTool,
		DelegationID:   delegationID,
		SubagentID:     delegationID,
		Actions:        append([]string(nil), CodingRunActions...),
		AcceptingSteer: false,
	}
	if controlHint != "" {
		payload["control_hint"] = controlHint + " " + ControlHandleSentence(delegationID)
	} else {
		payload["control_hint"] = ControlHandleSentence(delegationID)
	}
	return payload
}
