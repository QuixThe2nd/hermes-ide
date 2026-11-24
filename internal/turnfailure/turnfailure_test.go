package turnfailure

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type sharedCase map[string]any

func loadSharedCases(t *testing.T) []sharedCase {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "turnfailure_cases.json"))
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var doc struct {
		Cases []sharedCase `json:"cases"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&doc); err != nil {
		t.Fatalf("decode shared table: %v", err)
	}
	return doc.Cases
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func num(t *testing.T, v any) json.Number {
	t.Helper()
	n, ok := v.(json.Number)
	if !ok {
		t.Fatalf("expected json number, got %T (%v)", v, v)
	}
	return n
}

func floatPtr(t *testing.T, v any) *float64 {
	t.Helper()
	n, ok := v.(json.Number)
	if !ok {
		return nil
	}
	f, err := n.Float64()
	if err != nil {
		t.Fatalf("bad float %v: %v", n, err)
	}
	return &f
}

// TestSharedTableMatchesPythonExpectations runs the same
// testdata/turnfailure_cases.json the Python contract test drives: every
// case's expect was computed by the real Python implementation, so a
// failure here means the Go twin drifted from the runtime contract.
func TestSharedTableMatchesPythonExpectations(t *testing.T) {
	for _, c := range loadSharedCases(t) {
		name := str(c["name"])
		t.Run(name, func(t *testing.T) {
			answer := goAnswer(t, c)
			if !jsonEqual(answer, c["expect"]) {
				t.Fatalf("go answer ≠ python expect\n go:   %#v\n want: %#v", answer, c["expect"])
			}
		})
	}
}

// goAnswer is the in-process form of cmd/turnfailure's dispatch.
func goAnswer(t *testing.T, c sharedCase) any {
	switch op := str(c["op"]); op {
	case "site_code":
		return map[string]any{"member": SiteFailureCodes[str(c["code"])]}
	case "exit_reason_failure":
		verdict := ExitReasonFailure(str(c["reason"]))
		if verdict == nil {
			return nil
		}
		return map[string]any{"reason": verdict.Reason, "retryable": verdict.Retryable, "fails_turn": verdict.FailsTurn}
	case "invalid_response_reason":
		return map[string]any{"reason": InvalidResponseFailureReason(c["code"])}
	case "failed_turn_notice":
		rows, _ := c["messages"].([]any)
		msgs := make([]TurnMessage, 0, len(rows))
		for _, r := range rows {
			m, _ := r.(map[string]any)
			calls, _ := m["tool_calls"].(bool)
			msgs = append(msgs, TurnMessage{Role: str(m["role"]), ToolCalls: calls})
		}
		return map[string]any{"notice": FailedTurnNoticeFor(msgs)}
	case "add_failed_turn_notice":
		return map[string]any{"response": AddFailedTurnNotice(str(c["response"]), str(c["notice"]))}
	case "close_failed_turn":
		return map[string]any{"append": CloseFailedTurnBoundary(str(c["tail_role"]))}
	case "interrupt_tool_reason":
		return map[string]any{"tool_reason": InterruptToolReason(str(c["reason"]))}
	case "site_copy":
		fields, _ := c["fields"].(map[string]any)
		copy, err := SiteCopy(str(c["code"]), fields)
		if err != nil {
			t.Fatalf("site_copy: %v", err)
		}
		return map[string]any{"copy": copy}
	case "exhausted_copy":
		return map[string]any{"copy": ExhaustedCopy(
			str(c["reason"]), str(c["label"]), num(t, c["attempts"]), str(c["summary"]),
			floatPtr(t, c["reset_seconds"]),
		)}
	case "failure_cause_gloss":
		subject, possessive := "it", "its"
		if v, ok := c["subject"]; ok {
			subject = str(v)
		}
		if v, ok := c["possessive"]; ok {
			possessive = str(v)
		}
		gloss, ok := FailureCauseGloss(str(c["reason"]), subject, possessive)
		if !ok {
			return map[string]any{"gloss": nil}
		}
		return map[string]any{"gloss": gloss}
	case "stamp_failure":
		retryable, _ := c["retryable"].(bool)
		return StampFailure(str(c["reason"]), retryable)
	default:
		t.Fatalf("unknown op %q", op)
		return nil
	}
}

// jsonEqual compares via canonical JSON so nil/bool/string/number/map shapes
// match regardless of Go map ordering.
func jsonEqual(a, b any) bool {
	ab, err := json.Marshal(canonical(a))
	if err != nil {
		return false
	}
	bb, err := json.Marshal(canonical(b))
	if err != nil {
		return false
	}
	return bytes.Equal(ab, bb)
}

func canonical(v any) any {
	switch t := v.(type) {
	case json.Number:
		return t.String()
	case map[string]any:
		for k, item := range t {
			t[k] = canonical(item)
		}
		return t
	case []any:
		for i, item := range t {
			t[i] = canonical(item)
		}
		return t
	default:
		return v
	}
}

// TestNoticeNeverClaimsUnprocessedWhenToolMayHaveRun is the core boundary
// invariant: any tool row or tool-calling assistant row flips the copy to
// the verify-before-resend wording.
func TestNoticeNeverClaimsUnprocessedWhenToolMayHaveRun(t *testing.T) {
	if got := FailedTurnNoticeFor([]TurnMessage{{Role: "user"}, {Role: "tool"}}); got != PartialFailedTurnNotice {
		t.Fatalf("tool row must yield partial wording, got %q", got)
	}
	if got := FailedTurnNoticeFor([]TurnMessage{{Role: "assistant", ToolCalls: true}}); got != PartialFailedTurnNotice {
		t.Fatalf("tool-calling assistant must yield partial wording, got %q", got)
	}
	if got := FailedTurnNoticeFor([]TurnMessage{{Role: "user"}, {Role: "user", ToolCalls: true}}); got != FailedTurnNotice {
		t.Fatalf("a user row with a stray tool_calls flag is not tool evidence, got %q", got)
	}
	if got := FailedTurnNoticeFor(nil); got != FailedTurnNotice {
		t.Fatalf("empty turn must yield the not-processed wording, got %q", got)
	}
}

// TestEverySiteCodeButExplainerWordedOnesHasCopy pins the relationship
// between the code set and the copy table: empty_response (turn explainers)
// and session_busy (the lease) are worded elsewhere by design.
func TestEverySiteCodeButExplainerWordedOnesHasCopy(t *testing.T) {
	table := SiteCopyTable()
	for code := range SiteFailureCodes {
		if code == "empty_response" || code == "session_busy" {
			if _, in := failureCodeCopy[code]; in {
				t.Fatalf("%s is worded by another surface; it must not carry failure-code copy", code)
			}
			continue
		}
		if _, ok := failureCodeCopy[code]; !ok {
			t.Fatalf("site code %s has no failure-code copy row", code)
		}
	}
	for code := range table {
		if SiteFailureCodes[code] {
			continue
		}
		if _, isOneOff := oneOffCopy[code]; !isOneOff {
			t.Fatalf("site copy key %s is neither a site code nor a one-off outcome", code)
		}
	}
}

// TestPrefixDetailStillMatches: exit reasons carrying a parenthesised detail
// still resolve to their prefix verdict, and unknown reasons resolve to nil.
func TestPrefixDetailStillMatches(t *testing.T) {
	v := ExitReasonFailure("local_processing_error(KeyError: 'x')")
	if v == nil || v.Reason != "loop_error" || v.FailsTurn {
		t.Fatalf("local_processing_error(...) must be advisory loop_error, got %+v", v)
	}
	if ExitReasonFailure("max_iterations") != nil {
		t.Fatal("max_iterations carries no failure verdict")
	}
}

// TestUnknownSiteCodeRefuses mirrors site_copy's KeyError: the twin returns
// a typed error instead of empty copy.
func TestUnknownSiteCodeRefuses(t *testing.T) {
	_, err := SiteCopy("nope", nil)
	if err == nil {
		t.Fatal("unknown site code must refuse")
	}
	if !strings.Contains(err.Error(), "nope") {
		t.Fatalf("error must name the code, got %v", err)
	}
}

// TestResetWindowCeilings pins format_reset_window's ceiling behaviour for
// both the hours and the minutes branch.
func TestResetWindowCeilings(t *testing.T) {
	for _, tc := range []struct {
		seconds float64
		want    string
	}{
		{3600, "~1h"}, {3601, "~2h"}, {31000, "~9h"}, {7200, "~2h"},
		{120, "~2 min"}, {60, "~1 min"}, {61, "~2 min"}, {3599, "~60 min"},
	} {
		if got := FormatResetWindow(tc.seconds); got != tc.want {
			t.Fatalf("FormatResetWindow(%v) = %q, want %q", tc.seconds, got, tc.want)
		}
	}
}

// TestAddFailedTurnNoticeNeverLeadsWithBlankBlock pins the join shape: an
// empty response yields the notice alone.
func TestAddFailedTurnNoticeNeverLeadsWithBlankBlock(t *testing.T) {
	if got := AddFailedTurnNotice("  \n ", "N"); got != "N" {
		t.Fatalf("blank response must yield the bare notice, got %q", got)
	}
	if got := AddFailedTurnNotice("A", "N"); got != "A\n\nN" {
		t.Fatalf("non-empty response must join with a blank line, got %q", got)
	}
}

// TestCoerceIntTruncatesTowardZero pins int() parity for float codes.
func TestCoerceIntTruncatesTowardZero(t *testing.T) {
	for _, tc := range []struct {
		in   any
		want int64
	}{
		{json.Number("429.7"), 429}, {json.Number("-0.5"), 0},
		{json.Number("429"), 429}, {" 503 ", 503},
	} {
		got, ok := coerceInt(tc.in)
		if !ok || got != tc.want {
			t.Fatalf("coerceInt(%v) = %d,%v want %d,true", tc.in, got, ok, tc.want)
		}
	}
	if _, ok := coerceInt(true); ok {
		t.Fatal("bool codes must fail coercion (Python int(True)==1 is not in the table either)")
	}
}
