package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// TestRunExecutesSharedTable drives the compiled command form over the same
// shared table the package tests and the Python bridge contract test use:
// one JSON record on stdin, one JSON decision on stdout, exit 0.
func TestRunExecutesSharedTable(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", "provision_cases.json"))
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		Cases []struct {
			Name   string         `json:"name"`
			Record map[string]any `json:"record"`
			Expect map[string]any `json:"expect"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	for _, tc := range table.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			record, _ := json.Marshal(tc.Record)
			var stdout, stderr bytes.Buffer
			code := run(bytes.NewReader(record), &stdout, &stderr)
			if code != 0 {
				t.Fatalf("exit %d, stderr: %s", code, stderr.String())
			}
			var got map[string]any
			if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
				t.Fatalf("stdout is not one JSON decision: %v (%q)", err, stdout.String())
			}
			if !reflect.DeepEqual(got, tc.Expect) {
				t.Errorf("decision mismatch\n got: %#v\nwant: %#v", got, tc.Expect)
			}
		})
	}
}

// TestRunRejectsBadRecords: undecodable input and unknown ops are exit 2
// with a stderr line — never a zero-exit wrong answer.
func TestRunRejectsBadRecords(t *testing.T) {
	for _, tc := range []struct {
		name   string
		stdin  string
		stderr string
	}{
		{"junk", "not json", "unreadable record"},
		{"empty", "", "unreadable record"},
		{"unknown op", `{"op":"delete"}`, "unknown op"},
		{"missing op", `{"name":"researcher"}`, "unknown op"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			code := run(strings.NewReader(tc.stdin), &stdout, &stderr)
			if code != 2 {
				t.Errorf("exit = %d, want 2", code)
			}
			if !strings.Contains(stderr.String(), tc.stderr) {
				t.Errorf("stderr = %q, want it to contain %q", stderr.String(), tc.stderr)
			}
			if stdout.Len() != 0 {
				t.Errorf("stdout must stay empty on rejection, got %q", stdout.String())
			}
		})
	}
}

// TestExtraFieldsIgnored: the Python side may pass a superset of the record
// (forward-compatible protocol); unknown fields must not change the decision.
func TestExtraFieldsIgnored(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run(
		strings.NewReader(`{"op":"plan","name":"researcher","exists":false,"active_profile":"default","active_is_named":false,"future_field":{"nested":true}}`),
		&stdout, &stderr,
	)
	if code != 0 {
		t.Fatalf("exit %d, stderr: %s", code, stderr.String())
	}
	var got map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout is not JSON: %v", err)
	}
	if got["action"] != "create" || got["clone_config"] != true {
		t.Errorf("decision = %v, want create with clone_config", got)
	}
}
