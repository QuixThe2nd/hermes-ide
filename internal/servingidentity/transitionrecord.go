package servingidentity

import (
	"encoding/json"
	"strconv"
	"strings"
)

// MetaKeyPrefix is the session-meta key namespace for the durable
// served-by record (gateway/serving_transition.py): one
// "served_by:<session_id>" key per session holding {"pid","start_time"}.
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

// servedByRecord is the persisted wire shape {"pid","start_time"} — the pid
// alone is not identity (PIDs get reused), so the start fingerprint is part
// of the record.
type servedByRecord struct {
	PID       int   `json:"pid"`
	StartTime int64 `json:"start_time"`
}

// ParseRecord decodes a stored served-by record. False for absent, corrupt,
// or non-dict records: the absence of a provable predecessor is "no
// transition", never an error.
func ParseRecord(raw string) (Snapshot, bool) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return Snapshot{}, false
	}
	var record servedByRecord
	if err := json.Unmarshal([]byte(raw), &record); err != nil {
		return Snapshot{}, false
	}
	if record.PID <= 0 {
		return Snapshot{}, false
	}
	return Snapshot{PID: record.PID, StartTime: record.StartTime}, true
}

// EncodeRecord renders the durable record for THIS process (never carries a
// build sha — records fingerprint process identity, not code identity).
func EncodeRecord(s Snapshot) string {
	encoded, err := json.Marshal(servedByRecord{PID: s.PID, StartTime: s.StartTime})
	if err != nil {
		return "{}"
	}
	return string(encoded)
}

// RecordWriteNeeded is the pure write-on-change predicate for the served-by
// record: false only when the stored record parses and already carries
// exactly this pid + start fingerprint (an ordinary same-process turn is one
// cheap point read, not a write). Corrupt or absent records are writes — the
// durable predecessor must be learnable. Byte-parity with
// gateway.serving_transition._record_write_needed via the shared table.
func RecordWriteNeeded(storedRaw string, current Snapshot) bool {
	stored, ok := ParseRecord(storedRaw)
	if !ok {
		return true
	}
	return stored.PID != current.PID || stored.StartTime != current.StartTime
}

// OwnerAction answers: may a restart be queued through this control-socket
// answerer, given the pid serving THIS conversation?
//
// "proceed" — the answerer is the serving process, or nothing is provable
// (no stamp / unparseable probe keeps pre-fix behavior rather than refusing
// old installs); "refuse" — a different process answered and cannot attest
// this conversation's server as its hub-ready successor (the field incident:
// the draining predecessor owned the socket while its successor served, and
// the queued restart waited forever); "attested-chain" — a different
// process answered but attests the serving process as its hub-ready
// successor: queueing is correct and the result must name the serving
// process. Parity twin of plugins.gateway_restart.socket_owner_action,
// pinned by the shared honesty table.
func OwnerAction(servingPID int, id *Identify) string {
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
	return "refuse"
}

// OwnerRefusal renders the restart-tool refusal when the socket's answerer
// is not the conversation's serving process: it names both pids, states why
// queueing there cannot work, and points at the safe alternatives. Parity
// twin of plugins.gateway_restart._socket_owner_guard's message, pinned by
// the shared honesty table (byte-identical wording).
func OwnerRefusal(servingPID int, id *Identify) string {
	ownerClause := "reports no restart-handoff state (its build predates overlap reporting)"
	if id != nil && id.Role == RoleDrainingPredecessor {
		ownerClause = "is draining for a restart whose successor is not confirmed as this " +
			"conversation's server"
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

// ChainRewrite rewrites a chained restart ack so its pid names the process
// that will actually restart: the draining predecessor's chained restart
// runs on its hub-attested successor — the serving process. The ack's pid
// names the predecessor (the answerer); rendering it as the process would
// point the agent at the dying process. Parity twin of
// plugins.gateway_restart._socket_chain_rewrites_serving_process, pinned by
// the shared honesty table.
func ChainRewrite(ack map[string]any, servingPID int, id *Identify) map[string]any {
	if servingPID <= 0 || ack == nil || ack["status"] != "chained" {
		return ack
	}
	if id == nil || id.PID <= 0 || id.PID == servingPID {
		return ack
	}
	rewritten := make(map[string]any, len(ack)+2)
	for key, value := range ack {
		rewritten[key] = value
	}
	rewritten["pid"] = servingPID
	rewritten["note"] =
		"Restart chained for the serving process: 'process' names pid " + itoa(servingPID) +
			", the process serving this conversation. The control socket is still owned " +
			"by the draining predecessor (pid " + itoa(id.PID) + "); the chained restart runs " +
			"on pid " + itoa(servingPID) + " once that handoff completes."
	return rewritten
}

// ApplyServedBy is the decision twin of
// gateway.serving_transition.session_turn_notes: read the session's
// served-by record, render the turn notes against THIS process, then
// record this process on change only. Persistence is injected (get/set),
// so the orchestration is testable without a database; a nil set or a
// failing store degrades to "notes still render, nothing claimed".
// Read-before-write ordering matters: the transition note must describe
// the state BEFORE this turn's record lands.
func ApplyServedBy(
	sessionID string,
	s Snapshot,
	get func(string) string,
	set func(string, string) error,
) []string {
	if get == nil {
		get = func(string) string { return "" }
	}
	stored := ""
	if ValidSessionID(sessionID) {
		stored = get(MetaKey(sessionID))
	}
	notes := TurnNotes(s, stored)
	if ValidSessionID(sessionID) && set != nil && RecordWriteNeeded(stored, s) {
		// Write failures never break the turn: the identity note already
		// rendered and no transition is claimed from a record we could not
		// re-establish.
		_ = set(MetaKey(sessionID), EncodeRecord(s))
	}
	return notes
}
