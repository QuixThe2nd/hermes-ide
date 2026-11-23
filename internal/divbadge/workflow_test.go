// Contract tests for .github/workflows/divergence-badges.yml — the
// continuation of the workflow contracts that used to live in
// tests/scripts/test_divergence_badges.py, moved to Go because the
// go-line-balance gate forbids adding Python test code. The assertions are
// structural (ordering, bounds, presence), never snapshots: no badge
// percentage, commit SHA, or action version of the live workflow is frozen
// — the fixture's pinned SHA exists only to be broken by mutations.
package divbadge

import (
	"path/filepath"
	"strings"
	"testing"
)

// liveWorkflowPath is relative to this package directory: go test runs the
// test binary with the package dir as cwd, the same convention the other
// internal modules use for their testdata paths.
var liveWorkflowPath = filepath.Join("..", "..", ".github", "workflows", "divergence-badges.yml")

func loadLiveWorkflow(t *testing.T) *BadgeWorkflow {
	t.Helper()
	wf, err := LoadWorkflow(liveWorkflowPath)
	if err != nil {
		t.Fatalf("parse %s: %v", liveWorkflowPath, err)
	}
	return wf
}

// TestWorkflowComputesOnFetchedTipNotCheckout: every badge-compute step must
// fetch origin/main and land on it BEFORE the script runs. The badge commit
// used to be parented on the checkout-time SHA; once main moved, every push
// died with "! [rejected] main -> main (fetch first)". The README edit is
// the only local change, so the fix is fetch + hard-reset onto the tip +
// recompute — never a rebase — and the stale edit is dropped before the
// reset so the reset can never destroy it.
func TestWorkflowComputesOnFetchedTipNotCheckout(t *testing.T) {
	if err := loadLiveWorkflow(t).CheckComputeOnFetchedTip(); err != nil {
		t.Fatalf("divergence-badges.yml: %v", err)
	}
}

// TestWorkflowPushRetryIsBoundedAndFailsLoudly: the retry loop is bounded
// (exactly 3 attempts), not infinite, and when the attempts are exhausted
// the step fails loudly — ::error:: annotation plus exit 1 as the last
// statement, with no fall-through after the loop.
func TestWorkflowPushRetryIsBoundedAndFailsLoudly(t *testing.T) {
	if err := loadLiveWorkflow(t).CheckBoundedRetry(); err != nil {
		t.Fatalf("divergence-badges.yml: %v", err)
	}
}

// TestWorkflowKeepsTriggerAndPushPrerequisites: the badge commit must not
// retrigger the workflow (paths-ignore: [README.md]), manual re-runs stay
// possible (workflow_dispatch), GITHUB_TOKEN may push but nothing more
// (permissions: contents: write), the job keeps a timeout, every action is
// pinned to a commit SHA, and the read-only-upstream header note stays.
func TestWorkflowKeepsTriggerAndPushPrerequisites(t *testing.T) {
	if err := loadLiveWorkflow(t).CheckTriggerAndPushPrerequisites(); err != nil {
		t.Fatalf("divergence-badges.yml: %v", err)
	}
}

// goodFixture is a miniature divergence-badges workflow exercising every
// parser path the contracts touch. It must satisfy all three checks intact;
// each case below mutates one aspect and asserts the matching check fails
// with a message naming it — proof the checks have teeth on the live file,
// not vacuous passes.
const goodFixture = `name: Divergence badges
# NOTE: the upstream fetch inside scripts/divergence_badges.py is a READ-ONLY
# ancestry comparison against NousResearch/hermes-agent — never a sync, merge,
# or push target.
on:
  push:
    branches: [main]
    paths-ignore: ['README.md']
  workflow_dispatch:

permissions:
  contents: write

jobs:
  refresh:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4
      - name: Refresh badges on latest main, commit, push
        run: |
          for attempt in 1 2 3; do
            git fetch origin main
            git checkout -- README.md
            git reset --hard origin/main
            python scripts/divergence_badges.py --repo . --readme README.md
            git add README.md
            git commit -m "refresh badges"
            git push
          done
          echo "::error::exhausted"
          exit 1
`

func TestChecksRejectBrokenWorkflows(t *testing.T) {
	mustParse := func(t *testing.T, src string) *BadgeWorkflow {
		t.Helper()
		wf, err := ParseWorkflow([]byte(src))
		if err != nil {
			t.Fatalf("fixture does not parse: %v", err)
		}
		return wf
	}

	checks := []struct {
		name  string
		check func(*BadgeWorkflow) error
	}{
		{"compute-on-fetched-tip", (*BadgeWorkflow).CheckComputeOnFetchedTip},
		{"bounded-retry", (*BadgeWorkflow).CheckBoundedRetry},
		{"trigger-and-push-prerequisites", (*BadgeWorkflow).CheckTriggerAndPushPrerequisites},
	}

	good := mustParse(t, goodFixture)
	for _, c := range checks {
		if err := c.check(good); err != nil {
			t.Errorf("intact fixture must pass the %s check: %v", c.name, err)
		}
	}

	cases := []struct {
		name    string
		mutate  func(string) string
		check   func(*BadgeWorkflow) error
		wantErr string
	}{
		{
			name: "fetch lands after the compute",
			mutate: func(s string) string {
				return strings.Replace(s,
					"            git fetch origin main\n            git checkout -- README.md\n            git reset --hard origin/main\n            python scripts/divergence_badges.py --repo . --readme README.md",
					"            python scripts/divergence_badges.py --repo . --readme README.md\n            git checkout -- README.md\n            git reset --hard origin/main\n            git fetch origin main",
					1)
			},
			check:   (*BadgeWorkflow).CheckComputeOnFetchedTip,
			wantErr: "must run before divergence_badges.py",
		},
		{
			name: "reset before the stale edit is dropped",
			mutate: func(s string) string {
				return strings.Replace(s,
					"            git checkout -- README.md\n            git reset --hard origin/main",
					"            git reset --hard origin/main\n            git checkout -- README.md",
					1)
			},
			check:   (*BadgeWorkflow).CheckComputeOnFetchedTip,
			wantErr: "before git reset --hard",
		},
		{
			name: "push before the compute",
			mutate: func(s string) string {
				return strings.Replace(s,
					"            python scripts/divergence_badges.py --repo . --readme README.md",
					"            git push\n            python scripts/divergence_badges.py --repo . --readme README.md",
					1)
			},
			check:   (*BadgeWorkflow).CheckComputeOnFetchedTip,
			wantErr: "must come after divergence_badges.py",
		},
		{
			name: "retry loop unbounded",
			mutate: func(s string) string {
				return strings.Replace(s, "for attempt in 1 2 3; do", "for attempt in 1 2 3 4 5; do", 1)
			},
			check:   (*BadgeWorkflow).CheckBoundedRetry,
			wantErr: "bounded at exactly 3 attempts",
		},
		{
			name: "no error annotation on exhaustion",
			mutate: func(s string) string {
				return strings.Replace(s, "          echo \"::error::exhausted\"\n", "", 1)
			},
			check:   (*BadgeWorkflow).CheckBoundedRetry,
			wantErr: "::error::",
		},
		{
			name: "step falls through after the loop",
			mutate: func(s string) string {
				return strings.Replace(s, "exit 1", "exit 0", 1)
			},
			check:   (*BadgeWorkflow).CheckBoundedRetry,
			wantErr: "last line of the badge-compute step",
		},
		{
			name: "action pinned to a tag instead of a SHA",
			mutate: func(s string) string {
				return strings.Replace(s, "@11bd71901bbe5b1630ceea73d27597364c9af683", "@v4", 1)
			},
			check:   (*BadgeWorkflow).CheckTriggerAndPushPrerequisites,
			wantErr: "not pinned to a 40-hex commit SHA",
		},
		{
			name: "paths-ignore dropped",
			mutate: func(s string) string {
				return strings.Replace(s, "    paths-ignore: ['README.md']\n", "", 1)
			},
			check:   (*BadgeWorkflow).CheckTriggerAndPushPrerequisites,
			wantErr: "paths-ignore",
		},
		{
			name: "permissions narrowed",
			mutate: func(s string) string {
				return strings.Replace(s, "  contents: write", "  contents: read", 1)
			},
			check:   (*BadgeWorkflow).CheckTriggerAndPushPrerequisites,
			wantErr: "exactly contents: write",
		},
		{
			name: "read-only upstream note dropped",
			mutate: func(s string) string {
				return strings.Replace(s, "never a sync, merge,", "follows the sync policy,", 1)
			},
			check:   (*BadgeWorkflow).CheckTriggerAndPushPrerequisites,
			wantErr: "read-only upstream note",
		},
		{
			name: "workflow_dispatch dropped",
			mutate: func(s string) string {
				return strings.Replace(s, "  workflow_dispatch:\n", "", 1)
			},
			check:   (*BadgeWorkflow).CheckTriggerAndPushPrerequisites,
			wantErr: "workflow_dispatch",
		},
		{
			name: "job timeout dropped",
			mutate: func(s string) string {
				return strings.Replace(s, "    timeout-minutes: 30\n", "", 1)
			},
			check:   (*BadgeWorkflow).CheckTriggerAndPushPrerequisites,
			wantErr: "timeout-minutes",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			wf := mustParse(t, tc.mutate(goodFixture))
			err := tc.check(wf)
			if err == nil {
				t.Fatalf("expected failure containing %q, got nil", tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("error %q does not contain %q", err, tc.wantErr)
			}
		})
	}
}

// TestParseWorkflowRequiresBadgeStep: parsing a workflow whose steps never
// invoke the badge script is an error, not a silently-empty contract.
func TestParseWorkflowRequiresBadgeStep(t *testing.T) {
	const noCompute = `name: Divergence badges
on:
  workflow_dispatch:
jobs:
  refresh:
    steps:
      - run: |
          git push
`
	_, err := ParseWorkflow([]byte(noCompute))
	if err == nil || !strings.Contains(err.Error(), computeMarker) {
		t.Fatalf("expected a missing-badge-step error naming %s, got %v", computeMarker, err)
	}
}
