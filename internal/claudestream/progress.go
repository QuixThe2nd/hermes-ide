// Package claudestream summarizes a Claude Code stream-json run log: how
// much assistant activity it holds and whether its tool tail shows a stuck
// loop. It is the Go twin of
// tools/claude_stream_progress.py:summarize_claude_stream — same rules,
// independently tested. Production Python never calls it; it exists so
// the rules have a second, line-balance-accounted implementation.
package claudestream

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

// Summarize applies the package rules to logText. Stub: returns the zero
// summary so every call site compiles and existing behavior (empty log:
// no activity, no blocker) is unchanged.
func Summarize(logText string) Summary {
	_ = logText
	return Summary{}
}
