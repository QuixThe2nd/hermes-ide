// Package claudetransport is the Go contract for naming dead Claude Code
// runs and their run-log notice lines, mirroring
// classify_claude_terminal_status in tools/claude_agent_tool.py and the
// notice log line in tools/process_registry_notifications.py. The Python
// tree still executes the Python copy; this package is the Go contract,
// the same arrangement as internal/cronunreachable — it is not imported
// by Python.
package claudetransport

import "strings"

// transportDeathNeedles mirrors the Python classifier's needle list: prose
// that means the provider connection died mid-run. Matched
// case-insensitively as substrings of the run's result/error/log text.
var transportDeathNeedles = []string{
	"econnreset",
	"connection reset",
	"connection closed",
}

// TerminalStatus classifies a finished Claude Code run's terminal status
// exactly like tools.claude_agent_tool.classify_claude_terminal_status.
//
// existing is the status the run already carries. An interrupted or
// timeout verdict is final and returned unchanged — a log tail that
// happens to mention a closed connection must not rewrite it.
// terminalReason is the structured reason from the CLI's type=result
// event; "api_error" (case-insensitive, whitespace-tolerant) is
// authoritative. texts are the run's result/error/log strings; any
// transport-death prose in them names the run api_error so a caller can
// tell a retryable provider-transport failure from a task failure.
// Everything else keeps existing.
func TerminalStatus(existing, terminalReason string, texts ...string) string {
	if existing == "interrupted" || existing == "timeout" {
		return existing
	}
	if strings.ToLower(strings.TrimSpace(terminalReason)) == "api_error" {
		return "api_error"
	}
	for _, text := range texts {
		folded := strings.ToLower(text)
		for _, needle := range transportDeathNeedles {
			if strings.Contains(folded, needle) {
				return "api_error"
			}
		}
	}
	return existing
}

// NoticeLogLine renders the log pointer line of a delegation notice.
//
// A recorded path is echoed exactly as "Log: <trimmed path>"; an absent
// path reports that no log was recorded. It never invents a path — the
// caller only has something to point at when spawn actually saved one.
func NoticeLogLine(savedPath string) (line string, recorded bool) {
	trimmed := strings.TrimSpace(savedPath)
	if trimmed == "" {
		return "no log was recorded", false
	}
	return "Log: " + trimmed, true
}
