// Command forcebg replays one shared-table case (testdata/forcebg_cases.json)
// through the Go twin of the steer force-background contract and prints the
// observable transcript as JSON. The Python contract test
// (tests/tools/test_forcebg_go_contract.py) replays the SAME case through
// tools/delegate_tool_forcebg.py and requires byte-identical transcripts —
// the twin is the canonical statement, the runtime never shells out to it.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/forcebg"
)

// caseInput is one shared-table case: a config, the actors, and an op script.
type caseInput struct {
	Name            string      `json:"name"`
	SteerForceBg    *bool       `json:"steer_force_background,omitempty"` // nil = default (on)
	DeliverySupport *bool       `json:"delivery_supported,omitempty"`     // nil = default (capable)
	Agents          []agentSpec `json:"agents"`
	Runs            []runSpec   `json:"runs"`
	Ops             []opSpec    `json:"ops"`
}

type agentSpec struct {
	Key          string `json:"key"`
	DelegateDept int    `json:"delegate_depth"`
	HasSteer     bool   `json:"has_steer"`
	SteerRefused bool   `json:"steer_refused"`
}

type runSpec struct {
	Parent  string `json:"parent"`
	AdoptID string `json:"adopt_id"` // "" = the rail refuses this adoption
}

type opSpec struct {
	Op           string   `json:"op"`
	Run          int      `json:"run,omitempty"`
	Key          string   `json:"key,omitempty"`
	Text         string   `json:"text,omitempty"`
	ID           string   `json:"id,omitempty"`
	IDs          []string `json:"ids,omitempty"`
	Injected     string   `json:"injected,omitempty"`
	StatusDetail string   `json:"status_detail,omitempty"`
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(argv []string, stdin io.Reader, stdout, stderr io.Writer) int {
	for _, arg := range argv {
		if arg == "--help" || arg == "-h" {
			fmt.Fprintln(stdout, "usage: forcebg < case.json")
			return 0
		}
	}
	raw, err := io.ReadAll(stdin)
	if err != nil {
		fmt.Fprintf(stderr, "forcebg: read stdin: %v\n", err)
		return 1
	}
	var input caseInput
	if err := json.Unmarshal(raw, &input); err != nil {
		fmt.Fprintf(stderr, "forcebg: bad case JSON: %v\n", err)
		return 2
	}
	transcript, err := replay(input)
	if err != nil {
		fmt.Fprintf(stderr, "forcebg: %v\n", err)
		return 2
	}
	out, _ := json.Marshal(map[string]any{"name": input.Name, "steps": transcript})
	fmt.Fprintln(stdout, string(out))
	return 0
}

// replay executes the op script against a fresh registry and returns the
// observable steps. Every step's JSON shape is part of the shared contract.
func replay(input caseInput) ([]map[string]any, error) {
	cfg := forcebg.DefaultConfig()
	if input.SteerForceBg != nil {
		cfg.SteerForceBackground = *input.SteerForceBg
	}
	if input.DeliverySupport != nil {
		cfg.DeliverySupported = *input.DeliverySupport
	}
	agents := map[string]forcebg.Agent{}
	for _, a := range input.Agents {
		agents[a.Key] = forcebg.Agent{
			Key: a.Key, DelegateDepth: a.DelegateDept,
			HasSteer: a.HasSteer, SteerRefused: a.SteerRefused,
		}
	}
	registry := forcebg.NewRegistry(cfg)
	waits := make([]*forcebg.Wait, len(input.Runs))
	steps := []map[string]any{}
	for _, op := range input.Ops {
		step := map[string]any{"op": op.Op}
		switch op.Op {
		case "register":
			if op.Run < 0 || op.Run >= len(input.Runs) {
				return nil, fmt.Errorf("op register: run %d out of range", op.Run)
			}
			spec := input.Runs[op.Run]
			parent, ok := agents[spec.Parent]
			if !ok {
				return nil, fmt.Errorf("op register: unknown agent %q", spec.Parent)
			}
			waits[op.Run] = registry.Register(&forcebg.Run{Parent: parent, AdoptID: spec.AdoptID})
			step["registered"] = waits[op.Run] != nil
		case "live":
			step["key"] = op.Key
			step["count"] = registry.LiveCount(op.Key)
		case "has_eligible":
			step["key"] = op.Key
			step["value"] = registry.HasEligible(op.Key)
		case "steer":
			agent, ok := agents[op.Key]
			if !ok {
				return nil, fmt.Errorf("op steer: unknown agent %q", op.Key)
			}
			out := registry.SteerForcingBackground(agent, op.Text)
			step["key"] = op.Key
			step["accepted"] = out.Accepted
			step["ids"] = out.IDs
			if out.IDs == nil {
				step["ids"] = []string{}
			}
			step["summary"] = forcebg.Summary(out)
		case "adopt":
			if op.Run < 0 || op.Run >= len(waits) || waits[op.Run] == nil {
				return nil, fmt.Errorf("op adopt: run %d has no registered wait", op.Run)
			}
			step["id"] = waits[op.Run].Adopt()
		case "released":
			if op.Run < 0 || op.Run >= len(waits) || waits[op.Run] == nil {
				return nil, fmt.Errorf("op released: run %d has no registered wait", op.Run)
			}
			step["value"] = waits[op.Run].ReleasedBySteer()
		case "complete":
			if op.Run < 0 || op.Run >= len(waits) || waits[op.Run] == nil {
				return nil, fmt.Errorf("op complete: run %d has no registered wait", op.Run)
			}
			waits[op.Run].Complete()
		case "notice":
			step["text"] = forcebg.EntryNotice(op.ID)
		case "receipt":
			step["text"] = forcebg.BusyReceipt(op.IDs, op.Injected)
		case "steered_receipt":
			step["text"] = forcebg.SteeredQueueReceipt(op.StatusDetail)
		default:
			return nil, fmt.Errorf("unknown op %q", op.Op)
		}
		steps = append(steps, step)
	}
	return steps, nil
}
