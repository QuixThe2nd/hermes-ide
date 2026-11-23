package stashconflict

import (
	"strings"
	"testing"
)

func TestPromptIncludesStashRefAndPaths(t *testing.T) {
	cases := []struct {
		name         string
		paths        []string
		stashRef     string
		wantContains []string
	}{
		{
			name:         "single path",
			paths:        []string{"foo.go"},
			stashRef:     "stash@{0}",
			wantContains: []string{"stash@{0}", "- foo.go", "Conflicted files:"},
		},
		{
			name:         "multiple paths",
			paths:        []string{"a.go", "b.go"},
			stashRef:     "stash@{1}",
			wantContains: []string{"stash@{1}", "- a.go", "- b.go"},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := Prompt(c.paths, c.stashRef)
			for _, w := range c.wantContains {
				if !strings.Contains(got, w) {
					t.Errorf("Prompt missing %q in:\n%s", w, got)
				}
			}
		})
	}
}

func TestClassify(t *testing.T) {
	cases := []struct {
		name     string
		exitCode int
		unmerged []string
		want     Outcome
	}{
		{"still conflicted even exit 0", 0, []string{"x.go"}, StillConflicted},
		{"failed clean non-zero", 1, nil, Failed},
		{"resolved", 0, nil, Resolved},
		{"still conflicted overrides failed", 2, []string{"y.go"}, StillConflicted},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := Classify(c.exitCode, c.unmerged)
			if got != c.want {
				t.Errorf("Classify(%d, %v) = %s, want %s", c.exitCode, c.unmerged, got, c.want)
			}
		})
	}
}

func TestDriftNote(t *testing.T) {
	cases := []struct {
		name         string
		paths        []string
		wantEmpty    bool
		wantContains []string
	}{
		{"empty", nil, true, nil},
		{"names paths", []string{"run_agent.py", "cli.py"}, false, []string{"  • run_agent.py", "  • cli.py"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := DriftNote(c.paths)
			if c.wantEmpty && got != "" {
				t.Errorf("expected empty, got %q", got)
			}
			if !c.wantEmpty && got == "" {
				t.Errorf("expected non-empty")
			}
			for _, w := range c.wantContains {
				if !strings.Contains(got, w) {
					t.Errorf("DriftNote missing %q in:\n%s", w, got)
				}
			}
		})
	}
}

func TestPromptBulletOrder(t *testing.T) {
	got := Prompt([]string{"a.go", "b.go", "c.go"}, "stash@{0}")
	a := strings.Index(got, "- a.go")
	b := strings.Index(got, "- b.go")
	c := strings.Index(got, "- c.go")
	if a < 0 || b < 0 || c < 0 {
		t.Fatalf("not every path is listed:\n%s", got)
	}
	if !(a < b && b < c) {
		t.Errorf("paths must keep the caller's order, got a=%d b=%d c=%d", a, b, c)
	}
}

func TestPromptWithoutPathsHasNoBullets(t *testing.T) {
	got := Prompt(nil, "stash@{0}")
	if strings.Contains(got, "\n- ") {
		t.Errorf("empty path list must not emit bullets:\n%s", got)
	}
}

func TestPromptStatesTheResolverLimits(t *testing.T) {
	got := Prompt([]string{"x.go"}, "stash@{0}")
	for _, want := range []string{
		"no conflict markers",
		"Touch only the files above",
		"do not push",
		"do not drop the stash",
		"git diff --name-only --diff-filter=U",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("Prompt must keep the limit %q:\n%s", want, got)
		}
	}
}
