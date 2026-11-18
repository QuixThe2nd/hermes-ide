// Command lanereach is the executable form of the canonical Go twin: it
// reads one request as JSON on stdin and writes one JSON response on
// stdout.
//
// This is the bridge the Python tree executes: the contract test in
// tests/tools/test_delegate_lane_reach_contract.py pipes every shared-table
// record (testdata/lane_cases.json) through this binary and asserts
// tools/delegate_lane_specs.py answers identically, so the Python module —
// the one the runtime gates lane tools with — is pinned against the RUNNING
// Go implementation, not merely against the table. Production does not
// shell out to it: lane check_fns are uncached boot probes (a per-probe
// subprocess, or a cold go build, would stall gateway start), and
// deployments (pip, docker) carry no Go toolchain — the same reason every
// other internal/* twin is executed by tests rather than at runtime.
//
// Request shapes ("op" selects; "resolve" is the default):
//
//	{"op": "reachable", "harness": "claude", "omp_binary": "omp"}
//	  → {"reachable": false, "harness": "claude"}
//
//	{"op": "resolve", "harness": "omp", "omp_binary": "omp"}
//	  → {"reachable": true, "harness": "subagent"}
//
// With no "pin", the two external probes read the real host (PATH via
// exec.LookPath, CLAUDE_GLM_BIN, ~/.local/bin). A "pin" table freezes them
// for hermetic runs: a string pins the probe reachable at that path, null
// or "" pins it not-found, an absent key keeps the real lookup:
//
//	{"op": "resolve", "harness": "omp", "pin": {"omp": null, "claude": "/w/claude-glm"}}
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/lanereach"
)

func main() {
	var request lanereach.Request
	if err := json.NewDecoder(os.Stdin).Decode(&request); err != nil {
		fmt.Fprintf(os.Stderr, "lanereach: unreadable request: %v\n", err)
		os.Exit(2)
	}
	response, err := lanereach.Apply(lanereach.OSHost{}, request)
	if err != nil {
		fmt.Fprintf(os.Stderr, "lanereach: %v\n", err)
		os.Exit(2)
	}
	encoded, err := json.Marshal(response)
	if err != nil {
		fmt.Fprintf(os.Stderr, "lanereach: %v\n", err)
		os.Exit(2)
	}
	fmt.Println(string(encoded))
}
