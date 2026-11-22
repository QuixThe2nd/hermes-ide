package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/QuixThe2nd/hermes-ide/internal/jevnotices"
)

func TestRunRetrospectiveWithoutCredentialFailsClosed(t *testing.T) {
	input := `{"op":"classify_retrospective","message":"fix the proxy","final_response":"I explained DNS.","initial_label":"chat","initial_confidence":0.4}`
	var stdout, stderr bytes.Buffer
	if code := run(strings.NewReader(input), &stdout, &stderr); code != 0 {
		t.Fatalf("exit = %d, stderr = %q", code, stderr.String())
	}
	var result jevnotices.RetrospectiveResult
	if err := json.Unmarshal(stdout.Bytes(), &result); err != nil {
		t.Fatalf("stdout is not a retrospective result: %q: %v", stdout.String(), err)
	}
	if result.Label != "unknown" || result.Recover || result.Error != "missing_credential" {
		t.Fatalf("result = %+v", result)
	}
	if result.Notice != "```\n🔍 Retrospective Intent: failed (missing_credential)\n```" {
		t.Fatalf("notice = %q", result.Notice)
	}
}

func TestRunRetrospectiveValidationFailuresFailClosedOnStdout(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  string
	}{
		{"empty message", `{"op":"classify_retrospective","final_response":"done","initial_label":"chat","initial_confidence":0.5,"credential":"k"}`, "empty_message"},
		{"empty response", `{"op":"classify_retrospective","message":"hi","initial_label":"chat","initial_confidence":0.5,"credential":"k"}`, "empty_response"},
		{"unknown initial label", `{"op":"classify_retrospective","message":"hi","final_response":"done","initial_label":"bogus","initial_confidence":0.5,"credential":"k"}`, "invalid_initial_label"},
		{"missing initial label", `{"op":"classify_retrospective","message":"hi","final_response":"done","initial_confidence":0.5,"credential":"k"}`, "invalid_initial_label"},
		{"confidence above one", `{"op":"classify_retrospective","message":"hi","final_response":"done","initial_label":"chat","initial_confidence":2,"credential":"k"}`, "invalid_initial_confidence"},
		{"confidence below zero", `{"op":"classify_retrospective","message":"hi","final_response":"done","initial_label":"chat","initial_confidence":-1,"credential":"k"}`, "invalid_initial_confidence"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			if code := run(strings.NewReader(test.input), &stdout, &stderr); code != 0 {
				t.Fatalf("exit = %d, stderr = %q", code, stderr.String())
			}
			if stderr.String() != "" {
				t.Fatalf("stderr = %q, want quiet fail-closed result", stderr.String())
			}
			var result jevnotices.RetrospectiveResult
			if err := json.Unmarshal(stdout.Bytes(), &result); err != nil {
				t.Fatalf("stdout is not a retrospective result: %q: %v", stdout.String(), err)
			}
			if result.Label != "unknown" || result.Comparison != "unavailable" || result.Recover || result.Error != test.want {
				t.Fatalf("result = %+v, want error %q", result, test.want)
			}
		})
	}
}

// TestRunRetrospectiveResultIsOneJSONLine pins the stdout contract the gateway
// bridge parses: exactly one newline-terminated JSON object carrying the
// verdict fields, even for the fail-closed path.
func TestRunRetrospectiveResultIsOneJSONLine(t *testing.T) {
	input := `{"op":"classify_retrospective","message":"fix it","final_response":"done","initial_label":"task","initial_confidence":0.9}`
	var stdout, stderr bytes.Buffer
	if code := run(strings.NewReader(input), &stdout, &stderr); code != 0 {
		t.Fatalf("exit = %d, stderr = %q", code, stderr.String())
	}
	out := stdout.String()
	if strings.Count(out, "\n") != 1 || !strings.HasSuffix(out, "\n") {
		t.Fatalf("want exactly one newline-terminated line, got %q", out)
	}
	var fields map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &fields); err != nil {
		t.Fatalf("stdout is not one JSON object: %v", err)
	}
	for _, key := range []string{"label", "confidence", "comparison", "recover", "notice", "error"} {
		if _, ok := fields[key]; !ok {
			t.Errorf("fail-closed result omits %q: %v", key, fields)
		}
	}
}

func TestRunRetrospectiveRejectsWrongFieldTypes(t *testing.T) {
	inputs := []string{
		`{"op":"classify_retrospective","message":"hi","initial_confidence":"high"}`,
		`{"op":"classify_retrospective","initial_label":4}`,
	}
	for _, input := range inputs {
		var stdout, stderr bytes.Buffer
		if code := run(strings.NewReader(input), &stdout, &stderr); code != 2 {
			t.Errorf("input %s: exit = %d, want 2", input, code)
		}
		if stdout.String() != "" {
			t.Errorf("input %s: wrote %q to stdout on failure", input, stdout.String())
		}
		if !strings.Contains(stderr.String(), "unreadable retrospective request") {
			t.Errorf("input %s: stderr %q lacks the diagnostic", input, stderr.String())
		}
	}
}

func TestRunRejectsUnsupportedOperation(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run(strings.NewReader(`{"op":"delete_everything"}`), &stdout, &stderr); code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
	if stdout.String() != "" {
		t.Fatalf("wrote %q to stdout on failure", stdout.String())
	}
	if !strings.Contains(stderr.String(), "unsupported operation") {
		t.Fatalf("stderr %q lacks the diagnostic", stderr.String())
	}
}

func TestRunRejectsOversizedInput(t *testing.T) {
	input := `{"op":"classify_retrospective","message":"` + strings.Repeat("m", 8<<20) + `"}`
	var stdout, stderr bytes.Buffer
	if code := run(strings.NewReader(input), &stdout, &stderr); code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
	if stdout.String() != "" {
		t.Fatalf("wrote %d bytes to stdout on failure", stdout.Len())
	}
	if !strings.Contains(stderr.String(), "unreadable record") {
		t.Fatalf("stderr %q lacks the diagnostic", stderr.String())
	}
}

func TestRunRetrospectiveUnknownInitialLabelsDoNotReachTheNetwork(t *testing.T) {
	// A valid envelope whose fields fail validation must produce a fail-closed
	// result without any Decisions call; the credential here is deliberately
	// present so only the validation guard can stop the request.
	input := `{"op":"classify_retrospective","message":"hi","final_response":"done","initial_label":"UNKNOWN-ish","initial_confidence":0.5,"credential":"k"}`
	var stdout, stderr bytes.Buffer
	if code := run(strings.NewReader(input), &stdout, &stderr); code != 0 {
		t.Fatalf("exit = %d, stderr = %q", code, stderr.String())
	}
	var result jevnotices.RetrospectiveResult
	if err := json.Unmarshal(stdout.Bytes(), &result); err != nil {
		t.Fatalf("stdout is not a retrospective result: %v", err)
	}
	if result.Error != "invalid_initial_label" {
		t.Fatalf("result = %+v", result)
	}
}
