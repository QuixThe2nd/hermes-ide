// Package goalrun decides what one gateway turn does with its chat delivery
// while a persistent goal is active: the run stays silent in the chat, its
// progress goes to the statusbar, and the turn that ends the run is the one
// message the chat gets.
package goalrun

import (
	"fmt"
	"strings"
	"unicode"
)

const (
	// The objective lives in the bar header, so the line itself only carries
	// progress; both bounds keep the turn counter visible on a narrow phone.
	maxObjective = 60
	maxStep      = 80
)

// Turn is one turn's goal facts, as the gateway knows them at the post-turn
// boundary.
type Turn struct {
	Active         bool   `json:"active"`
	ShouldContinue bool   `json:"should_continue"`
	Turn           int    `json:"turn"`
	MaxTurns       int    `json:"max_turns"`
	Objective      string `json:"objective"`
	LastStep       string `json:"last_step"`
}

// Plan is what the turn does with its chat delivery and its statusbar line.
type Plan struct {
	// Suppress keeps this turn's reply out of the chat.
	Suppress bool `json:"suppress"`
	// Final marks the turn that ended the run; its reply is delivered.
	Final bool `json:"final"`
	// Objective is the sanitized run objective for the bar header.
	Objective string `json:"objective"`
	// Line is the one statusbar line for this turn.
	Line string `json:"line"`
}

// PlanForTurn maps one turn's facts to a Plan.
//
// No goal, no change: an inactive run returns the zero Plan, so a conversation
// without a goal suppresses nothing and claims no final turn. While the run
// continues the reply stays out of the chat; the turn that ends the run (goal
// achieved, exhausted or paused) delivers its answer.
func PlanForTurn(t Turn) Plan {
	if !t.Active {
		return Plan{}
	}
	plan := Plan{
		Objective: oneLine(t.Objective, maxObjective),
		Line:      progressLine(t.Turn, t.MaxTurns, oneLine(t.LastStep, maxStep)),
	}
	if t.ShouldContinue {
		plan.Suppress = true
	} else {
		plan.Final = true
	}
	return plan
}

// progressLine renders "🎯 turn 7/1000 · <step>", degrading to the parts that
// are actually known.
func progressLine(turn, max int, step string) string {
	var head string
	switch {
	case max > 0 && turn > 0:
		head = fmt.Sprintf("🎯 turn %d/%d", turn, max)
	case turn > 0:
		head = fmt.Sprintf("🎯 turn %d", turn)
	default:
		head = "🎯"
	}
	if step == "" {
		return head
	}
	return head + " · " + step
}

// oneLine flattens arbitrary text to a single bounded line: whitespace, control
// runes and format runes (bidi overrides, joiners) collapse to one space, and the
// result is capped at limit runes with an ellipsis.
func oneLine(text string, limit int) string {
	var b strings.Builder
	pendingSpace := false
	for _, r := range text {
		if unicode.IsSpace(r) || unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			pendingSpace = b.Len() > 0
			continue
		}
		if pendingSpace {
			b.WriteByte(' ')
			pendingSpace = false
		}
		b.WriteRune(r)
	}
	out := b.String()
	runes := []rune(out)
	if limit > 0 && len(runes) > limit {
		out = string(runes[:limit-1]) + "…"
	}
	return out
}
