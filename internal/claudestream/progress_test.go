package claudestream

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// Fixture builders mirroring tests/tools/test_claude_stream_progress.py:
// the exact stream-json shapes a delegated Claude Code run emits.

func use(callID, name string, input any) map[string]any {
	return map[string]any{"type": "tool_use", "id": callID, "name": name, "input": input}
}

func assistant(msgID string, uses ...map[string]any) string {
	content := make([]any, 0, len(uses))
	for _, u := range uses {
		content = append(content, u)
	}
	if len(content) == 0 {
		content = append(content, map[string]any{"type": "text", "text": "ok"})
	}
	return eventLine("assistant", msgID, "", content)
}

func assistantStopReason(stopReason string, uses ...map[string]any) string {
	content := make([]any, 0, len(uses))
	for _, u := range uses {
		content = append(content, u)
	}
	return eventLine("assistant", "", stopReason, content)
}

func eventLine(kind, msgID, stopReason string, content []any) string {
	msg := map[string]any{"role": "assistant", "content": content}
	if msgID != "" {
		msg["id"] = msgID
	}
	if stopReason != "" {
		msg["stop_reason"] = stopReason
	}
	raw, _ := json.Marshal(map[string]any{"type": kind, "message": msg})
	return string(raw)
}

func toolResult(callID, text string, isError any) string {
	block := map[string]any{
		"type": "tool_result", "tool_use_id": callID, "content": text,
	}
	if isError != nil {
		block["is_error"] = isError
	}
	raw, _ := json.Marshal(map[string]any{
		"type":    "user",
		"message": map[string]any{"role": "user", "content": []any{block}},
	})
	return string(raw)
}

// pair is one rejected-or-successful round trip: tool_use then result.
func pair(msgID, callID, tool string, input any, text string, isError any) string {
	return assistant(msgID, use(callID, tool, input)) + "\n" +
		toolResult(callID, text, isError)
}

func readPairs(n, start int) string {
	var b strings.Builder
	for i := start; i < start+n; i++ {
		fmt.Fprintf(&b, "%s\n%s\n",
			assistant(fmt.Sprintf("msg_r%d", i),
				use(fmt.Sprintf("call_r%d", i), "Read",
					map[string]any{"file_path": fmt.Sprintf("f%d.py", i)})),
			toolResult(fmt.Sprintf("call_r%d", i), fmt.Sprintf("contents %d", i), nil))
	}
	return strings.TrimSuffix(b.String(), "\n")
}

func rejectedTaskCreatePairs(n int, subject string) string {
	var b strings.Builder
	for i := 1; i <= n; i++ {
		fmt.Fprintf(&b, "%s\n", pair(fmt.Sprintf("msg_%d", i), fmt.Sprintf("call_%d", i),
			"TaskCreate", map[string]any{"subject": subject, "description": "d"},
			"rejected: tool not allowed", true))
	}
	return strings.TrimSuffix(b.String(), "\n")
}

func TestAPICallsCountsDistinctAssistantIDsNotSystemEvents(t *testing.T) {
	log := strings.Join([]string{
		assistant("msg_a", use("c1", "TaskCreate", map[string]any{"subject": "a"})),
		toolResult("c1", "Task #1 created successfully: a", nil),
		assistant("msg_b", use("c2", "TaskCreate", map[string]any{"subject": "b"})),
		toolResult("c2", "Task #2 created successfully: b", nil),
		`{"type":"system","subtype":"thinking_tokens","estimated_tokens":1}`,
	}, "\n")
	s := Summarize(log)
	if s.APICalls != 2 {
		t.Errorf("APICalls = %d, want 2 (thinking_tokens is not an api call)", s.APICalls)
	}
	if s.AssistantRecords != 2 {
		t.Errorf("AssistantRecords = %d, want 2", s.AssistantRecords)
	}
}

func TestAPICallsDuplicateIDCountsOnce(t *testing.T) {
	log := strings.Join([]string{
		assistant("msg_same", use("c1", "Read", map[string]any{"file_path": "a.py"})),
		toolResult("c1", "contents", nil),
		assistant("msg_same", use("c2", "Read", map[string]any{"file_path": "b.py"})),
		toolResult("c2", "contents", nil),
	}, "\n")
	if got := Summarize(log).APICalls; got != 1 {
		t.Errorf("APICalls = %d, want 1 (distinct ids)", got)
	}
}

func TestAPICallsFallsBackToStopReason(t *testing.T) {
	log := assistantStopReason("tool_use", use("c1", "Bash", map[string]any{"command": "pytest -q"}))
	if got := Summarize(log).APICalls; got != 1 {
		t.Errorf("APICalls = %d, want 1 via stop_reason fallback", got)
	}
}

func TestAPICallsFallsBackToEventCount(t *testing.T) {
	log := strings.Join([]string{
		assistant("", use("c1", "Read", map[string]any{"file_path": "a.py"})),
		assistant("", use("c2", "Read", map[string]any{"file_path": "b.py"})),
	}, "\n")
	if got := Summarize(log).APICalls; got != 2 {
		t.Errorf("APICalls = %d, want 2 via event-count fallback", got)
	}
}

func TestEmptyAndMalformedInputIsInert(t *testing.T) {
	for name, log := range map[string]string{
		"empty":          "",
		"blank lines":    "\n\n",
		"not json":       "not json line\n",
		"non-dict event": "[1,2]\n" + `{"type":"system"}` + "\n",
	} {
		s := Summarize(log)
		if s.APICalls != 0 || s.Abort || s.Blocker != "" {
			t.Errorf("%s: got %+v, want inert zero summary", name, s)
		}
	}
}

func TestThreeIdenticalRejectionsAbortWithIdenticalBlocker(t *testing.T) {
	s := Summarize(rejectedTaskCreatePairs(3, "fix login"))
	if !s.Abort {
		t.Fatalf("Abort = false, want true for 3 identical rejections")
	}
	for _, want := range []string{
		"repeated identical TaskCreate attempts without progress",
		"last tool TaskCreate",
		"rejected: tool not allowed",
	} {
		if !strings.Contains(s.Blocker, want) {
			t.Errorf("blocker %q missing %q", s.Blocker, want)
		}
	}
}

func TestTwoRejectionsDoNotAbort(t *testing.T) {
	s := Summarize(rejectedTaskCreatePairs(2, "fix login"))
	if s.Abort || s.Blocker != "" {
		t.Errorf("got abort=%v blocker=%q, want neither for 2 rejections", s.Abort, s.Blocker)
	}
}

func TestThreeRejectionsWithDifferentInputsAbortWithoutIdentical(t *testing.T) {
	var b strings.Builder
	for i := 1; i <= 3; i++ {
		fmt.Fprintf(&b, "%s\n", pair(fmt.Sprintf("msg_%d", i), fmt.Sprintf("call_%d", i),
			"TaskCreate", map[string]any{"subject": fmt.Sprintf("subject %d", i)},
			fmt.Sprintf("rejected: tool not allowed (%d)", i), true))
	}
	s := Summarize(strings.TrimSuffix(b.String(), "\n"))
	if !s.Abort {
		t.Fatalf("Abort = false, want true")
	}
	for _, want := range []string{"last tool TaskCreate", "rejected: tool not allowed (3)"} {
		if !strings.Contains(s.Blocker, want) {
			t.Errorf("blocker %q missing %q", s.Blocker, want)
		}
	}
	if strings.Contains(s.Blocker, "identical") {
		t.Errorf("blocker %q must not claim identical attempts", s.Blocker)
	}
}

func TestIsErrorFalseIsNeverARejection(t *testing.T) {
	var b strings.Builder
	for i := 1; i <= 3; i++ {
		fmt.Fprintf(&b, "%s\n", pair(fmt.Sprintf("msg_%d", i), fmt.Sprintf("call_%d", i),
			"TaskCreate", map[string]any{"subject": "s"},
			"rejected: tool not allowed", false))
	}
	s := Summarize(strings.TrimSuffix(b.String(), "\n"))
	if s.Abort {
		t.Errorf("is_error=false results must never abort, blocker=%q", s.Blocker)
	}
}

func TestFiveSuccessfulSameToolAttemptsSetTailBlockerWithoutAbort(t *testing.T) {
	var b strings.Builder
	for i := 1; i <= 5; i++ {
		fmt.Fprintf(&b, "%s\n", pair(fmt.Sprintf("msg_%d", i), fmt.Sprintf("call_%d", i),
			"TaskCreate", map[string]any{"subject": fmt.Sprintf("s%d", i)},
			fmt.Sprintf("Task #%d created successfully: s%d", i, i), nil))
	}
	s := Summarize(strings.TrimSuffix(b.String(), "\n"))
	if s.Abort {
		t.Fatalf("successful attempts must not abort")
	}
	for _, want := range []string{
		"repeated TaskCreate attempts without progress",
		"last tool TaskCreate",
		"Task #5 created successfully: s5",
	} {
		if !strings.Contains(s.Blocker, want) {
			t.Errorf("blocker %q missing %q", s.Blocker, want)
		}
	}
}

func TestProgressToolAttemptSuppressesTailBlocker(t *testing.T) {
	log := strings.Join([]string{
		pair("msg_e", "c_e", "Edit", map[string]any{"file_path": "a.py"},
			"The file a.py has been updated.", nil),
		readPairs(5, 1),
	}, "\n")
	s := Summarize(log)
	if s.Blocker != "" || s.Abort {
		t.Errorf("got blocker=%q abort=%v, want none: an Edit was attempted", s.Blocker, s.Abort)
	}
}

func TestProgressToolBetweenRejectionsPreventsAbort(t *testing.T) {
	log := strings.Join([]string{
		pair("m1", "c1", "TaskCreate", map[string]any{"subject": "s"},
			"rejected: tool not allowed", true),
		pair("m2", "c2", "Edit", map[string]any{"file_path": "a.py"},
			"updated a.py", nil),
		pair("m3", "c3", "TaskCreate", map[string]any{"subject": "s"},
			"rejected: tool not allowed", true),
	}, "\n")
	if s := Summarize(log); s.Abort {
		t.Errorf("tail is rejection/progress/rejection: abort fired with %q", s.Blocker)
	}
}

func TestLongReadDumpIsNotARejectionButTripsTail(t *testing.T) {
	dump := strings.Repeat("def f():\n    # error handling lives here\n", 20)
	if len(dump) < 400 {
		t.Fatalf("fixture dump too short: %d", len(dump))
	}
	var b strings.Builder
	for i := 1; i <= 5; i++ {
		fmt.Fprintf(&b, "%s\n", pair(fmt.Sprintf("msg_%d", i), fmt.Sprintf("call_%d", i),
			"Read", map[string]any{"file_path": fmt.Sprintf("f%d.py", i)}, dump, nil))
	}
	s := Summarize(strings.TrimSuffix(b.String(), "\n"))
	if s.Abort {
		t.Errorf("a long Read dump is tool output, not a rejection; abort fired")
	}
	if !strings.Contains(s.Blocker, "last tool Read") {
		t.Errorf("blocker %q must still name the Read tail", s.Blocker)
	}
}

func TestReadonlyBashIsNotProgress(t *testing.T) {
	log := strings.Join([]string{
		pair("msg_b", "b1", "Bash", map[string]any{"command": "git status"},
			"nothing to commit", nil),
		readPairs(5, 1),
	}, "\n")
	if s := Summarize(log); s.Blocker == "" {
		t.Error("git status is not progress: the no-progress tail must fire")
	}
}

func TestMutatingBashIsProgress(t *testing.T) {
	log := strings.Join([]string{
		pair("msg_b", "b1", "Bash", map[string]any{"command": "pytest -q"},
			"3 passed", nil),
		readPairs(5, 1),
	}, "\n")
	if s := Summarize(log); s.Blocker != "" || s.Abort {
		t.Errorf("pytest counts as progress; got blocker=%q abort=%v", s.Blocker, s.Abort)
	}
}

func TestLastToolAndLastReasonTrackFinalToolUse(t *testing.T) {
	log := strings.Join([]string{
		pair("m1", "c1", "Read", map[string]any{"file_path": "a.py"}, "contents", nil),
		pair("m2", "c2", "TaskCreate", map[string]any{"subject": "s"},
			"Task #2 created successfully: s", nil),
	}, "\n")
	s := Summarize(log)
	if s.LastTool != "TaskCreate" {
		t.Errorf("LastTool = %q, want TaskCreate", s.LastTool)
	}
	if s.LastReason == nil || *s.LastReason != "Task #2 created successfully: s" {
		t.Errorf("LastReason = %v, want the paired result text", s.LastReason)
	}
}

func TestLastReasonIsNilWithoutPairedResult(t *testing.T) {
	log := assistant("m1", use("c1", "TaskCreate", map[string]any{"subject": "s"}))
	if s := Summarize(log); s.LastReason != nil {
		t.Errorf("LastReason = %v, want nil with no result event", *s.LastReason)
	}
}

func TestReasonTextIsCappedAtTwoHundredChars(t *testing.T) {
	long := strings.Repeat("x", 500)
	log := strings.Join([]string{
		pair("m1", "c1", "TaskCreate", map[string]any{"subject": long}, long, true),
		pair("m2", "c2", "TaskCreate", map[string]any{"subject": long}, long, true),
		pair("m3", "c3", "TaskCreate", map[string]any{"subject": long}, long, true),
	}, "\n")
	s := Summarize(log)
	if !s.Abort {
		t.Fatalf("three rejections must abort")
	}
	want := "reason " + long[:200]
	if !strings.Contains(s.Blocker, want) {
		t.Errorf("blocker must carry the reason capped at 200 chars: %q…", s.Blocker[:80])
	}
}

func TestFiveIdenticalRejectionsAbortWinsOverTail(t *testing.T) {
	s := Summarize(rejectedTaskCreatePairs(5, "fix login"))
	if !s.Abort {
		t.Fatalf("Abort = false, want true: the rejection rule wins")
	}
	if !strings.Contains(s.Blocker, "repeated identical TaskCreate attempts") {
		t.Errorf("both rules matched: the identical-rejection sentence must win, got %q", s.Blocker)
	}
}

func TestBashReadonlyPrefixIsWordBounded(t *testing.T) {
	cases := []struct {
		command  string
		progress bool
	}{
		{"git status", false},
		{"git diff HEAD~1", false},
		{"ls -la", false},
		{"cat notes.txt", false},
		{"echo hi", false},
		{"  git status  ", false},
		{"git statusx", true},
		{"catalog file.py", true},
		{"rg pattern src/", false},
		{"rgen something", true},
	}
	for _, tc := range cases {
		log := strings.Join([]string{
			pair("msg_b", "b1", "Bash", map[string]any{"command": tc.command},
				"output", nil),
			readPairs(5, 1),
		}, "\n")
		got := Summarize(log).Blocker != ""
		if got != !tc.progress {
			t.Errorf("command %q: tail blocker=%v, want progress=%v",
				tc.command, got, tc.progress)
		}
	}
}

func TestToolResultContentBlocksAreJoined(t *testing.T) {
	blockContent, _ := json.Marshal([]any{
		map[string]any{"type": "text", "text": "first part "},
		map[string]any{"type": "text", "text": "second part"},
	})
	line, _ := json.Marshal(map[string]any{
		"type": "user",
		"message": map[string]any{
			"role": "user",
			"content": []any{map[string]any{
				"type": "tool_result", "tool_use_id": "c1",
				"content": json.RawMessage(blockContent),
			}},
		},
	})
	log := assistant("m1", use("c1", "Read", map[string]any{"file_path": "a.py"})) +
		"\n" + string(line)
	s := Summarize(log)
	if s.LastReason == nil || *s.LastReason != "first part second part" {
		t.Errorf("LastReason = %v, want the joined block text", s.LastReason)
	}
}

func TestMultipleToolUsesInOneAssistantEvent(t *testing.T) {
	log := strings.Join([]string{
		assistant("msg_one",
			use("c1", "Read", map[string]any{"file_path": "a.py"}),
			use("c2", "TaskCreate", map[string]any{"subject": "s"})),
		toolResult("c1", "contents", nil),
		toolResult("c2", "Task #1 created successfully: s", nil),
	}, "\n")
	s := Summarize(log)
	if s.APICalls != 1 {
		t.Errorf("APICalls = %d, want 1 (one message id, two tool uses)", s.APICalls)
	}
	if s.LastTool != "TaskCreate" {
		t.Errorf("LastTool = %q, want the second block's tool", s.LastTool)
	}
}

func TestUnpairedToolUseIsNotARejection(t *testing.T) {
	log := strings.Join([]string{
		pair("m1", "c1", "TaskCreate", map[string]any{"subject": "s"},
			"rejected: tool not allowed", true),
		pair("m2", "c2", "TaskCreate", map[string]any{"subject": "s"},
			"rejected: tool not allowed", true),
		assistant("m3", use("c3", "TaskCreate", map[string]any{"subject": "s"})),
	}, "\n")
	s := Summarize(log)
	if s.Abort {
		t.Errorf("the unpaired third attempt carries no rejection evidence")
	}
	if s.LastReason != nil {
		t.Errorf("LastReason = %v, want nil for the unpaired attempt", *s.LastReason)
	}
}

func TestAllRejectionMarkersHeuristicallyReject(t *testing.T) {
	for _, marker := range []string{
		"rejected", "not allowed", "permission denied", "unknown tool",
		"tool not found", "invalid tool", "denied by",
	} {
		var b strings.Builder
		for i := 1; i <= 3; i++ {
			fmt.Fprintf(&b, "%s\n", pair(fmt.Sprintf("msg_%d", i), fmt.Sprintf("call_%d", i),
				"TaskCreate", map[string]any{"subject": "s"}, marker, nil))
		}
		if s := Summarize(strings.TrimSuffix(b.String(), "\n")); !s.Abort {
			t.Errorf("marker %q: is_error-unset short result must reject", marker)
		}
	}
}

func TestHeuristicRejectionIsCaseInsensitive(t *testing.T) {
	log := strings.Join([]string{
		pair("m1", "c1", "TaskCreate", map[string]any{"subject": "s"},
			"REJECTED: Tool Not Allowed", nil),
		pair("m2", "c2", "TaskCreate", map[string]any{"subject": "s"},
			"REJECTED: Tool Not Allowed", nil),
		pair("m3", "c3", "TaskCreate", map[string]any{"subject": "s"},
			"REJECTED: Tool Not Allowed", nil),
	}, "\n")
	if s := Summarize(log); !s.Abort {
		t.Error("uppercase markers must still reject")
	}
}

func TestUnicodeReasonCappedAtTwoHundredRunes(t *testing.T) {
	long := strings.Repeat("世", 300)
	log := strings.Join([]string{
		pair("m1", "c1", "TaskCreate", map[string]any{"subject": "s"}, long, true),
		pair("m2", "c2", "TaskCreate", map[string]any{"subject": "s"}, long, true),
		pair("m3", "c3", "TaskCreate", map[string]any{"subject": "s"}, long, true),
	}, "\n")
	s := Summarize(log)
	want := "reason " + strings.Repeat("世", 200)
	if !strings.HasSuffix(s.Blocker, want) {
		t.Error("blocker reason must cap at 200 runes, not bytes")
	}
	if strings.Contains(s.Blocker, "reason "+strings.Repeat("世", 201)) {
		t.Error("blocker reason exceeded the 200-rune cap")
	}
}

func TestSignatureDifferenceInInputOnlyStillAborts(t *testing.T) {
	log := strings.Join([]string{
		pair("m1", "c1", "TaskCreate", map[string]any{"subject": "same"}, "no", true),
		pair("m2", "c2", "TaskCreate", map[string]any{"subject": "same"}, "no", true),
		pair("m3", "c3", "TaskCreate", map[string]any{"subject": "different"}, "no", true),
	}, "\n")
	s := Summarize(log)
	if !s.Abort {
		t.Fatal("three same-name rejections must abort")
	}
	if strings.Contains(s.Blocker, "identical") {
		t.Errorf("one differing input must drop the identical claim: %q", s.Blocker)
	}
}

func TestEmptyLogSummaryIsFullyZero(t *testing.T) {
	s := Summarize("")
	if s.AssistantRecords != 0 || s.APICalls != 0 || s.LastTool != "" ||
		s.LastReason != nil || s.Blocker != "" || s.Abort {
		t.Errorf("empty log must give the zero summary, got %+v", s)
	}
}
