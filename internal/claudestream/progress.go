// Package claudestream summarizes a Claude Code stream-json run log: how
// much assistant activity it holds and whether its tool tail shows a stuck
// loop. It is the Go twin of
// tools/claude_stream_progress.py:summarize_claude_stream — same rules,
// independently tested. Production Python never calls it; it exists so
// the rules have a second, line-balance-accounted implementation.
//
// Rules (shared with the Python twin):
//   - api_calls = distinct non-empty assistant message.id values; falls
//     back to assistant events with a non-nil stop_reason, then to the
//     assistant event count. System events (thinking_tokens) never count.
//   - Tool attempts come from assistant message.content tool_use blocks,
//     paired with user tool_result blocks by tool_use_id. An attempt is
//     rejected when is_error is true, or is_error is unset and the short
//     (<400 chars) result text contains a refusal marker. is_error false
//     is never a rejection.
//   - Progress tools are Edit/Write/MultiEdit/NotebookEdit; Bash counts
//     unless its command starts with a read-only inspection.
//   - The last 3 attempts sharing a tool name, all rejected, abort the
//     run ("repeated [identical] <tool> attempts without progress: last
//     tool <tool>, reason <last rejection>").
//   - Otherwise >=5 attempts, the last 5 on one tool name, and zero
//     progress attempts anywhere set a blocker (reason: last result text
//     or "no source change") without aborting — planning bursts are legal.
package claudestream

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"unicode/utf8"
)

const (
	heuristicResultMaxChars = 400
	maxReasonChars          = 200
	abortRejections         = 3
	tailTools               = 5
)

// rejectionMarkers are case-insensitive result-text markers meaning "the
// wrapper refused this tool call" when is_error was not set explicitly.
var rejectionMarkers = []string{
	"rejected",
	"not allowed",
	"permission denied",
	"unknown tool",
	"tool not found",
	"invalid tool",
	"denied by",
}

// progressTools change the repo; any attempt at one means the run is
// making progress, whatever the tail looks like.
var progressTools = map[string]bool{
	"Edit": true, "Write": true, "MultiEdit": true, "NotebookEdit": true,
}

// bashReadonlyRE matches the read-only inspections that do NOT make a
// Bash call progress, word-bounded at the start of the stripped command.
var bashReadonlyRE = regexp.MustCompile(
	`^(?:git status|git diff|git log|git show|git rev-parse|` +
		`ls|cat|rg|grep|find|head|tail|wc|pwd|echo)\b`)

// Summary is what Summarize derives from one run log.
//
// AssistantRecords counts type=="assistant" events. APICalls counts
// distinct non-empty assistant message.id values (falling back to
// stop_reason-bearing events, then to the event count); system events
// such as thinking_tokens never count. LastTool/LastReason describe the
// final tool_use and its paired result text (nil when there is none).
// Blocker names a stuck tool tail; Abort marks a tail that must kill the
// live run.
type Summary struct {
	AssistantRecords int
	APICalls         int
	LastTool         string
	LastReason       *string
	Blocker          string
	Abort            bool
}

// attempt is one tool_use paired (when possible) with its tool_result.
type attempt struct {
	name       string
	callID     string
	signature  string
	resultText string
	hasResult  bool
	isError    any
	progress   bool
}

// result is the flattened tool_result an attempt is paired with.
type result struct {
	text    string
	isError any
}

// Summarize applies the package rules to logText. Malformed lines and
// non-dict events are skipped; a system thinking_tokens blip is never an
// API call.
func Summarize(logText string) Summary {
	assistantRecords := 0
	ids := map[string]bool{}
	stopReasonRecords := 0
	var attempts []attempt
	results := map[string]result{}

	for _, rawLine := range strings.Split(logText, "\n") {
		line := strings.TrimSpace(rawLine)
		if line == "" {
			continue
		}
		var event map[string]any
		if err := json.Unmarshal([]byte(line), &event); err != nil || event == nil {
			continue
		}
		message, _ := event["message"].(map[string]any)
		switch event["type"] {
		case "assistant":
			assistantRecords++
			if message == nil {
				continue
			}
			if id, ok := message["id"].(string); ok && id != "" {
				ids[id] = true
			}
			if message["stop_reason"] != nil {
				stopReasonRecords++
			}
			collectToolUses(message["content"], &attempts)
		case "user":
			if message != nil {
				collectToolResults(message["content"], results)
			}
		}
	}

	apiCalls := len(ids)
	if apiCalls == 0 {
		apiCalls = stopReasonRecords
	}
	if apiCalls == 0 {
		apiCalls = assistantRecords
	}

	// Pair each attempt with its result; an unpaired attempt (run killed
	// between call and result) carries no rejection evidence.
	for i := range attempts {
		if r, ok := results[attempts[i].callID]; ok {
			attempts[i].resultText = r.text
			attempts[i].hasResult = true
			attempts[i].isError = r.isError
		}
	}

	s := Summary{AssistantRecords: assistantRecords, APICalls: apiCalls}
	if len(attempts) > 0 {
		last := attempts[len(attempts)-1]
		s.LastTool = last.name
		if last.hasResult && last.resultText != "" {
			capped := capRunes(last.resultText)
			s.LastReason = &capped
		}
	}

	tail3 := attempts
	if len(tail3) > abortRejections {
		tail3 = tail3[len(tail3)-abortRejections:]
	}
	if len(tail3) == abortRejections && sameNames(tail3) &&
		allRejected(tail3) {
		s.Abort = true
		reason := capRunes(orDefault(tail3[len(tail3)-1].resultText, "rejected"))
		s.Blocker = blockerSentence(tail3[len(tail3)-1].name, reason, sameSignatures(tail3))
		return s
	}

	tail5 := attempts
	if len(tail5) > tailTools {
		tail5 = tail5[len(tail5)-tailTools:]
	}
	if len(attempts) >= tailTools && sameNames(tail5) && !anyProgress(attempts) {
		// Planning bursts are legal: annotate the receipt, never kill.
		reason := capRunes(orDefault(attempts[len(attempts)-1].resultText, "no source change"))
		s.Blocker = blockerSentence(attempts[len(attempts)-1].name, reason, false)
	}
	return s
}

// collectToolUses appends the tool_use blocks of one assistant message.
func collectToolUses(content any, attempts *[]attempt) {
	blocks, ok := content.([]any)
	if !ok {
		return
	}
	for _, raw := range blocks {
		block, ok := raw.(map[string]any)
		if !ok || block["type"] != "tool_use" {
			continue
		}
		name, _ := block["name"].(string)
		callID := ""
		if id, ok := block["id"].(string); ok {
			callID = id
		}
		input := block["input"]
		*attempts = append(*attempts, attempt{
			name:      name,
			callID:    callID,
			signature: name + "\n" + canonicalInput(input),
			progress:  isProgress(name, input),
		})
	}
}

// collectToolResults records the tool_result blocks of one user message.
func collectToolResults(content any, results map[string]result) {
	blocks, ok := content.([]any)
	if !ok {
		return
	}
	for _, raw := range blocks {
		block, ok := raw.(map[string]any)
		if !ok || block["type"] != "tool_result" {
			continue
		}
		callID, ok := block["tool_use_id"].(string)
		if !ok {
			continue
		}
		results[callID] = result{
			text:    contentText(block["content"]),
			isError: block["is_error"],
		}
	}
}

// contentText flattens a tool_result content (string or block list).
func contentText(content any) string {
	switch c := content.(type) {
	case string:
		return c
	case []any:
		var b strings.Builder
		for _, raw := range c {
			switch block := raw.(type) {
			case string:
				b.WriteString(block)
			case map[string]any:
				if text, ok := block["text"].(string); ok {
					b.WriteString(text)
					continue
				}
				if inner, ok := block["content"].(string); ok {
					b.WriteString(inner)
				}
			}
		}
		return b.String()
	default:
		return ""
	}
}

// canonicalInput is the deterministic text for one tool call's input
// (loop-identity key), mirroring Python's sort_keys/compact dumps.
func canonicalInput(v any) string {
	switch v.(type) {
	case map[string]any, []any:
		var buf bytes.Buffer
		enc := json.NewEncoder(&buf)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(v); err != nil {
			return fmt.Sprintf("%v", v)
		}
		return strings.TrimSuffix(buf.String(), "\n")
	default:
		return fmt.Sprintf("%v", v)
	}
}

// isRejected reports whether one paired result is a rejection: is_error
// true always; is_error false never; otherwise only a short result whose
// text contains a refusal marker.
func isRejected(a attempt) bool {
	if b, ok := a.isError.(bool); ok {
		return b
	}
	text := a.resultText
	if utf8.RuneCountInString(text) >= heuristicResultMaxChars {
		return false
	}
	lower := strings.ToLower(text)
	for _, marker := range rejectionMarkers {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

// isProgress reports whether an attempt changed (or could change) the
// repo: the editing tools always; Bash unless it is a read-only
// inspection.
func isProgress(name string, input any) bool {
	if progressTools[name] {
		return true
	}
	if name != "Bash" {
		return false
	}
	command := ""
	if m, ok := input.(map[string]any); ok {
		command, _ = m["command"].(string)
	}
	return !bashReadonlyRE.MatchString(strings.TrimSpace(command))
}

func sameNames(as []attempt) bool {
	for _, a := range as[1:] {
		if a.name != as[0].name {
			return false
		}
	}
	return true
}

func sameSignatures(as []attempt) bool {
	for _, a := range as[1:] {
		if a.signature != as[0].signature {
			return false
		}
	}
	return true
}

func allRejected(as []attempt) bool {
	for _, a := range as {
		if !isRejected(a) {
			return false
		}
	}
	return true
}

func anyProgress(as []attempt) bool {
	for _, a := range as {
		if a.progress {
			return true
		}
	}
	return false
}

func orDefault(text, fallback string) string {
	if text == "" {
		return fallback
	}
	return text
}

// capRunes caps text at maxReasonChars code points, like Python slicing.
func capRunes(text string) string {
	runes := []rune(text)
	if len(runes) <= maxReasonChars {
		return text
	}
	return string(runes[:maxReasonChars])
}

// blockerSentence builds the exact sentence the Python twin builds.
func blockerSentence(tool, reason string, identical bool) string {
	prefix := "repeated"
	if identical {
		prefix = "repeated identical"
	}
	return fmt.Sprintf("%s %s attempts without progress: last tool %s, reason %s",
		prefix, tool, tool, reason)
}
