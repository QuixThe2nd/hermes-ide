// Package servingidentity is the Go twin of the gateway serving-process
// identity contract in gateway/serving_identity.py: the role classification
// of the current gateway process during an overlap restart, the per-turn
// runtime note that names the process actually serving the conversation, and
// the status-surface lines that separate the authoritative serving gateway
// from predecessor/supervisor evidence. Same rules, independently tested.
// Production Python never imports or shells out to it; it exists so the
// identity contract has a second, line-balance-accounted implementation.
//
// Contract (shared with the Python twin):
//   - Roles, exactly: "serving", "overlap-successor",
//     "draining-predecessor".
//   - Classification reads restart-handoff state only: a restart-requested
//     process is a draining predecessor (its spawned successor takes new
//     turns); a process booted pending behind a live predecessor is the
//     overlap successor serving now; anything else — including a pure
//     shutdown/pause drain without a restart request — is serving.
//   - The runtime note ALWAYS names the serving pid and states that pids
//     reported by systemctl, the pid file, or status files are not the
//     conversation's process; a knowable transition appends the
//     predecessor/successor explanation. A draining predecessor without a
//     spawned successor says "a successor is being started", never a pid it
//     cannot prove.
//   - Status lines resolve the serving gateway by ladder: a live identify
//     answer is authoritative (a draining-predecessor answer points at its
//     reported successor, with a liveness warning when that successor is not
//     running); then a live, fresh gateway_state.json record names the pid by
//     source; otherwise "unknown" with the reason — never a serving claim
//     that cannot be proven. A pending chained-restart marker adds its line.
package servingidentity

import (
	"strconv"
	"strings"
)

// Role classifies the current process's position in a restart handoff.
// Values are wire-visible (control-socket identify "overlap.role").
type Role string

const (
	RoleServing             Role = "serving"
	RoleOverlapSuccessor    Role = "overlap-successor"
	RoleDrainingPredecessor Role = "draining-predecessor"
)

// Snapshot mirrors gateway.serving_identity.serving_identity_snapshot input:
// static process identity plus the runner's restart-handoff state. StartTime
// is the opaque per-process start fingerprint (jiffies or centiseconds —
// units differ per platform; only same-host equality is meaningful).
type Snapshot struct {
	PID       int
	StartTime int64
	BuildSHA  string

	RestartRequested        bool
	Draining                bool
	OverlapSuccessorPending bool
	PredecessorPID          int
	SuccessorPID            int
}

// Classify returns the snapshot's role.
func Classify(s Snapshot) Role {
	switch {
	case s.RestartRequested:
		return RoleDrainingPredecessor
	case s.OverlapSuccessorPending:
		return RoleOverlapSuccessor
	default:
		return RoleServing
	}
}

func buildClause(pid int, startTime int64, buildSHA string) string {
	rendered := "pid " + strconv.Itoa(pid)
	if startTime != 0 {
		rendered += ", start " + strconv.FormatInt(startTime, 10)
	}
	if buildSHA != "" {
		sha := buildSHA
		if len(sha) > 8 {
			sha = sha[:8]
		}
		rendered += ", build " + sha
	}
	return rendered
}

// RuntimeNote renders the per-turn runtime note for the snapshot. Wording is
// byte-identical to gateway.serving_identity.serving_runtime_note.
func RuntimeNote(s Snapshot) string {
	identity := buildClause(s.PID, s.StartTime, s.BuildSHA)
	switch Classify(s) {
	case RoleOverlapSuccessor:
		if s.PredecessorPID > 0 {
			p := strconv.Itoa(s.PredecessorPID)
			return "[System note: This turn is served by Hermes gateway " + identity +
				". It is the restart successor: predecessor pid " + p +
				" is draining its final in-flight turns, so systemctl, the pid file and gateway_state.json may still report pid " +
				p + " until it exits. That drain is the expected handoff — the restart already succeeded; do not wait for the predecessor or treat its drain as an incomplete restart.]"
		}
	case RoleDrainingPredecessor:
		successor := "a successor is being started and will take new turns"
		if s.SuccessorPID > 0 {
			successor = "successor pid " + strconv.Itoa(s.SuccessorPID) + " already takes new turns"
		}
		return "[System note: This turn is served by Hermes gateway " + identity +
			". This process is draining for a queued restart; " + successor +
			". Finish this turn's work and do not call restart again for the same restart.]"
	}
	return "[System note: This turn is served by Hermes gateway " + identity +
		". That pid — not any pid reported by systemctl, the pid file, or gateway status files — is the process running this conversation.]"
}

// Identify is one control-socket identify answer as consumed by the status
// surface: the answering process plus its overlap enrichment.
type Identify struct {
	PID            int
	StartTime      int64
	Role           Role
	PredecessorPID int
	SuccessorPID   int
}

// StateFile is the gateway_state.json fallback input, pre-judged: PIDIsLive
// and IsStale come from the caller's liveness/staleness helpers.
type StateFile struct {
	PID          int
	GatewayState string
	PIDIsLive    bool
	IsStale      bool
}

// StatusLines renders the serving-gateway status block. pidLive may be nil
// (no probe available: a reported successor is trusted as reported).
// chainedSource is empty when no chained-restart marker exists.
// Wording is byte-identical to gateway.serving_identity.serving_status_lines.
func StatusLines(id *Identify, st *StateFile, chainedSource string, pidLive func(int) bool) []string {
	var lines []string
	switch {
	case id != nil && id.PID > 0:
		answering := buildClause(id.PID, id.StartTime, "")
		switch {
		case id.Role == RoleDrainingPredecessor && id.SuccessorPID > 0:
			if pidLive == nil || pidLive(id.SuccessorPID) {
				lines = append(lines,
					"✓ Serving gateway: pid "+strconv.Itoa(id.SuccessorPID)+" — restart successor; new turns are served here",
					"  Draining predecessor: pid "+strconv.Itoa(id.PID)+" — finishing its in-flight turns; "+
						"systemctl, the pid file and gateway_state.json still show pid "+strconv.Itoa(id.PID)+
						" until it exits (expected handoff, not an incomplete restart)")
			} else {
				lines = append(lines,
					"⚠ Serving gateway: unknown — predecessor pid "+strconv.Itoa(id.PID)+" is draining, but its "+
						"reported successor pid "+strconv.Itoa(id.SuccessorPID)+" is not running; the restart may have failed, "+
						"check the gateway log before restarting anything")
			}
		case id.Role == RoleOverlapSuccessor:
			lines = append(lines, "✓ Serving gateway: "+answering+" — restart successor, now authoritative")
		default:
			lines = append(lines, "✓ Serving gateway: "+answering+" — answering on the gateway control socket")
		}
	case st != nil && st.PID > 0:
		switch {
		case !st.IsStale && st.PIDIsLive:
			lines = append(lines, "✓ Serving gateway: pid "+strconv.Itoa(st.PID)+
				" (from gateway_state.json, state '"+st.GatewayState+"')")
		case st.IsStale:
			lines = append(lines, "⚠ Serving gateway: unknown — control socket not answering and gateway_state.json "+
				"is stale (state '"+st.GatewayState+"')")
		default:
			lines = append(lines, "⚠ Serving gateway: unknown — control socket not answering and gateway_state.json "+
				"pid "+strconv.Itoa(st.PID)+" is not running")
		}
	default:
		lines = append(lines, "⚠ Serving gateway: unknown — no control socket answer and no gateway_state.json record")
	}
	if chainedSource != "" {
		lines = append(lines, "⏳ Chained restart queued: another restart will run once the current handoff "+
			"completes (requested by "+chainedSource+")")
	}
	return lines
}

// OverlapFields renders the identify "overlap" merge for a snapshot: the
// role always, predecessor/successor pids only when known (> 0). Key order
// is irrelevant (wire JSON), but the present-key set must match the Python
// twin's overlap_fields.
func OverlapFields(s Snapshot) map[string]any {
	fields := map[string]any{"role": string(Classify(s))}
	if s.PredecessorPID > 0 {
		fields["predecessor_pid"] = s.PredecessorPID
	}
	if s.SuccessorPID > 0 {
		fields["successor_pid"] = s.SuccessorPID
	}
	return fields
}

// ChainedSource renders the status-source summary for a peeked
// .restart_chain.json payload: the recorded source when present, else the
// honest fallback. Mirrors the Python twin's chained-line source handling.
func ChainedSource(source string) string {
	source = strings.TrimSpace(source)
	if source == "" {
		return "an earlier restart request"
	}
	return source
}

// ---------------------------------------------------------------------------
// Restart-result identity: the queued payload and the completion notice.
// Mirrors plugins/gateway_restart/tool.py (_result_json/_socket_result_json,
// _QUEUED_NOTE) and gateway/run_turn_recovery.py
// (_restart_success_content/_parse_restart_success_content), which share
// their constants with agent/turn_control.py.
// ---------------------------------------------------------------------------

// Restart notice lines (agent/turn_control.py).
const (
	NoticeFirstLine     = "Restarted successfully"
	OldProcessPrefix    = "Old process: "
	NewProcessPrefix    = "New process: "
	TurnControlField    = "_hermes_turn_control"
	GatewayRestartValue = "gateway_restart"
)

// QueuedNote is the self-describing queue state every committed restart
// reply carries (plugins/gateway_restart/tool.py::_QUEUED_NOTE).
const QueuedNote = "Restart queued, not completed: 'process' names the OLD gateway. " +
	"The successor process rewrites this result with the " +
	"'Restarted successfully' completion notice and Old/New process " +
	"identity."

// TerminalRestartStatuses are the queue-committing statuses that stamp the
// turn-control field and end the requesting turn (agent/turn_control.py).
var TerminalRestartStatuses = map[string]bool{
	"restarting":          true,
	"already_in_progress": true,
	"chained":             true,
}

// TurnControlFieldFor returns the reserved control-field stamp for a
// committed restart status, or nil.
func TurnControlFieldFor(status string) map[string]string {
	if TerminalRestartStatuses[status] {
		return map[string]string{TurnControlField: GatewayRestartValue}
	}
	return nil
}

// QueuedResult assembles the committed-restart reply payload (the
// `_socket_result_json` field set). Process names the OLD gateway; the
// reply says it is queued, never completed.
func QueuedResult(status string, activeAgents int, viaService any, process string) map[string]any {
	payload := map[string]any{
		"success":       true,
		"status":        status,
		"active_agents": activeAgents,
		"via_service":   viaService,
		"queued":        true,
		"note":          QueuedNote,
	}
	if process != "" {
		payload["process"] = process
	}
	if stamp := TurnControlFieldFor(status); stamp != nil {
		for k, v := range stamp {
			payload[k] = v
		}
	}
	return payload
}

// RestartSuccessContent builds the exact completion notice the successor
// rewrites the restart tool result with (gateway/run_turn_recovery.py).
// oldProcess may be empty (legacy results without the old identity);
// hubLine is the optional opt-in "Hub: " report line.
func RestartSuccessContent(oldProcess, newProcess, hubLine string) string {
	lines := []string{NoticeFirstLine}
	if oldProcess != "" {
		lines = append(lines, OldProcessPrefix+oldProcess)
	}
	lines = append(lines, NewProcessPrefix+newProcess)
	if oldProcess != "" {
		lines = append(lines,
			"This is a tool result for you, not a message to send or quote "+
				"to the user. This chat is no longer running in "+oldProcess+"; "+
				"you are now running in "+newProcess+". Continue the interrupted "+
				"task with your tools until the original work is verified, then "+
				"reply to the user about that work.")
	} else {
		lines = append(lines,
			"This is a tool result for you, not a message to send or quote "+
				"to the user. You are now running in "+newProcess+". Continue "+
				"the interrupted task with your tools until the original work is "+
				"verified, then reply to the user about that work.")
	}
	if hubLine != "" {
		lines = append(lines, "Hub: "+hubLine)
	}
	return strings.Join(lines, "\n")
}

// ParseRestartSuccessContent accepts ONLY the exact builder output and
// recovers the old identity (empty when the notice lacked it). Builder and
// parser stay in lockstep so a reworded instruction cannot be trusted
// half-way through a handoff.
func ParseRestartSuccessContent(content string) (oldProcess, newProcess string, ok bool) {
	lines := strings.Split(content, "\n")
	hubLine := ""
	if len(lines) >= 4 && strings.HasPrefix(lines[len(lines)-1], "Hub: ") {
		hubLine = strings.TrimSpace(lines[len(lines)-1][len("Hub: "):])
		if hubLine == "" {
			return "", "", false
		}
		lines = lines[:len(lines)-1]
	}
	if len(lines) == 0 || lines[0] != NoticeFirstLine {
		return "", "", false
	}
	newLine := ""
	switch len(lines) {
	case 4:
		if !strings.HasPrefix(lines[1], OldProcessPrefix) {
			return "", "", false
		}
		oldProcess = strings.TrimSpace(lines[1][len(OldProcessPrefix):])
		newLine = lines[2]
	case 3:
		newLine = lines[1]
	default:
		return "", "", false
	}
	if !strings.HasPrefix(newLine, NewProcessPrefix) {
		return "", "", false
	}
	newProcess = strings.TrimSpace(newLine[len(NewProcessPrefix):])
	if newProcess == "" || (len(lines) == 4 && oldProcess == "") {
		return "", "", false
	}
	if content != RestartSuccessContent(oldProcess, newProcess, hubLine) {
		return "", "", false
	}
	return oldProcess, newProcess, true
}
