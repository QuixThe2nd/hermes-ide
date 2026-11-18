// Command jevnotice is the executable form of the canonical Go formatter:
// it reads one notice record as JSON on stdin (the same record shape as
// testdata/notice_cases.json — purpose, subject, author, attempt, attempts,
// label, confidence, allowed, scores, emoji, abstain, error) and writes the
// rendered notice line to stdout.
//
// This is the bridge the Python tree executes: the contract test in
// tests/gateway/test_jev_notice_contract.py pipes every shared-table record
// through this binary and asserts gateway/jev_notice.py renders the identical
// line, so the twin is verified against the running Go implementation, not
// merely against the table. Production gateways do not shell out to it —
// deployments (pip, docker) carry no Go toolchain, the same reason every
// other internal/* twin is executed by tests rather than at runtime.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strconv"

	"github.com/QuixThe2nd/hermes-ide/internal/jevnotices"
)

func main() {
	var record map[string]any
	if err := json.NewDecoder(os.Stdin).Decode(&record); err != nil {
		fmt.Fprintf(os.Stderr, "jevnotice: unreadable record: %v\n", err)
		os.Exit(2)
	}
	fmt.Println(jevnotices.Format(noticeFromRecord(record)))
}

// noticeFromRecord maps the shared record shape onto Notice. Presence, not
// value, decides the optional verdict fields (a null confidence is not a 0).
func noticeFromRecord(record map[string]any) jevnotices.Notice {
	n := jevnotices.Notice{Scores: map[string]float64{}}
	for key, value := range record {
		switch key {
		case "purpose", "subject", "author", "label", "emoji", "error":
			if s, ok := value.(string); ok {
				switch key {
				case "purpose":
					n.Purpose = s
				case "subject":
					n.Subject = s
				case "author":
					n.Author = s
				case "label":
					n.Label = s
				case "emoji":
					n.Emoji = s
				case "error":
					n.Error = s
				}
			}
		case "attempt":
			n.Attempt = intValue(value)
		case "attempts":
			n.Attempts = intValue(value)
		case "confidence":
			if f, ok := floatValue(value); ok {
				n.Confidence, n.HasConfidence = f, true
			}
		case "allowed":
			if b, ok := value.(bool); ok {
				n.Allowed = &b
			}
		case "scores":
			if m, ok := value.(map[string]any); ok {
				keys := make([]string, 0, len(m))
				for k := range m {
					keys = append(keys, k)
				}
				sort.Strings(keys) // deterministic fill; Format sorts anyway
				for _, k := range keys {
					if f, ok := floatValue(m[k]); ok {
						n.Scores[k] = f
					}
				}
			}
		case "abstain":
			if f, ok := floatValue(value); ok {
				n.Abstain, n.HasAbstain = f, true
			}
		}
	}
	return n
}

func intValue(v any) int {
	switch t := v.(type) {
	case float64:
		return int(t)
	case string:
		i, _ := strconv.Atoi(t)
		return i
	default:
		return 0
	}
}

func floatValue(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case string:
		f, err := strconv.ParseFloat(t, 64)
		return f, err == nil
	default:
		return 0, false
	}
}
