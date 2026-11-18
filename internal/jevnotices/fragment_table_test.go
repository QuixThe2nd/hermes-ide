package jevnotices

import (
	"fmt"
	"strings"
	"testing"
)

// TestSubjectAuthorFragmentTable pins the head of the line (purpose, attempt,
// subject, author) across representative fragment shapes. The tail (verdict)
// is covered by the verdict tests; this table keeps the joinery — spaces,
// "from", ":" — byte-stable, because the Python twin must match it exactly.
func TestSubjectAuthorFragmentTable(t *testing.T) {
	cases := []struct {
		name   string
		notice Notice
		want   string
	}{
		{
			"plain subject and author",
			Notice{Purpose: PurposeGoalIntent, Subject: "user message", Author: "Alice"},
			Prefix + "goal-intent user message from Alice:",
		},
		{
			"author omitted when empty",
			Notice{Purpose: PurposeGoalIntent, Subject: "user message"},
			Prefix + "goal-intent user message:",
		},
		{
			"author omitted when whitespace only",
			Notice{Purpose: PurposeGoalIntent, Subject: "user message", Author: " \t "},
			Prefix + "goal-intent user message:",
		},
		{
			"subject collapses internal runs",
			Notice{Purpose: PurposeResponseGate, Subject: "user   message", Author: "Tek"},
			Prefix + "response-gate user message from Tek:",
		},
		{
			"subject falls back when empty",
			Notice{Purpose: PurposeReactionGate, Subject: "", Author: "Tek"},
			Prefix + "reaction-gate message from Tek:",
		},
		{
			"assistant subject renders verbatim",
			Notice{Purpose: PurposeGoalIntent, Subject: "assistant reply"},
			Prefix + "goal-intent assistant reply:",
		},
		{
			"attempt fragment sits between purpose and subject",
			Notice{Purpose: PurposeGoalIntent, Subject: "user message", Attempt: 2, Attempts: 3},
			Prefix + "goal-intent (attempt 2/3) user message:",
		},
		{
			"attempt fragment with author",
			Notice{Purpose: PurposeGoalIntent, Subject: "user message", Author: "Bob", Attempt: 1, Attempts: 2},
			Prefix + "goal-intent (attempt 1/2) user message from Bob:",
		},
		{
			"purpose is sanitized like a token",
			Notice{Purpose: "goal intent!", Subject: "user message"},
			Prefix + "goal_intent user message:",
		},
		{
			"author strips ping markers",
			Notice{Purpose: PurposeResponseGate, Subject: "user message", Author: "@channel"},
			Prefix + "response-gate user message from channel:",
		},
		{
			"unicode author survives",
			Notice{Purpose: PurposeReactionGate, Subject: "user message", Author: "北原"},
			Prefix + "reaction-gate user message from 北原:",
		},
		{
			"long author is capped not dropped",
			Notice{Purpose: PurposeResponseGate, Subject: "user message", Author: strings.Repeat("x", 200)},
			Prefix + "response-gate user message from " + strings.Repeat("x", 80) + ":",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// Head comparison: render with a fixed verdict tail and cut there.
			line := Format(tc.notice)
			if !strings.HasPrefix(line, tc.want) {
				t.Fatalf("head mismatch\n got:  %q\n want: %q", line, tc.want)
			}
			if !strings.HasSuffix(line, ": ") && !strings.Contains(line, ": ") {
				t.Fatalf("line %q lost the verdict separator", line)
			}
		})
	}
}

// TestFragmentHeadNeverEndsInDoubledSpaces: sanitization joinery bugs show up
// as doubled or trailing spaces before the verdict; sweep the fragment table
// shapes to make sure none can produce one.
func TestFragmentHeadNeverEndsInDoubledSpaces(t *testing.T) {
	shapes := []Notice{
		{Purpose: PurposeGoalIntent, Subject: "user message", Author: ""},
		{Purpose: PurposeGoalIntent, Subject: "  ", Author: "   "},
		{Purpose: PurposeResponseGate, Subject: "user message", Author: "@", Attempt: 1, Attempts: 2},
		{Purpose: PurposeReactionGate, Subject: "user message", Author: "` `"},
	}
	for i, n := range shapes {
		line := Format(n)
		if strings.Contains(line, "  ") {
			t.Errorf("shape %d produced doubled spaces: %q", i, line)
		}
		if strings.HasSuffix(strings.TrimRight(line, " "), ": ") == false && !strings.Contains(line, ": ") {
			t.Errorf("shape %d lost its separator: %q", i, line)
		}
	}
}

// ExampleFormat shows the three caller shapes end to end; the output block is
// part of the contract (the Python twin renders the same lines — see
// testdata/notice_cases.json for the full shared table).
func ExampleFormat() {
	allowed := true
	fmt.Println(Format(Notice{
		Purpose: PurposeGoalIntent, Subject: "user message", Author: "Alice",
		Attempt: 1, Attempts: 2, Label: "task", HasConfidence: true, Confidence: 0.82,
	}))
	fmt.Println(Format(Notice{
		Purpose: PurposeResponseGate, Subject: "user message", Author: "Tek",
		Allowed: &allowed,
		Scores: map[string]float64{
			"addresses_bot": 0.91, "continues_bot_thread": 0.12,
			"joins_bot_thread": 0.03, "noise": 0.05,
		},
	}))
	fmt.Println(Format(Notice{
		Purpose: PurposeReactionGate, Subject: "user message", Author: "Tek",
		HasAbstain: true, Abstain: 0.63,
	}))
	// Output:
	// 🔎 jev goal-intent (attempt 1/2) user message from Alice: task (confidence 0.82)
	// 🔎 jev response-gate user message from Tek: allow (addresses_bot=0.91 continues_bot_thread=0.12 joins_bot_thread=0.03 noise=0.05)
	// 🔎 jev reaction-gate user message from Tek: no reaction (abstain 0.63)
}
