package servingidentity

import (
	"encoding/json"
	"strconv"
	"strings"
)

// MetaKeyPrefix is the session-meta key namespace for the durable
// served-by record (gateway/serving_transition.py): one
// "served_by:<session_id>" key per session holding {"pid","start_time","note"}.
const MetaKeyPrefix = "served_by:"

// MetaKey renders the session-meta key for a session's served-by record.
func MetaKey(sessionID string) string {
	return MetaKeyPrefix + sessionID
}

// ValidSessionID reports whether a session id can anchor a durable record:
// a non-blank string. Anything else means "no persistence available" and
// callers degrade to claiming no transition.
func ValidSessionID(sessionID string) bool {
	return strings.TrimSpace(sessionID) != ""
}

// servedByRecord is the persisted wire shape {"pid","start_time","note"} —
// the pid alone is not identity (PIDs get reused), so the start fingerprint
// is part of the record; the note is the exact runtime note the session
// last saw, the durable proof that suppresses an unchanged repeat.
type servedByRecord struct {
	PID       int    `json:"pid"`
	StartTime int64  `json:"start_time"`
	Note      string `json:"note,omitempty"`
}

// parseStoredRecord decodes a stored served-by record in full. False for
// absent, corrupt, or non-positive-pid records: the absence of a provable
// predecessor is "no transition", never an error.
func parseStoredRecord(raw string) (servedByRecord, bool) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return servedByRecord{}, false
	}
	var record servedByRecord
	if err := json.Unmarshal([]byte(raw), &record); err != nil {
		return servedByRecord{}, false
	}
	if record.PID <= 0 {
		return servedByRecord{}, false
	}
	return record, true
}

// ParseRecord decodes a stored served-by record's process identity.
func ParseRecord(raw string) (Snapshot, bool) {
	record, ok := parseStoredRecord(raw)
	if !ok {
		return Snapshot{}, false
	}
	return Snapshot{PID: record.PID, StartTime: record.StartTime}, true
}

// StoredNote extracts the note-proof field of a stored served-by record
// ("" when absent or corrupt): the exact runtime note the session already
// saw, which suppresses an unchanged repeat.
func StoredNote(raw string) string {
	record, ok := parseStoredRecord(raw)
	if !ok {
		return ""
	}
	return record.Note
}

// EncodeRecord renders the durable record for THIS process plus the note
// the session just saw (never carries a build sha — records fingerprint
// process identity, not code identity).
func EncodeRecord(s Snapshot, note string) string {
	encoded, err := json.Marshal(servedByRecord{PID: s.PID, StartTime: s.StartTime, Note: note})
	if err != nil {
		return "{}"
	}
	return string(encoded)
}

// RecordWriteNeeded is the pure write-on-change predicate for the served-by
// record: false only when the stored record parses and already carries
// exactly this pid + start fingerprint + note (an ordinary same-process turn
// whose note wording did not change is one cheap point read, not a write).
// Corrupt or absent records are writes — the durable predecessor must be
// learnable. Byte-parity with
// gateway.serving_transition._record_write_needed via the shared table.
func RecordWriteNeeded(storedRaw string, current Snapshot, note string) bool {
	stored, ok := parseStoredRecord(storedRaw)
	if !ok {
		return true
	}
	return stored.PID != current.PID || stored.StartTime != current.StartTime ||
		stored.Note != note
}

// RegistryRow is one live row of the hub's generation registry (the
// overlap-successors mirror file): a spawned successor, in spawn order.
// Parity twin of the rows gateway.overlap_registry.read_live_rows hands
// plugins.gateway_restart.socket_owner_action.
type RegistryRow struct {
	PID            int
	PredecessorPID int
}

// registryRowsAttestForward proves the generation-array forward: rows are
// live, spawn-ordered successor records, so the LAST row is the newest
// generation, and the forward holds when the answerer IS that newest
// registered generation and the requesting serving pid is positively
// listed — as a registered successor or as some row's predecessor (the
// original generation that spawned the line). A requester the registry
// never mentions is an unrelated process: unprovable, keep refusing.
// Parity twin of plugins.gateway_restart._registry_rows_attest_forward,
// pinned by the shared honesty table.
func registryRowsAttestForward(rows []RegistryRow, servingPID, answerPID int) bool {
	rowPIDs := make([]int, 0, len(rows))
	listed := make(map[int]bool, 2*len(rows))
	for _, row := range rows {
		if row.PID <= 0 {
			continue
		}
		rowPIDs = append(rowPIDs, row.PID)
		listed[row.PID] = true
		if row.PredecessorPID > 0 {
			listed[row.PredecessorPID] = true
		}
	}
	if len(rowPIDs) == 0 {
		return false
	}
	return rowPIDs[len(rowPIDs)-1] == answerPID && listed[servingPID]
}

// OwnerAction answers: may a restart be queued through this control-socket
// answerer, given the pid serving THIS conversation?
//
// "proceed" — the answerer is the serving process, or nothing is provable
// (no stamp / unparseable probe keeps pre-fix behavior rather than
// refusing old installs); "refuse" — a different process answered and
// cannot attest this conversation's server as its hub-ready successor (the
// field incident: the draining predecessor owned the socket while its
// successor served, and the queued restart waited forever);
// "attested-chain" — a different process answered but attests the serving
// process as its hub-ready successor: queueing is correct and the result
// must name the serving process; "attested-forward" — a different, NEWER
// process answered: it reports role "serving" and the generation registry
// (rows) proves it the newest live generation of an array that positively
// lists the requesting server, so this is a forward to the newest (the
// array design: a restart on a draining generation forwards to the
// newest); queueing is correct and the result must name the newest
// answerer. Parity twin of plugins.gateway_restart.socket_owner_action,
// pinned by the shared honesty table.
func OwnerAction(servingPID int, id *Identify, rows []RegistryRow) string {
	if servingPID <= 0 || id == nil || id.PID <= 0 {
		return "proceed"
	}
	if id.PID == servingPID {
		return "proceed"
	}
	if id.Role == RoleDrainingPredecessor && id.SuccessorPID == servingPID &&
		id.SuccessorReady {
		return "attested-chain"
	}
	if id.Role == RoleServing && registryRowsAttestForward(rows, servingPID, id.PID) {
		return "attested-forward"
	}
	return "refuse"
}

// OwnerRefusal renders the restart-tool refusal when the socket's answerer
// is not the conversation's serving process: it names both pids, states why
// queueing there cannot work, and points at the safe alternatives. The
// owner clause is truthful per reported role — "its build predates overlap
// reporting" only for an answerer with no overlap block (the empty role),
// never for a current-build serving answerer. Parity twin of
// plugins.gateway_restart.socket_owner_refusal, pinned by the shared
// honesty table (byte-identical wording).
func OwnerRefusal(servingPID int, id *Identify) string {
	role := Role("")
	if id != nil {
		role = id.Role
	}
	ownerClause := "reports no restart-handoff state (its build predates overlap reporting)"
	switch role {
	case RoleDrainingPredecessor:
		ownerClause = "is draining for a restart whose successor is not confirmed as this " +
			"conversation's server"
	case RoleServing:
		ownerClause = "is serving, not draining for a restart, and is not proven to share " +
			"this conversation's generation array"
	case RoleOverlapSuccessor:
		ownerClause = "is an overlap successor that does not attest this conversation's server"
	case "":
		// The legacy answerer: no overlap block at all.
	default:
		ownerClause = "could not classify its own restart-handoff state"
	}
	answerPID := 0
	if id != nil {
		answerPID = id.PID
	}
	return "No restart was queued: the gateway control socket is owned by pid " +
		itoa(answerPID) + ", which " + ownerClause + ", but this conversation is served by " +
		"pid " + itoa(servingPID) + ". A restart queued through pid " + itoa(answerPID) +
		" would not target the process running this conversation. It is already served by " +
		"pid " + itoa(servingPID) + "; if it still needs a restart, retry after the current " +
		"handoff completes or run `hermes gateway restart` from an operator shell."
}

// itoa is strconv.Itoa aliased locally to keep the message builders terse.
func itoa(v int) string {
	return strconv.Itoa(v)
}

// ForwardedProcessNote renders the self-describing note for a restart whose
// result names the NEWEST generation (the process that took the request
// and spawns its own successor). Parity twin of
// plugins.gateway_restart._forwarded_process_note.
func ForwardedProcessNote(targetPID, forwardingPID int) string {
	forwarder := ""
	if forwardingPID > 0 {
		forwarder = " (pid " + itoa(forwardingPID) + ")"
	}
	return "Restart queued, not completed: 'process' names the newest generation " +
		"(pid " + itoa(targetPID) + "), which took the request and spawns its own successor. " +
		"The generation that forwarded it" + forwarder + " is superseded and will not " +
		"perform the restart; that successor process rewrites this result with " +
		"the 'Restarted successfully' completion notice."
}

// ackInt decodes an ack value that may be an int (library callers) or a
// JSON float64 (table/cmd decode); positive ints only.
func ackInt(v any) (int, bool) {
	switch n := v.(type) {
	case int:
		if n > 0 {
			return n, true
		}
	case float64:
		if n > 0 && n == float64(int(n)) {
			return int(n), true
		}
	}
	return 0, false
}

// AckRewrite rewrites a control-socket restart ack so its pid names the
// process that will actually restart. Three proven shapes rename it: a
// "forwarded" ack names the newest generation (forwarded_to — the one that
// took the request and spawns its own successor); a "chained" ack from an
// older build is proven by the identify attestation (attested-chain) and
// names the serving process; and an attested-forward ask — a restart
// requested from an OLDER draining generation of the array — names the
// newest answerer, with the note stating the conversation's own process is
// the superseded forwarder. Rendering the answerer as the process would
// point the agent at a dying process. Parity twin of
// plugins.gateway_restart._socket_ack_names_restarting_process, pinned by
// the shared honesty table.
func AckRewrite(ack map[string]any, servingPID int, id *Identify, rows []RegistryRow) map[string]any {
	if ack == nil {
		return ack
	}
	status, _ := ack["status"].(string)
	action := OwnerAction(servingPID, id, rows)
	var target int
	proven := false
	switch {
	case status == "forwarded":
		if value, ok := ackInt(ack["forwarded_to"]); ok {
			target, proven = value, true
		}
	case status == "chained":
		target = servingPID
		proven = action == "attested-chain"
	case action == "attested-forward":
		if id != nil {
			target, proven = id.PID, id.PID > 0
		}
	}
	if !proven {
		return ack
	}
	answerPID := 0
	if value, ok := ackInt(ack["pid"]); ok {
		answerPID = value
	} else if id != nil {
		answerPID = id.PID
	}
	rewritten := make(map[string]any, len(ack)+2)
	for key, value := range ack {
		rewritten[key] = value
	}
	rewritten["pid"] = target
	if status == "chained" {
		rewritten["note"] =
			"Restart chained for the serving process: 'process' names pid " + itoa(target) +
				", the process serving this conversation. The control socket is still owned " +
				"by the draining predecessor (pid " + itoa(answerPID) + "); the chained restart " +
				"runs on pid " + itoa(target) + " once that handoff completes."
	} else {
		forwarder := answerPID
		if action == "attested-forward" {
			forwarder = servingPID
		}
		rewritten["note"] = ForwardedProcessNote(target, forwarder)
	}
	return rewritten
}

// StageServedBy is the decision twin of
// gateway.serving_transition.session_turn_notes: read the session's
// served-by record, render the turn notes against THIS process (the note
// gate above applies), then record this process on change only — with the
// STORED note proof, NOT the new one. The proof is acknowledgement-gated:
// it advances only via ConfirmServedNote once the note's api_content
// sidecar stamp lands (agent.turn_context._persist_turn_user_api_content),
// so a turn that crashes before the stamp re-emits the note instead of
// suppressing it forever. Read-before-write ordering matters: the notes
// must describe the state BEFORE this turn's record lands. Persistence is
// injected (get/set), so the orchestration is testable without a database;
// a nil set or a failing store degrades to "notes still render, nothing
// claimed". Returns the notes and the pending note the stamp ack must
// confirm ("" when nothing is pending).
func StageServedBy(
	sessionID string,
	s Snapshot,
	get func(string) string,
	set func(string, string) error,
) ([]string, string) {
	if get == nil {
		get = func(string) string { return "" }
	}
	stored := ""
	if ValidSessionID(sessionID) {
		stored = get(MetaKey(sessionID))
	}
	notes := TurnNotes(s, stored)
	// The note the stamp ack must confirm: the one just emitted, or — on a
	// suppressed repeat — the stored proof it matched (a no-op confirm).
	pending := StoredNote(stored)
	if note := RuntimeNote(s); note != pending {
		pending = note
	}
	if ValidSessionID(sessionID) && set != nil && RecordWriteNeeded(stored, s, StoredNote(stored)) {
		// Write failures never break the turn: the identity note already
		// rendered and no transition is claimed from a record we could not
		// re-establish.
		_ = set(MetaKey(sessionID), EncodeRecord(s, StoredNote(stored)))
	}
	return notes, pending
}

// ConfirmServedNote is the ack half of the note gate, the decision twin of
// gateway.serving_transition.confirm_served_note: advance the durable note
// proof once the note's api_content sidecar stamp landed. Write-on-change,
// keyed on the session's own record: an absent/corrupt record (the stage
// write could not be established) or an already-current proof is a silent
// no-op, and a nil/failing store degrades to false — never an error.
func ConfirmServedNote(
	sessionID, note string,
	get func(string) string,
	set func(string, string) error,
) bool {
	if !ValidSessionID(sessionID) || note == "" || get == nil || set == nil {
		return false
	}
	var record servedByRecord
	if err := json.Unmarshal([]byte(strings.TrimSpace(get(MetaKey(sessionID)))), &record); err != nil {
		return false
	}
	if record.Note == note {
		return false
	}
	if err := set(MetaKey(sessionID), EncodeRecord(Snapshot{
		PID: record.PID, StartTime: record.StartTime,
	}, note)); err != nil {
		return false
	}
	return true
}
