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
// Request shapes (one JSON object, "op" selects; "" means "decide"):
//
//	{"op": "decide", "trigger": "user" | "update_signal",
//	 "supervised": bool, "container": bool}
//	  → prints the transport plan.
//
//	{"op": "exit_code", "restart_requested": bool, "via_service": bool,
//	 "successor_started": bool, "supervised": bool,
//	 "newer_live_generation": bool}
//	  → prints the planned exit code as {"code": N, "special": bool}.
//	  With "probe_failed": true the newer-generation probe raised instead
//	  of answering (newer_live_generation is ignored); when no started
//	  successor decides the exit, that is an error → exit 3.
//
//	{"op": "hard_exit", "successor_identity": "live" | "dead" | "unknown" | "none",
//	 "newer_live_generation": bool, "systemd_managed": bool,
//	 "mainpid_handover_done": bool}
//	  → prints the backstop's action as {"action": "requested" | "overlap" | "park"}.
//	  This is the watchdog-exit decision (ResolveHardExitAction): what a
//	  hard-exit backstop on a draining predecessor must do so it can never
//	  kill a live overlap successor's cgroup.
//
//	{"op": "hard_exit_runner", "successor_started": bool,
//	 "successor_identity": "live" | "dead" | "unknown" | "none",
//	 "newer_live_generation": bool, "systemd_managed": bool,
//	 "mainpid_transfer_succeeds": bool}
//	  → prints the resolver's action as {"action": ...}. This is the
//	  evidence-selection layer (ResolveRunnerHardExit): which successor
//	  evidence a draining runner's resolver may let decide — the mirror
//	  only for a dead or absent successor, the runner's own successor
//	  state (plus the MAINPID transfer outcome) otherwise.
//
//	{"op": "hard_exit_code", "action": "requested" | "overlap" | "park",
//	 "armed_exit_code": int}
//	  → prints the code a firing backstop exits with as
//	  {"code": N, "exited": bool}; exited=false is PARK (the backstop must
//	  not exit — HardExitCode).
//
// An unknown op or trigger exits 2 with a message on stderr.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/restartplan"
)

func main() {
	var request struct {
		Op                      string `json:"op"`
		Trigger                 string `json:"trigger"`
		Supervised              bool   `json:"supervised"`
		Container               bool   `json:"container"`
		RestartRequested        bool   `json:"restart_requested"`
		ViaService              bool   `json:"via_service"`
		SuccessorStarted        bool   `json:"successor_started"`
		NewerLiveGeneration     bool   `json:"newer_live_generation"`
		ProbeFailed             bool   `json:"probe_failed"`
		SuccessorIdentity       string `json:"successor_identity"`
		SystemdManaged          bool   `json:"systemd_managed"`
		MainpidHandoverDone     bool   `json:"mainpid_handover_done"`
		MainpidTransferSucceeds bool   `json:"mainpid_transfer_succeeds"`
		Action                  string `json:"action"`
		ArmedExitCode           int    `json:"armed_exit_code"`
	}
	if err := json.NewDecoder(os.Stdin).Decode(&request); err != nil {
		fmt.Fprintf(os.Stderr, "restartplan: unreadable request: %v\n", err)
		os.Exit(2)
	}
	switch op := request.Op; op {
	case "", "decide":
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
		emit(plan)
	case "exit_code":
		if request.ProbeFailed {
			code, err := restartplan.ResolveExitCodeProbeFailed(
				request.RestartRequested, request.ViaService,
				request.SuccessorStarted, request.Supervised,
			)
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(3)
			}
			emit(code)
			return
		}
		emit(restartplan.ResolveExitCode(
			request.RestartRequested, request.ViaService,
			request.SuccessorStarted, request.Supervised,
			request.NewerLiveGeneration,
		))
	case "hard_exit":
		emit(struct {
			Action restartplan.HardExitAction `json:"action"`
		}{restartplan.ResolveHardExitAction(
			request.SuccessorIdentity, request.NewerLiveGeneration,
			request.SystemdManaged, request.MainpidHandoverDone,
		)})
	case "hard_exit_runner":
		emit(struct {
			Action restartplan.HardExitAction `json:"action"`
		}{restartplan.ResolveRunnerHardExit(
			request.SuccessorStarted, request.SuccessorIdentity,
			request.NewerLiveGeneration, request.SystemdManaged,
			request.MainpidTransferSucceeds,
		)})
	case "hard_exit_code":
		code, exited := restartplan.HardExitCode(
			restartplan.HardExitAction(request.Action), request.ArmedExitCode)
		emit(struct {
			Code   int  `json:"code"`
			Exited bool `json:"exited"`
		}{code, exited})
	default:
		fmt.Fprintf(os.Stderr, "restartplan: unknown op %q\n", op)
		os.Exit(2)
	}
}

func emit(value any) {
	encoded, err := json.Marshal(value)
	if err != nil {
		fmt.Fprintf(os.Stderr, "restartplan: encode decision: %v\n", err)
		os.Exit(2)
	}
	fmt.Println(string(encoded))
}
