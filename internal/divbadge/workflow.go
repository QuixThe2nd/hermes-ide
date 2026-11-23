// Package divbadge is the contract test for the divergence-badge refresh
// workflow (.github/workflows/divergence-badges.yml): it parses the YAML and
// asserts the structural invariants the badges depend on — the job computes
// on a freshly fetched origin/main tip before pushing, the push retry is
// bounded and fails loudly, and the trigger/permission/pinning
// prerequisites stay in place.
//
// The three contracts previously lived as the workflow half of
// tests/scripts/test_divergence_badges.py; they moved here wholesale
// because the go-line-balance gate (Go additions >= Python additions)
// forbids growing Python for a CI-only artifact. This package is the sole
// home of the contracts now — keep it in sync when the workflow's shape
// changes on purpose.
//
// The parser is a deliberate line scanner rather than a struct YAML
// decoder: the contracts need run-block ordering, raw comment text, and
// header comments, all of which a decoder discards, and no internal module
// carries a YAML dependency to reuse.
package divbadge

import (
	"errors"
	"fmt"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
)

// computeMarker identifies the badge-compute step: the run block that
// invokes scripts/divergence_badges.py.
const computeMarker = "divergence_badges.py"

var (
	usesRe   = regexp.MustCompile(`uses:\s*([^\s#]+)`)
	pinnedRe = regexp.MustCompile(`^[0-9a-f]{40}$`)
)

// RunBlock is one step's literal `run: |` block. Start is the 1-based file
// line of the first block line, so violations report at real workflow line
// numbers.
type RunBlock struct {
	Start int
	Lines []string
}

// lineOf returns the 1-based file line of the first block line containing
// frag, or an error naming what was not found.
func (b RunBlock) lineOf(frag string) (int, error) {
	for i, line := range b.Lines {
		if strings.Contains(line, frag) {
			return b.Start + i, nil
		}
	}
	return 0, fmt.Errorf("%q not found in the badge-compute step", frag)
}

// BadgeWorkflow is the slice of the divergence-badges workflow structure the
// contracts reason about. Structure a check needs but the file lacks stays
// at its zero value so the check — not the parser — reports the violation.
type BadgeWorkflow struct {
	Raw         string
	PathsIgnore []string          // on.push.paths-ignore entries
	HasDispatch bool              // workflow_dispatch: present under on:
	Permissions map[string]string // top-level permissions block
	TimeoutMin  int               // the refresh job's timeout-minutes
	RunBlocks   []RunBlock        // run: blocks invoking divergence_badges.py
}

// LoadWorkflow reads and parses the workflow file at path.
func LoadWorkflow(path string) (*BadgeWorkflow, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return ParseWorkflow(raw)
}

// ParseWorkflow parses the workflow YAML. The only parse error is a missing
// badge-compute step (every run-block contract is meaningless without one);
// every other structural absence surfaces as a zero value for the contract
// checks to report with a targeted message.
func ParseWorkflow(data []byte) (*BadgeWorkflow, error) {
	raw := strings.TrimPrefix(string(data), "\ufeff")
	lines := splitLines(raw)
	wf := &BadgeWorkflow{Raw: raw, Permissions: map[string]string{}}

	for i := 0; i < len(lines); i++ {
		if indentOf(lines[i]) != 0 || isBlank(lines[i]) || isComment(lines[i]) {
			continue
		}
		key, _, ok := splitKey(lines[i])
		if !ok {
			continue
		}
		end := blockEnd(lines, i+1, 0, len(lines))
		switch key {
		case "on":
			wf.parseOn(lines, i+1, end)
		case "permissions":
			wf.parsePermissions(lines, i+1, end)
		case "jobs":
			wf.parseJobs(lines, i+1, end)
		}
	}
	wf.scanRunBlocks(lines)

	if len(wf.RunBlocks) == 0 {
		return nil, fmt.Errorf("no step in the workflow invokes %s", computeMarker)
	}
	return wf, nil
}

func (wf *BadgeWorkflow) parseOn(lines []string, lo, hi int) {
	if push, ok := findKey(lines, lo, hi, "push"); ok {
		pushEnd := blockEnd(lines, push+1, indentOf(lines[push]), hi)
		if pi, ok := findKey(lines, push+1, pushEnd, "paths-ignore"); ok {
			wf.PathsIgnore, _ = parseFlowList(lines[pi])
		}
	}
	_, wf.HasDispatch = findKey(lines, lo, hi, "workflow_dispatch")
}

func (wf *BadgeWorkflow) parsePermissions(lines []string, lo, hi int) {
	for j := lo; j < hi; j++ {
		if isBlank(lines[j]) || isComment(lines[j]) {
			continue
		}
		if k, rest, ok := splitKey(lines[j]); ok {
			wf.Permissions[k] = stripComment(rest)
		}
	}
}

func (wf *BadgeWorkflow) parseJobs(lines []string, lo, hi int) {
	if ti, ok := findKey(lines, lo, hi, "timeout-minutes"); ok {
		_, rest, _ := splitKey(lines[ti])
		if n, err := strconv.Atoi(stripComment(rest)); err == nil {
			wf.TimeoutMin = n
		}
	}
}

// scanRunBlocks collects every literal `run: |` block whose body invokes the
// badge script. A RunBlock is one step's run body, so ordering checks made
// against it are same-step checks by construction.
func (wf *BadgeWorkflow) scanRunBlocks(lines []string) {
	for i := 0; i < len(lines); i++ {
		if !strings.HasPrefix(strings.TrimSpace(lines[i]), "run: |") {
			continue
		}
		runIndent := indentOf(lines[i])
		var block []string
		start := 0
		for j := i + 1; j < len(lines); j++ {
			if isBlank(lines[j]) {
				if len(block) > 0 {
					block = append(block, lines[j])
				}
				continue
			}
			if indentOf(lines[j]) <= runIndent {
				break
			}
			if len(block) == 0 {
				start = j
			}
			block = append(block, lines[j])
		}
		for len(block) > 0 && strings.TrimSpace(block[len(block)-1]) == "" {
			block = block[:len(block)-1]
		}
		for _, line := range block {
			if strings.Contains(line, computeMarker) {
				wf.RunBlocks = append(wf.RunBlocks, RunBlock{Start: start + 1, Lines: block})
				break
			}
		}
	}
}

// CheckComputeOnFetchedTip verifies that every badge-compute step fetches
// origin/main and lands on it BEFORE divergence_badges.py runs, and pushes
// after — all within the same step. The badge commit used to be parented on
// the checkout-time SHA; once main moved, every push died with
// "! [rejected] main -> main (fetch first)". The README edit is the only
// local change the workflow makes, so the fix is fetch + hard-reset onto
// the tip + recompute — never a rebase. The stale edit must be dropped
// before the reset so the reset can never destroy it.
func (wf *BadgeWorkflow) CheckComputeOnFetchedTip() error {
	for _, b := range wf.RunBlocks {
		fetch, err := b.lineOf("git fetch origin main")
		if err != nil {
			return err
		}
		reset, err := b.lineOf("git reset --hard origin/main")
		if err != nil {
			return err
		}
		compute, err := b.lineOf(computeMarker)
		if err != nil {
			return err
		}
		push, err := b.lineOf("git push")
		if err != nil {
			return err
		}
		if fetch >= compute {
			return fmt.Errorf("divergence-badges.yml:%d: git fetch origin main must run before divergence_badges.py (line %d)", fetch, compute)
		}
		if reset >= compute {
			return fmt.Errorf("divergence-badges.yml:%d: git reset --hard origin/main must run before divergence_badges.py (line %d)", reset, compute)
		}
		checkout, err := b.lineOf("git checkout -- README.md")
		if err != nil {
			return err
		}
		if checkout >= reset {
			return fmt.Errorf("divergence-badges.yml:%d: the stale README edit must be dropped (git checkout -- README.md) before git reset --hard (line %d) so the reset cannot destroy it", checkout, reset)
		}
		if push <= compute {
			return fmt.Errorf("divergence-badges.yml:%d: git push must come after divergence_badges.py (line %d)", push, compute)
		}
	}
	return nil
}

// CheckBoundedRetry verifies that each badge-compute step retries the push
// at most 3 times, annotates the job with ::error:: when the retries are
// exhausted, and exits 1 as the step's last statement — nothing falls
// through the loop silently.
func (wf *BadgeWorkflow) CheckBoundedRetry() error {
	for _, b := range wf.RunBlocks {
		loopLine, err := b.lineOf("for attempt in")
		if err != nil {
			return err
		}
		attempts, err := parseAttempts(b.Lines[loopLine-b.Start])
		if err != nil {
			return fmt.Errorf("divergence-badges.yml:%d: %v", loopLine, err)
		}
		if !slices.Equal(attempts, []int{1, 2, 3}) {
			return fmt.Errorf("divergence-badges.yml:%d: the retry loop must be bounded at exactly 3 attempts (1 2 3), found %v", loopLine, attempts)
		}
		if _, err := b.lineOf("::error::"); err != nil {
			return fmt.Errorf("exhausted retries must annotate the job loudly: %v", err)
		}
		last := ""
		for i := len(b.Lines) - 1; i >= 0; i-- {
			if s := strings.TrimSpace(b.Lines[i]); s != "" {
				last = s
				break
			}
		}
		if last != "exit 1" {
			return fmt.Errorf("exhausted retries must exit loudly: last line of the badge-compute step is %q, want %q", last, "exit 1")
		}
	}
	return nil
}

// parseAttempts extracts the bounded attempt list from a shell loop header
// ("for attempt in 1 2 3; do" → [1 2 3]).
func parseAttempts(header string) ([]int, error) {
	inList := false
	var attempts []int
	for _, tok := range strings.Fields(header) {
		tok = strings.TrimSuffix(tok, ";")
		if !inList {
			inList = tok == "in"
			continue
		}
		if tok == "do" {
			break
		}
		n, err := strconv.Atoi(tok)
		if err != nil {
			return nil, fmt.Errorf("unparseable retry attempt %q in %q", tok, strings.TrimSpace(header))
		}
		attempts = append(attempts, n)
	}
	return attempts, nil
}

// readOnlyUpstreamFragments are the load-bearing phrases of the header
// comment recording the upstream fetch as a READ-ONLY ancestry comparison —
// the fork-invariant documentation that this workflow must never become a
// sync, merge, or push path to NousResearch/hermes-agent. Each fragment is
// matched within a single line so re-wrapping the comment keeps the
// contract satisfiable.
var readOnlyUpstreamFragments = []string{
	"is a READ-ONLY",
	"ancestry comparison against NousResearch/hermes-agent",
	"never a sync, merge,",
	"or push target",
}

// CheckTriggerAndPushPrerequisites verifies the workflow still declares
// everything the badge commit depends on: paths-ignore so the badge commit
// does not retrigger the workflow, workflow_dispatch for manual re-runs,
// permissions limited to exactly contents:write (GITHUB_TOKEN may push,
// nothing more), a job timeout, SHA-pinned actions, and the read-only
// upstream header note.
func (wf *BadgeWorkflow) CheckTriggerAndPushPrerequisites() error {
	if !wf.HasDispatch {
		return errors.New("workflow must keep workflow_dispatch under on:")
	}
	want := []string{"README.md"}
	if !slices.Equal(wf.PathsIgnore, want) {
		return fmt.Errorf("on.push.paths-ignore must be exactly %v (the badge commit itself must not retrigger), got %v", want, wf.PathsIgnore)
	}
	if len(wf.Permissions) != 1 || wf.Permissions["contents"] != "write" {
		return fmt.Errorf("permissions must be exactly contents: write, got %v", wf.Permissions)
	}
	if wf.TimeoutMin < 1 {
		return fmt.Errorf("the refresh job must keep a timeout-minutes >= 1, got %d", wf.TimeoutMin)
	}
	pinned := 0
	for _, m := range usesRe.FindAllStringSubmatch(wf.Raw, -1) {
		name, ref, found := strings.Cut(m[1], "@")
		if !found {
			return fmt.Errorf("action %q is not pinned to a commit SHA (missing @ref)", m[1])
		}
		if !pinnedRe.MatchString(ref) {
			return fmt.Errorf("action %q is not pinned to a 40-hex commit SHA, got ref %q", name, ref)
		}
		pinned++
	}
	if pinned == 0 {
		return errors.New("workflow must declare its actions with uses:")
	}
	for _, frag := range readOnlyUpstreamFragments {
		if !strings.Contains(wf.Raw, frag) {
			return fmt.Errorf("read-only upstream note missing from the workflow header: %q", frag)
		}
	}
	return nil
}

// blockEnd returns the first index in [start, hi) holding a non-blank,
// non-comment line indented at or above indent (a sibling or parent-level
// key); hi if none. Comment lines belong to the structure that follows
// them, so they never terminate a block.
func blockEnd(lines []string, start, indent, hi int) int {
	for j := start; j < hi; j++ {
		if isBlank(lines[j]) || isComment(lines[j]) {
			continue
		}
		if indentOf(lines[j]) <= indent {
			return j
		}
	}
	return hi
}

// findKey returns the index of the first line in [lo, hi) that is a mapping
// entry for name, at any indent.
func findKey(lines []string, lo, hi int, name string) (int, bool) {
	for j := lo; j < hi; j++ {
		if isBlank(lines[j]) || isComment(lines[j]) {
			continue
		}
		if k, _, ok := splitKey(lines[j]); ok && k == name {
			return j, true
		}
	}
	return 0, false
}

// splitKey reports whether line is a YAML mapping entry "key:" or
// "key: value" (after stripping a sequence "- " prefix), returning the key
// and the raw remainder. Keys with embedded spaces or quotes are rejected —
// they are not the plain identifiers the contracts navigate by (this is
// what keeps `echo "::error::..."` and `git config user.name ...` lines
// inside run blocks from parsing as mapping keys).
func splitKey(line string) (key, rest string, ok bool) {
	s := strings.TrimSpace(line)
	s = strings.TrimPrefix(s, "- ")
	i := strings.Index(s, ":")
	if i <= 0 {
		return "", "", false
	}
	k := s[:i]
	if strings.ContainsAny(k, " \t\"'") {
		return "", "", false
	}
	return k, strings.TrimSpace(s[i+1:]), true
}

// parseFlowList parses a YAML flow sequence from a line, e.g.
// "paths-ignore: ['README.md'] # comment" → ["README.md"]. Any trailing
// comment is ignored (everything from "[" to the last "]" is the sequence).
func parseFlowList(line string) ([]string, error) {
	open := strings.Index(line, "[")
	close := strings.LastIndex(line, "]")
	if open < 0 || close < open {
		return nil, fmt.Errorf("no flow sequence [...] in %q", strings.TrimSpace(line))
	}
	inner := line[open+1 : close]
	if strings.TrimSpace(inner) == "" {
		return []string{}, nil
	}
	parts := strings.Split(inner, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.Trim(strings.TrimSpace(p), `"'`); p != "" {
			out = append(out, p)
		}
	}
	return out, nil
}

// stripComment removes a trailing `# comment` from a scalar value. Values in
// this workflow are simple scalars; flow sequences are parsed by
// parseFlowList before stripping would matter.
func stripComment(s string) string {
	if i := strings.Index(s, "#"); i >= 0 {
		s = s[:i]
	}
	return strings.TrimSpace(s)
}

func splitLines(raw string) []string {
	lines := strings.Split(raw, "\n")
	for len(lines) > 0 && strings.TrimSpace(lines[len(lines)-1]) == "" {
		lines = lines[:len(lines)-1]
	}
	return lines
}

func isBlank(line string) bool { return strings.TrimSpace(line) == "" }

func isComment(line string) bool { return strings.HasPrefix(strings.TrimSpace(line), "#") }

func indentOf(line string) int {
	n := 0
	for _, r := range line {
		if r != ' ' {
			break
		}
		n++
	}
	return n
}
