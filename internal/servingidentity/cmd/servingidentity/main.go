// Command servingidentity is the executable form of the canonical Go twin:
// it reads one request as JSON on stdin and writes the rendered result to
// stdout.
//
// This is the bridge the Python tree executes: the contract test in
// tests/gateway/test_serving_identity_contract.py pipes every shared-table
// record (testdata/serving_cases.json) through this binary and asserts
// gateway/serving_identity.py renders the identical output, so the Python
// module — the one the runtime renders with — is pinned against the RUNNING
// Go implementation, not merely against the table. Production gateways do
// not shell out to it: deployments (pip, docker) carry no Go toolchain, the
// same reason every other internal/* twin is executed by tests rather than
// at runtime.
//
// Request shapes (one JSON object, "op" selects):
//
//	{"op": "note", "identity": {"pid": 8412, "start_time": 2007821,
//	 "code_sha": "abcd…"}, "restart_requested": false,
//	 "overlap_successor_pending": false, "predecessor_pid": 0,
//	 "successor_pid": 0, "successor_start_time": 0, "successor_ready": false}
//	  → prints the runtime note line.
//
//	{"op": "status", "identify": {"pid": …, "start_time": …,
//	 "overlap": {"role": …, …}} | null, "state": {"pid": …,
//	 "gateway_state": "…", "pid_is_live": bool, "is_stale": bool,
//	 "restart_requested": bool} | null,
//	 "successor_identity": "live" | "dead" | "unknown" | null}
//	  → prints the status lines as a JSON array.
//
//	{"op": "owner_guard" | "owner_refusal", "serving_pid": …,
//	 "identify": {"pid": …, "overlap": {"role": …, …}} | null,
//	 "registry_rows": [{pid, predecessor_pid}, …] | null}
//	  → prints the owner-action verdict / the refusal message; registry_rows
//	    is the live generation-registry evidence (spawn-ordered, last row =
//	    the newest generation).
//
//	{"op": "ack_rewrite", "ack": {…socket ack…}, "serving_pid": …,
//	 "identify": {…} | null, "registry_rows": […]}
//	  → prints the rewritten ack as JSON, with pid naming the process that
//	    will actually restart.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/servingidentity"
)

func main() {
	var request map[string]any
	if err := json.NewDecoder(os.Stdin).Decode(&request); err != nil {
		fmt.Fprintf(os.Stderr, "servingidentity: unreadable request: %v\n", err)
		os.Exit(2)
	}
	switch op, _ := request["op"].(string); op {
	case "note":
		fmt.Println(servingidentity.RuntimeNote(snapshotFromRequest(request)))
	case "transition":
		fmt.Println(servingidentity.TransitionNote(
			snapshotFromRecord(request["previous"]),
			snapshotFromIdentityRecord(request)))
	case "record":
		fmt.Println(recordVerdictFromRequest(request))
	case "owner_guard":
		fmt.Println(servingidentity.OwnerAction(
			intValue(request["serving_pid"]),
			identifyFromRecord(request["identify"]),
			registryRowsFromRequest(request["registry_rows"])))
	case "owner_refusal":
		fmt.Println(servingidentity.OwnerRefusal(
			intValue(request["serving_pid"]),
			identifyFromRecord(request["identify"])))
	case "ack_rewrite":
		rewritten := servingidentity.AckRewrite(
			objectFromRequest(request["ack"]),
			intValue(request["serving_pid"]),
			identifyFromRecord(request["identify"]),
			registryRowsFromRequest(request["registry_rows"]))
		encoded, err := json.Marshal(rewritten)
		if err != nil {
			fmt.Fprintf(os.Stderr, "servingidentity: %v\n", err)
			os.Exit(2)
		}
		fmt.Println(string(encoded))
	case "stamp":
		snapshot, ok := servingidentity.SessionStamp(
			stringValue(request["pid"]), stringValue(request["start_time"]))
		if !ok {
			fmt.Println("none")
			return
		}
		fmt.Println(snapshot.PID, snapshot.StartTime)
	case "turn_notes":
		encoded, err := json.Marshal(servingidentity.TurnNotes(
			snapshotFromIdentityRecord(request), stringValue(request["stored"])))
		if err != nil {
			fmt.Fprintf(os.Stderr, "servingidentity: %v\n", err)
			os.Exit(2)
		}
		fmt.Println(string(encoded))
	case "status":
		encoded, err := json.Marshal(statusFromRequest(request))
		if err != nil {
			fmt.Fprintf(os.Stderr, "servingidentity: %v\n", err)
			os.Exit(2)
		}
		fmt.Println(string(encoded))
	default:
		fmt.Fprintf(os.Stderr, "servingidentity: unknown op: %q\n", op)
		os.Exit(2)
	}
}

// snapshotFromRecord maps a persisted served-by record {pid, start_time}
// onto Snapshot (records carry no sha; identity snapshots do).
func snapshotFromRecord(raw any) servingidentity.Snapshot {
	record, _ := raw.(map[string]any)
	return servingidentity.Snapshot{
		PID:       intValue(record["pid"]),
		StartTime: int64Value(record["start_time"]),
	}
}

// snapshotFromIdentityRecord maps the current identity record
// {pid, start_time, code_sha} onto Snapshot for the transition op.
func snapshotFromIdentityRecord(request map[string]any) servingidentity.Snapshot {
	snapshot := snapshotFromRecord(request["identity"])
	if identity, ok := request["identity"].(map[string]any); ok {
		snapshot.BuildSHA = stringValue(identity["code_sha"])
	}
	return snapshot
}

// recordVerdictFromRequest renders the write-on-change verdict ("write" |
// "skip") for a session's served-by record: an invalid session id or a
// stored record already matching this process means no write.
func recordVerdictFromRequest(request map[string]any) string {
	sessionID := stringValue(request["session_id"])
	if !servingidentity.ValidSessionID(sessionID) {
		return "skip"
	}
	current := snapshotFromRecord(request["record"])
	stored := stringValue(request["stored"])
	if servingidentity.RecordWriteNeeded(stored, current) {
		return "write"
	}
	return "skip"
}

// identifyFromRecord maps a table identify answer {pid, overlap{...}}
// onto Identify for the owner_guard op.
func identifyFromRecord(raw any) *servingidentity.Identify {
	record, ok := raw.(map[string]any)
	if !ok || intValue(record["pid"]) <= 0 {
		return nil
	}
	overlap, _ := record["overlap"].(map[string]any)
	return &servingidentity.Identify{
		PID:            intValue(record["pid"]),
		Role:           servingidentity.Role(stringValue(overlap["role"])),
		SuccessorPID:   intValue(overlap["successor_pid"]),
		SuccessorReady: boolValue(overlap["successor_ready"]),
	}
}

// registryRowsFromRequest maps the optional registry_rows array (live
// generation-registry evidence) onto []RegistryRow for the owner ops.
// Rows are spawn-ordered; the table's last row is the newest generation.
func registryRowsFromRequest(raw any) []servingidentity.RegistryRow {
	items, ok := raw.([]any)
	if !ok {
		return nil
	}
	rows := make([]servingidentity.RegistryRow, 0, len(items))
	for _, item := range items {
		record, ok := item.(map[string]any)
		if !ok {
			continue
		}
		rows = append(rows, servingidentity.RegistryRow{
			PID:            intValue(record["pid"]),
			PredecessorPID: intValue(record["predecessor_pid"]),
		})
	}
	return rows
}

// objectFromRequest maps an optional JSON object (the ack) onto
// map[string]any; null/absent decodes to a nil map.
func objectFromRequest(raw any) map[string]any {
	object, _ := raw.(map[string]any)
	return object
}

func snapshotFromRequest(request map[string]any) servingidentity.Snapshot {
	identity, _ := request["identity"].(map[string]any)
	overlap, _ := request["overlap"].(map[string]any)
	return servingidentity.Snapshot{
		PID:                     intValue(identity["pid"]),
		StartTime:               int64Value(identity["start_time"]),
		BuildSHA:                stringValue(identity["code_sha"]),
		Draining:                boolValue(request["draining"]),
		PredecessorPID:          orInt(intValue(request["predecessor_pid"]), intValue(overlap["predecessor_pid"])),
		SuccessorPID:            orInt(intValue(request["successor_pid"]), intValue(overlap["successor_pid"])),
		SuccessorStartTime:      int64Value(orAny(request["successor_start_time"], overlap["successor_start_time"])),
		SuccessorReady:          boolValue(orAny(request["successor_ready"], overlap["successor_ready"])),
		RestartRequested:        boolValue(orAny(request["restart_requested"], overlap["restart_requested"])),
		OverlapSuccessorPending: boolValue(orAny(request["overlap_successor_pending"], overlap["overlap_successor_pending"])),
	}
}

func statusFromRequest(request map[string]any) []string {
	var identify *servingidentity.Identify
	if raw, ok := request["identify"].(map[string]any); ok && intValue(raw["pid"]) > 0 {
		overlap, _ := raw["overlap"].(map[string]any)
		identify = &servingidentity.Identify{
			PID:                intValue(raw["pid"]),
			StartTime:          int64Value(raw["start_time"]),
			Role:               servingidentity.Role(stringValue(overlap["role"])),
			PredecessorPID:     intValue(overlap["predecessor_pid"]),
			SuccessorPID:       intValue(overlap["successor_pid"]),
			SuccessorStartTime: int64Value(overlap["successor_start_time"]),
			SuccessorReady:     boolValue(overlap["successor_ready"]),
		}
	}

	var state *servingidentity.StateFile
	if raw, ok := request["state"].(map[string]any); ok && intValue(raw["pid"]) > 0 {
		state = &servingidentity.StateFile{
			PID:              intValue(raw["pid"]),
			GatewayState:     stringValue(raw["gateway_state"]),
			PIDIsLive:        boolValue(raw["pid_is_live"]),
			IsStale:          boolValue(raw["is_stale"]),
			RestartRequested: boolValue(raw["restart_requested"]),
		}
	}
	var probe servingidentity.SuccessorIdentity
	if v, ok := request["successor_identity"].(string); ok && v != "" {
		probe = func(int, int64) string { return v }
	}
	return servingidentity.StatusLines(identify, state, probe, pidsFromRequest(request["gateway_pids"]))
}

// pidsFromRequest maps the optional gateway_pids array (live process scan)
// onto []int.
func pidsFromRequest(raw any) []int {
	items, _ := raw.([]any)
	pids := make([]int, 0, len(items))
	for _, item := range items {
		if pid := intValue(item); pid > 0 {
			pids = append(pids, pid)
		}
	}
	return pids
}

func orAny(primary, fallback any) any {
	if primary != nil {
		return primary
	}
	return fallback
}

func orInt(primary, fallback int) int {
	if primary != 0 {
		return primary
	}
	return fallback
}

func stringValue(v any) string {
	s, _ := v.(string)
	return s
}

func intValue(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case int64:
		return int(n)
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	}
	return 0
}

func int64Value(v any) int64 {
	switch n := v.(type) {
	case float64:
		return int64(n)
	case int:
		return int64(n)
	case int64:
		return n
	case json.Number:
		i, _ := n.Int64()
		return i
	}
	return 0
}

func boolValue(v any) bool {
	b, _ := v.(bool)
	return b
}
