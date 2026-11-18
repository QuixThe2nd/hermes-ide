// Command restartplan is the executable form of the canonical Go twin: it
// reads one request as JSON on stdin and writes the decision as JSON to
// stdout.
//
// This is the bridge the Python tree executes: the contract test in
// tests/gateway/test_restart_plan_contract.py pipes every shared-table
// record (testdata/restart_cases.json) through this binary and asserts
// gateway/restart.py's restart_transport_plan decides identically, so the
// Python function — the one the runtime uses — is pinned against the
// RUNNING Go implementation, not merely against the table. Production
// gateways never shell out to it: deployments (pip, docker) carry no Go
// toolchain, the same reason every other internal/* twin is executed by
// tests rather than at runtime.
//
// Request shape (one JSON object):
//
//	{"trigger": "user" | "update_signal", "supervised": bool, "container": bool}
//
// Response (one JSON object on stdout):
//
//	{"via_service": bool, "detached": bool, "spawns_successor": bool}
//
// An unknown trigger exits 2 with a message on stderr.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/restartplan"
)

func main() {
	var request struct {
		Trigger    string `json:"trigger"`
		Supervised bool   `json:"supervised"`
		Container  bool   `json:"container"`
	}
	if err := json.NewDecoder(os.Stdin).Decode(&request); err != nil {
		fmt.Fprintf(os.Stderr, "restartplan: unreadable request: %v\n", err)
		os.Exit(2)
	}
	trigger, err := restartplan.ParseTrigger(request.Trigger)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	plan, err := restartplan.Decide(trigger, request.Supervised, request.Container)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	encoded, err := json.Marshal(plan)
	if err != nil {
		fmt.Fprintf(os.Stderr, "restartplan: encode decision: %v\n", err)
		os.Exit(2)
	}
	fmt.Println(string(encoded))
}
