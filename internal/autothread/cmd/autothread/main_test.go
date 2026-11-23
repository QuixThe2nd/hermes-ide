package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/QuixThe2nd/hermes-ide/internal/autothread"
)

// buildBinary compiles the real command once per test run so the CLI
// contract is exercised through the same binary the Python bridge spawns.
func buildBinary(t *testing.T) string {
	t.Helper()
	binary := filepath.Join(t.TempDir(), "autothread")
	build := exec.Command("go", "build", "-o", binary, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, out)
	}
	return binary
}

// runBinary feeds one JSON payload to the binary and returns its exit code
// and stdout.
func runBinary(t *testing.T, binary, payload string) (int, string) {
	t.Helper()
	cmd := exec.Command(binary)
	cmd.Stdin = strings.NewReader(payload)
	out, err := cmd.Output()
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			return exit.ExitCode(), string(out)
		}
		t.Fatalf("run binary: %v", err)
	}
	return 0, string(out)
}

// TestCLIAgainstSharedTable drives every row of
// testdata/classification_cases.json through the compiled binary: the
// process boundary must not change a single decision.
func TestCLIAgainstSharedTable(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "testdata", "classification_cases.json"))
	if err != nil {
		t.Fatalf("read table: %v", err)
	}
	var table struct {
		Cases []struct {
			Name     string              `json:"name"`
			Status   int                 `json:"status"`
			Code     int                 `json:"code"`
			Message  string              `json:"message"`
			Decision autothread.Decision `json:"decision"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &table); err != nil {
		t.Fatalf("parse table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("table is empty")
	}
	binary := buildBinary(t)
	for _, tc := range table.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			payload := fmt.Sprintf(`{"status": %d, "code": %d, "message": %s}`,
				tc.Status, tc.Code, mustQuote(t, tc.Message))
			code, stdout := runBinary(t, binary, payload)
			if code != 0 {
				t.Fatalf("exit code = %d, want 0", code)
			}
			var resp response
			if err := json.Unmarshal([]byte(stdout), &resp); err != nil {
				t.Fatalf("unparseable response %q: %v", stdout, err)
			}
			if resp.Decision != tc.Decision {
				t.Errorf("decision = %q, want %q", resp.Decision, tc.Decision)
			}
		})
	}
}

// TestCLIRejectsGarbage envelopes: the bridge fails safe (treats the error
// as non-adopt) when the helper cannot answer, so the binary must signal
// unreadable input with a non-zero exit instead of guessing.
func TestCLIRejectsGarbage(t *testing.T) {
	binary := buildBinary(t)
	for _, payload := range []string{
		`not json`,
		``,
		`[1, 2, 3]`,
		`{"status": "400"}`, // status must decode as a number
	} {
		code, stdout := runBinary(t, binary, payload)
		if code != 2 {
			t.Errorf("payload %q: exit = %d (stdout %q), want 2", payload, code, stdout)
		}
	}
}

// TestCLIEmptyEnvelopeFails: a request naming no recognizable failure class
// answers "fail", never "adopt" — the zero value must be the safe one.
func TestCLIEmptyEnvelopeFails(t *testing.T) {
	binary := buildBinary(t)
	code, stdout := runBinary(t, binary, `{}`)
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	var resp response
	if err := json.Unmarshal([]byte(stdout), &resp); err != nil {
		t.Fatalf("unparseable response %q: %v", stdout, err)
	}
	if resp.Decision != autothread.Fail {
		t.Errorf("empty envelope decision = %q, want %q", resp.Decision, autothread.Fail)
	}
}

// mustQuote renders a Go string as a JSON string literal.
func mustQuote(t *testing.T, s string) string {
	t.Helper()
	quoted, err := json.Marshal(s)
	if err != nil {
		t.Fatalf("quote %q: %v", s, err)
	}
	return string(quoted)
}

// TestCLIFieldOmission: each field is optional on the wire — the bridge
// sends best-effort extracts from duck-typed exceptions, and a missing field
// must decode as its zero value without changing the failure-safe default.
func TestCLIFieldOmission(t *testing.T) {
	binary := buildBinary(t)
	for _, tc := range []struct {
		name    string
		payload string
		want    autothread.Decision
	}{
		{"code only", `{"code": 160004}`, autothread.Adopt},
		{"status only", `{"status": 429}`, autothread.RateLimited},
		{"message only", `{"message": "Cannot connect to host discord.com:443"}`, autothread.Retry},
		{"unknown fields ignored", `{"status": 400, "code": 50035, "extra": true}`, autothread.Fail},
		{"trailing newline tolerated", "{\"code\": 160004}\n", autothread.Adopt},
		{"unicode message round-trips", `{"message": "スレッドはすでに作成されています"}`, autothread.Fail},
	} {
		t.Run(tc.name, func(t *testing.T) {
			code, stdout := runBinary(t, binary, tc.payload)
			if code != 0 {
				t.Fatalf("exit = %d for %s, want 0", code, tc.payload)
			}
			var resp response
			if err := json.Unmarshal([]byte(stdout), &resp); err != nil {
				t.Fatalf("unparseable response %q: %v", stdout, err)
			}
			if resp.Decision != tc.want {
				t.Errorf("decision = %q, want %q", resp.Decision, tc.want)
			}
		})
	}
}

// TestCLIResponseShape pins the response envelope the Python bridge parses:
// exactly one JSON object on one line with a "decision" key.
func TestCLIResponseShape(t *testing.T) {
	binary := buildBinary(t)
	code, stdout := runBinary(t, binary, `{"status": 400, "code": 160004, "message": "x"}`)
	if code != 0 {
		t.Fatalf("exit = %d, want 0", code)
	}
	trimmed := strings.TrimRight(stdout, "\n")
	if strings.Contains(trimmed, "\n") {
		t.Errorf("response spans multiple lines: %q", stdout)
	}
	var raw map[string]any
	if err := json.Unmarshal([]byte(trimmed), &raw); err != nil {
		t.Fatalf("response is not a JSON object: %v", err)
	}
	if _, ok := raw["decision"]; !ok {
		t.Errorf("response %q lacks the decision key", trimmed)
	}
}

// runBinaryWithStderr feeds one JSON payload to the binary and returns its
// exit code, stdout, and stderr.
func runBinaryWithStderr(t *testing.T, binary, payload string) (int, string, string) {
	t.Helper()
	cmd := exec.Command(binary)
	cmd.Stdin = strings.NewReader(payload)
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			return exit.ExitCode(), stdout.String(), stderr.String()
		}
		t.Fatalf("run binary: %v", err)
	}
	return 0, stdout.String(), stderr.String()
}

// TestCLIRejectsNonObjectEnvelopes: only a JSON object is a request. Scalar
// payloads are decode errors (exit 2), never a guessed classification — the
// bridge relies on a non-zero exit to fail safe.
func TestCLIRejectsNonObjectEnvelopes(t *testing.T) {
	binary := buildBinary(t)
	for _, payload := range []string{`42`, `"text"`, `true`} {
		code, stdout := runBinary(t, binary, payload)
		if code != 2 {
			t.Errorf("payload %q: exit = %d (stdout %q), want 2", payload, code, stdout)
		}
	}
}

// TestCLINullEnvelopeFailsSafe: a null payload decodes to the zero request,
// which names no failure class — the answer is "fail", never "adopt".
func TestCLINullEnvelopeFailsSafe(t *testing.T) {
	binary := buildBinary(t)
	code, stdout := runBinary(t, binary, `null`)
	if code != 0 {
		t.Fatalf("exit = %d for null, want 0 (zero request decodes cleanly)", code)
	}
	var resp response
	if err := json.Unmarshal([]byte(stdout), &resp); err != nil {
		t.Fatalf("unparseable response %q: %v", stdout, err)
	}
	if resp.Decision != autothread.Fail {
		t.Errorf("decision = %q, want %q (the zero value must be the safe one)",
			resp.Decision, autothread.Fail)
	}
}

// TestCLIClassifiesOnlyTheFirstRequest: one process invocation answers one
// request — bytes after the first JSON value are not a second verdict.
func TestCLIClassifiesOnlyTheFirstRequest(t *testing.T) {
	binary := buildBinary(t)
	code, stdout := runBinary(t, binary, `{"code": 160004} {"status": 500}`)
	if code != 0 {
		t.Fatalf("exit = %d, want 0", code)
	}
	trimmed := strings.TrimRight(stdout, "\n")
	if strings.Contains(trimmed, "\n") {
		t.Errorf("response spans multiple lines: %q", stdout)
	}
	var resp response
	if err := json.Unmarshal([]byte(trimmed), &resp); err != nil {
		t.Fatalf("unparseable response %q: %v", trimmed, err)
	}
	if resp.Decision != autothread.Adopt {
		t.Errorf("decision = %q, want %q (only the first request is classified)",
			resp.Decision, autothread.Adopt)
	}
}

// TestCLISuccessKeepsStderrClean: the bridge surfaces stderr only as a
// failure diagnostic, so a successful classification must not chat on it.
func TestCLISuccessKeepsStderrClean(t *testing.T) {
	binary := buildBinary(t)
	code, _, stderr := runBinaryWithStderr(
		t, binary, `{"status": 400, "code": 160004, "message": "x"}`)
	if code != 0 {
		t.Fatalf("exit = %d, want 0", code)
	}
	if stderr != "" {
		t.Errorf("stderr = %q on a successful run, want empty", stderr)
	}
}
