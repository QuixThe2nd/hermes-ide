package modelcmd

import (
	"strings"
	"testing"
)

// TestParseSharedTable runs every shared-table record through Parse and
// compares the full wire object (JSON equality, so nil-vs-empty Errors
// differences would surface too).
func TestParseSharedTable(t *testing.T) {
	for _, tc := range loadTable(t).Parse {
		got := Parse(tc.Raw)
		if a, b := asJSON(t, got), asJSON(t, tc.Want); a != b {
			t.Errorf("Parse(%q):\n got %s\nwant %s", tc.Raw, a, b)
		}
	}
}

// TestParseValueFlagConsumesNextToken pins the historical tokenizer rule:
// --provider/--reasoning blindly take the NEXT token as their value, even
// when it looks like another flag (shell quoting was never required).
func TestParseValueFlagConsumesNextToken(t *testing.T) {
	p := Parse("z --provider --global")
	if p.ExplicitProvider != "--global" || p.IsGlobal {
		t.Fatalf("--provider consumed token = %q, is_global = %v; want %q/true-false",
			p.ExplicitProvider, p.IsGlobal, "--global")
	}
	if p.Target != "z" {
		t.Fatalf("target = %q, want %q", p.Target, "z")
	}
}

// TestParseTrailingValueFlagStaysInTarget pins the other tokenizer edge:
// a value flag as the FINAL token has nothing to consume, so it stays part
// of the model text rather than erroring.
func TestParseTrailingValueFlagStaysInTarget(t *testing.T) {
	p := Parse("model-x --provider")
	if p.Target != "model-x --provider" || p.ExplicitProvider != "" {
		t.Fatalf("target = %q explicit = %q; want %q/empty",
			p.Target, p.ExplicitProvider, "model-x --provider")
	}
}

// TestParseUnicodeDashNormalization: mobile keyboards auto-convert "--"
// into U+2012..U+2015 dashes; each must be treated as the flag prefix.
func TestParseUnicodeDashNormalization(t *testing.T) {
	for _, dash := range []string{"\u2012", "\u2013", "\u2014", "\u2015"} {
		p := Parse("x " + dash + "global")
		if !p.IsGlobal {
			t.Errorf("dash %q not normalized to --global", dash)
		}
	}
	// A dash before a non-flag word must be left alone.
	if p := Parse("we—ird name"); p.Target != "we—ird name" {
		t.Errorf("non-flag dash mangled: target = %q", p.Target)
	}
}

// TestParseScopePrecedence pins once > session > global > default.
func TestParseScopePrecedence(t *testing.T) {
	cases := map[string]string{
		"x --once --session":   "once",
		"x --once --global":    "once",
		"x --session --global": "session",
		"x --global":           "global",
		"x":                    "default",
	}
	for raw, want := range cases {
		if got := Parse(raw).Scope; got != want {
			t.Errorf("Parse(%q).Scope = %q, want %q", raw, got, want)
		}
	}
}

// TestParseReasoningVocabulary pins the accepted --reasoning values:
// the effort ladder plus the disable words, case-insensitively.
func TestParseReasoningVocabulary(t *testing.T) {
	for _, ok := range []string{"none", "false", "disabled", "minimal", "low",
		"medium", "high", "xhigh", "max", "ultra", "HIGH", "Ultra "} {
		if p := Parse("x --reasoning " + ok); len(p.Errors) != 0 {
			t.Errorf("--reasoning %q wrongly rejected: %v", ok, p.Errors)
		}
	}
	for _, bad := range []string{"bogus", "none-x", ""} {
		if bad == "" {
			continue // empty value means the flag was absent
		}
		if p := Parse("x --reasoning " + bad); len(p.Errors) == 0 {
			t.Errorf("--reasoning %q wrongly accepted", bad)
		}
	}
}

// TestParseErrorCopyIsCanonical: error messages are user-facing contract
// copy — decoration may be prepended by surfaces but the sentence frozen.
func TestParseErrorCopyIsCanonical(t *testing.T) {
	p := Parse("--once --global")
	if len(p.Errors) != 2 {
		t.Fatalf("errors = %v, want both once_with_global and once_requires_target", p.Errors)
	}
	if !strings.HasPrefix(p.Errors[0].Message, "/model --once cannot be combined with --global") {
		t.Errorf("canonical copy drifted: %q", p.Errors[0].Message)
	}
	if p.Errors[0].Code != ErrOnceWithGlobal {
		t.Errorf("code = %q, want %q", p.Errors[0].Code, ErrOnceWithGlobal)
	}
}

// TestParseWhitespaceCollapse pins strings.Fields semantics: any run of
// whitespace between tokens collapses to one space in the target.
func TestParseWhitespaceCollapse(t *testing.T) {
	if p := Parse("gpt  5.1\t preview"); p.Target != "gpt 5.1 preview" {
		t.Fatalf("target = %q, want %q", p.Target, "gpt 5.1 preview")
	}
	if p := Parse("   "); p.Target != "" {
		t.Fatalf("blank input produced target %q", p.Target)
	}
}
