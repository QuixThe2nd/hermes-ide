package citationstrip

import (
	"strings"
	"testing"
)

const ledgered = "https://example.org/primary"

// TestStripRules is the table-driven twin of the five Python unit cases in
// tests/plugins/deep_research/test_citations.py::TestStripSafeUnledgeredURLs.
func TestStripRules(t *testing.T) {
	tests := []struct {
		name    string
		in      string
		evidence []string
		wantIn  []string // substrings that must remain
		wantOut []string // substrings that must be gone
	}{
		{
			name:    "sources item with only unledgered URL is dropped",
			in:      "## Sources\n\n- https://example.com/homepage\n- kept\n",
			evidence: []string{ledgered},
			wantIn:  []string{"- kept"},
			wantOut: []string{"https://example.com/homepage"},
		},
		{
			name:    "mixed sources item keeps ledgered URL and label",
			in:      "## Sources\n\n- label [s](" + ledgered + ") plus https://example.net/x\n",
			evidence: []string{ledgered},
			wantIn:  []string{ledgered, "label"},
			wantOut: []string{"https://example.net/x"},
		},
		{
			name:    "body line with only invented URL is unchanged",
			in:      "A body claim on [fake](https://example.com/i-made-this-up) alone.\n",
			evidence: []string{ledgered},
			wantIn:  []string{"https://example.com/i-made-this-up"},
		},
		{
			name:    "table row loses only the unledgered URL",
			in:      "| a | [good](" + ledgered + ") and https://example.org/redundant-row |\n",
			evidence: []string{ledgered},
			wantIn:  []string{ledgered},
			wantOut: []string{"https://example.org/redundant-row"},
		},
		{
			name: "notes heading ends the summary section",
			in: "## Sources\n\n- https://example.com/homepage\n\n" +
				"## Notes\n\n- https://example.com/also-fake\n",
			evidence:  []string{ledgered},
			wantIn:   []string{"https://example.com/also-fake"},
			wantOut:  []string{"https://example.com/homepage"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := StripSafeUnledgeredURLs(tt.in, tt.evidence)
			for _, sub := range tt.wantIn {
				if !strings.Contains(got, sub) {
					t.Errorf("output %q must still contain %q", got, sub)
				}
			}
			for _, sub := range tt.wantOut {
				if strings.Contains(got, sub) {
					t.Errorf("output %q must no longer contain %q", got, sub)
				}
			}
		})
	}
}

func TestNormalizeURLAgreement(t *testing.T) {
	cases := map[string]string{
		"https://Example.org/Primary": "https://example.org/Primary",
		"https://example.org/)":       "https://example.org/",
		"https://example.org/a.":      "https://example.org/a",
	}
	for in, want := range cases {
		if got := NormalizeURL(in); got != want {
			t.Errorf("NormalizeURL(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestExtractBalancedParens(t *testing.T) {
	got := ExtractReportURLs("[source](https://example.org/wiki/Foo_(bar))")
	if len(got) != 1 || got[0] != "https://example.org/wiki/Foo_(bar)" {
		t.Errorf("got %v, want the full paren URL intact", got)
	}
}

func TestLedgeredURLsStayByteForByte(t *testing.T) {
	in := "See [docs](" + ledgered + ") and <" + ledgered + ">.\n"
	got := StripSafeUnledgeredURLs(in, []string{ledgered})
	if got != in {
		t.Errorf("fully ledgered line changed: %q", got)
	}
}
