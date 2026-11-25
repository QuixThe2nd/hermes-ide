package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/QuixThe2nd/hermes-ide/internal/goalrun"
)

// TestRunIsTheGatewayContract pins the executable boundary the Python bridge
// depends on: one Turn JSON in, one Plan JSON out, exit 2 on a record it cannot
// read, and nothing on stdout when it refuses.
func TestRunIsTheGatewayContract(t *testing.T) {
	cases := []struct {
		name  string
		input string
		code  int
		want  goalrun.Plan
	}{
		{"continuing", `{"active":true,"should_continue":true,"turn":2,"max_turns":5,"objective":"ship it"}`, 0,
			goalrun.Plan{Suppress: true, Objective: "ship it", Line: "🎯 turn 2/5"}},
		{"finishing", `{"active":true,"should_continue":false,"turn":5,"max_turns":5}`, 0,
			goalrun.Plan{Final: true, Line: "🎯 turn 5/5"}},
		{"inactive", `{"active":false,"should_continue":true}`, 0, goalrun.Plan{}},
		{"truncated", `{"active":`, 2, goalrun.Plan{}},
		{"empty", ``, 2, goalrun.Plan{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var out, errOut bytes.Buffer
			code := run(strings.NewReader(tc.input), &out, &errOut)
			if code != tc.code {
				t.Fatalf("exit = %d, want %d (stderr %q)", code, tc.code, errOut.String())
			}
			if code != 0 {
				if out.Len() != 0 {
					t.Fatalf("error path wrote %q to stdout", out.String())
				}
				return
			}
			var plan goalrun.Plan
			if err := json.Unmarshal(out.Bytes(), &plan); err != nil {
				t.Fatalf("stdout is not a plan: %v (%q)", err, out.String())
			}
			if plan != tc.want {
				t.Fatalf("plan = %+v, want %+v", plan, tc.want)
			}
		})
	}
}
