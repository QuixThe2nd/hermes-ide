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

// TestBinaryHardExitWireContract pins the hard_exit op added for the
// 2026-11-20 watchdog incident: the executable form of
// ResolveHardExitAction, the decision every predecessor-teardown backstop
// (shutdown watchdog, loop-liveness watchdog) consults before it may
// os._exit. The rows are the failure modes that decision exists for: an
// armed exit code must never kill a verified live successor, and an
// identity we cannot fingerprint must never be guessed about.
func TestBinaryHardExitWireContract(t *testing.T) {
	bin := buildBinary(t)
	requests := []struct {
		name    string
		stdin   string
		wantOut string
	}{
		{
			name: "live_unmanaged_successor_hands_over",
			stdin: `{"op":"hard_exit","successor_identity":"live",` +
				`"newer_live_generation":false,"systemd_managed":false,` +
				`"mainpid_handover_done":false}`,
			wantOut: `{"action":"overlap"}`,
		},
		{
			name: "live_managed_without_handover_parks",
			stdin: `{"op":"hard_exit","successor_identity":"live",` +
				`"newer_live_generation":false,"systemd_managed":true,` +
				`"mainpid_handover_done":false}`,
			wantOut: `{"action":"park"}`,
		},
		{
			name: "live_managed_after_handover_hands_over",
			stdin: `{"op":"hard_exit","successor_identity":"live",` +
				`"newer_live_generation":false,"systemd_managed":true,` +
				`"mainpid_handover_done":true}`,
			wantOut: `{"action":"overlap"}`,
		},
		{
			name: "unknown_identity_parks_even_with_newer_generation",
			stdin: `{"op":"hard_exit","successor_identity":"unknown",` +
				`"newer_live_generation":true,"systemd_managed":false,` +
				`"mainpid_handover_done":false}`,
			wantOut: `{"action":"park"}`,
		},
		{
			name: "unrecognized_identity_parks",
			stdin: `{"op":"hard_exit","successor_identity":"",` +
				`"newer_live_generation":false,"systemd_managed":false,` +
				`"mainpid_handover_done":false}`,
			wantOut: `{"action":"park"}`,
		},
		{
			name: "dead_successor_allows_serial_replacement",
			stdin: `{"op":"hard_exit","successor_identity":"dead",` +
				`"newer_live_generation":false,"systemd_managed":false,` +
				`"mainpid_handover_done":false}`,
			wantOut: `{"action":"requested"}`,
		},
		{
			name: "dead_successor_but_newer_live_generation_hands_over",
			stdin: `{"op":"hard_exit","successor_identity":"dead",` +
				`"newer_live_generation":true,"systemd_managed":false,` +
				`"mainpid_handover_done":false}`,
			wantOut: `{"action":"overlap"}`,
		},
	}
	for _, tc := range requests {
		t.Run(tc.name, func(t *testing.T) {
			cmd := exec.Command(bin)
			cmd.Stdin = strings.NewReader(tc.stdin)
			out, err := cmd.Output()
			if err != nil {
				t.Fatalf("run binary: %v", err)
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

// TestBinaryHardExitCodeWireContract pins the hard_exit_code op: how a
// resolved action maps onto the code the backstop exits with. Overlap
// overrides whatever the backstop was armed with (arming happens before
// the drain, when no successor state is known); requested keeps it; park
// answers exited=false — the backstop stays alive and re-plans.
func TestBinaryHardExitCodeWireContract(t *testing.T) {
	bin := buildBinary(t)
	requests := []struct {
		name    string
		stdin   string
		wantOut string
	}{
		{
			name:    "overlap_overrides_the_armed_failure_code",
			stdin:   `{"op":"hard_exit_code","action":"overlap","armed_exit_code":1}`,
			wantOut: `{"code":76,"exited":true}`,
		},
		{
			name:    "overlap_overrides_the_armed_restart_code",
			stdin:   `{"op":"hard_exit_code","action":"overlap","armed_exit_code":75}`,
			wantOut: `{"code":76,"exited":true}`,
		},
		{
			name:    "requested_keeps_the_armed_code",
			stdin:   `{"op":"hard_exit_code","action":"requested","armed_exit_code":1}`,
			wantOut: `{"code":1,"exited":true}`,
		},
		{
			name:    "park_does_not_exit",
			stdin:   `{"op":"hard_exit_code","action":"park","armed_exit_code":75}`,
			wantOut: `{"code":0,"exited":false}`,
		},
	}
	for _, tc := range requests {
		t.Run(tc.name, func(t *testing.T) {
			cmd := exec.Command(bin)
			cmd.Stdin = strings.NewReader(tc.stdin)
			out, err := cmd.Output()
			if err != nil {
				t.Fatalf("run binary: %v", err)
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

// TestBinaryProbeFailedExit3 pins the exit_code op's probe-failure wire:
// when the newer-live-generation probe raised where its answer would
// decide the code, the binary exits 3 (an unanswered question) instead of
// guessing 75 or 76 — the row that keeps a broken probe from arming the
// unit-level restart that kills a live successor's cgroup.
func TestBinaryProbeFailedExit3(t *testing.T) {
	bin := buildBinary(t)
	cmd := exec.Command(bin)
	cmd.Stdin = strings.NewReader(
		`{"op":"exit_code","restart_requested":true,"via_service":true,` +
			`"successor_started":false,"supervised":true,"probe_failed":true}`)
	out, err := cmd.Output()
	stderr := ""
	if exitErr, ok := err.(*exec.ExitError); ok {
		stderr = string(exitErr.Stderr)
	} else if err != nil {
		t.Fatalf("run binary: %v", err)
	}
	if code := cmd.ProcessState.ExitCode(); code != 3 {
		t.Fatalf("exit = %d, want 3 (stdout %q, stderr %q)", code, out, stderr)
	}
	if len(out) > 0 {
		t.Fatalf("a failed probe must not print a decision, got %q", out)
	}
	if !strings.Contains(stderr, "probe") {
		t.Fatalf("stderr %q must name the failed probe", stderr)
	}
}
