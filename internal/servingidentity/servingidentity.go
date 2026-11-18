// Package servingidentity is the Go twin of the gateway serving-process
// identity contract in gateway/serving_identity.py: the role classification
// of the current gateway process during an overlap restart, the per-turn
// runtime note that names the process actually serving the conversation, and
// the status-surface lines that separate the authoritative serving gateway
// from predecessor/supervisor evidence.
//
// This is the canonical statement of the contract; the Python module is what
// the runtime renders with (executed by tests, not imported at runtime —
// deployments (pip, docker) carry no Go toolchain, the same arrangement as
// every other internal/* twin). cmd/servingidentity is the executable form:
// the Python contract test (tests/gateway/test_serving_identity_contract.py)
// builds it and asserts byte-identical rendering for every shared-table
// record in testdata/serving_cases.json, and the Go tests run the same table
// as a second cross-check.
//
// Contract (shared with the Python twin):
//   - Roles, exactly: "serving", "overlap-successor",
//     "draining-predecessor", "unknown".
//   - Classification reads restart-handoff state only: a restart-requested
//     process is a draining predecessor (its spawned successor takes new
//     turns); a process booted pending behind a live predecessor is the
//     overlap successor serving now; a pure shutdown/pause drain without a
//     restart request is serving. "unknown" appears on the wire when
//     classification failed — no role claim may be invented from an error.
//   - The runtime note ALWAYS names the serving pid and states that pids
//     reported by systemctl, the pid file, or status files are not the
//     conversation's process. A draining predecessor may claim its successor
//     "already took over new turns" ONLY on the hub-attested readiness ack
//     that gates real event handoff — never a bare spawned PID (spawn is not
//     readiness, and PIDs get reused); without the ack the successor is
//     "starting", and with no successor at all the note never names a pid it
//     cannot prove.
//   - Status lines resolve the serving gateway by ladder: a live identify
//     answer is authoritative — EXCEPT a draining-predecessor answer, which
//     names the serving process only when its reported successor carries the
//     readiness ack AND passes the start-fingerprint check (PID reuse →
//     dead); a spawned-but-unattested successor is "starting", never a green
//     serving claim. Without a socket answer, a live fresh
//     gateway_state.json record names the pid — unless the record itself is
//     draining (gateway_state "draining" or restart_requested), in which
//     case serving identity is unknown. Otherwise "unknown" with the reason
//     — never a serving claim that cannot be proven.
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
	RoleUnknown             Role = "unknown"
)

// Snapshot mirrors gateway.serving_identity.serving_identity_snapshot: static
// process identity plus the runner's restart-handoff state. StartTime is the
// opaque per-process start fingerprint (jiffies or centiseconds — units
// differ per platform; only same-host equality is meaningful).
// SuccessorReady is the hub-attested readiness ack
// (gateway.overlap_restart.overlap_successor_readiness) — the same signal
// that gates real event handoff; a bare SuccessorPID never implies it.
type Snapshot struct {
	PID       int
	StartTime int64
	BuildSHA  string

	RestartRequested        bool
	Draining                bool
	OverlapSuccessorPending bool
	PredecessorPID          int
	SuccessorPID            int
	SuccessorStartTime      int64
	SuccessorReady          bool
}

// Classify returns the snapshot's role. A pure drain without a restart
// request is NOT a restart handoff and stays serving.
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
// byte-identical to gateway.serving_identity.serving_runtime_note; an
// unclassifiable snapshot (RoleUnknown on the wire) renders the generic
// serving note — it still names its own pid and claims nothing else.
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
		successor := "new turns will be served by the replacement process once this one exits"
		if s.SuccessorReady && s.SuccessorPID > 0 {
			successor = "its restart successor (pid " + strconv.Itoa(s.SuccessorPID) +
				") has already taken over new turns"
		} else if s.SuccessorPID > 0 {
			successor = "a successor process (pid " + strconv.Itoa(s.SuccessorPID) +
				") is starting and will take over new turns once its hub attach is confirmed"
		}
		return "[System note: This turn is served by Hermes gateway " + identity +
			". This process is draining for a queued restart; " + successor +
			". Finish this turn's work and do not call restart again for the same restart.]"
	}
	return "[System note: This turn is served by Hermes gateway " + identity +
		". That pid — not any pid reported by systemctl, the pid file, or gateway status files — is the process running this conversation.]"
}

// startClause renders "pid N" plus ", start F" when the fingerprint is
// known — the persisted-record form (records carry no build sha).
func startClause(pid int, startTime int64) string {
	rendered := "pid " + strconv.Itoa(pid)
	if startTime != 0 {
		rendered += ", start " + strconv.FormatInt(startTime, 10)
	}
	return rendered
}

// TransitionState compares a persisted last-served identity (the session
// DB's served_by:<session> record, gateway/serving_transition.py) against
// the current process: "changed" (provably a different process — pid
// inequality, or equal pids with differing start fingerprints, since PIDs
// get reused), "same" (identical pid AND fingerprint), or "unknown" (no
// usable record, or the fingerprints needed to compare are missing: claim
// nothing rather than guess).
func TransitionState(prev, cur Snapshot) string {
	if prev.PID <= 0 || cur.PID <= 0 {
		return "unknown"
	}
	if prev.PID != cur.PID {
		return "changed"
	}
	if prev.StartTime <= 0 || cur.StartTime <= 0 {
		return "unknown"
	}
	if prev.StartTime == cur.StartTime {
		return "same"
	}
	return "changed"
}

// TransitionNote renders the first-turn-after-handoff note separating
// carried-over context from process-local state; empty unless
// TransitionState reports "changed" — never on a session's first-ever turn,
// ordinary same-process turns, or unprovable comparisons. Wording is
// byte-identical to gateway.serving_identity.serving_transition_note: it
// names both identities, states the transition is not a new task (persisted
// conversation/task context stays valid), and demands revalidation of
// process-local state (terminals, background processes, kernels, sockets,
// caches, tool handles, recorded pids).
func TransitionNote(prev, cur Snapshot) string {
	if TransitionState(prev, cur) != "changed" {
		return ""
	}
	return "[System note: This conversation moved to a new Hermes gateway process: " +
		buildClause(cur.PID, cur.StartTime, cur.BuildSHA) + " replaced " +
		startClause(prev.PID, prev.StartTime) + ". That was a process transition, " +
		"not a new task: the persisted conversation history and task context " +
		"carried over and stay valid. Process-local state did NOT carry over — " +
		"terminal sessions, background processes, kernels, sockets, in-memory " +
		"caches, tool handles, and any pid you recorded belonged to the old " +
		"process: revalidate them (re-open, re-list, re-check) before relying " +
		"on them again.]"
}

// SessionStamp parses the session's serving-stamp env pair
// (HERMES_SESSION_GATEWAY_PID / HERMES_SESSION_GATEWAY_START) as the
// restart tool and turn wiring bind them. False when either half is
// missing or non-positive: no provable stamp means today's behavior is
// kept, never a refusal based on a guess.
func SessionStamp(pidRaw, startRaw string) (Snapshot, bool) {
	pid, err := strconv.Atoi(strings.TrimSpace(pidRaw))
	if err != nil || pid <= 0 {
		return Snapshot{}, false
	}
	start, err := strconv.ParseInt(strings.TrimSpace(startRaw), 10, 64)
	if err != nil || start <= 0 {
		return Snapshot{}, false
	}
	return Snapshot{PID: pid, StartTime: start}, true
}

// TurnNotes composes the per-turn note pair from a runtime snapshot and the
// session's DURABLE served-by record: the runtime identity note always, the
// transition note only when the record proves a different process served
// the previous turns. Parity twin of
// gateway.serving_identity.serving_turn_notes composition with
// gateway.serving_transition.session_turn_notes' read step; the record's
// write step is ApplyServedBy.
func TurnNotes(s Snapshot, storedRecord string) []string {
	var notes []string
	if note := RuntimeNote(s); note != "" {
		notes = append(notes, note)
	}
	if previous, ok := ParseRecord(storedRecord); ok {
		if note := TransitionNote(previous, s); note != "" {
			notes = append(notes, note)
		}
	}
	return notes
}

// Identify is one control-socket identify answer as consumed by the status
// surface: the answering process plus its overlap enrichment.
type Identify struct {
	PID                int
	StartTime          int64
	Role               Role
	PredecessorPID     int
	SuccessorPID       int
	SuccessorStartTime int64
	SuccessorReady     bool
}

// StateFile is the gateway_state.json fallback input, pre-judged: PIDIsLive,
// IsStale and RestartRequested come from the caller's helpers. A live, fresh
// record that is itself draining never labels its pid as the serving
// gateway: status files keep naming the predecessor until the handoff
// completes.
type StateFile struct {
	PID              int
	GatewayState     string
	PIDIsLive        bool
	IsStale          bool
	RestartRequested bool
}

// SuccessorIdentity resolves a reported successor pid against its expected
// start fingerprint: "live", "dead" (not running, or a reused PID whose
// fingerprint differs) or "unknown" (no probe / no fingerprint). A nil
// resolver means no probe is available.
type SuccessorIdentity func(pid int, expectedStart int64) string

// statePIDFor extracts the state-file pid (0 when absent) for candidate
// collection.
func statePIDFor(st *StateFile) int {
	if st == nil {
		return 0
	}
	return st.PID
}

// candidatePIDs collects positive pids from the passed values, de-duped in
// first-seen order — the multi-process evidence set for ambiguity clauses.
func candidatePIDs(values ...int) []int {
	var pids []int
	for _, pid := range values {
		if pid <= 0 {
			continue
		}
		seen := false
		for _, existing := range pids {
			if existing == pid {
				seen = true
				break
			}
		}
		if !seen {
			pids = append(pids, pid)
		}
	}
	return pids
}

// pidList renders "pid A, pid B" for an ambiguity clause.
func pidList(pids []int) string {
	rendered := make([]string, 0, len(pids))
	for _, pid := range pids {
		rendered = append(rendered, "pid "+strconv.Itoa(pid))
	}
	return strings.Join(rendered, ", ")
}

// StatusLines renders the serving-gateway status block. Wording is
// byte-identical to gateway.serving_identity.serving_status_lines;
// chainedSource is empty when no chained-restart marker exists.
//
// gatewayPIDs is the live gateway process scan (hermes_cli.find_gateway_pids):
// EVIDENCE, never a claim. It never upgrades an answer — a serving claim
// requires a proven role — but it demotes unproven answers to ambiguous when
// more than one gateway process is live and the answer cannot say which one
// serves turns. A proven serving/overlap-successor/attested-successor answer
// stays green with multiple live pids: that overlap window is exactly when
// two gateway processes legitimately coexist. A legacy answerer reporting no
// overlap role is NEVER green: during overlap that answerer is the draining
// predecessor, and its serving role cannot be proven from here (the incident
// where status green-served the predecessor while the successor served turns).
func StatusLines(id *Identify, st *StateFile, chainedSource string, successorIdentity SuccessorIdentity, gatewayPIDs []int) []string {
	resolve := func(pid int, expectedStart int64) string {
		if successorIdentity == nil {
			if expectedStart == 0 {
				return "unknown"
			}
			return "live"
		}
		state := successorIdentity(pid, expectedStart)
		if state != "live" && state != "dead" {
			return "unknown"
		}
		return state
	}
	var lines []string
	switch {
	case id != nil && id.PID > 0:
		answering := buildClause(id.PID, id.StartTime, "")
		switch {
		case id.Role == RoleDrainingPredecessor:
			if id.SuccessorPID > 0 {
				state := resolve(id.SuccessorPID, id.SuccessorStartTime)
				switch {
				case state == "dead":
					lines = append(lines,
						"⚠ Serving gateway: unknown — predecessor pid "+strconv.Itoa(id.PID)+" is draining, "+
							"but its reported successor pid "+strconv.Itoa(id.SuccessorPID)+" is not "+
							"running or its PID was reused; the restart may have failed, check "+
							"the gateway log before restarting anything")
				case id.SuccessorReady && state == "live":
					lines = append(lines,
						"✓ Serving gateway: pid "+strconv.Itoa(id.SuccessorPID)+" — restart successor "+
							"(hub-confirmed serving); new turns are served here",
						"  Draining predecessor: pid "+strconv.Itoa(id.PID)+" — finishing its "+
							"in-flight turns; systemctl, the pid file and gateway_state.json "+
							"still show pid "+strconv.Itoa(id.PID)+" until it exits (expected "+
							"handoff, not an incomplete restart)")
				default:
					lines = append(lines,
						"⏳ Serving gateway: successor pid "+strconv.Itoa(id.SuccessorPID)+" is starting — "+
							"draining predecessor pid "+strconv.Itoa(id.PID)+" spawned it, but its "+
							"readiness is not yet confirmed; re-check in a moment")
				}
			} else {
				lines = append(lines,
					"⚠ Serving gateway: unknown — gateway pid "+strconv.Itoa(id.PID)+" is draining "+
						"for a restart with no successor recorded; its supervisor will revive or "+
						"respawn it — check the gateway log before restarting anything")
			}
		case id.Role == RoleOverlapSuccessor:
			predecessorClause := ""
			if id.PredecessorPID > 0 {
				predecessorClause = "; predecessor pid " + strconv.Itoa(id.PredecessorPID) +
					" is draining its in-flight turns"
			}
			lines = append(lines, "✓ Serving gateway: "+answering+
				" — restart successor, serving new turns"+predecessorClause)
		case id.Role == RoleServing:
			lines = append(lines, "✓ Serving gateway: "+answering+
				" — answering on the gateway control socket")
		case id.Role == "":
			// Legacy answerer (pre-overlap build): never green. Its serving
			// role is unprovable, and during an overlap window this answerer
			// is exactly the draining predecessor.
			legacyCandidates := candidatePIDs(append([]int{id.PID, statePIDFor(st)}, gatewayPIDs...)...)
			drainingState := st != nil && st.PID > 0 &&
				(st.GatewayState == "draining" || st.RestartRequested)
			switch {
			case len(legacyCandidates) > 1:
				lines = append(lines,
					"⚠ Serving gateway: ambiguous — the control socket is owned by pid "+
						strconv.Itoa(id.PID)+", which reports no restart-handoff state "+
						"(its build predates overlap reporting), while gateway processes "+
						pidList(legacyCandidates)+" are live; which one serves turns cannot "+
						"be proven from here — re-check once the handoff completes")
			case drainingState:
				lines = append(lines,
					"⚠ Serving gateway: unknown — the control socket is owned by pid "+
						strconv.Itoa(id.PID)+", which reports no restart-handoff state "+
						"(its build predates overlap reporting) while its own state record "+
						"says it is draining; the serving process cannot be proven from here")
			default:
				lines = append(lines,
					"⚠ Serving gateway: unknown — gateway "+answering+" answered without "+
						"restart-handoff state (its build predates overlap reporting); it is "+
						"the only gateway process found, but its serving role cannot be proven")
			}
		default:
			lines = append(lines, "⚠ Serving gateway: unknown — gateway "+answering+
				" answered, but its restart-handoff state could not be classified")
		}
	case st != nil && st.PID > 0:
		draining := st.GatewayState == "draining" || st.RestartRequested
		stateCandidates := candidatePIDs(append([]int{st.PID}, gatewayPIDs...)...)
		switch {
		case !st.IsStale && st.PIDIsLive && draining:
			drainingLine := "⚠ Serving gateway: unknown — gateway_state.json names pid " +
				strconv.Itoa(st.PID) + " draining for a restart (state '" + st.GatewayState +
				"'); status files keep naming the predecessor until the handoff completes"
			if len(stateCandidates) > 1 {
				drainingLine += "; live gateway processes: " + pidList(stateCandidates)
			}
			lines = append(lines, drainingLine)
		case !st.IsStale && st.PIDIsLive:
			if len(stateCandidates) > 1 {
				lines = append(lines,
					"⚠ Serving gateway: ambiguous — gateway_state.json names pid "+strconv.Itoa(st.PID)+
						" but "+pidList(stateCandidates)+" gateway processes are live; which one "+
						"serves turns cannot be proven from status files alone")
			} else {
				lines = append(lines, "✓ Serving gateway: pid "+strconv.Itoa(st.PID)+
					" (from gateway_state.json, state '"+st.GatewayState+"')")
			}
		case st.IsStale:
			lines = append(lines, "⚠ Serving gateway: unknown — control socket not answering and gateway_state.json "+
				"is stale (state '"+st.GatewayState+"')")
		default:
			lines = append(lines, "⚠ Serving gateway: unknown — control socket not answering and gateway_state.json "+
				"pid "+strconv.Itoa(st.PID)+" is not running")
		}
	default:
		if len(gatewayPIDs) > 1 {
			lines = append(lines,
				"⚠ Serving gateway: ambiguous — no control socket answer and no gateway_state.json record; "+
					"gateway processes "+pidList(gatewayPIDs)+" are live and which one serves turns "+
					"cannot be proven from here")
		} else {
			lines = append(lines, "⚠ Serving gateway: unknown — no control socket answer and no gateway_state.json record")
		}
	}
	if chainedSource != "" {
		lines = append(lines, "⏳ Chained restart queued: another restart will run once the current handoff "+
			"completes (requested by "+chainedSource+")")
	}
	return lines
}

// OverlapFields renders the identify "overlap" merge for a snapshot: the
// role always, predecessor/successor pids only when known (> 0), the
// successor start fingerprint alongside a successor pid, and the hub-attested
// readiness flag only when a successor is reported. Key order is irrelevant
// (wire JSON), but the present-key set must match the Python twin's
// overlap_fields.
func OverlapFields(s Snapshot) map[string]any {
	fields := map[string]any{"role": string(Classify(s))}
	if s.PredecessorPID > 0 {
		fields["predecessor_pid"] = s.PredecessorPID
	}
	if s.SuccessorPID > 0 {
		fields["successor_pid"] = s.SuccessorPID
		if s.SuccessorStartTime != 0 {
			fields["successor_start_time"] = s.SuccessorStartTime
		}
		fields["successor_ready"] = s.SuccessorReady
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
