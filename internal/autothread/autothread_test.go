package autothread

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// classificationCase is one row of testdata/classification_cases.json — the
// contract the Python bridge's callers rely on. The Python test drives the
// same rows through the real cmd/autothread binary, so both sides move
// together or both fail.
type classificationCase struct {
	Name     string   `json:"name"`
	Status   int      `json:"status"`
	Code     int      `json:"code"`
	Message  string   `json:"message"`
	Decision Decision `json:"decision"`
}

func loadClassificationCases(t *testing.T) []classificationCase {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "classification_cases.json"))
	if err != nil {
		t.Fatalf("read table: %v", err)
	}
	var table struct {
		Cases []classificationCase `json:"cases"`
	}
	if err := json.Unmarshal(data, &table); err != nil {
		t.Fatalf("parse table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("table is empty")
	}
	return table.Cases
}

// TestClassificationTable pins Classify against every shared-table row.
func TestClassificationTable(t *testing.T) {
	for _, tc := range loadClassificationCases(t) {
		t.Run(tc.Name, func(t *testing.T) {
			if got := Classify(tc.Status, tc.Code, tc.Message); got != tc.Decision {
				t.Errorf("Classify(%d, %d, %q) = %q, want %q",
					tc.Status, tc.Code, tc.Message, got, tc.Decision)
			}
		})
	}
}

// TestAdoptRequiresTheSpecificSignal guards the regression the package
// exists for: only code 160004 (or its canonical text) may answer Adopt.
// Neighboring 400s and partial text matches must never adopt — a false
// adopt resolves a thread that may not exist.
func TestAdoptRequiresTheSpecificSignal(t *testing.T) {
	cases := []struct {
		name    string
		status  int
		code    int
		message string
	}{
		{"off-by-one code", 400, 160003, "A thread has already been ... no"},
		{"off-by-one code up", 400, 160005, "Bad Request"},
		{"code without 400 status", 200, 160004, "odd but the code is authoritative"},
		{"partial text", 400, 0, "thread has already been"},
		{"past-tense text", 400, 0, "thread was already created"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := Classify(tc.status, tc.code, tc.message)
			if tc.code == ThreadAlreadyCreatedCode {
				if got != Adopt {
					t.Errorf("code %d must adopt regardless of status, got %q", tc.code, got)
				}
				return
			}
			if got == Adopt {
				t.Errorf("Classify(%d, %d, %q) = adopt; want a non-adopt decision",
					tc.status, tc.code, tc.message)
			}
		})
	}
}

// TestRateLimitSignals covers the 429 recognizers the table cannot express
// combinatorially: status alone, text alone, and a numeric mention buried in
// unrelated prose.
func TestRateLimitSignals(t *testing.T) {
	for _, tc := range []struct {
		name    string
		status  int
		message string
	}{
		{"status only", 429, "slow down"},
		{"text only", 0, "429 Too Many Requests"},
		{"case-insensitive text", 0, "TOO MANY REQUESTS"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := Classify(tc.status, 0, tc.message); got != RateLimited {
				t.Errorf("Classify(%d, 0, %q) = %q, want %q", tc.status, tc.message, got, RateLimited)
			}
		})
	}
}

// TestTransientMarkers walks every marker and status the package claims to
// recognize so a dropped entry fails loudly instead of silently downgrading
// a retry to a fail.
func TestTransientMarkers(t *testing.T) {
	for _, marker := range transientMarkers {
		t.Run("marker "+marker, func(t *testing.T) {
			if got := Classify(0, 0, "upstream said: "+marker); got != Retry {
				t.Errorf("marker %q not recognized: got %q, want %q", marker, got, Retry)
			}
		})
	}
	for status := range transientStatuses {
		t.Run("status", func(t *testing.T) {
			if got := Classify(status, 0, "server error"); got != Retry {
				t.Errorf("status %d: got %q, want %q", status, got, Retry)
			}
		})
	}
}

// TestPrecedence documents the decision order: Adopt beats RateLimited beats
// Retry, so a payload carrying several signals resolves to the safest one.
func TestPrecedence(t *testing.T) {
	if got := Classify(429, ThreadAlreadyCreatedCode, "too many requests"); got != Adopt {
		t.Errorf("160004 + 429 = %q, want adopt (the thread exists)", got)
	}
	if got := Classify(429, 0, "too many requests, connection reset"); got != RateLimited {
		t.Errorf("429 + reset = %q, want rate_limited (wait beats blind retry)", got)
	}
}

// TestDecisionWireValues pins the exact strings the JSON wire protocol and
// the Python bridge's validation set rely on. Renaming a constant's value
// breaks the bridge silently unless this fails.
func TestDecisionWireValues(t *testing.T) {
	want := map[Decision]string{
		Adopt:       "adopt",
		RateLimited: "rate_limited",
		Retry:       "retry",
		Fail:        "fail",
	}
	for decision, wire := range want {
		if string(decision) != wire {
			t.Errorf("Decision wire value = %q, want %q", string(decision), wire)
		}
	}
	if got := Decisions(); len(got) != len(want) {
		t.Errorf("Decisions() = %v, want %d entries", got, len(want))
	}
	for _, decision := range Decisions() {
		if _, ok := want[decision]; !ok {
			t.Errorf("Decisions() contains unpinned value %q", decision)
		}
	}
}

// TestTableCoversEveryDecision is a relationship guard: the shared table
// must exercise every decision at least once, on both the Go and the Python
// side. A class that disappears from the table is a dead path neither test
// suite would notice.
func TestTableCoversEveryDecision(t *testing.T) {
	seen := map[Decision]int{}
	for _, tc := range loadClassificationCases(t) {
		seen[tc.Decision]++
	}
	for _, decision := range Decisions() {
		if seen[decision] == 0 {
			t.Errorf("shared table has no %q case", decision)
		}
	}
}

// TestTableNamesUnique keeps failure output addressable: both suites cite
// rows by name, so duplicates would make a failing row ambiguous.
func TestTableNamesUnique(t *testing.T) {
	seen := map[string]bool{}
	for _, tc := range loadClassificationCases(t) {
		if seen[tc.Name] {
			t.Errorf("duplicate case name %q", tc.Name)
		}
		seen[tc.Name] = true
	}
}

// TestClassifyNeverPanics feeds adversarial shapes: the input is a
// best-effort extract from a duck-typed exception, so nothing the Python
// side can plausibly send may panic or misclassify into adopt.
func TestClassifyNeverPanics(t *testing.T) {
	inputs := []struct {
		status  int
		code    int
		message string
	}{
		{-1, -1, ""},
		{0, 0, "\x00\x01\x02"},
		{1 << 30, 1 << 30, "huge numbers"},
		{400, 1600040, "code with a trailing zero is not 160004"},
		{400, 0, "fractional codes cannot exist, but zero is safe"},
		{4290, 0, "status containing 429 digits is not a 429"},
		{0, 0, "日本語のエラーメッセージ"},
		{0, 0, string(rune(0xFFFD)) + "invalid utf8 \xff\xfe"},
	}
	for _, in := range inputs {
		got := Classify(in.status, in.code, in.message)
		if got == Adopt {
			t.Errorf("Classify(%d, %d, %q) = adopt on an adversarial input",
				in.status, in.code, in.message)
		}
		switch got {
		case Adopt, RateLimited, Retry, Fail:
		default:
			t.Errorf("Classify(%d, %d, %q) = %q, not a valid Decision",
				in.status, in.code, in.message, got)
		}
	}
}

// TestAdoptIgnoresStatus is the load-bearing property behind the duck-typing
// contract: the JSON error code alone decides adoption, because re-wrapped
// or mocked errors can lose or mangle the HTTP status.
func TestAdoptForAnyStatusWithCode160004(t *testing.T) {
	for _, status := range []int{-1, 0, 200, 400, 403, 429, 500, 503} {
		if got := Classify(status, ThreadAlreadyCreatedCode, "anything"); got != Adopt {
			t.Errorf("Classify(%d, 160004, ...) = %q, want adopt", status, got)
		}
	}
}

// TestAdoptTextMatchIsCaseInsensitive pins the text fallback for errors
// that lost the numeric code entirely.
func TestAdoptTextMatchIsCaseInsensitive(t *testing.T) {
	for _, message := range []string{
		"A thread has already been created for this message",
		"A THREAD HAS ALREADY BEEN CREATED FOR THIS MESSAGE",
		"wrapped: a Thread Has Already Been Created for this message (id 42)",
	} {
		if got := Classify(0, 0, message); got != Adopt {
			t.Errorf("Classify(0, 0, %q) = %q, want adopt", message, got)
		}
	}
}

// TestDecisionsPrecedenceOrder pins the declaration order of Decisions() to
// the switch precedence in Classify: when several shapes apply at once,
// adoption wins over rate limiting (a 429 carrying code 160004 means the
// thread already exists — there is nothing to wait for).
func TestDecisionsPrecedenceOrder(t *testing.T) {
	want := []Decision{Adopt, RateLimited, Retry, Fail}
	got := Decisions()
	if len(got) != len(want) {
		t.Fatalf("Decisions() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("Decisions()[%d] = %q, want %q (order is the precedence)", i, got[i], want[i])
		}
	}
	if got := Classify(429, ThreadAlreadyCreatedCode, "x"); got != Adopt {
		t.Errorf("Classify(429, 160004, ...) = %q, want adopt (adoption beats waiting)", got)
	}
}

// TestRateLimitedKeysOnStatusAlone: any 429 without an adopt shape is
// rate_limited regardless of the numeric code or message text.
func TestRateLimitedKeysOnStatusAlone(t *testing.T) {
	for _, c := range []struct {
		code    int
		message string
	}{
		{0, ""},
		{50035, "invalid form body"},
		{-3, "weird negative code"},
	} {
		if got := Classify(429, c.code, c.message); got != RateLimited {
			t.Errorf("Classify(429, %d, %q) = %q, want rate_limited", c.code, c.message, got)
		}
	}
}

// TestFailIsTheDefault pins the boundary around the recognized classes: a
// status just outside the retry set and durable rejections carrying no
// adopt or rate-limit shape all land on Fail, never on a stronger verdict.
func TestFailIsTheDefault(t *testing.T) {
	for _, tc := range []struct {
		name    string
		status  int
		code    int
		message string
	}{
		{"missing access", 403, 50001, "Missing Access"},
		{"unknown message", 404, 0, "Unknown Message"},
		{"501 is not a retryable gateway status", 501, 0, "Not Implemented"},
		{"negative status with no signals", -1, 0, "boom"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := Classify(tc.status, tc.code, tc.message); got != Fail {
				t.Errorf("Classify(%d, %d, %q) = %q, want %q",
					tc.status, tc.code, tc.message, got, Fail)
			}
		})
	}
}

// TestRateLimitedTextBeatsTransientStatus: precedence is decided by the
// strongest signal anywhere in the payload, so rate-limit text wins even
// when the status alone would only be worth a retry.
func TestRateLimitedTextBeatsTransientStatus(t *testing.T) {
	if got := Classify(503, 0, "Too Many Requests"); got != RateLimited {
		t.Errorf("503 + rate-limit text = %q, want rate_limited (wait beats retry)", got)
	}
}
