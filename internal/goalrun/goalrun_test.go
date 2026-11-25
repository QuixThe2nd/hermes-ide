package goalrun

import (
	"strings"
	"testing"
)

func TestPlanForTurnShapes(t *testing.T) {
	cases := []struct {
		name          string
		turn          Turn
		wantSuppress  bool
		wantFinal     bool
		wantLine      string
		wantObjective string
	}{
		{
			name: "inactive run changes nothing",
			turn: Turn{Active: false, ShouldContinue: true, Turn: 3, MaxTurns: 10, Objective: "ship", LastStep: "step"},
		},
		{
			name:          "continuing run is suppressed",
			turn:          Turn{Active: true, ShouldContinue: true, Turn: 3, MaxTurns: 10, Objective: "ship it", LastStep: "ran tests"},
			wantSuppress:  true,
			wantLine:      "🎯 turn 3/10 · ran tests",
			wantObjective: "ship it",
		},
		{
			name:          "finishing run is delivered",
			turn:          Turn{Active: true, ShouldContinue: false, Turn: 10, MaxTurns: 10, Objective: "ship it"},
			wantFinal:     true,
			wantLine:      "🎯 turn 10/10",
			wantObjective: "ship it",
		},
		{
			name:         "unknown turn counter degrades",
			turn:         Turn{Active: true, ShouldContinue: true},
			wantSuppress: true,
			wantLine:     "🎯",
		},
		{
			name:         "turn without max degrades",
			turn:         Turn{Active: true, ShouldContinue: true, Turn: 4, LastStep: "x"},
			wantSuppress: true,
			wantLine:     "🎯 turn 4 · x",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := PlanForTurn(tc.turn)
			if got.Suppress != tc.wantSuppress || got.Final != tc.wantFinal {
				t.Fatalf("suppress/final = %v/%v, want %v/%v", got.Suppress, got.Final, tc.wantSuppress, tc.wantFinal)
			}
			if got.Line != tc.wantLine {
				t.Fatalf("line = %q, want %q", got.Line, tc.wantLine)
			}
			if got.Objective != tc.wantObjective {
				t.Fatalf("objective = %q, want %q", got.Objective, tc.wantObjective)
			}
		})
	}
}

func TestPlanLineIsOneLine(t *testing.T) {
	got := PlanForTurn(Turn{
		Active: true, ShouldContinue: true, Turn: 1, MaxTurns: 2,
		Objective: "a\nb\tc\x00d\u202ee",
		LastStep:  "line1\nline2\r\nline3",
	})
	if strings.ContainsAny(got.Line, "\n\r\t\x00") {
		t.Fatalf("line carries control runes: %q", got.Line)
	}
	if got.Line != "🎯 turn 1/2 · line1 line2 line3" {
		t.Fatalf("line = %q", got.Line)
	}
	if strings.ContainsAny(got.Objective, "\n\r\t\x00") {
		t.Fatalf("objective carries control runes: %q", got.Objective)
	}
}

func TestOneLineCaps(t *testing.T) {
	long := strings.Repeat("a", 200)
	got := oneLine(long, maxStep)
	if n := len([]rune(got)); n != maxStep {
		t.Fatalf("rune length = %d, want %d", n, maxStep)
	}
	if !strings.HasSuffix(got, "…") {
		t.Fatalf("capped line has no ellipsis: %q", got)
	}
	if oneLine("  short  ", 10) != "short" {
		t.Fatalf("trim failed: %q", oneLine("  short  ", 10))
	}
	if oneLine("", 10) != "" {
		t.Fatalf("empty input must stay empty")
	}
}

// TestOneLineFlattensControlRunes keeps a chat line one line: a goal objective
// is user text, and newlines, bell bytes or a bidi override must never reach
// the statusbar or split a message.
func TestOneLineFlattensControlRunes(t *testing.T) {
	cases := []struct {
		in    string
		limit int
		want  string
	}{
		{"line\nbreak", 40, "line break"},
		{"  padded  ", 40, "padded"},
		{"tab\there", 40, "tab here"},
		{"bell\x07ring", 40, "bell ring"},
		{"bidi \u202eoverride\u202c", 40, "bidi override"},
		{"", 40, ""},
		{"abcdef", 5, "abcd…"},
		{"exactly", 7, "exactly"},
	}
	for _, tc := range cases {
		if got := oneLine(tc.in, tc.limit); got != tc.want {
			t.Errorf("oneLine(%q, %d) = %q, want %q", tc.in, tc.limit, got, tc.want)
		}
	}
}
