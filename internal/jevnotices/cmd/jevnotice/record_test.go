package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/QuixThe2nd/hermes-ide/internal/jevnotices"
)

type sharedCase struct {
	Name   string         `json:"name"`
	Record map[string]any `json:"record"`
	Expect string         `json:"expect"`
}

// TestRunRendersTheSharedTable drives every shared contract case through the
// command itself — JSON decoding and noticeFromRecord included — so the
// mapping the production binary uses is pinned in Go, not only through the
// Python bridge (the package tests build Notice values directly).
func TestRunRendersTheSharedTable(t *testing.T) {
	raw, err := os.ReadFile("../../testdata/notice_cases.json")
	if err != nil {
		t.Fatalf("read shared cases: %v", err)
	}
	var table struct {
		Cases []sharedCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil || len(table.Cases) == 0 {
		t.Fatalf("parse shared cases: %v (%d cases)", err, len(table.Cases))
	}
	for _, tc := range table.Cases {
		t.Run(tc.Name, func(t *testing.T) {
			record, err := json.Marshal(tc.Record)
			if err != nil {
				t.Fatalf("marshal record: %v", err)
			}
			out, errOut, code := runRecord(t, string(record))
			if code != 0 || errOut != "" {
				t.Fatalf("exit=%d stderr=%q", code, errOut)
			}
			if got := strings.TrimSuffix(out, "\n"); got != tc.Expect {
				t.Fatalf("run mismatch\n got:  %q\n want: %q", got, tc.Expect)
			}
		})
	}
}

// TestNoticeFromRecordDecoding pins the tolerant decoding of a record: the
// gateway passes Python values through json.dumps, so numbers may arrive as
// strings, optional fields as null, and a malformed field must drop out
// rather than fail the whole notice.
func TestNoticeFromRecordDecoding(t *testing.T) {
	cases := []struct {
		name   string
		record string
		check  func(n jevnotices.Notice) bool
	}{
		{"string attempt numbers parse", `{"attempt":"2","attempts":"3"}`,
			func(n jevnotices.Notice) bool { return n.Attempt == 2 && n.Attempts == 3 }},
		{"unparseable attempt is zero", `{"attempt":"two","attempts":true}`,
			func(n jevnotices.Notice) bool { return n.Attempt == 0 && n.Attempts == 0 }},
		{"string confidence parses and is present", `{"confidence":"0.5"}`,
			func(n jevnotices.Notice) bool { return n.HasConfidence && n.Confidence == 0.5 }},
		{"null confidence is absent, not zero", `{"confidence":null}`,
			func(n jevnotices.Notice) bool { return !n.HasConfidence }},
		{"junk confidence is absent", `{"confidence":"high"}`,
			func(n jevnotices.Notice) bool { return !n.HasConfidence }},
		{"non-bool allowed is no decision", `{"allowed":"true"}`,
			func(n jevnotices.Notice) bool { return n.Allowed == nil }},
		{"bool allowed is kept", `{"allowed":false}`,
			func(n jevnotices.Notice) bool { return n.Allowed != nil && !*n.Allowed }},
		{"junk scores drop out individually", `{"scores":{"noise":"x","addresses_bot":0.25,"extra":"0.5"}}`,
			func(n jevnotices.Notice) bool {
				_, junk := n.Scores["noise"]
				return !junk && n.Scores["addresses_bot"] == 0.25 && n.Scores["extra"] == 0.5
			}},
		{"non-mapping scores are ignored", `{"scores":[1,2]}`,
			func(n jevnotices.Notice) bool { return len(n.Scores) == 0 }},
		{"non-string text fields are ignored", `{"purpose":7,"author":["a"],"error":false}`,
			func(n jevnotices.Notice) bool { return n.Purpose == "" && n.Author == "" && n.Error == "" }},
		{"abstain is present only when numeric", `{"abstain":"0.7"}`,
			func(n jevnotices.Notice) bool { return n.HasAbstain && n.Abstain == 0.7 }},
		{"unknown keys are ignored", `{"purpose":"goal-intent","prompt":"secret text"}`,
			func(n jevnotices.Notice) bool { return n.Purpose == "goal-intent" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var record map[string]any
			if err := json.Unmarshal([]byte(tc.record), &record); err != nil {
				t.Fatalf("bad fixture: %v", err)
			}
			if n := noticeFromRecord(record); !tc.check(n) {
				t.Fatalf("noticeFromRecord(%s) = %+v", tc.record, n)
			}
		})
	}
}
