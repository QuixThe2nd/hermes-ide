package jevnotices

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// TestSharedCasesPass executes the shared contract table in
// testdata/notice_cases.json — the same table gateway/jev_notice.py's tests
// execute — so the Go formatter and the Python twin cannot drift apart
// without at least one side going red.
func TestSharedCasesPass(t *testing.T) {
	for _, tc := range loadSharedCases(t) {
		t.Run(tc.Name, func(t *testing.T) {
			got := Format(tc.notice(t))
			if got != tc.Expect {
				t.Fatalf("Format mismatch\n got:  %q\n want: %q", got, tc.Expect)
			}
		})
	}
}

// TestSharedCasesCoverTheRequiredSurfaces guards the table itself: every
// purpose, a failure per purpose, the decision-shaped negative/abstention
// outcomes users troubleshoot (deny for the response gate, no-reaction for
// the reaction gate), and the goal-intent missing-confidence status — the
// one goal-intent outcome besides success and failure.
func TestSharedCasesCoverTheRequiredSurfaces(t *testing.T) {
	cases := loadSharedCases(t)
	seenPurpose, seenFailure, seenNegative, seenNoConfidence := map[string]bool{}, map[string]bool{}, map[string]bool{}, map[string]bool{}
	for _, tc := range cases {
		rec := tc.Record
		seenPurpose[rec["purpose"].(string)] = true
		if _, ok := rec["error"]; ok {
			seenFailure[rec["purpose"].(string)] = true
		}
		switch rec["purpose"].(string) {
		case PurposeGoalIntent:
			confidence, hasConfidence := rec["confidence"]
			if _, isFloat := confidence.(float64); !hasConfidence || !isFloat {
				if _, hasErr := rec["error"]; !hasErr {
					seenNoConfidence["goal-intent"] = true
				}
			}
		case PurposeResponseGate:
			if allowed, ok := rec["allowed"].(bool); ok && !allowed {
				seenNegative["response-gate"] = true
			}
		case PurposeReactionGate:
			if _, hasEmoji := rec["emoji"]; !hasEmoji {
				if _, hasErr := rec["error"]; !hasErr {
					seenNegative["reaction-gate"] = true
				}
			}
		}
	}
	for _, purpose := range []string{PurposeGoalIntent, PurposeResponseGate, PurposeReactionGate} {
		if !seenPurpose[purpose] {
			t.Errorf("shared table lost purpose %q", purpose)
		}
		if !seenFailure[purpose] {
			t.Errorf("shared table lost a failure case for %q", purpose)
		}
	}
	if !seenNoConfidence["goal-intent"] {
		t.Errorf("shared table lost the goal-intent missing-confidence case")
	}
	if !seenNegative[PurposeResponseGate] {
		t.Errorf("shared table lost the response-gate deny case")
	}
	if !seenNegative[PurposeReactionGate] {
		t.Errorf("shared table lost the reaction-gate no-reaction case")
	}
}

func TestSanitizeToken(t *testing.T) {
	cases := []struct{ raw, want string }{
		{"transport_error", "transport_error"},
		{"http_502", "http_502"},
		{"schema_mismatch: no answers object", "schema_mismatch"},
		{"classification unavailable: no litellm virtual key", "classification_unavailable"},
		{"  spaced   out  token ", "spaced_out_token"},
		{"", "error"},
		{":leading detail", "error"},
		{"!!!", "error"},
		{strings.Repeat("x", 100), strings.Repeat("x", maxTokenLen)},
		{"token\nwith\nnewlines", "token_with_newlines"},
	}
	for _, tc := range cases {
		if got := SanitizeToken(tc.raw); got != tc.want {
			t.Errorf("SanitizeToken(%q) = %q, want %q", tc.raw, got, tc.want)
		}
	}
}

func TestSanitizeTokenNeverContainsUnsafeRuns(t *testing.T) {
	// Fuzz-ish sweep: whatever the input, the output only carries
	// [A-Za-z0-9_.-] and never a colon, whitespace or control byte.
	inputs := []string{
		"a:b\nc\x00d\rd", "🎉unicode", "drop'; --sql", "@everyone", "`tick`",
		"tab\ttoken", "mixed_/-mixed", "label (confidence 0.9)",
	}
	for _, raw := range inputs {
		got := SanitizeToken(raw)
		for _, r := range got {
			switch {
			case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
			case r == '_' || r == '.' || r == '-':
			default:
				t.Fatalf("SanitizeToken(%q) = %q carries unsafe rune %q", raw, got, r)
			}
		}
	}
}

func TestSanitizeAuthor(t *testing.T) {
	cases := []struct{ raw, want string }{
		{"Alice", "Alice"},
		{"  Alice  ", "Alice"},
		{"line\nbreak", "line break"},
		{"@everyone", "everyone"},
		{"`code`", "code"},
		{"a\x00b", "ab"},
		{"", ""},
		{"   ", ""},
		{strings.Repeat("名", 100), strings.Repeat("名", 80)}, // capped at 80 runes
	}
	for _, tc := range cases {
		if got := SanitizeAuthor(tc.raw); got != tc.want {
			t.Errorf("SanitizeAuthor(%q) = %q, want %q", tc.raw, got, tc.want)
		}
	}
}

func TestSanitizeText(t *testing.T) {
	cases := []struct{ raw, want string }{
		{"user message", "user message"},
		{"user  message", "user message"},
		{"", "message"},
		{"\n\t", "message"},
		{"assistant reply", "assistant reply"},
	}
	for _, tc := range cases {
		if got := SanitizeText(tc.raw); got != tc.want {
			t.Errorf("SanitizeText(%q) = %q, want %q", tc.raw, got, tc.want)
		}
	}
}

func TestFormatFailsClosedOnMalformedNotices(t *testing.T) {
	// Unknown purpose, no verdict fields, garbage everywhere: Format still
	// returns a single safe line — a troubleshooting notice must never crash
	// or silently vanish because the caller handed it junk.
	got := Format(Notice{Purpose: "??? !!!", Subject: "", Error: ""})
	want := "🔎 jev error message: decision unavailable"
	if got != want {
		t.Fatalf("malformed notice = %q, want %q", got, want)
	}
}

// TestGoalIntentNoticeDropsNumberingSubjectAuthor: the fenced goal-intent
// status renders the known label and the confidence — attempt numbering,
// subject and author are accepted record inputs that must never reach the
// rendered notice, on any attempt shape.
func TestGoalIntentNoticeDropsNumberingSubjectAuthor(t *testing.T) {
	for _, shape := range [][2]int{{0, 0}, {1, 1}, {1, 2}, {2, 2}, {3, 4}} {
		got := Format(Notice{
			Purpose: PurposeGoalIntent, Subject: "user message", Author: "Alice",
			Attempt: shape[0], Attempts: shape[1],
			Label: "task", HasConfidence: true, Confidence: 0.61,
		})
		want := "```\n🔍 Goal Intent: Task (0.61)\n```"
		if got != want {
			t.Fatalf("attempt shape %v: notice = %q, want %q", shape, got, want)
		}
		for _, banned := range []string{"attempt", "user message", "Alice", "jev"} {
			if strings.Contains(got, banned) {
				t.Fatalf("attempt shape %v: notice %q leaks %q", shape, got, banned)
			}
		}
	}
}

// TestGoalIntentLabelForms pins every goal-intent status shape: the four
// known labels title-cased with the fixed-two-decimal confidence in
// parentheses, the label alone when the attempt carried no confidence, the
// confidence-only fallback for absent or unknown labels, and the garbage and
// detail-bearing label shapes.
func TestGoalIntentLabelForms(t *testing.T) {
	cases := []struct {
		name   string
		notice Notice
		want   string
	}{
		{
			"chat with confidence",
			Notice{Purpose: PurposeGoalIntent, Label: "chat", HasConfidence: true, Confidence: 0.61},
			"```\n🔍 Goal Intent: Chat (0.61)\n```",
		},
		{
			"task with confidence",
			Notice{Purpose: PurposeGoalIntent, Label: "task", HasConfidence: true, Confidence: 0.82},
			"```\n🔍 Goal Intent: Task (0.82)\n```",
		},
		{
			"approval with confidence",
			Notice{Purpose: PurposeGoalIntent, Label: "approval", HasConfidence: true, Confidence: 0.74},
			"```\n🔍 Goal Intent: Approval (0.74)\n```",
		},
		{
			"chase with confidence",
			Notice{Purpose: PurposeGoalIntent, Label: "chase", HasConfidence: true, Confidence: 0.61},
			"```\n🔍 Goal Intent: Chase (0.61)\n```",
		},
		{
			"label without confidence",
			Notice{Purpose: PurposeGoalIntent, Label: "chase"},
			"```\n🔍 Goal Intent: Chase\n```",
		},
		{
			"unknown label falls back to the confidence readout",
			Notice{Purpose: PurposeGoalIntent, Label: "submarine", HasConfidence: true, Confidence: 0.61},
			"```\n🔍 Goal Intent: 0.61 Confidence\n```",
		},
		{
			"garbage label sanitizes out of the render",
			Notice{Purpose: PurposeGoalIntent, Label: "!!!", HasConfidence: true, Confidence: 0.5},
			"```\n🔍 Goal Intent: 0.50 Confidence\n```",
		},
		{
			"label detail after the colon is dropped",
			Notice{Purpose: PurposeGoalIntent, Label: "task: contract draft", HasConfidence: true, Confidence: 0.9},
			"```\n🔍 Goal Intent: Task (0.90)\n```",
		},
		{
			"uppercase label still title-cases",
			Notice{Purpose: PurposeGoalIntent, Label: "CHASE", HasConfidence: true, Confidence: 0.5},
			"```\n🔍 Goal Intent: Chase (0.50)\n```",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Format(tc.notice); got != tc.want {
				t.Fatalf("Format = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestFormatExtraScoreKeysAreSortedAfterTheFixedFour(t *testing.T) {
	allowed := true
	got := Format(Notice{
		Purpose: PurposeResponseGate, Subject: "user message", Allowed: &allowed,
		Scores: map[string]float64{"zz_extra": 0.5, "addresses_bot": 0.9, "aa_extra": 0.1, "noise": 0.2},
	})
	want := "🔎 jev response-gate user message: allow (addresses_bot=0.90 continues_bot_thread=0.00 joins_bot_thread=0.00 noise=0.20 aa_extra=0.10 zz_extra=0.50)"
	if got != want {
		t.Fatalf("score order = %q, want %q", got, want)
	}
}

func TestFormatConfidenceRoundingIsFixedTwoDecimals(t *testing.T) {
	got := Format(Notice{Purpose: PurposeGoalIntent, Subject: "user message", Label: "task", HasConfidence: true, Confidence: 0.005})
	want := "```\n🔍 Goal Intent: Task (0.01)\n```"
	if got != want {
		t.Fatalf("rounding = %q, want %q", got, want)
	}
	got = Format(Notice{Purpose: PurposeGoalIntent, Subject: "user message", Label: "chase", HasConfidence: true, Confidence: 0.66666})
	want = "```\n🔍 Goal Intent: Chase (0.67)\n```"
	if got != want {
		t.Fatalf("rounding = %q, want %q", got, want)
	}
}

// --- shared table plumbing -------------------------------------------------

type sharedCase struct {
	Name   string         `json:"name"`
	Record map[string]any `json:"record"`
	Expect string         `json:"expect"`
}

func (tc sharedCase) notice(t *testing.T) Notice {
	t.Helper()
	n := Notice{Scores: map[string]float64{}}
	for key, value := range tc.Record {
		switch key {
		case "purpose":
			n.Purpose = value.(string)
		case "subject":
			n.Subject = value.(string)
		case "author":
			n.Author = value.(string)
		case "attempt":
			n.Attempt = int(value.(float64))
		case "attempts":
			n.Attempts = int(value.(float64))
		case "label":
			n.Label = value.(string)
		case "confidence":
			// null is absent, not zero — the same presence rule as cmd/jevnotice.
			if f, ok := value.(float64); ok {
				n.Confidence, n.HasConfidence = f, true
			}
		case "allowed":
			v := value.(bool)
			n.Allowed = &v
		case "scores":
			for k, v := range value.(map[string]any) {
				n.Scores[k] = v.(float64)
			}
		case "emoji":
			n.Emoji = value.(string)
		case "abstain":
			n.Abstain, n.HasAbstain = value.(float64), true
		case "error":
			n.Error = value.(string)
		default:
			t.Fatalf("shared case %q has unknown field %q", tc.Name, key)
		}
	}
	return n
}

func loadSharedCases(t *testing.T) []sharedCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/notice_cases.json")
	if err != nil {
		t.Fatalf("read shared cases: %v", err)
	}
	var table struct {
		Cases []sharedCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared cases: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared cases table is empty")
	}
	return table.Cases
}
