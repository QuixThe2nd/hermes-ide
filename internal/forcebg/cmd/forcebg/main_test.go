package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Golden tests over the SAME shared table the Python contract test replays
// (tests/tools/test_forcebg_go_contract.py). The Python side executes this
// binary only when a Go toolchain exists; these tests keep the CLI wiring
// pinned in the Go lane unconditionally.

func sharedTable(t *testing.T) map[string]any {
	t.Helper()
	path := filepath.Join("..", "..", "testdata", "forcebg_cases.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table map[string]any
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	return table
}

func tableCases(t *testing.T) []map[string]any {
	t.Helper()
	table := sharedTable(t)
	rawCases, ok := table["cases"].([]any)
	if !ok {
		t.Fatal("shared table has no cases array")
	}
	cases := make([]map[string]any, 0, len(rawCases))
	for _, c := range rawCases {
		m, ok := c.(map[string]any)
		if !ok {
			t.Fatalf("case is not an object: %v", c)
		}
		cases = append(cases, m)
	}
	return cases
}

func TestSharedTableReplaysMatchEmbeddedExpectations(t *testing.T) {
	for _, caseVal := range tableCases(t) {
		name, _ := caseVal["name"].(string)
		t.Run(name, func(t *testing.T) {
			input, err := json.Marshal(caseVal)
			if err != nil {
				t.Fatalf("marshal case: %v", err)
			}
			var stdout, stderr bytes.Buffer
			code := run(nil, strings.NewReader(string(input)), &stdout, &stderr)
			if code != 0 {
				t.Fatalf("exit %d: %s", code, stderr.String())
			}
			var answer struct {
				Name  string           `json:"name"`
				Steps []map[string]any `json:"steps"`
			}
			if err := json.Unmarshal(stdout.Bytes(), &answer); err != nil {
				t.Fatalf("parse transcript: %v\n%s", err, stdout.String())
			}
			expect, _ := caseVal["expect"].([]any)
			if len(expect) != len(answer.Steps) {
				t.Fatalf("transcript length %d, want %d", len(answer.Steps), len(expect))
			}
			for i, wantAny := range expect {
				want, _ := wantAny.(map[string]any)
				got := answer.Steps[i]
				if !stepsEqual(want, got) {
					t.Fatalf("step %d mismatch:\n want %v\n got  %v", i, want, got)
				}
			}
		})
	}
}

// stepsEqual compares two transcript steps treating missing nil-vs-empty list
// as equal (encoding/json decodes an absent [] as nil).
func stepsEqual(a, b map[string]any) bool {
	if len(a) != len(b) {
		return false
	}
	for key, av := range a {
		bv, ok := b[key]
		if !ok {
			return false
		}
		as, aIsList := av.([]any)
		bs, bIsList := bv.([]any)
		if aIsList || bIsList {
			if !aIsList || !bIsList || len(as) != len(bs) {
				return false
			}
			for i := range as {
				if as[i] != bs[i] {
					return false
				}
			}
			continue
		}
		if av != bv {
			return false
		}
	}
	return true
}

func TestReplayRejectsMalformedOps(t *testing.T) {
	cases := []struct {
		name    string
		caseVal map[string]any
		wantErr string
	}{
		{
			"unknown op",
			map[string]any{"name": "bad", "agents": []any{}, "runs": []any{}, "ops": []any{map[string]any{"op": "explode"}}},
			"unknown op",
		},
		{
			"adopt without registration",
			map[string]any{"name": "bad", "agents": []any{}, "runs": []any{}, "ops": []any{map[string]any{"op": "adopt", "run": 0}}},
			"no registered wait",
		},
		{
			"register with unknown agent",
			map[string]any{"name": "bad", "agents": []any{}, "runs": []any{map[string]any{"parent": "ghost", "adopt_id": "d"}}, "ops": []any{map[string]any{"op": "register", "run": 0}}},
			"unknown agent",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			input, err := json.Marshal(tc.caseVal)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			var stdout, stderr bytes.Buffer
			code := run(nil, strings.NewReader(string(input)), &stdout, &stderr)
			if code != 2 {
				t.Fatalf("exit %d, want 2 (stderr: %s)", code, stderr.String())
			}
			if !strings.Contains(stderr.String(), tc.wantErr) {
				t.Fatalf("stderr %q missing %q", stderr.String(), tc.wantErr)
			}
		})
	}
}

func TestRunRejectsJunkJSON(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run(nil, strings.NewReader("{not json"), &stdout, &stderr)
	if code != 2 {
		t.Fatalf("exit %d, want 2", code)
	}
	if !strings.Contains(stderr.String(), "bad case JSON") {
		t.Fatalf("stderr %q missing junk message", stderr.String())
	}
}

func TestHelpFlagSucceeds(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run([]string{"--help"}, strings.NewReader(""), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("exit %d, want 0", code)
	}
	if !strings.Contains(stdout.String(), "usage: forcebg") {
		t.Fatalf("stdout %q missing usage", stdout.String())
	}
}
