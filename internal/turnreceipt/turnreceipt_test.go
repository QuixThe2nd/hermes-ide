package turnreceipt

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

type fixtureCase struct {
	Name    string   `json:"name"`
	Input   Input    `json:"input"`
	InOrder []string `json:"in_order"`
	Absent  []string `json:"absent"`
}

func loadFixture(t *testing.T) []fixtureCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/receipt.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Cases []fixtureCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	return doc.Cases
}

func TestSharedFixtureInvariantsHold(t *testing.T) {
	for _, tc := range loadFixture(t) {
		t.Run(tc.Name, func(t *testing.T) {
			page := Render(tc.Input)
			cursor := 0
			for _, want := range tc.InOrder {
				i := strings.Index(page[cursor:], want)
				if i < 0 {
					t.Fatalf("missing %q after offset %d", want, cursor)
				}
				cursor += i + len(want)
			}
			for _, bad := range tc.Absent {
				if strings.Contains(page, bad) {
					t.Fatalf("page must not contain %q", bad)
				}
			}
		})
	}
}

func TestEveryInterpolatedValueIsEscaped(t *testing.T) {
	page := Render(Input{
		UserMessage: `<b onclick="x">&`,
		Rows:        []string{`it's "quoted" <i>`},
		FinalReply:  `</main><script>`,
	})
	for _, raw := range []string{`<b onclick`, `<i>`, `<script>`, `"quoted"`} {
		if strings.Contains(page, raw) {
			t.Fatalf("raw %q leaked into the page", raw)
		}
	}
	for _, escaped := range []string{"&lt;b onclick=&quot;x&quot;&gt;&amp;", "it&#39;s &quot;quoted&quot; &lt;i&gt;", "&lt;/main&gt;&lt;script&gt;"} {
		if !strings.Contains(page, escaped) {
			t.Fatalf("expected escaped %q", escaped)
		}
	}
}

func TestRowsRenderInInputOrderBetweenUserAndReply(t *testing.T) {
	page := Render(Input{UserMessage: "u", Rows: []string{"first", "second", "third"}, FinalReply: "r"})
	order := []string{`bubble user">u`, "first", "second", "third", `bubble reply">r`}
	last := -1
	for _, marker := range order {
		i := strings.Index(page, marker)
		if i <= last {
			t.Fatalf("%q out of order (at %d, previous %d)", marker, i, last)
		}
		last = i
	}
}

func TestEmptyRowsRenderNoProgressBubble(t *testing.T) {
	page := Render(Input{UserMessage: "u", FinalReply: "r"})
	if strings.Contains(page, "bubble progress") {
		t.Fatal("no rows must mean no progress bubble")
	}
	if !strings.Contains(page, "bubble user") || !strings.Contains(page, "bubble reply") {
		t.Fatal("user and reply bubbles must survive an empty row list")
	}
}

func TestOneProgressBubblePerRow(t *testing.T) {
	rows := []string{"one", "two", "three", "four", "five"}
	page := Render(Input{Rows: rows})
	if got := strings.Count(page, `bubble progress"`); got != len(rows) {
		t.Fatalf("got %d progress bubbles for %d rows", got, len(rows))
	}
}

func TestPageIsStandaloneWithNoExternalReferences(t *testing.T) {
	page := Render(Input{UserMessage: "u", Rows: []string{"r1"}, FinalReply: "f"})
	for _, ref := range []string{"<script", "src=", "href=", "http://", "https://", "url(", "@import"} {
		if strings.Contains(page, ref) {
			t.Fatalf("standalone page must not reference %q", ref)
		}
	}
}

func TestBlankPartsAreOmittedAndPageStillValid(t *testing.T) {
	page := Render(Input{UserMessage: " \n\t", Rows: []string{""}, FinalReply: ""})
	for _, class := range []string{"bubble user", "bubble progress", "bubble reply"} {
		if strings.Contains(page, class) {
			t.Fatalf("blank parts must not render %q", class)
		}
	}
	if !strings.HasPrefix(page, "<!doctype html>") || !strings.HasSuffix(page, "</html>\n") {
		t.Fatal("page must still be a complete document")
	}
}

func TestEachMarkupCharacterEscapesToItsEntity(t *testing.T) {
	entities := map[string]string{"&": "&amp;", "<": "&lt;", ">": "&gt;", `"`: "&quot;", "'": "&#39;"}
	for raw, want := range entities {
		if got := clean(raw); got != want {
			t.Fatalf("clean(%q) = %q, want %q", raw, got, want)
		}
	}
}

func TestControlCharactersAreStrippedExceptNewlineAndTab(t *testing.T) {
	var controls []rune
	for r := rune(0); r <= 0x1f; r++ {
		controls = append(controls, r)
	}
	for r := rune(0x7f); r <= 0x9f; r++ {
		controls = append(controls, r)
	}
	for _, r := range controls {
		got := clean("a" + string(r) + "b")
		want := "ab"
		if r == '\n' || r == '\t' {
			want = "a" + string(r) + "b"
		}
		if got != want {
			t.Fatalf("clean with control %U = %q, want %q", r, got, want)
		}
	}
}

func TestMultilineRowKeepsItsLineBreaks(t *testing.T) {
	page := Render(Input{Rows: []string{"first line\nsecond line"}})
	if !strings.Contains(page, "first line\nsecond line") {
		t.Fatal("row line breaks must survive; the CSS preserves them with pre-wrap")
	}
}
