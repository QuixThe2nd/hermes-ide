package main

import (
	"bytes"
	"strings"
	"testing"
)

func runRecord(t *testing.T, stdin string) (string, string, int) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := run(strings.NewReader(stdin), &stdout, &stderr)
	return stdout.String(), stderr.String(), code
}

// TestRunRendersOneLinePerRecord pins the stdin/stdout contract the gateway
// bridge relies on: one JSON record in, exactly one newline-terminated line out.
func TestRunRendersOneLinePerRecord(t *testing.T) {
	out, errOut, code := runRecord(t, `{"purpose":"response-gate","subject":"user message",`+
		`"author":"Alice","allowed":false,"scores":{"noise":0.75}}`)
	if code != 0 || errOut != "" {
		t.Fatalf("run exit=%d stderr=%q", code, errOut)
	}
	if !strings.HasPrefix(out, "🔎 jev response-gate user message from Alice: deny (") {
		t.Fatalf("unexpected line %q", out)
	}
	if strings.Count(out, "\n") != 1 || !strings.HasSuffix(out, "\n") {
		t.Fatalf("want exactly one newline-terminated line, got %q", out)
	}
}

// TestRunRejectsAnUnreadableRecord keeps the failure exit code distinct from
// a rendered line: the bridge treats a non-zero exit as "post nothing".
func TestRunRejectsAnUnreadableRecord(t *testing.T) {
	for _, stdin := range []string{"", "not json", "[1,2]", `{"purpose":`} {
		out, errOut, code := runRecord(t, stdin)
		if code != 2 {
			t.Errorf("stdin %q: exit=%d, want 2", stdin, code)
		}
		if out != "" {
			t.Errorf("stdin %q: wrote %q to stdout on failure", stdin, out)
		}
		if !strings.Contains(errOut, "unreadable record") {
			t.Errorf("stdin %q: stderr %q lacks the diagnostic", stdin, errOut)
		}
	}
}

// TestRunSilencedRecordWritesNothing: a record carrying notices=false (the
// owning profile's display.jev_notices) exits 0 with empty stdout, which the
// bridge reads as "no notice" — never an error and never a partial line.
func TestRunSilencedRecordWritesNothing(t *testing.T) {
	for _, value := range []string{"false", `"off"`, `"no"`, "0"} {
		out, errOut, code := runRecord(t, `{"purpose":"reaction-gate","subject":"user message",`+
			`"emoji":"🔥","notices":`+value+`}`)
		if code != 0 || out != "" || errOut != "" {
			t.Errorf("notices=%s: exit=%d stdout=%q stderr=%q, want silent success", value, code, out, errOut)
		}
	}
	out, _, code := runRecord(t, `{"purpose":"reaction-gate","subject":"user message","emoji":"🔥","notices":true}`)
	if code != 0 || !strings.HasPrefix(out, "🔎 jev reaction-gate ") {
		t.Fatalf("notices=true: exit=%d stdout=%q, want the rendered line", code, out)
	}
}
