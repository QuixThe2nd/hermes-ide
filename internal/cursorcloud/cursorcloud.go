// Package cursorcloud is the Go contract for recognizing the Cursor Cloud
// Agents create refusal that must fail over to a local Claude run,
// mirroring tools/cursor_agent_private_org.py. The Python tree still
// executes the Python copy; this package is the Go contract, the same
// arrangement as internal/claudetransport — it is not imported by Python.
package cursorcloud

import (
	"encoding/json"
	"regexp"
	"strings"
)

const (
	// ClaudeFallbackNote is the exact facade fallback_note once Claude ran.
	ClaudeFallbackNote = "Cursor refused the private-org branch and Claude ran"

	// DoNotRetryStartingRef is appended to the Cursor error when Claude
	// could not run either: retrying the same or a dropped startingRef can
	// never produce a cloud agent for the private org.
	DoNotRetryStartingRef = "do not retry this startingRef"
)

// branchVerifyRe matches "Failed to verify existence of branch '<name>' in
// repository <owner>/<repo>". RE2 has no backreference, so the opening and
// closing quotes are separate captures the caller compares — pairs must
// match, single with single, double with double. <name> may contain
// anything but a quote (branch names carry slashes); a trailing sentence
// period after the repo still matches because '.' sits inside the
// owner/repo class.
var branchVerifyRe = regexp.MustCompile(
	`Failed to verify existence of branch (['"])([^'"]+)(['"]) in repository ([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)`)

// MatchPrivateOrgBranchVerifyRefusal reports whether message is Cursor's
// private-org branch-verify refusal: a POST /v1/agents create that failed
// with HTTP 400 validation_error and the branch-verify sentence. Anything
// else — another 400 (a bad model id), a 5xx carrying the same sentence, a
// 409 conflict — is not a refusal and keeps the ordinary hard failure.
func MatchPrivateOrgBranchVerifyRefusal(message string) bool {
	if !strings.Contains(message, "HTTP 400") {
		return false
	}
	if !strings.Contains(message, "validation_error") {
		return false
	}
	if !strings.Contains(message, "POST /v1/agents") {
		return false
	}
	m := branchVerifyRe.FindStringSubmatch(message)
	return m != nil && m[1] == m[3]
}

// ClaudeUnavailable reports whether a delegate_claude_agent result means
// Claude could not run at all: unparseable JSON, not an object, no boolean
// success, or success false with the missing-wrapper error. A genuine
// failed run (tests failed) is NOT unavailability — Claude executed and
// its outcome must be reported as Claude's own.
func ClaudeUnavailable(claudeResultJSON string) bool {
	var payload map[string]any
	if err := json.Unmarshal([]byte(claudeResultJSON), &payload); err != nil {
		return true
	}
	success, ok := payload["success"].(bool)
	if !ok {
		return true
	}
	if success {
		return false
	}
	errText, _ := payload["error"].(string)
	return strings.Contains(errText, "binary not found")
}

// FormatUnavailableError appends the do-not-retry instruction to the
// original Cursor error exactly once, separated by one space.
func FormatUnavailableError(original string) string {
	if strings.HasSuffix(original, DoNotRetryStartingRef) {
		return original
	}
	return original + " " + DoNotRetryStartingRef
}
