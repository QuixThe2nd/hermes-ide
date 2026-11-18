// Package jevnotices composes the concise chat notice line posted for every
// real Jev classification request (goal-intent, the Discord response gate,
// the Discord reaction gate).
//
// The notice is outbound-only troubleshooting surface: purpose, classified
// subject, the actual decision/confidence (negative, chat, low-confidence and
// no-reaction outcomes included), a sanitized error class on failure, and the
// attempt number for retried callers. It NEVER carries request/response
// bodies, credentials, message content or prompt text — the inputs are ids,
// display names, label/score/confidence numbers and already-sanitized error
// tokens.
//
// This package is the canonical formatter and is executable as
// cmd/jevnotice (record JSON on stdin, notice line on stdout). The Python
// twin (gateway/jev_notice.py) renders the lines the gateway actually sends
// — deployments carry no Go toolchain — and is pinned against the RUNNING
// Go binary by tests/gateway/test_jev_notice_contract.py, plus the shared
// table in testdata/notice_cases.json which each side's tests execute, so
// the two implementations cannot drift apart silently.
package jevnotices

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode"
)

// Purpose identifies which Jev caller made the request.
const (
	PurposeGoalIntent   = "goal-intent"
	PurposeResponseGate = "response-gate"
	PurposeReactionGate = "reaction-gate"
)

// scoreKeys is the fixed display order of the response gate's four component
// nouls (the judge's question order). Keys missing from Scores render as 0.00.
var scoreKeys = []string{
	"addresses_bot",
	"continues_bot_thread",
	"joins_bot_thread",
	"noise",
}

// tokenRE is what a sanitized error/label token may carry after cleaning.
var tokenRE = regexp.MustCompile(`[^A-Za-z0-9_.\-]+`)

const (
	// maxTokenLen bounds any single sanitized token in the line.
	maxTokenLen = 48
	// maxAuthorLen bounds the display-name fragment.
	maxAuthorLen = 80
	// Prefix starts every notice line.
	Prefix = "🔎 jev "
)

// Notice is one Jev attempt's outcome, in the shape the owning caller fills.
// Exactly one verdict source is set: Label+Confidence (goal-intent success),
// Allowed+Scores (response gate), Emoji/Abstain (reaction gate), or Error
// (any caller's failed attempt). Attempt/Attempts carry retry numbering;
// Attempts <= 1 hides the attempt fragment (the gates never retry).
type Notice struct {
	Purpose       string
	Subject       string
	Author        string
	Attempt       int
	Attempts      int
	Label         string
	Confidence    float64
	HasConfidence bool
	Allowed       *bool
	Scores        map[string]float64
	Emoji         string
	Abstain       float64
	HasAbstain    bool
	Error         string
}

// SanitizeToken cleans one error/label fragment: anything from the first ':'
// on is dropped (judge helpers append human detail there — only the class
// token is notice-safe), any run of characters outside [A-Za-z0-9_.-] becomes
// a single underscore, the result bounded. Empty or fully unusable input
// yields "error" — a notice never fails to render because a judge returned
// garbage.
func SanitizeToken(raw string) string {
	head, _, _ := strings.Cut(raw, ":")
	cleaned := tokenRE.ReplaceAllString(strings.TrimSpace(head), "_")
	cleaned = strings.Trim(cleaned, "_")
	if cleaned == "" {
		return "error"
	}
	runes := []rune(cleaned)
	if len(runes) > maxTokenLen {
		cleaned = string(runes[:maxTokenLen])
	}
	return cleaned
}

// SanitizeAuthor cleans one display-name fragment: control characters and
// collapsed whitespace removed, bounded. Empty result means "omit author".
func SanitizeAuthor(raw string) string {
	var b strings.Builder
	space := false
	count := 0
	for _, r := range raw {
		switch {
		case unicode.IsSpace(r):
			space = count > 0
		case unicode.IsControl(r) || r == '`' || r == '@':
			// Controls never render; backticks and leading-@ pings would make
			// the fragment actionable chat syntax, which a notice must not be.
		default:
			if space {
				b.WriteByte(' ')
				space = false
			}
			b.WriteRune(r)
			count++
		}
		if count >= maxAuthorLen {
			break
		}
	}
	return strings.TrimSpace(b.String())
}

// SanitizeText cleans one free-text fragment (the subject): control
// characters stripped, whitespace collapsed to single spaces, bounded. Empty
// result yields "message" so the line still names its subject.
func SanitizeText(raw string) string {
	var b strings.Builder
	space := false
	count := 0
	for _, r := range raw {
		switch {
		case unicode.IsSpace(r):
			space = count > 0
		case unicode.IsControl(r):
		default:
			if space {
				b.WriteByte(' ')
				space = false
			}
			b.WriteRune(r)
			count++
		}
		if count >= maxAuthorLen {
			break
		}
	}
	out := strings.TrimSpace(b.String())
	if out == "" {
		return "message"
	}
	return out
}

// Format renders the single notice line. Pure: no I/O, no clock, no locale
// (numbers are formatted with fixed 2-decimal rendering).
func Format(n Notice) string {
	var b strings.Builder
	b.WriteString(Prefix)
	b.WriteString(SanitizeToken(n.Purpose))
	if n.Attempts > 1 && n.Attempt > 0 {
		fmt.Fprintf(&b, " (attempt %d/%d)", n.Attempt, n.Attempts)
	}
	b.WriteByte(' ')
	b.WriteString(SanitizeText(n.Subject))
	if author := SanitizeAuthor(n.Author); author != "" {
		b.WriteString(" from ")
		b.WriteString(author)
	}
	b.WriteString(": ")
	b.WriteString(verdict(n))
	return b.String()
}

func verdict(n Notice) string {
	if n.Error != "" {
		return "failed (" + SanitizeToken(n.Error) + ")"
	}
	switch n.Purpose {
	case PurposeGoalIntent:
		if n.HasConfidence {
			return fmt.Sprintf("%s (confidence %s)", SanitizeToken(n.Label), fixed2(n.Confidence))
		}
		return SanitizeToken(n.Label) + " (confidence unavailable)"
	case PurposeResponseGate:
		if n.Allowed == nil {
			return "decision unavailable"
		}
		outcome := "deny"
		if *n.Allowed {
			outcome = "allow"
		}
		keys := append([]string(nil), scoreKeys...)
		scores := map[string]float64{}
		for k, v := range n.Scores {
			scores[k] = v
			found := false
			for _, known := range keys {
				if known == k {
					found = true
					break
				}
			}
			if !found {
				keys = append(keys, k)
			}
		}
		sort.Strings(keys[len(scoreKeys):])
		parts := make([]string, 0, len(keys))
		for _, k := range keys {
			parts = append(parts, k+"="+fixed2(scores[k]))
		}
		return outcome + " (" + strings.Join(parts, " ") + ")"
	case PurposeReactionGate:
		switch {
		case n.Emoji != "":
			return "react " + SanitizeAuthor(n.Emoji) + abstainSuffix(n)
		case n.HasAbstain:
			return "no reaction" + abstainSuffix(n)
		default:
			return "no reaction"
		}
	default:
		return "decision unavailable"
	}
}

func abstainSuffix(n Notice) string {
	if !n.HasAbstain {
		return ""
	}
	return " (abstain " + fixed2(n.Abstain) + ")"
}

// fixed2 renders a float with exactly two decimals, dot separator.
func fixed2(v float64) string {
	return fmt.Sprintf("%.2f", v)
}
