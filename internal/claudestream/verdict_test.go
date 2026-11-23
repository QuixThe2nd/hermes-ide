package claudestream

import "testing"

func TestWatchdogVerdictExactTokens(t *testing.T) {
	for _, token := range []string{"timeout", "interrupted"} {
		got, ok := WatchdogVerdict(token)
		if !ok || got != token {
			t.Errorf("WatchdogVerdict(%q) = %q, %v; want %q, true", token, got, ok, token)
		}
	}
}

func TestWatchdogVerdictBlockerEnrichedPrefixes(t *testing.T) {
	cases := []struct {
		text string
		want string
	}{
		{
			"timeout: repeated TaskCreate attempts without progress: last tool TaskCreate, reason x",
			"timeout",
		},
		{"timeout Claude Code burned the budget", "timeout"},
		{"interrupted: repeated Read attempts without progress: last tool Read, reason denied", "interrupted"},
		{"interrupted by /stop", "interrupted"},
	}
	for _, tc := range cases {
		got, ok := WatchdogVerdict(tc.text)
		if !ok || got != tc.want {
			t.Errorf("WatchdogVerdict(%q) = %q, %v; want %q, true", tc.text, got, ok, tc.want)
		}
	}
}

func TestWatchdogVerdictRejectsNonVerdicts(t *testing.T) {
	cases := []string{
		"",
		"timeoutfoo",
		"interrupted-tool",
		"a timeout occurred",
		"Claude Code exited with code 1: timeout text in tail",
		"tool_loop: repeated TaskCreate attempts without progress",
	}
	for _, text := range cases {
		if got, ok := WatchdogVerdict(text); ok {
			t.Errorf("WatchdogVerdict(%q) = %q, true; want \"\", false", text, got)
		}
	}
}

func TestWatchdogVerdictTimeoutWinsWhenBothCouldMatch(t *testing.T) {
	// "timeout" is checked first; a text cannot start with both tokens, so
	// this pins the precedence order the status collapse relies on.
	got, ok := WatchdogVerdict("timeout: interrupted downstream")
	if !ok || got != "timeout" {
		t.Errorf("WatchdogVerdict = %q, %v; want timeout, true", got, ok)
	}
}
