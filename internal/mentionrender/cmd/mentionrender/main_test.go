package main

import (
	"bytes"
	"encoding/json"
	"os/exec"
	"path/filepath"
	"testing"
)

// binary builds cmd/mentionrender into a temp dir so the tests exercise the
// real stdin/stdout/exit-code contract, not a reimplementation.
func binary(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "mentionrender")
	build := exec.Command("go", "build", "-o", path, ".")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, out)
	}
	return path
}

// query pipes one request through the binary and returns the decoded
// response plus the exit code.
func query(t *testing.T, bin, request string) (response, int) {
	t.Helper()
	cmd := exec.Command(bin)
	cmd.Stdin = bytes.NewBufferString(request)
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	err := cmd.Run()
	code := 0
	if exitErr, ok := err.(*exec.ExitError); ok {
		code = exitErr.ExitCode()
	} else if err != nil {
		t.Fatalf("run: %v", err)
	}
	var resp response
	if code == 0 {
		if err := json.Unmarshal(stdout.Bytes(), &resp); err != nil {
			t.Fatalf("response is not JSON: %v (%q)", err, stdout.String())
		}
	}
	return resp, code
}

func TestRenders(t *testing.T) {
	bin := binary(t)
	resp, code := query(t, bin,
		`{"content":"ask <@111> about it","bot_id":"222","bot_name":"Hermes","names":{"111":"Alice"},"mode":"body"}`)
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	if want := "ask @Alice (<@111>) about it"; resp.Text != want {
		t.Fatalf("text = %q, want %q", resp.Text, want)
	}
}

func TestRendersTitleMode(t *testing.T) {
	bin := binary(t)
	resp, code := query(t, bin,
		`{"content":"<@222> please introduce urselves to <@111>","bot_id":"222","bot_name":"Hermes","names":{"111":"Alice"},"mode":"title"}`)
	if code != 0 {
		t.Fatalf("exit code = %d, want 0", code)
	}
	if want := "please introduce urselves to @Alice"; resp.Text != want {
		t.Fatalf("text = %q, want %q", resp.Text, want)
	}
}

func TestUnknownModeExitsTwo(t *testing.T) {
	bin := binary(t)
	_, code := query(t, bin, `{"content":"hi","mode":"sideways"}`)
	if code != 2 {
		t.Fatalf("exit code = %d, want 2", code)
	}
}

func TestInvalidRequestExitsTwo(t *testing.T) {
	bin := binary(t)
	_, code := query(t, bin, `not json`)
	if code != 2 {
		t.Fatalf("exit code = %d, want 2", code)
	}
}
