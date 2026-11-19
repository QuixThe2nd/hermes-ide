// Command generations is the executable form of the canonical Go twin: it
// reads one request as JSON on stdin and writes the decision results as
// JSON on stdout.
//
// This is the bridge the Python tree executes: the contract test in
// tests/gateway/test_generations_contract.py pipes every shared-table
// record (testdata/cases.json) through this binary and asserts
// gateway/generations.py computes identical answers, so the Python module —
// the one the runtime uses — is pinned against the RUNNING Go
// implementation, not merely the table. Production gateways do not shell
// out to it (deployments carry no Go toolchain), the same arrangement as
// every other internal/* twin.
//
// Request shape (one JSON object):
//
//	{"generations": [{"pid": 101, "predecessor": 0, "spawn_seq": 0,
//	  "attached": true, "draining": false, "live": true}, …],
//	 "me": 101, "pid_file": {"pid": 102, "live": true}}
//
// Response shape:
//
//	{"newest_live": 103, "restart_target": 103,
//	 "may_claim": false, "may_start_shared": true,
//	 "ancestors": [102, 101], "takeover": true,
//	 "describe": ["103: serving", "102: draining", "101: draining"]}
//
// "takeover" answers whether me may rewrite the PID file described by the
// "pid_file" field (absent field → no opinion, null).
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/generations"
)

func main() {
	var request struct {
		Generations []generations.Generation `json:"generations"`
		Me          int                      `json:"me"`
		PIDFile     *struct {
			PID  int  `json:"pid"`
			Live bool `json:"live"`
		} `json:"pid_file"`
	}
	if err := json.NewDecoder(os.Stdin).Decode(&request); err != nil {
		fmt.Fprintf(os.Stderr, "generations: unreadable request: %v\n", err)
		os.Exit(2)
	}
	array := generations.Array(request.Generations)
	ancestors := array.AncestorsOf(request.Me)

	response := map[string]any{
		"newest_live":      pidOf(array.NewestLive()),
		"restart_target":   pidOf(array.NewestLiveOther(request.Me)),
		"may_claim":        array.MayClaimAuthority(request.Me),
		"may_start_shared": array.MayStartSharedAdapters(request.Me),
		"ancestors":        ancestors,
		"describe":         array.Describe(),
		"takeover":         nil,
	}
	if request.PIDFile != nil {
		response["takeover"] = generations.TakeoverAllowed(
			request.PIDFile.PID, request.PIDFile.Live, ancestors)
	}
	encoded, err := json.Marshal(response)
	if err != nil {
		fmt.Fprintf(os.Stderr, "generations: %v\n", err)
		os.Exit(2)
	}
	fmt.Println(string(encoded))
}

func pidOf(g *generations.Generation) int {
	if g == nil {
		return 0
	}
	return g.PID
}
