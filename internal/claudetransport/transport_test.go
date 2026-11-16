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

// TestTerminalStatusAcrossExistingStatuses walks the existing-status axis:
// every non-interrupted/timeout status keeps itself when no transport
// signal exists, and every one of them is named api_error when the prose
// names a transport death.
func TestTerminalStatusAcrossExistingStatuses(t *testing.T) {
	quiet := "exit 2 after a bad flag"
	deadly := "read tcp: ECONNRESET"
	for _, existing := range []string{
		"error", "failed", "unknown", "cancelled", "completed", "",
	} {
		if got := TerminalStatus(existing, "", quiet); got != existing {
			t.Errorf("quiet text: TerminalStatus(%q) = %q, want unchanged", existing, got)
		}
		if got := TerminalStatus(existing, "", deadly); got != "api_error" {
			t.Errorf("deadly text: TerminalStatus(%q) = %q, want api_error", existing, got)
		}
	}
}

// TestTerminalStatusReasonNormalization pins how the structured reason is
// normalized before comparison: case and surrounding whitespace never
// matter, near-miss spellings never match.
func TestTerminalStatusReasonNormalization(t *testing.T) {
	tests := []struct {
		name   string
		reason string
		want   string
	}{
		{"exact", "api_error", "api_error"},
		{"upper", "API_ERROR", "api_error"},
		{"mixed", "Api_Error", "api_error"},
		{"leading space", " api_error", "api_error"},
		{"trailing newline", "api_error\n", "api_error"},
		{"surrounded by tabs", "\tapi_error\t", "api_error"},
		{"hyphen variant is not the token", "api-error", "error"},
		{"plural is not the token", "api_errors", "error"},
		{"empty stays empty", "", "error"},
		{"other provider reason passes through", "usage_limit", "error"},
		{"canceled-by-user reason passes through", "canceled_by_user", "error"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := TerminalStatus("error", tt.reason)
			if got != tt.want {
				t.Errorf("TerminalStatus(%q) = %q, want %q", tt.reason, got, tt.want)
			}
		})
	}
}

// TestTerminalStatusProseNeedleEdges pins the prose matcher's boundaries:
// near-miss wording that a substring matcher could over- or under-fire on.
func TestTerminalStatusProseNeedleEdges(t *testing.T) {
	tests := []struct {
		name string
		text string
		want string
	}{
		{"errno 104 python exception", "ConnectionResetError: (Errno 104) Connection reset by peer", "api_error"},
		{"needle at very start", "connection closed before the first turn", "api_error"},
		{"needle at very end", "stream died: connection closed", "api_error"},
		{"needle alone", "connection reset", "api_error"},
		{"reversed wording does not match", "reset connection after failure", "error"},
		{"closed without connection prefix does not match", "the socket was closed by the user", "error"},
		{"interposed word breaks the needle", "connection was closed mid-stream", "error"},
		{"different errno does not match", "ECONNREFUSED while dialing", "error"},
		{"econnreset inside a longer token still matches", "xECONNRESETx", "api_error"},
		{"timeout wording is not transport prose", "request timed out after 30s", "error"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := TerminalStatus("error", "", tt.text)
			if got != tt.want {
				t.Errorf("TerminalStatus(%q) = %q, want %q", tt.text, got, tt.want)
			}
		})
	}
}

// TestTerminalStatusPythonPipelineScenarios mirrors the exact scenarios
// the Python contract suite pins, one row each, so either copy drifting
// shows up against this shared table.
func TestTerminalStatusPythonPipelineScenarios(t *testing.T) {
	tests := []struct {
		name           string
		existing       string
		terminalReason string
		texts          []string
		want           string
	}{
		{
			name:     "nonzero exit whose log says ECONNRESET",
			existing: "error",
			texts:    []string{"{\"result\": \"read tcp: ECONNRESET\"}", "Claude Code exited with code 1"},
			want:     "api_error",
		},
		{
			name:           "result event terminal_reason api_error with closed text",
			existing:       "error",
			terminalReason: "api_error",
			texts:          []string{"connection closed", "Claude Code exited with code 1"},
			want:           "api_error",
		},
		{
			name:     "clean success result text",
			existing: "error",
			texts:    []string{"done", ""},
			want:     "error",
		},
		{
			name:     "watchdog timeout token with closed-connection tail",
			existing: "timeout",
			texts:    []string{"connection closed"},
			want:     "timeout",
		},
		{
			name:     "watchdog interrupted token with closed-connection tail",
			existing: "interrupted",
			texts:    []string{"connection closed"},
			want:     "interrupted",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := TerminalStatus(tt.existing, tt.terminalReason, tt.texts...)
			if got != tt.want {
				t.Errorf("TerminalStatus = %q, want %q", got, tt.want)
			}
		})
	}
}

// TestNoticeLogLinePathShapes pins that a recorded path is echoed
// verbatim whatever its shape — the notice must point at the exact file
// spawn saved, never a normalized rewrite of it.
func TestNoticeLogLinePathShapes(t *testing.T) {
	tests := []struct {
		name      string
		savedPath string
		wantLine  string
		wantFound bool
	}{
		{
			name:      "relative path echoed verbatim",
			savedPath: "claude-runs/20261116-000001-42.jsonl",
			wantLine:  "Log: claude-runs/20261116-000001-42.jsonl",
			wantFound: true,
		},
		{
			name:      "windows-style path echoed verbatim",
			savedPath: `C:\hermes\claude-runs\run.jsonl`,
			wantLine:  `Log: C:\hermes\claude-runs\run.jsonl`,
			wantFound: true,
		},
		{
			name:      "path with interior spaces echoed verbatim",
			savedPath: "/tmp/my runs/sept closed.jsonl",
			wantLine:  "Log: /tmp/my runs/sept closed.jsonl",
			wantFound: true,
		},
		{
			name:      "single-character path is still a recorded path",
			savedPath: "0",
			wantLine:  "Log: 0",
			wantFound: true,
		},
		{
			name:      "newline-only path reports no log",
			savedPath: "\n",
			wantLine:  "no log was recorded",
			wantFound: false,
		},
		{
			name:      "tab-only path reports no log",
			savedPath: "\t",
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

// TestTerminalStatusNoTextsOptions exercises the variadic edge directly:
// zero texts and nil-slice texts behave identically.
func TestTerminalStatusNoTextsOptions(t *testing.T) {
	if got := TerminalStatus("error", ""); got != "error" {
		t.Errorf("zero texts: got %q, want error", got)
	}
	var none []string
	if got := TerminalStatus("error", "", none...); got != "error" {
		t.Errorf("nil texts: got %q, want error", got)
	}
	if got := TerminalStatus("interrupted", "api_error", none...); got != "interrupted" {
		t.Errorf("nil texts with interrupted existing: got %q, want interrupted", got)
	}
}

// TestTerminalStatusNeedleEverywhere cross-pins the three needles against
// signal position and casing: whichever needle, whichever casing, in
// whichever text slot the run's artifacts put it, the run is named
// api_error.
func TestTerminalStatusNeedleEverywhere(t *testing.T) {
	casings := []struct {
		name      string
		transform func(string) string
	}{
		{"asis", func(s string) string { return s }},
		{"upper", strings.ToUpper},
	}
	wrappers := []struct {
		name string
		wrap func(string) string
	}{
		{"bare", func(s string) string { return s }},
		{"prose-embedded", func(s string) string { return "the run died: " + s + " (turn 3)" }},
	}
	for _, needle := range transportDeathNeedles {
		for _, casing := range casings {
			for _, wrapper := range wrappers {
				name := needle + "/" + wrapper.name + "/" + casing.name
				t.Run(name, func(t *testing.T) {
					signal := wrapper.wrap(casing.transform(needle))
					for slot := range 3 {
						texts := []string{"result text", "log tail", "error line"}
						texts[slot] = signal
						if got := TerminalStatus("error", "", texts...); got != "api_error" {
							t.Errorf("needle %q in slot %d = %q, want api_error",
								signal, slot, got)
						}
					}
				})
			}
		}
	}
}

// TestTerminalStatusPunctuationAndSpacingEdges pins exact-substring
// semantics for the connection-closed needle family: punctuation around
// the needle still matches, spacing inside it never does.
func TestTerminalStatusPunctuationAndSpacingEdges(t *testing.T) {
	tests := []struct {
		name string
		text string
		want string
	}{
		{"trailing period still matches", "stream ended connection closed.", "api_error"},
		{"parenthesized still matches", "upstream reported (connection reset) mid-turn", "api_error"},
		{"newline between words does not match", "connection\nclosed", "error"},
		{"double space does not match", "connection  closed", "error"},
		{"missing space does not match", "connectionclosed", "error"},
		{"hyphen between words does not match", "connection-closed", "error"},
		{"both needles in one line matches once", "read ECONNRESET, then connection reset by peer", "api_error"},
		{"unicode prose around needle matches", "上游连接被重置: ECONNRESET, retry later", "api_error"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := TerminalStatus("error", "", tt.text)
			if got != tt.want {
				t.Errorf("TerminalStatus(%q) = %q, want %q", tt.text, got, tt.want)
			}
		})
	}
}

// TestTerminalStatusNeedleBuriedDeep proves the matcher is a substring
// scan of the whole text, not a prefix or tail check: a needle at the far
// end of a long log still names the death.
func TestTerminalStatusNeedleBuriedDeep(t *testing.T) {
	filler := strings.Repeat("ordinary log line without trouble\n", 500)
	log := filler + "connection reset"
	if got := TerminalStatus("error", "", log); got != "api_error" {
		t.Errorf("needle buried at end of long log = %q, want api_error", got)
	}
	if got := TerminalStatus("error", "", filler); got != "error" {
		t.Errorf("long quiet log = %q, want error", got)
	}
}

// TestNoticeLogLinePrefixInvariants pins the two notice shapes as a
// cross-invariant: recorded lines all start with "Log: " and none of them
// ever carries the no-log sentence, while the no-log line never carries
// the prefix.
func TestNoticeLogLinePrefixInvariants(t *testing.T) {
	recorded := []string{
		"/tmp/a.jsonl",
		" /tmp/b.jsonl ",
		"claude-runs/run.jsonl",
	}
	for _, path := range recorded {
		line, ok := NoticeLogLine(path)
		if !ok {
			t.Errorf("path %q should count as recorded", path)
		}
		if !strings.HasPrefix(line, "Log: ") {
			t.Errorf("recorded line %q must start with 'Log: '", line)
		}
		if strings.Contains(line, "no log was recorded") {
			t.Errorf("recorded line %q must not claim no-log", line)
		}
	}
	line, ok := NoticeLogLine("")
	if ok || line != "no log was recorded" {
		t.Errorf("empty path = (%q, %v), want (no log was recorded, false)", line, ok)
	}
	if strings.HasPrefix(line, "Log: ") {
		t.Errorf("no-log line %q must not carry the Log: prefix", line)
	}
}
