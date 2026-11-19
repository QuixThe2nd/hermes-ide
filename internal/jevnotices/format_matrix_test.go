package jevnotices

import (
	"fmt"
	"strings"
	"testing"
)

// TestFormatMatrixInvariants sweeps every purpose × verdict shape × author
// presence × attempt-numbering combination and asserts the structural
// invariants a chat notice must hold on EVERY rendering path: the gates
// render one single line with the fixed prefix, a colon before the verdict,
// no control runes, no backtick/@ in the author fragment, sanitized tokens
// only, and the attempt fragment exactly when the caller retries; the
// goal-intent status renders as a fenced code block whose only content is
// the confidence line — never numbering, subject, author or label.
func TestFormatMatrixInvariants(t *testing.T) {
	purposes := []string{PurposeGoalIntent, PurposeResponseGate, PurposeReactionGate}
	authors := []string{"", "Alice", "@everyone", "line\nbreak", strings.Repeat("long", 60)}
	attemptShapes := [][2]int{{0, 0}, {1, 1}, {1, 2}, {2, 2}, {3, 4}}
	allowedValues := []*bool{nil, boolPtr(true), boolPtr(false)}

	for _, purpose := range purposes {
		for _, author := range authors {
			for _, shape := range attemptShapes {
				for _, notice := range verdictShapes(purpose, allowedValues) {
					notice.Purpose = purpose
					notice.Author = author
					notice.Attempt, notice.Attempts = shape[0], shape[1]
					assertNoticeInvariants(t, Format(notice), notice)
				}
			}
		}
	}
}

// verdictShapes enumerates the outcome payloads each purpose can carry:
// success, failure, and the decision-shaped variants.
func verdictShapes(purpose string, allowedValues []*bool) []Notice {
	base := func() Notice { return Notice{Scores: map[string]float64{}} }
	shapes := []Notice{
		base(), // no verdict fields at all
		func() Notice { n := base(); n.Error = "transport_error"; return n }(),
		func() Notice { n := base(); n.Error = "schema_mismatch: no answers object"; return n }(),
		func() Notice { n := base(); n.Error = ""; return n }(),
	}
	switch purpose {
	case PurposeGoalIntent:
		shapes = append(shapes,
			func() Notice { n := base(); n.Label, n.HasConfidence, n.Confidence = "task", true, 0.9; return n }(),
			func() Notice { n := base(); n.Label, n.HasConfidence, n.Confidence = "chat", true, 0.01; return n }(),
			func() Notice { n := base(); n.Label = "unknown"; return n }(),
			func() Notice { n := base(); n.Label, n.HasConfidence, n.Confidence = "chase", true, 0.666666; return n }(),
		)
	case PurposeResponseGate:
		for _, allowed := range allowedValues {
			n := base()
			if allowed != nil {
				n.Allowed = allowed
				n.Scores = map[string]float64{
					"addresses_bot": 0.9, "continues_bot_thread": 0.1,
					"joins_bot_thread": 0.0, "noise": 0.2,
				}
			}
			shapes = append(shapes, n)
		}
		shapes = append(shapes, func() Notice {
			n := base()
			n.Allowed = boolPtr(true)
			n.Scores = map[string]float64{"extra_key": 0.5}
			return n
		}())
	case PurposeReactionGate:
		shapes = append(shapes,
			func() Notice { n := base(); n.Emoji, n.HasAbstain, n.Abstain = "🔥", true, 0.2; return n }(),
			func() Notice { n := base(); n.Emoji = "👍"; return n }(),
			func() Notice { n := base(); n.HasAbstain, n.Abstain = true, 0.99; return n }(),
			func() Notice { n := base(); n.Emoji, n.HasAbstain, n.Abstain = "👉👈", true, 0.0; return n }(),
		)
	}
	return shapes
}

func assertNoticeInvariants(t *testing.T, line string, n Notice) {
	t.Helper()
	if n.Purpose == PurposeGoalIntent {
		assertGoalIntentNoticeInvariants(t, line, n)
		return
	}
	if !strings.HasPrefix(line, Prefix) {
		t.Fatalf("line %q lacks prefix", line)
	}
	if strings.ContainsAny(line, "\n\r\x00") {
		t.Fatalf("line %q is not a single clean line", line)
	}
	if strings.Contains(line, "`") || strings.Contains(line, "@") {
		t.Fatalf("line %q carries actionable chat syntax", line)
	}
	verdict := line[strings.LastIndex(line, ": ")+2:]
	if verdict == "" {
		t.Fatalf("line %q has an empty verdict", line)
	}
	if n.Error != "" && !strings.HasPrefix(verdict, "failed (") {
		t.Fatalf("error notice %q verdict %q is not a failure", line, verdict)
	}
	hasAttempt := strings.Contains(line, "(attempt ")
	wantAttempt := n.Attempts > 1 && n.Attempt > 0
	if hasAttempt != wantAttempt {
		t.Fatalf("line %q attempt-fragment=%v, want %v", line, hasAttempt, wantAttempt)
	}
}

// assertGoalIntentNoticeInvariants pins the fenced goal-intent status:
// opening fence on its own line, exactly one status line starting with the
// fixed head, closing fence on its own line, and none of the fragments the
// record may still carry (numbering, subject, author, label) leaking in.
func assertGoalIntentNoticeInvariants(t *testing.T, block string, n Notice) {
	t.Helper()
	const head = "```\n🔍 Goal Intent: "
	if !strings.HasPrefix(block, head) || !strings.HasSuffix(block, "\n```") {
		t.Fatalf("goal-intent notice %q is not a fenced status block", block)
	}
	if got := strings.Count(block, "\n"); got != 2 {
		t.Fatalf("goal-intent notice %q has %d newlines, want exactly the two fence lines", block, got)
	}
	status := strings.TrimSuffix(strings.TrimPrefix(block, head), "\n```")
	switch {
	case n.Error != "":
		if !strings.HasPrefix(status, "failed (") || !strings.HasSuffix(status, ")") {
			t.Fatalf("error notice status %q is not a failure", status)
		}
	case n.HasConfidence:
		if !strings.HasSuffix(status, " Confidence") || len(status) != len("0.00 Confidence") {
			t.Fatalf("confidence status %q is not a fixed-two-decimal readout", status)
		}
	default:
		if status != "confidence unavailable" {
			t.Fatalf("no-confidence status %q must say confidence unavailable", status)
		}
	}
	for _, banned := range []string{"attempt", "user message", "message", "from ", "jev", n.Label} {
		if banned != "" && strings.Contains(status, banned) {
			t.Fatalf("goal-intent status %q leaks %q", status, banned)
		}
	}
	if author := SanitizeAuthor(n.Author); author != "" && strings.Contains(block, author) {
		t.Fatalf("goal-intent notice %q leaks author %q", block, author)
	}
}

func boolPtr(v bool) *bool { return &v }

// TestFormatIsPureAndDeterministic: repeated rendering of the same notice
// (including map-valued Scores, whose iteration order varies) must produce
// the identical line every time — the fixed key order, not map order, wins.
func TestFormatIsPureAndDeterministic(t *testing.T) {
	allowed := false
	notice := Notice{
		Purpose: PurposeResponseGate, Subject: "user message", Author: "Tek",
		Allowed: &allowed,
		Scores: map[string]float64{
			"noise": 0.4, "joins_bot_thread": 0.3, "addresses_bot": 0.1,
			"continues_bot_thread": 0.2, "z_key": 0.05, "a_key": 0.06,
		},
	}
	first := Format(notice)
	for i := 0; i < 8; i++ {
		if got := Format(notice); got != first {
			t.Fatalf("nondeterministic rendering:\n %q\n %q", first, got)
		}
	}
	if !strings.Contains(first, "addresses_bot=0.10 continues_bot_thread=0.20 joins_bot_thread=0.30 noise=0.40 a_key=0.06 z_key=0.05") {
		t.Fatalf("fixed-then-sorted key order broken: %q", first)
	}
}

// TestFormatEveryPurposeFailureUsesTheSanitizedClassOnly: the failure verdict
// never leaks the human detail after ':' and never carries whitespace.
func TestFormatEveryPurposeFailureUsesTheSanitizedClassOnly(t *testing.T) {
	detailed := "classification unavailable: no litellm virtual key in scope"
	for _, purpose := range []string{PurposeResponseGate, PurposeReactionGate} {
		line := Format(Notice{Purpose: purpose, Subject: "user message", Error: detailed})
		want := Prefix + purpose + " user message: failed (classification_unavailable)"
		if line != want {
			t.Fatalf("purpose %s failure line = %q, want %q", purpose, line, want)
		}
	}
	goalLine := Format(Notice{Purpose: PurposeGoalIntent, Subject: "user message", Error: detailed})
	if goalLine != "```\n🔍 Goal Intent: failed (classification_unavailable)\n```" {
		t.Fatalf("goal-intent failure status = %q", goalLine)
	}
}

// TestReactionVerdictCoversReactAbstainAndMissingFragments.
func TestReactionVerdictCoversReactAbstainAndMissingFragments(t *testing.T) {
	cases := []struct {
		notice Notice
		want   string
	}{
		{Notice{Purpose: PurposeReactionGate, Emoji: "🔥", HasAbstain: true, Abstain: 0.20},
			Prefix + "reaction-gate message: react 🔥 (abstain 0.20)"},
		{Notice{Purpose: PurposeReactionGate, Emoji: "👍"},
			Prefix + "reaction-gate message: react 👍"},
		{Notice{Purpose: PurposeReactionGate, HasAbstain: true, Abstain: 0.63},
			Prefix + "reaction-gate message: no reaction (abstain 0.63)"},
		{Notice{Purpose: PurposeReactionGate},
			Prefix + "reaction-gate message: no reaction"},
		{Notice{Purpose: PurposeReactionGate, HasAbstain: true, Abstain: 1.0},
			Prefix + "reaction-gate message: no reaction (abstain 1.00)"},
	}
	for _, tc := range cases {
		if got := Format(tc.notice); got != tc.want {
			t.Errorf("Format = %q, want %q", got, tc.want)
		}
	}
}

// TestGoalIntentStatusIgnoresLabelAndCoversMissingConfidence: labels never
// render — every success is the same confidence readout — and a missing
// confidence says so instead of inventing one.
func TestGoalIntentStatusIgnoresLabelAndCoversMissingConfidence(t *testing.T) {
	for _, label := range []string{"chat", "task", "approval", "chase", "unknown"} {
		line := Format(Notice{Purpose: PurposeGoalIntent, Label: label, HasConfidence: true, Confidence: 0.5})
		want := "```\n🔍 Goal Intent: 0.50 Confidence\n```"
		if line != want {
			t.Errorf("label %s: Format = %q, want %q", label, line, want)
		}
	}
	line := Format(Notice{Purpose: PurposeGoalIntent, Label: "task"})
	want := "```\n🔍 Goal Intent: confidence unavailable\n```"
	if line != want {
		t.Errorf("no-confidence: Format = %q, want %q", line, want)
	}
}

// TestResponseGateVerdictBoundaryReadout: the four fixed keys always render,
// in order, defaulted to 0.00 when the judge's map omits them.
func TestResponseGateVerdictBoundaryReadout(t *testing.T) {
	deny := false
	line := Format(Notice{
		Purpose: PurposeResponseGate, Subject: "user message", Allowed: &deny,
		Scores: map[string]float64{"addresses_bot": 0.4999},
	})
	want := Prefix + "response-gate user message: deny (addresses_bot=0.50 continues_bot_thread=0.00 joins_bot_thread=0.00 noise=0.00)"
	if line != want {
		t.Fatalf("Format = %q, want %q", line, want)
	}
}

// TestAttemptNumberingRendersAcrossTheRetryWindow: the non-goal-intent line
// keeps its attempt fragment (the gates never retry today, but the fragment
// is generic line shape); the goal-intent fence never carries one.
func TestAttemptNumberingRendersAcrossTheRetryWindow(t *testing.T) {
	for attempt := 1; attempt <= 4; attempt++ {
		for attempts := attempt + 1; attempts <= 4; attempts++ {
			line := Format(Notice{
				Purpose: PurposeResponseGate, Subject: "user message",
				Attempt: attempt, Attempts: attempts,
			})
			fragment := fmt.Sprintf("(attempt %d/%d)", attempt, attempts)
			if !strings.Contains(line, fragment) {
				t.Fatalf("line %q lacks %q", line, fragment)
			}
			goalLine := Format(Notice{
				Purpose: PurposeGoalIntent, Subject: "user message",
				Attempt: attempt, Attempts: attempts,
				Label: "task", HasConfidence: true, Confidence: 0.5,
			})
			if strings.Contains(goalLine, fragment) {
				t.Fatalf("goal-intent notice %q carries %q", goalLine, fragment)
			}
		}
	}
}

// TestUnknownPurposeFailsClosedWithoutPanic.
func TestUnknownPurposeFailsClosedWithoutPanic(t *testing.T) {
	for _, purpose := range []string{"", "???", "goal intent", "Response-Gate", "goal_intent\x00"} {
		line := Format(Notice{Purpose: purpose, Subject: "user message", Label: "task", HasConfidence: true, Confidence: 0.5})
		if !strings.HasPrefix(line, Prefix) {
			t.Fatalf("unknown purpose %q broke the prefix: %q", purpose, line)
		}
	}
}
