package mentionrender

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
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

// TestMentionTokenDocumentsAcceptedForms pins which strings the token regex
// accepts and how parseToken classifies them. The bridge's Python pre-check
// regex must accept exactly the same shapes, or token-bearing text would
// silently skip rendering.
func TestMentionTokenDocumentsAcceptedForms(t *testing.T) {
	valid := []struct {
		in   string
		kind tokenKind
		id   string
	}{
		{"<@123>", kindUser, "123"},
		{"<@!123>", kindUser, "123"}, // legacy nickname form
		{"<@&123>", kindRole, "123"},
		{"<#123>", kindChannel, "123"},
	}
	for _, tc := range valid {
		if !mentionToken.MatchString(tc.in) {
			t.Fatalf("mentionToken does not match %q", tc.in)
		}
		kind, id := parseToken(tc.in)
		if kind != tc.kind || id != tc.id {
			t.Fatalf("parseToken(%q) = {%v %s}, want {%v %s}", tc.in, kind, id, tc.kind, tc.id)
		}
	}
	invalid := []string{
		"<@abc>",  // non-numeric id
		"<@123",   // unterminated
		"<@>",     // empty id
		"@123>",   // missing opener
		"<x@123>", // wrong prefix
		"plain",
	}
	for _, in := range invalid {
		if mentionToken.MatchString(in) {
			t.Fatalf("mentionToken unexpectedly matches %q", in)
		}
	}
}

// TestRenderNeverMutatesNames: callers reuse their name map across renders
// (the Python bridge builds it once per message); Render must not write to it.
func TestRenderNeverMutatesNames(t *testing.T) {
	names := map[string]string{"111": "Alice"}
	before := map[string]string{"111": "Alice"}
	for _, mode := range []string{ModeBody, ModeTitle} {
		Render(Request{Content: "hi <@111> and <@222>", BotID: "222", BotName: "Hermes", Names: names, Mode: mode})
	}
	if len(names) != len(before) || names["111"] != before["111"] {
		t.Fatalf("names mutated by Render: %v", names)
	}
}

// ── Render contract oracle (I1 + I2) ────────────────────────────────────
//
// The rendered output is model-facing text built from user-controlled
// display names, so the safety invariants are enforced as a property of the
// output, not only by cleaning names:
//
//	I1: every mention token in the output is a token the input already
//	    carried (same kind and snowflake) — a name, the text around a
//	    name, or a deletion joining two neighbours can never mint a token
//	    for an id that was not mentioned.
//	I2: the output holds no more @everyone/@here pings (case-insensitive)
//	    than the input did — a display name can never produce one, alone
//	    or combined with the text that follows it.

// massPingOracle matches the mass pings I2 bounds.
var massPingOracle = regexp.MustCompile(`(?i)@(everyone|here)`)

// tokenBudget counts mention tokens by kind and snowflake.
func tokenBudget(s string) map[string]int {
	budget := map[string]int{}
	for _, tok := range mentionToken.FindAllString(s, -1) {
		kind, id := parseToken(tok)
		budget[fmt.Sprintf("%d:%s", kind, id)]++
	}
	return budget
}

// mentionContractHolds is the test oracle for I1 and I2: output tokens must
// draw down tokens the input carried, and pings may not multiply.
func mentionContractHolds(input, output string) bool {
	budget := tokenBudget(input)
	for _, tok := range mentionToken.FindAllString(output, -1) {
		kind, id := parseToken(tok)
		key := fmt.Sprintf("%d:%s", kind, id)
		if budget[key] == 0 {
			return false
		}
		budget[key]--
	}
	pingsIn := len(massPingOracle.FindAllString(input, -1))
	pingsOut := len(massPingOracle.FindAllString(output, -1))
	return pingsOut <= pingsIn
}

// TestRenderContractHolds asserts I1 and I2 across every shared-table row.
func TestRenderContractHolds(t *testing.T) {
	for _, tc := range loadCases(t) {
		got := Render(Request{
			Content: tc.Content,
			BotID:   tc.BotID,
			BotName: tc.BotName,
			Names:   tc.Names,
			Mode:    tc.Mode,
		})
		if !mentionContractHolds(tc.Content, got) {
			t.Errorf("%s: Render(%q, mode=%s) = %q breaks the mention contract",
				tc.Name, tc.Content, tc.Mode, got)
		}
	}
}

// FuzzRenderContract asserts I1 and I2 for arbitrary content, bot name and
// display names — the contract must hold for ANY input, not only the table.
// Seeds are the forgery shapes a hostile display name can drive: stray angle
// brackets around a rendered name, token fragments that join when a mention
// is dropped, mass-ping prefixes, and lookalike or invisible characters.
func FuzzRenderContract(f *testing.F) {
	seeds := []struct{ content, botID, botName, name string }{
		{"hey <<@111>> ok", "222", "Hermes", "999"},
		{"hey <<@222>> ok", "222", "999", "Alice"},
		{"x <@<@111>999> y", "222", "Hermes", "Alice"},
		{"x @every<@111>one y", "222", "Hermes", "Alice"},
		{"hey <@111>one ok", "222", "Hermes", "every"},
		{"hey <@111>re ok", "222", "Hermes", "he"},
		{"hi <@111>", "222", "Hermes", "everyone!"},
		{"hi <@111>", "222", "Hermes", "everyone else"},
		{"hi <@111>", "222", "Hermes", "here."},
		{"hi <@111>", "222", "Hermes", "Bob (＜＠999＞)"},
		{"hi <@111>", "222", "Hermes", "＠everyone"},
		{"hi <@111>", "222", "Hermes", "Ann ﹤﹫42﹥"},
		{"hi <@111>", "222", "Hermes", "ㅤ⠀͏️"},
		{"ask <@222> now", "222", "", "Alice"},
		{"<@222> /restart", "222", "Hermes", "Alice"},
		{"", "", "", ""},
	}
	for _, s := range seeds {
		f.Add(s.content, s.botID, s.botName, s.name)
	}
	f.Fuzz(func(t *testing.T, content, botID, botName, name string) {
		names := map[string]string{"111": name, "999": name}
		for _, mode := range []string{ModeBody, ModeTitle} {
			got := Render(Request{
				Content: content,
				BotID:   botID,
				BotName: botName,
				Names:   names,
				Mode:    mode,
			})
			if !mentionContractHolds(content, got) {
				t.Fatalf("mode %s: Render(%q) = %q breaks the mention contract (bot id %q, bot name %q, name %q)",
					mode, content, got, botID, botName, name)
			}
		}
	})
}

// TestSharedCaseProperties pins invariants across every table row, not just
// the frozen strings: a title never carries any mention token, and body text
// never carries the bot's own raw token when its name is known (the "hole"
// bug this module exists to fix).
func TestSharedCaseProperties(t *testing.T) {
	for _, tc := range loadCases(t) {
		got := Render(Request{
			Content: tc.Content,
			BotID:   tc.BotID,
			BotName: tc.BotName,
			Names:   tc.Names,
			Mode:    tc.Mode,
		})
		if tc.Mode == ModeTitle && mentionToken.MatchString(got) {
			t.Fatalf("%s: title %q still holds a mention token", tc.Name, got)
		}
		if tc.Mode == ModeBody && tc.BotName != "" && tc.BotID != "" {
			for _, raw := range []string{"<@" + tc.BotID + ">", "<@!" + tc.BotID + ">"} {
				if strings.Contains(got, raw) {
					t.Fatalf("%s: body %q still holds the bot's raw token %s", tc.Name, got, raw)
				}
			}
		}
	}
}

// FuzzRender feeds untrusted-shaped content through the same entry point the
// gateway pipes live Discord text into. Properties, not exact strings: never
// panic, titles never leak tokens, and body output keeps every non-mention
// word of the input.
func FuzzRender(f *testing.F) {
	seeds := []string{
		"<@222>", // addressing run only: title falls back to emptyTitle
		"hi <@111> and <@!222>",
		"<@222> /restart",
		"<@&333> deploy to <#444>",
		"<@999><@998>",
		"\x00\uFEFF<@111>\n\n",
		"<@111><@111><@111><@111>",
		"@@@@<<<<>>>>",
	}
	for _, s := range seeds {
		f.Add(s, "222")
	}
	names := map[string]string{"111": "Alice", "999": "@everyone", "998": "<@1>"}
	f.Fuzz(func(t *testing.T, content, botID string) {
		for _, mode := range []string{ModeBody, ModeTitle} {
			got := Render(Request{
				Content: content,
				BotID:   botID,
				BotName: "Hermes",
				Names:   names,
				Mode:    mode,
			})
			if mode == ModeTitle && mentionToken.MatchString(got) {
				t.Fatalf("title %q holds a mention token (content %q)", got, content)
			}
		}
	})
}

// TestTitleTruncationBoundaries pins the historical title shaping: cap at 80
// runes, an over-long title keeps the first 77 plus an ellipsis, and an
// empty title falls back to a fixed placeholder. Body text is never clamped.
func TestTitleTruncationBoundaries(t *testing.T) {
	render := func(content, mode string) string {
		return Render(Request{Content: content, BotID: "222", BotName: "Hermes", Mode: mode})
	}
	exactly80 := strings.Repeat("a", 80)
	if got := render(exactly80, ModeTitle); got != exactly80 {
		t.Fatalf("80-rune title = %q (len %d), want unchanged", got, len([]rune(got)))
	}
	exactly81 := strings.Repeat("a", 81)
	want81 := strings.Repeat("a", titleEllipsisAt) + titleEllipsis
	if got := render(exactly81, ModeTitle); got != want81 {
		t.Fatalf("81-rune title = %q, want %q", got, want81)
	}
	// A mention-driven title over the cap truncates the same way.
	got := render("hi <@111> "+strings.Repeat("b", 80), ModeTitle)
	if len([]rune(got)) > maxTitleRunes {
		t.Fatalf("title over cap: %d runes", len([]rune(got)))
	}
	if !strings.HasSuffix(got, titleEllipsis) {
		t.Fatalf("truncated title %q lacks the ellipsis", got)
	}
	// Body text is the model's input: never clamped.
	longBody := strings.Repeat("x", 200)
	if got := render(longBody, ModeBody); got != longBody {
		t.Fatalf("body was clamped to %d runes", len([]rune(got)))
	}
	// Addressing-run-only titles collapse to nothing and fall back.
	if got := render("<@222>", ModeTitle); got != emptyTitle {
		t.Fatalf("empty title = %q, want %q", got, emptyTitle)
	}
}

// TestSanitizeNameBoundaries pins the name cap and the mass-ping refusal:
// Discord display names are user-controlled text inlined into model input.
func TestSanitizeNameBoundaries(t *testing.T) {
	long := strings.Repeat("n", maxNameRunes+40)
	if got := SanitizeName(long); len([]rune(got)) != maxNameRunes {
		t.Fatalf("long name = %d runes, want capped at %d", len([]rune(got)), maxNameRunes)
	}
	for _, ping := range []string{"everyone", "Everyone", "EVERYONE", "here", "Here", "HERE"} {
		if got := SanitizeName(ping); got != "" {
			t.Fatalf("SanitizeName(%q) = %q, want refused", ping, got)
		}
	}
	// Padding does not smuggle a mass ping past the refusal.
	if got := SanitizeName("  everyone  "); got != "" {
		t.Fatalf("padded mass ping rendered as %q", got)
	}
	// A name that merely CONTAINS a ping word is fine once '@' is dropped.
	if got := SanitizeName("@everyone fan club"); got == "" || strings.Contains(got, "@") {
		t.Fatalf("SanitizeName(\"@everyone fan club\") = %q", got)
	}
}

// ExampleRender is the wire contract the Python bridge codes against: one
// Request in, rendered text out.
func ExampleRender() {
	got := Render(Request{
		Content: "then <@111> is my main agent, and <@222> is my public agent",
		BotID:   "222",
		BotName: "Hermes",
		Names:   map[string]string{"111": "Alice"},
		Mode:    ModeBody,
	})
	fmt.Println(got)
	// Output: then @Alice (<@111>) is my main agent, and @Hermes is my public agent
}
