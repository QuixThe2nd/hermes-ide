// Package stashconflict decides what happens when the pre-update autostash does
// not re-apply cleanly.
//
// Canonical contract; the Python twin in hermes_cli/update_cmd_stash.py
// (_conflict_resolution_prompt / _classify_conflict_outcome / _conflict_drift_note)
// must produce the same text and the same verdicts.
package stashconflict

import "strings"

type Outcome string

const (
	Resolved        Outcome = "resolved"
	StillConflicted Outcome = "still-conflicted"
	Failed          Outcome = "failed"
)

// Prompt is the instruction handed to a fresh one-shot session that resolves the
// conflicted re-apply. It names the stash so the session leaves it alone.
func Prompt(unmergedPaths []string, stashRef string) string {
	var b strings.Builder
	b.WriteString("Resolve a git stash conflict in this checkout.\n\n")
	b.WriteString("`hermes update` stashed local edits, pulled upstream, then re-applied " + stashRef + " and hit\n")
	b.WriteString("conflicts. Leave the stash in place.\n\n")
	b.WriteString("Conflicted files:\n")
	for _, p := range unmergedPaths {
		b.WriteString("- " + p + "\n")
	}
	b.WriteString("\n")
	b.WriteString("Keep the newly pulled upstream behaviour and re-apply the local intent in the smallest form\n")
	b.WriteString("that still works: no conflict markers left, no unrelated rewrites, no dropped features.\n")
	b.WriteString("Touch only the files above. Do not commit, do not push, do not drop the stash.\n")
	b.WriteString("Finish when `git diff --name-only --diff-filter=U` prints nothing.\n")
	return b.String()
}

// Classify decides the verdict after the session exits. Unmerged paths outrank the
// exit code: a tree that did not merge is never resolved, however cleanly it exited.
func Classify(exitCode int, unmergedPaths []string) Outcome {
	if len(unmergedPaths) > 0 {
		return StillConflicted
	}
	if exitCode != 0 {
		return Failed
	}
	return Resolved
}

// DriftNote names the resolved files as local-only core edits, which belong in a pull
// request or a plugin rather than in the checkout.
func DriftNote(resolvedPaths []string) string {
	if len(resolvedPaths) == 0 {
		return ""
	}
	lines := make([]string, 0, len(resolvedPaths))
	for _, p := range resolvedPaths {
		lines = append(lines, "  • "+p)
	}
	return "These were local-only changes to core files. Upstream keeps editing them, so they will\n" +
		"conflict again: open a PR for what belongs in Hermes, or make it a plugin.\n" +
		strings.Join(lines, "\n")
}
