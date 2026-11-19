package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/QuixThe2nd/hermes-ide/internal/sparkscron"
)

// TestRunRoundTrip drives the real CLI entry with real stdin payloads and
// decodes the stdout verdict: the exact bytes the Python bridge exchanges.
func TestRunRoundTrip(t *testing.T) {
	tests := []struct {
		name       string
		stdin      string
		wantExit   int
		wantAction string
		wantReason string
	}{
		{
			name:       "create verdict carries the full spec",
			stdin:      `{"job_names":["other"],"seeded":false,"sparks_cron":true,"prereq_ok":true}`,
			wantExit:   0,
			wantAction: "create",
		},
		{
			name:       "skip verdict carries a reason",
			stdin:      `{"job_names":["inbox-sparks"],"seeded":false,"sparks_cron":true,"prereq_ok":true}`,
			wantExit:   0,
			wantAction: "skip",
			wantReason: "job-exists",
		},
		{
			name:       "plugin_disabled in the request skips with that reason",
			stdin:      `{"job_names":[],"seeded":false,"plugin_disabled":true,"sparks_cron":true,"prereq_ok":true}`,
			wantExit:   0,
			wantAction: "skip",
			wantReason: "plugin-disabled",
		},
		{
			name:       "unknown JSON fields are ignored",
			stdin:      `{"job_names":[],"seeded":false,"sparks_cron":true,"prereq_ok":true,"future_field":{"a":1}}`,
			wantExit:   0,
			wantAction: "create",
		},
		{
			name:     "absent fields default to false (no prereq → skip)",
			stdin:    `{}`,
			wantExit: 0, wantAction: "skip", wantReason: "prerequisite-missing",
		},
		{
			name:     "malformed JSON is exit 2, never a create",
			stdin:    `{"job_names":`,
			wantExit: 2,
		},
		{
			name:     "non-object JSON is exit 2",
			stdin:    `["inbox-sparks"]`,
			wantExit: 2,
		},
		{
			name:     "empty input is exit 2",
			stdin:    ``,
			wantExit: 2,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var out, errOut bytes.Buffer
			code := run(strings.NewReader(tt.stdin), &out, &errOut)
			if code != tt.wantExit {
				t.Fatalf("run() exit = %d, want %d (stderr: %s)", code, tt.wantExit, errOut.String())
			}
			if tt.wantExit != 0 {
				return
			}
			var verdict struct {
				Action string           `json:"action"`
				Reason string           `json:"reason"`
				Job    *sparkscron.Spec `json:"job"`
			}
			if err := json.Unmarshal(out.Bytes(), &verdict); err != nil {
				t.Fatalf("stdout is not JSON (%q): %v", out.String(), err)
			}
			if verdict.Action != tt.wantAction {
				t.Fatalf("action = %q, want %q", verdict.Action, tt.wantAction)
			}
			if tt.wantReason != "" && verdict.Reason != tt.wantReason {
				t.Fatalf("reason = %q, want %q", verdict.Reason, tt.wantReason)
			}
			if tt.wantAction == "create" {
				if verdict.Job == nil {
					t.Fatalf("create verdict must carry job, got stdout %q", out.String())
				}
				if verdict.Job.Name != "inbox-sparks" || verdict.Job.Schedule != "every 120m" {
					t.Fatalf("job spec wrong: %+v", verdict.Job)
				}
				if err := verdict.Job.Validate(); err != nil {
					t.Fatalf("job spec invalid: %v", err)
				}
			}
		})
	}
}

// TestStdoutIsSingleLine: the bridge reads exactly one JSON document per
// invocation; trailing junk (logs, warnings) would break json.loads.
func TestStdoutIsSingleLine(t *testing.T) {
	var out, errOut bytes.Buffer
	code := run(strings.NewReader(`{"job_names":[],"sparks_cron":true,"prereq_ok":true}`), &out, &errOut)
	if code != 0 {
		t.Fatalf("exit = %d, stderr %q", code, errOut.String())
	}
	if n := strings.Count(strings.TrimRight(out.String(), "\n"), "\n"); n != 0 {
		t.Fatalf("stdout must be one line, got %d newlines: %q", n, out.String())
	}
	if !strings.HasSuffix(out.String(), "\n") {
		t.Fatalf("stdout must end with a newline: %q", out.String())
	}
}
