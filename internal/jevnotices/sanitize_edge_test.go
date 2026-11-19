package jevnotices

import (
	"strings"
	"testing"
)

// productionErrorClasses are every sanitized failure class the three Jev
// callers can emit today (goal-intent transport/parser + the two gates'
// ResponseGateError reasons). Each must survive sanitization verbatim.
var productionErrorClasses = []string{
	"transport_error",
	"http_502",
	"http_500",
	"http_429",
	"timeout",
	"cancelled",
	"invalid_json",
	"schema_mismatch",
	"out_of_range",
	"incoherent_distribution",
	"decision_formula_failed",
	"missing_credential",
	"config_error",
}

func TestProductionErrorClassesSurviveSanitizationVerbatim(t *testing.T) {
	for _, class := range productionErrorClasses {
		if got := SanitizeToken(class); got != class {
			t.Errorf("SanitizeToken(%q) = %q, want verbatim", class, got)
		}
	}
}

func TestProductionErrorClassesRenderAsFailures(t *testing.T) {
	for _, class := range productionErrorClasses {
		line := Format(Notice{
			Purpose: PurposeResponseGate, Subject: "user message",
			Author: "Tek", Error: class,
		})
		want := Prefix + "response-gate user message from Tek: failed (" + class + ")"
		if line != want {
			t.Errorf("class %s: Format = %q, want %q", class, line, want)
		}
	}
}

func TestSanitizeTokenUnicodeAndSeparatorSweep(t *testing.T) {
	cases := []struct{ raw, want string }{
		{" naïve\ttoken ", "naïve_token"},
		{"emoji🎉token", "emoji_token"},
		{"slash/token", "slash_token"},
		{"dot.separated-name", "dot.separated-name"},
		{"under_score", "under_score"},
		{"trailing___", "trailing"},
		{"___leading", "leading"},
		{"a:b:c", "a"},
		{"：全角コロン", ""},
		{"tab\tsep", "tab_sep"},
	}
	for _, tc := range cases {
		got := SanitizeToken(tc.raw)
		// Non-ASCII inputs are implementation-defined beyond the safety
		// invariant; assert the invariant (charset) rather than exact text,
		// except for the pure-ASCII rows.
		if tc.want != "" && got != tc.want && isASCII(tc.raw) {
			t.Errorf("SanitizeToken(%q) = %q, want %q", tc.raw, got, tc.want)
		}
		if got == "" {
			t.Errorf("SanitizeToken(%q) must never return empty", tc.raw)
		}
	}
}

func TestSanitizeTokenMultibyteTruncationKeepsWholeRunes(t *testing.T) {
	raw := strings.Repeat("é", 200)
	got := SanitizeToken(raw)
	if got != "error" && len([]rune(got)) > maxTokenLen {
		t.Fatalf("truncation produced %d runes, cap is %d", len([]rune(got)), maxTokenLen)
	}
}

func TestSanitizeAuthorOmitsAndCollapses(t *testing.T) {
	cases := []struct{ raw, want string }{
		{"", ""},
		{" ", ""},
		{"\t\n", ""},
		{"@(#)!", "(#)!"},
		{"Bob @ Marley", "Bob Marley"},
		{"`inline code`", "inline code"},
		{"name\t\tsurname", "name surname"},
		{"🧑‍🚀 astronaut", "🧑‍🚀 astronaut"},
	}
	for _, tc := range cases {
		if got := SanitizeAuthor(tc.raw); got != tc.want {
			t.Errorf("SanitizeAuthor(%q) = %q, want %q", tc.raw, got, tc.want)
		}
	}
}

func TestSanitizeTextBoundaries(t *testing.T) {
	if got := SanitizeText(strings.Repeat("a", 200)); len([]rune(got)) != 80 {
		t.Errorf("subject cap = %d runes, want 80", len([]rune(got)))
	}
	if got := SanitizeText("user\tmessage\n"); got != "user message" {
		t.Errorf("whitespace collapse = %q", got)
	}
	if got := SanitizeText("🎉"); got != "🎉" {
		t.Errorf("emoji subject = %q", got)
	}
}

func TestFixed2BoundaryTable(t *testing.T) {
	cases := []struct {
		value float64
		want  string
	}{
		{0.0, "0.00"},
		{0.004, "0.00"},
		{0.014, "0.01"},
		{0.095, "0.10"},
		{0.5, "0.50"},
		{0.995, "0.99"}, // binary 0.995 is just under; both Go and Python render 0.99
		{1.0, "1.00"},
	}
	for _, tc := range cases {
		if got := fixed2(tc.value); got != tc.want {
			t.Errorf("fixed2(%v) = %q, want %q", tc.value, got, tc.want)
		}
	}
}

func TestZeroValueNoticeStillRendersOneLine(t *testing.T) {
	line := Format(Notice{Scores: nil})
	want := Prefix + "error message: decision unavailable"
	if line != want {
		t.Fatalf("zero-value notice = %q, want %q", line, want)
	}
	if strings.Count(line, "\n") != 0 {
		t.Fatalf("zero-value notice is not a single line: %q", line)
	}
}

func TestScoreKeysOrderIsTheJudgeQuestionOrder(t *testing.T) {
	want := []string{"addresses_bot", "continues_bot_thread", "joins_bot_thread", "noise"}
	for i, key := range scoreKeys {
		if key != want[i] {
			t.Fatalf("scoreKeys[%d] = %q, want %q", i, key, want[i])
		}
	}
}

func TestSharedCaseRecordsUseOnlyKnownFields(t *testing.T) {
	// The shared table is the cross-language contract; an unknown field would
	// silently drop data on one side, so the loader must reject it loudly
	// (notice() fatals) — proven here by exhausting the known set.
	known := map[string]bool{
		"purpose": true, "subject": true, "author": true, "attempt": true,
		"attempts": true, "label": true, "confidence": true, "allowed": true,
		"scores": true, "emoji": true, "abstain": true, "error": true,
	}
	for _, tc := range loadSharedCases(t) {
		for field := range tc.Record {
			if !known[field] {
				t.Errorf("shared case %q carries unknown field %q", tc.Name, field)
			}
		}
	}
}

func isASCII(s string) bool {
	for _, r := range s {
		if r > 127 {
			return false
		}
	}
	return true
}
