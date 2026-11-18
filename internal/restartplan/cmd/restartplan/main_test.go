package main

import (
	"encoding/json"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// buildBinary compiles cmd/restartplan once per test binary so the wire
// contract is exercised against the real executable the Python contract
// test (tests/gateway/test_restart_plan_contract.py) drives.
func buildBinary(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no Go toolchain available")
	}
	bin := filepath.Join(t.TempDir(), "restartplan")
	build := exec.Command("go", "build", "-o", bin, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	return bin
}

// TestBinaryWireContract pins the wire surface: one JSON request on stdin,
// one JSON decision on stdout, exit 2 with a stderr message on a malformed
// request. Success rows decode the answer (key order and whitespace must
// not matter); failure rows match the message class.
func TestBinaryWireContract(t *testing.T) {
	bin := buildBinary(t)
	requests := []struct {
		name    string
		stdin   string
		exit    int
		wantOut string
		wantErr string
	}{
		{
			name:    "decide_manual_host",
			stdin:   `{"op":"decide","trigger":"user","supervised":false,"container":false}`,
			exit:    0,
			wantOut: `{"via_service":false,"detached":true,"spawns_successor":true}`,
		},
		{
			name:    "decide_defaults_when_op_omitted",
			stdin:   `{"trigger":"update_signal","supervised":true,"container":false}`,
			exit:    0,
			wantOut: `{"via_service":true,"detached":false,"spawns_successor":true}`,
		},
		{
			name:    "exit_code_service_restart",
			stdin:   `{"op":"exit_code","restart_requested":true,"via_service":true,"successor_started":false,"supervised":true}`,
			exit:    0,
			wantOut: `{"code":75,"special":true}`,
		},
		{
			name:    "exit_code_clean_exit",
			stdin:   `{"op":"exit_code","restart_requested":true,"via_service":false,"successor_started":false,"supervised":false}`,
			exit:    0,
			wantOut: `{"code":0,"special":false}`,
		},
		{
			name:    "unknown_op",
			stdin:   `{"op":"drain","trigger":"user"}`,
			exit:    2,
			wantErr: `unknown op "drain"`,
		},
		{
			name:    "unknown_trigger",
			stdin:   `{"trigger":"cron","supervised":false,"container":false}`,
			exit:    2,
			wantErr: "unknown trigger",
		},
		{
			name:    "unreadable_request",
			stdin:   `not json`,
			exit:    2,
			wantErr: "unreadable request",
		},
	}
	for _, tc := range requests {
		t.Run(tc.name, func(t *testing.T) {
			cmd := exec.Command(bin)
			cmd.Stdin = strings.NewReader(tc.stdin)
			out, err := cmd.Output()
			stderr := ""
			if exitErr, ok := err.(*exec.ExitError); ok {
				stderr = string(exitErr.Stderr)
			} else if err != nil {
				t.Fatalf("run binary: %v", err)
			}
			if code := cmd.ProcessState.ExitCode(); code != tc.exit {
				t.Fatalf("exit = %d, want %d (stderr %q)", code, tc.exit, stderr)
			}
			if tc.exit != 0 {
				if !strings.Contains(stderr, tc.wantErr) {
					t.Fatalf("stderr %q missing %q", stderr, tc.wantErr)
				}
				return
			}
			var got, want any
			if err := json.Unmarshal(out, &got); err != nil {
				t.Fatalf("stdout %q is not JSON: %v", out, err)
			}
			if err := json.Unmarshal([]byte(tc.wantOut), &want); err != nil {
				t.Fatalf("expected %q is not JSON: %v", tc.wantOut, err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("stdout = %v, want %v", got, want)
			}
		})
	}
}
