// Command jevnotice is the shared executable boundary for Jev notices and
// retrospective goal-intent classification. With no "op", it reads one notice
// record as JSON on stdin and writes the rendered line (or nothing when the
// owning profile disables notices). With op=classify_retrospective, it reads
// one completed exchange and its scoped credential on stdin and writes a
// structured RetrospectiveResult; classification and formatting stay in Go.
//
// The production gateway resolves this binary from a content-hash-keyed cache
// under $HERMES_HOME/cache/jevnotices and builds it from the repo checkout when
// missing. tests/gateway/test_jev_notice_contract.py drives the formatting
// contract over testdata/notice_cases.json.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strconv"

	"github.com/QuixThe2nd/hermes-ide/internal/jevnotices"
)

func main() {
	os.Exit(run(os.Stdin, os.Stdout, os.Stderr))
}

// run is main over explicit streams: one record in, the notice line out (or
// nothing for a silenced record), and the process exit code returned
// (2 = unreadable record).
func run(stdin io.Reader, stdout, stderr io.Writer) int {
	data, err := io.ReadAll(io.LimitReader(stdin, (8<<20)+1))
	if err != nil || len(data) > 8<<20 {
		fmt.Fprintln(stderr, "jevnotice: unreadable record")
		return 2
	}
	var envelope struct {
		Op string `json:"op"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		fmt.Fprintf(stderr, "jevnotice: unreadable record: %v\n", err)
		return 2
	}
	switch envelope.Op {
	case "":
	case "classify_retrospective":
		return runRetrospective(data, stdout, stderr)
	default:
		fmt.Fprintln(stderr, "jevnotice: unsupported operation")
		return 2
	}
	var record map[string]any
	if err := json.Unmarshal(data, &record); err != nil {
		fmt.Fprintf(stderr, "jevnotice: unreadable record: %v\n", err)
		return 2
	}
	if !jevnotices.NoticesEnabled(record) {
		return 0 // the owning profile turned notices off: no line, not an error
	}
	fmt.Fprintln(stdout, jevnotices.Format(noticeFromRecord(record)))
	return 0
}

func runRetrospective(data []byte, stdout, stderr io.Writer) int {
	var request jevnotices.RetrospectiveRequest
	if err := json.Unmarshal(data, &request); err != nil {
		fmt.Fprintf(stderr, "jevnotice: unreadable retrospective request: %v\n", err)
		return 2
	}
	result := jevnotices.ClassifyRetrospective(
		context.Background(), jevnotices.DefaultRetrospectiveClient(), jevnotices.JEVDecisionsURL, request,
	)
	if err := json.NewEncoder(stdout).Encode(result); err != nil {
		fmt.Fprintln(stderr, "jevnotice: could not encode retrospective result")
		return 2
	}
	return 0
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
