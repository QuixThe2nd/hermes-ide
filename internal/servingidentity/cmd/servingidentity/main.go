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
//	 "restart_requested": bool} | null, "chained_source": "",
//	 "successor_identity": "live" | "dead" | "unknown" | null}
//	  → prints the status lines as a JSON array.
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
	chained := stringValue(request["chained_source"])
	if chained != "" {
		chained = servingidentity.ChainedSource(chained)
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
	return servingidentity.StatusLines(identify, state, chained, probe)
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
