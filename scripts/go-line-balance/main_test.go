package main

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// gitEnv isolates the test git commands from host and user configuration
// and from any inherited GIT_DIR-class working-directory overrides (an
// empty-string value is an invalid path to git, so they must be removed).
func gitEnv() []string {
	drop := map[string]bool{
		"GIT_DIR":              true,
		"GIT_WORK_TREE":        true,
		"GIT_INDEX_FILE":       true,
		"GIT_OBJECT_DIRECTORY": true,
	}
	env := make([]string, 0, len(os.Environ())+3)
	for _, kv := range os.Environ() {
		if !drop[strings.SplitN(kv, "=", 2)[0]] {
			env = append(env, kv)
		}
	}
	return append(env,
		"GIT_CONFIG_GLOBAL=/dev/null",
		"GIT_CONFIG_NOSYSTEM=1",
		"GIT_CONFIG_SYSTEM=/dev/null",
	)
}

// runGit executes git in repo and fails the test on any git error.
func runGit(t *testing.T, repo string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", repo}, args...)...)
	cmd.Env = gitEnv()
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	if err := cmd.Run(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out.String())
	}
	return strings.TrimSpace(out.String())
}

// newRepo creates a real git repository on branch main with one root commit.
func newRepo(t *testing.T) string {
	t.Helper()
	repo := t.TempDir()
	runGit(t, repo, "init", "-q", "-b", "main")
	runGit(t, repo, "config", "user.email", "test@example.com")
	runGit(t, repo, "config", "user.name", "Test")
	runGit(t, repo, "commit", "-q", "--allow-empty", "-m", "root")
	return repo
}

func writeFile(t *testing.T, repo, name string, content []byte) {
	t.Helper()
	path := filepath.Join(repo, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, content, 0o644); err != nil {
		t.Fatal(err)
	}
}

// lines returns n lines "prefix1".."prefixn"; lines(m) is a prefix of
// lines(n) for m < n, so shrinking a file yields a clean delete-only diff.
func lines(n int, prefix string) []byte {
	var b strings.Builder
	for i := 1; i <= n; i++ {
		fmt.Fprintf(&b, "%s%d\n", prefix, i)
	}
	return []byte(b.String())
}

// commit stages everything and returns the new HEAD sha.
func commit(t *testing.T, repo, msg string) string {
	t.Helper()
	runGit(t, repo, "add", "-A")
	runGit(t, repo, "commit", "-q", "--allow-empty", "-m", msg)
	return runGit(t, repo, "rev-parse", "HEAD")
}

// check runs the checker against explicit refs and returns
// (exit code, stdout, stderr).
func check(t *testing.T, repo, base, head string) (int, string, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := run([]string{"--repo", repo, "--base", base, "--head", head}, &stdout, &stderr)
	return code, stdout.String(), stderr.String()
}

func assertContains(t *testing.T, out, want string) {
	t.Helper()
	if !strings.Contains(out, want) {
		t.Fatalf("output missing %q:\n%s", want, out)
	}
}

func TestPythonExceedsGoFails(t *testing.T) {
	repo := newRepo(t)
	base := commit(t, repo, "base")
	writeFile(t, repo, "app.py", lines(5, "p"))
	writeFile(t, repo, "app.go", lines(1, "g"))
	commit(t, repo, "add")

	code, out, _ := check(t, repo, base, "HEAD")
	if code != 1 {
		t.Fatalf("exit = %d, want 1\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 5")
	assertContains(t, out, "go added lines: 1")
	assertContains(t, out, "python added lines (5) exceed go added lines (1)")
}

func TestEqualCountsPasses(t *testing.T) {
	repo := newRepo(t)
	base := commit(t, repo, "base")
	writeFile(t, repo, "a.py", lines(3, "p"))
	writeFile(t, repo, "a.go", lines(3, "g"))
	commit(t, repo, "add")

	code, out, _ := check(t, repo, base, "HEAD")
	if code != 0 {
		t.Fatalf("exit = %d, want 0\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 3")
	assertContains(t, out, "go added lines: 3")
}

func TestGoExceedsPythonPasses(t *testing.T) {
	repo := newRepo(t)
	base := commit(t, repo, "base")
	writeFile(t, repo, "a.py", lines(2, "p"))
	writeFile(t, repo, "a.go", lines(7, "g"))
	commit(t, repo, "add")

	code, out, _ := check(t, repo, base, "HEAD")
	if code != 0 {
		t.Fatalf("exit = %d, want 0\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 2")
	assertContains(t, out, "go added lines: 7")
}

func TestPythonOnlyAdditionFails(t *testing.T) {
	repo := newRepo(t)
	base := commit(t, repo, "base")
	writeFile(t, repo, "x.py", lines(4, "p"))
	commit(t, repo, "add python")

	code, out, _ := check(t, repo, base, "HEAD")
	if code != 1 {
		t.Fatalf("exit = %d, want 1\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 4")
	assertContains(t, out, "go added lines: 0")
}

func TestGoOnlyAdditionPasses(t *testing.T) {
	repo := newRepo(t)
	base := commit(t, repo, "base")
	writeFile(t, repo, "x.go", lines(4, "g"))
	commit(t, repo, "add go")

	code, out, _ := check(t, repo, base, "HEAD")
	if code != 0 {
		t.Fatalf("exit = %d, want 0\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 0")
	assertContains(t, out, "go added lines: 4")
}

func TestDeletionsDoNotOffsetAdditions(t *testing.T) {
	repo := newRepo(t)
	writeFile(t, repo, "big.py", lines(10, "p"))
	writeFile(t, repo, "big.go", lines(10, "g"))
	base := commit(t, repo, "base")
	if err := os.Remove(filepath.Join(repo, "big.go")); err != nil {
		t.Fatal(err)
	}
	writeFile(t, repo, "big.py", lines(8, "p")) // pure deletions
	writeFile(t, repo, "small.py", lines(2, "s"))
	commit(t, repo, "shrink")

	// 10 deleted Go lines and 2 deleted Python lines must not reduce or
	// offset the counts: python=2 added, go=0 added, so the check fails.
	code, out, _ := check(t, repo, base, "HEAD")
	if code != 1 {
		t.Fatalf("exit = %d, want 1\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 2")
	assertContains(t, out, "go added lines: 0")
}

func TestMixedFileCountsOnlyAddedLines(t *testing.T) {
	repo := newRepo(t)
	writeFile(t, repo, "m.py", []byte("a\nb\nc\n"))
	base := commit(t, repo, "base")
	writeFile(t, repo, "m.py", []byte("a\nc\nd\ne\n")) // +2 -1
	writeFile(t, repo, "m.go", []byte("f\ng\n"))       // +2
	commit(t, repo, "mixed")

	// The removed line must not be netted out: python=2 (not 1), go=2.
	code, out, _ := check(t, repo, base, "HEAD")
	if code != 0 {
		t.Fatalf("exit = %d, want 0\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 2")
	assertContains(t, out, "go added lines: 2")
}

func TestBinaryDiffIgnored(t *testing.T) {
	repo := newRepo(t)
	base := commit(t, repo, "base")
	writeFile(t, repo, "blob.bin", []byte{0x00, 0x01, 0x02, 0x00})
	writeFile(t, repo, "looks-like.py", []byte("x = 1\n\x00binary\n")) // NUL => binary
	commit(t, repo, "binaries")

	code, out, _ := check(t, repo, base, "HEAD")
	if code != 0 {
		t.Fatalf("exit = %d, want 0\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 0")
	assertContains(t, out, "go added lines: 0")
}

func TestNoChangesPasses(t *testing.T) {
	repo := newRepo(t)
	base := commit(t, repo, "base")
	runGit(t, repo, "commit", "-q", "--allow-empty", "-m", "empty")

	code, out, _ := check(t, repo, base, "HEAD")
	if code != 0 {
		t.Fatalf("exit = %d, want 0\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 0")
	assertContains(t, out, "go added lines: 0")
}

// TestMergeBaseNotBaseTip pins the base semantics: the diff starts at the
// merge-base of base and head, not at the base ref's tip. "MAINLINE" is
// added by both sides; only a merge-base diff counts it as a head addition.
func TestMergeBaseNotBaseTip(t *testing.T) {
	repo := newRepo(t)
	writeFile(t, repo, "shared.py", []byte("base1\nbase2\n"))
	commit(t, repo, "base")

	runGit(t, repo, "checkout", "-q", "-b", "feature")
	writeFile(t, repo, "shared.py", []byte("base1\nbase2\nMAINLINE\nFEAT\n"))
	commit(t, repo, "feature adds MAINLINE and FEAT")

	runGit(t, repo, "checkout", "-q", "main")
	writeFile(t, repo, "shared.py", []byte("base1\nbase2\nMAINLINE\n"))
	commit(t, repo, "main also adds MAINLINE")

	code, out, _ := check(t, repo, "main", "feature")
	if code != 1 {
		t.Fatalf("exit = %d, want 1\n%s", code, out)
	}
	// Merge-base diff: +MAINLINE +FEAT = 2. A naive base-tip diff would
	// see MAINLINE already on main and count only FEAT = 1.
	assertContains(t, out, "python added lines: 2")
}

func TestRenameCountsOnlyAddedLines(t *testing.T) {
	repo := newRepo(t)
	writeFile(t, repo, "old.py", lines(3, "keep"))
	base := commit(t, repo, "base")
	runGit(t, repo, "mv", "old.py", "new.py")
	writeFile(t, repo, "new.py", append(lines(3, "keep"), []byte("extra1\nextra2\n")...))
	commit(t, repo, "rename with additions")

	// The 3 moved lines are not additions; only extra1/extra2 count, and
	// they still classify as Python on the post-image path.
	code, out, _ := check(t, repo, base, "HEAD")
	if code != 1 {
		t.Fatalf("exit = %d, want 1\n%s", code, out)
	}
	assertContains(t, out, "python added lines: 2")
}

func TestInvalidRepoExits2(t *testing.T) {
	code, _, stderr := check(t, t.TempDir(), "HEAD", "HEAD")
	if code != 2 {
		t.Fatalf("exit = %d, want 2\n%s", code, stderr)
	}
	if stderr == "" {
		t.Fatal("expected an error message on stderr")
	}
}

func TestUnresolvableRefExits2(t *testing.T) {
	repo := newRepo(t)
	commit(t, repo, "base")
	code, _, stderr := check(t, repo, "no-such-ref", "HEAD")
	if code != 2 {
		t.Fatalf("exit = %d, want 2\n%s", code, stderr)
	}
}

func TestUnknownFlagExits2(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run([]string{"--nope"}, &stdout, &stderr); code != 2 {
		t.Fatalf("exit = %d, want 2\n%s", code, stderr.String())
	}
}

func TestBPath(t *testing.T) {
	cases := map[string]string{
		"a.py":                  "a.py",
		"old => new":            "new",
		"old.py => new.go":      "new.go",
		"dir/{a.py => b.py}":    "dir/b.py",
		"{a => b}/x.go":         "b/x.go",
		"dir1/dir2/{ => sub}/f": "dir1/dir2/sub/f",
	}
	for in, want := range cases {
		if got := bPath(in); got != want {
			t.Errorf("bPath(%q) = %q, want %q", in, got, want)
		}
	}
}
