// Command restartsuccessor is the executable form of the canonical Go
// twin: it reads the shared contract table on stdin and writes one
// decision per case to stdout as a JSON array.
//
// This is the bridge the Python tree executes: the contract test in
// tests/gateway/test_restart_successor_contract.py pipes
// testdata/restart_cases.json through this binary and asserts
// gateway/overlap_restart.py::spawn_successor reaches the identical
// decision for every record, so the Python function — the one the runtime
// runs — is pinned against the RUNNING Go implementation, not merely
// against the table. Production gateways do not shell out to it:
// deployments (pip, docker) carry no Go toolchain, the same reason every
// other internal/* twin is executed by tests rather than at runtime.
//
// Request shape:
//
//	{"cases": [{"name": ..., "input": {...}}, ...]}
//	  → prints [{"name": ..., "result": ..., "pid": ..., "started": ...,
//	             "spawn_attempted": [...], "readiness_marked": [...],
//	             "service_managed": ...}, ...]
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/restartsuccessor"
)

func main() {
	var table struct {
		Cases []struct {
			Name  string                    `json:"name"`
			Input restartsuccessor.Scenario `json:"input"`
		} `json:"cases"`
	}
	if err := json.NewDecoder(os.Stdin).Decode(&table); err != nil {
		fmt.Fprintf(os.Stderr, "restartsuccessor: unreadable table: %v\n", err)
		os.Exit(2)
	}
	type record struct {
		Name                     string `json:"name"`
		restartsuccessor.Outcome        // inlined fields keep the parity output flat
	}
	records := make([]record, 0, len(table.Cases))
	for _, c := range table.Cases {
		records = append(records, record{Name: c.Name, Outcome: restartsuccessor.Decide(c.Input)})
	}
	encoded, err := json.Marshal(records)
	if err != nil {
		fmt.Fprintf(os.Stderr, "restartsuccessor: %v\n", err)
		os.Exit(2)
	}
	fmt.Println(string(encoded))
}
