package claudetransport

import (
	"strings"
	"testing"
)

// TestTerminalStatus walks the classifier contract: structured api_error
// reasons, transport-death prose in every casing, the interrupted/timeout
// veto, and generic/success texts staying exactly as they came.
func TestTerminalStatus(t *testing.T) {
	tests := []struct {
		name           string
		existing       string
		terminalReason string
		texts          []string
		want           string
	}{
		{
			name:           "structured api_error reason names api_error",
			existing:       "error",
			terminalReason: "api_error",
			want:           "api_error",
		},
		{
			name:           "reason matches with padding and mixed case",
			existing:       "error",
			terminalReason: "  API_ERROR ",
			want:           "api_error",
		},
		{
			name:           "other structured reasons are not api_error",
			existing:       "error",
			terminalReason: "usage_limit",
			want:           "error",
		},
		{
			name:     "ECONNRESET in log text names api_error",
			existing: "error",
			texts:    []string{"read tcp 10.0.0.1:443->10.0.0.2:99: read: connection reset by peer"},
			want:     "api_error",
		},
		{
			name:     "bare econnreset token matches",
			existing: "error",
			texts:    []string{"ECONNRESET"},
			want:     "api_error",
		},
		{
			name:     "connection reset prose matches",
			existing: "error",
			texts:    []string{"upstream said connection reset while streaming"},
			want:     "api_error",
		},
		{
			name:     "connection closed prose matches",
			existing: "error",
			texts:    []string{"the stream ended: Connection Closed"},
			want:     "api_error",
		},
		{
			name:     "needle found in a later text still matches",
			existing: "error",
			texts:    []string{"exit code 1", "", "retrying after CONNECTION CLOSED"},
			want:     "api_error",
		},
		{
			name:     "shouted prose matches case-insensitively",
			existing: "error",
			texts:    []string{"FATAL: ECONNRESET, CONNECTION RESET, CONNECTION CLOSED"},
			want:     "api_error",
		},
		{
			name:     "generic failure text keeps the generic error",
			existing: "error",
			texts:    []string{"exit 2 after a bad flag"},
			want:     "error",
		},
		{
			name:     "clean success text keeps completed",
			existing: "completed",
			texts:    []string{"all tests pass"},
			want:     "completed",
		},
		{
			name:     "no texts at all keeps the existing status",
			existing: "error",
			want:     "error",
		},
		{
			name:           "interrupted verdict survives an api_error reason",
			existing:       "interrupted",
			terminalReason: "api_error",
			texts:          []string{"connection closed"},
			want:           "interrupted",
		},
		{
			name:           "timeout verdict survives transport prose",
			existing:       "timeout",
			terminalReason: "api_error",
			texts:          []string{"read tcp: ECONNRESET"},
			want:           "timeout",
		},
		{
			name:     "interrupted verdict with no other signal stays interrupted",
			existing: "interrupted",
			want:     "interrupted",
		},
		{
			name:     "timeout verdict with no other signal stays timeout",
			existing: "timeout",
			want:     "timeout",
		},
		{
			name:           "empty reason is not api_error",
			existing:       "error",
			terminalReason: "",
			want:           "error",
		},
		{
			name:     "whitespace-only text does not match anything",
			existing: "error",
			texts:    []string{"   ", "\n\t"},
			want:     "error",
		},
		{
			name:     "needle substring inside another word does not false-match closed",
			existing: "error",
			texts:    []string{"the door closed quietly"},
			want:     "error",
		},
		{
			name:     "needle split across texts is not a match",
			existing: "error",
			texts:    []string{"connection", "reset"},
			want:     "error",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := TerminalStatus(tt.existing, tt.terminalReason, tt.texts...)
			if got != tt.want {
				t.Errorf("TerminalStatus(%q, %q, %v) = %q, want %q",
					tt.existing, tt.terminalReason, tt.texts, got, tt.want)
			}
		})
	}
}

// TestEveryNeedleMatches pins the needle list itself: each needle must
// fire both embedded in prose and fully uppercased, proving the match is
// a case-insensitive substring test.
func TestEveryNeedleMatches(t *testing.T) {
	for _, needle := range transportDeathNeedles {
		t.Run(needle, func(t *testing.T) {
			embedded := "upstream said " + needle + " while streaming"
			if got := TerminalStatus("error", "", embedded); got != "api_error" {
				t.Errorf("embedded needle %q did not match %q (got %q)", needle, embedded, got)
			}
			shouted := strings.ToUpper("prefix " + needle + " suffix")
			if got := TerminalStatus("error", "", shouted); got != "api_error" {
				t.Errorf("uppercased needle %q did not match %q (got %q)", needle, shouted, got)
			}
		})
	}
}

// TestNoticeLogLine pins the notice pointer: a recorded path is echoed
// exactly, an absent path says so, and nothing is ever invented.
func TestNoticeLogLine(t *testing.T) {
	tests := []struct {
		name      string
		savedPath string
		wantLine  string
		wantFound bool
	}{
		{
			name:      "recorded path is echoed exactly",
			savedPath: "/tmp/sept-closed.jsonl",
			wantLine:  "Log: /tmp/sept-closed.jsonl",
			wantFound: true,
		},
		{
			name:      "surrounding whitespace is trimmed, path kept verbatim",
			savedPath: "  /tmp/spawn-saved.jsonl\n",
			wantLine:  "Log: /tmp/spawn-saved.jsonl",
			wantFound: true,
		},
		{
			name:      "empty path reports no log",
			savedPath: "",
			wantLine:  "no log was recorded",
			wantFound: false,
		},
		{
			name:      "whitespace-only path reports no log",
			savedPath: "   ",
			wantLine:  "no log was recorded",
			wantFound: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			line, recorded := NoticeLogLine(tt.savedPath)
			if line != tt.wantLine || recorded != tt.wantFound {
				t.Errorf("NoticeLogLine(%q) = (%q, %v), want (%q, %v)",
					tt.savedPath, line, recorded, tt.wantLine, tt.wantFound)
			}
		})
	}
}

// TestNoticeLogLineNeverInventsPath asserts the failure branch never
// carries a path-looking token: the whole point of "no log was recorded"
// is that recovery has nothing to point at.
func TestNoticeLogLineNeverInventsPath(t *testing.T) {
	line, recorded := NoticeLogLine("")
	if recorded {
		t.Error("empty saved path must not report a recorded log")
	}
	if strings.Contains(line, "/") || strings.Contains(line, ".jsonl") {
		t.Errorf("no-log line invented a path: %q", line)
	}
}
