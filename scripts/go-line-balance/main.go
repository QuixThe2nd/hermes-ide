// Command go-line-balance enforces the repo's Go-over-Python growth
// policy: a diff may not add more Python lines than Go lines.
//
// It counts lines added between the merge-base of a base ref and a head
// ref, classifies them by path suffix (.py / .go), and fails when Python
// additions exceed Go additions. Added lines only — deletions never
// offset additions — and binary diffs are ignored. No path filters.
//
// One exemption: a diff whose Python deleted lines are at least 10x its
// Python added lines passes even when additions exceed Go additions.
// Removal and cleanup PRs can leave a few dedented lines that git counts
// as additions; when deletions dominate by an order of magnitude, the
// change is still net-removal and is not blocked.
//
// Exit codes:
//
//	0  go added lines >= python added lines, or net-deletion exemption
//	   (python deleted lines >= 10x python added lines)
//	1  python added lines > go added lines
//	2  git or input error
package main

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

const (
	exitBalanced      = 0
	exitViolation     = 1
	exitError         = 2
	netDeletionFactor = 10
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run parses args, evaluates the diff, and returns the process exit code.
func run(argv []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("go-line-balance", flag.ContinueOnError)
	fs.SetOutput(stderr)
	repo := fs.String("repo", ".", "path to the git repository")
	base := fs.String("base", "", "base ref; the diff starts at its merge-base with head (default: origin's default branch)")
	head := fs.String("head", "HEAD", "head ref of the change")
	if err := fs.Parse(argv); err != nil {
		return exitError
	}

	counts, err := countAddedLines(context.Background(), *repo, *base, *head)
	if err != nil {
		fmt.Fprintf(stderr, "go-line-balance: %v\n", err)
		return exitError
	}

	fmt.Fprintf(stdout, "base: %s\n", counts.Base)
	fmt.Fprintf(stdout, "head: %s\n", counts.Head)
	fmt.Fprintf(stdout, "python added lines: %d\n", counts.Python)
	fmt.Fprintf(stdout, "python deleted lines: %d\n", counts.PythonDeleted)
	fmt.Fprintf(stdout, "go added lines: %d\n", counts.Go)
	if counts.Python > counts.Go && counts.netPythonDeletion() {
		fmt.Fprintf(stdout, "net-deletion exemption: python deleted >= %dx added\n", netDeletionFactor)
		fmt.Fprintf(stdout, "OK: python deleted lines (%d) >= %d x python added lines (%d)\n",
			counts.PythonDeleted, netDeletionFactor, counts.Python)
		return exitBalanced
	}
	if counts.Python > counts.Go {
		fmt.Fprintf(stdout, "FAIL: python added lines (%d) exceed go added lines (%d)\n",
			counts.Python, counts.Go)
		return exitViolation
	}
	fmt.Fprintf(stdout, "OK: go added lines (%d) >= python added lines (%d)\n",
		counts.Go, counts.Python)
	return exitBalanced
}

// lineCounts holds the resolved refs and added-line totals of one diff.
type lineCounts struct {
	Base          string
	Head          string
	Python        int
	PythonDeleted int
	Go            int
}

// netPythonDeletion reports whether the diff removes at least 10 Python
// lines for every Python line it adds — a net Python deletion.
func (c lineCounts) netPythonDeletion() bool {
	return c.Python > 0 && c.PythonDeleted >= netDeletionFactor*c.Python
}

func countAddedLines(ctx context.Context, repo, base, head string) (lineCounts, error) {
	if base == "" {
		var err error
		base, err = defaultBaseRef(ctx, repo)
		if err != nil {
			return lineCounts{}, err
		}
	}
	headSha, err := resolveCommit(ctx, repo, head)
	if err != nil {
		return lineCounts{}, err
	}
	baseSha, err := resolveCommit(ctx, repo, base)
	if err != nil {
		return lineCounts{}, err
	}
	mergeBase, err := gitOut(ctx, repo, "merge-base", baseSha, headSha)
	if err != nil {
		return lineCounts{}, fmt.Errorf("no merge-base between %s and %s: %w", base, head, err)
	}
	numstat, err := gitOut(ctx, repo, "diff", "--numstat", "--find-renames", mergeBase, headSha)
	if err != nil {
		return lineCounts{}, err
	}
	counts := lineCounts{Base: mergeBase, Head: headSha}
	counts.addNumstat(numstat)
	return counts, nil
}

// addNumstat accumulates `git diff --numstat` output. Each line is
// "added<TAB>deleted<TAB>path"; binary files render "-<TAB>-<TAB>path".
func (c *lineCounts) addNumstat(out string) {
	for _, line := range strings.Split(out, "\n") {
		fields := strings.Split(line, "\t")
		if len(fields) < 3 || fields[0] == "-" {
			continue // binary diff
		}
		n, err := strconv.Atoi(fields[0])
		if err != nil {
			continue
		}
		d, err := strconv.Atoi(fields[1])
		if err != nil {
			continue
		}
		path := bPath(fields[2])
		switch {
		case strings.HasSuffix(path, ".py"):
			c.Python += n
			c.PythonDeleted += d
		case strings.HasSuffix(path, ".go"):
			c.Go += n
		}
	}
}

// bPath returns the post-image path of a --numstat path column, which git
// renders as "old => new" or "{old => new}tail" when renames are detected.
func bPath(p string) string {
	const arrow = " => "
	i := strings.Index(p, arrow)
	if i < 0 {
		return p
	}
	if open := strings.LastIndex(p[:i], "{"); open >= 0 {
		if j := strings.Index(p[i:], "}"); j > len(arrow) {
			return p[:open] + p[i+len(arrow):i+j] + p[i+j+1:]
		}
	}
	return p[i+len(arrow):]
}

// defaultBaseRef picks the ref the diff is taken against when --base is
// omitted: origin's checked-out default branch, else origin/main.
func defaultBaseRef(ctx context.Context, repo string) (string, error) {
	if ref, err := gitOut(ctx, repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"); err == nil && ref != "" {
		return ref, nil
	}
	if _, err := gitOut(ctx, repo, "rev-parse", "--verify", "--quiet", "origin/main^{commit}"); err == nil {
		return "origin/main", nil
	}
	return "", fmt.Errorf("no --base given and no origin default branch (origin/HEAD or origin/main) in %s", repo)
}

func resolveCommit(ctx context.Context, repo, ref string) (string, error) {
	sha, err := gitOut(ctx, repo, "rev-parse", "--verify", ref+"^{commit}")
	if err != nil {
		return "", fmt.Errorf("cannot resolve ref %q: %w", ref, err)
	}
	return sha, nil
}

func gitOut(ctx context.Context, repo string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", repo}, args...)...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return "", fmt.Errorf("git %s: %s", strings.Join(args, " "), msg)
	}
	return strings.TrimSpace(stdout.String()), nil
}
