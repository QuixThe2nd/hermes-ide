package mentionrender

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// renderCase is one row of the shared table (testdata/cases.json) that the
// Python bridge test also consumes: both sides pin the same rendering.
type renderCase struct {
	Name    string            `json:"name"`
	Mode    string            `json:"mode"`
	Content string            `json:"content"`
	BotID   string            `json:"bot_id"`
	BotName string            `json:"bot_name"`
	Names   map[string]string `json:"names"`
	Want    string            `json:"want"`
}

func loadCases(t *testing.T) []renderCase {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "cases.json"))
	if err != nil {
		t.Fatalf("read cases: %v", err)
	}
	var table struct {
		Cases []renderCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse cases: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared table is empty")
	}
	return table.Cases
}

func TestSharedCases(t *testing.T) {
	for _, tc := range loadCases(t) {
		t.Run(tc.Name, func(t *testing.T) {
			got := Render(Request{
				Content: tc.Content,
				BotID:   tc.BotID,
				BotName: tc.BotName,
				Names:   tc.Names,
				Mode:    tc.Mode,
			})
			if got != tc.Want {
				t.Fatalf("Render(%q, mode=%s) = %q, want %q", tc.Content, tc.Mode, got, tc.Want)
			}
		})
	}
}

// TestSanitizeNamePinsInvariants pins the safety contract directly so a table
// edit cannot quietly weaken it: a cleaned name can never forge a mention
// token or a mass ping, and never carries control or zero-width characters.
func TestSanitizeNamePinsInvariants(t *testing.T) {
	for _, hostile := range []string{
		"<@123>", "@everyone", "@here", "Everyone", "HERE",
		"zero\uFEFFwidth", "a\x00b", "line\nbreak",
		"<", ">", "@", "   ",
	} {
		got := SanitizeName(hostile)
		if got == "" {
			continue // unresolvable is always safe
		}
		for _, bad := range []string{"<", ">", "@", "\n", "\r", "\t", "\uFEFF"} {
			if strings.Contains(got, bad) {
				t.Fatalf("SanitizeName(%q) = %q still contains %q", hostile, got, bad)
			}
		}
		if mentionToken.MatchString(got) {
			t.Fatalf("SanitizeName(%q) = %q still holds a mention token", hostile, got)
		}
	}
}
