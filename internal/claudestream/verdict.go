package claudestream

import "strings"

// WatchdogVerdict maps a watchdog error string to its status token, the Go
// twin of tools/claude_agent_tool.py:_watchdog_error_verdict. errText is a
// verdict when it equals "timeout"/"interrupted" exactly or carries the
// token as a "token:"/"token " prefix — the enrichment a stream-derived
// blocker leaves on the error ("timeout: repeated TaskCreate attempts…").
// "timeoutfoo" and "a timeout occurred" are ordinary error strings, not
// verdicts. Production Python never calls this.
func WatchdogVerdict(errText string) (string, bool) {
	for _, token := range []string{"timeout", "interrupted"} {
		if errText == token ||
			strings.HasPrefix(errText, token+":") ||
			strings.HasPrefix(errText, token+" ") {
			return token, true
		}
	}
	return "", false
}
