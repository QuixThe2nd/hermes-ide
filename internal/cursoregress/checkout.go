package cursoregress

import (
	"fmt"
	"sort"
	"strings"
)

// WorkerDiscoveryName is the machine name the My Machines worker registers
// under; a checkout only counts when the worker owns it.
const WorkerDiscoveryName = "hermes-server"

// FlagValue extracts the value of flag from an argv in both accepted
// spellings: "--flag value" and "--flag=value". Returns ok=false when the
// flag is absent or carries no value.
func FlagValue(argv []string, flag string) (string, bool) {
	for i, tok := range argv {
		if tok == flag {
			if i+1 < len(argv) && !strings.HasPrefix(argv[i+1], "-") {
				return argv[i+1], true
			}
			return "", false
		}
		if val, ok := strings.CutPrefix(tok, flag+"="); ok {
			return val, true
		}
	}
	return "", false
}

// ParseWorkerCmdline reports whether argv is a My Machines worker command
// line for machineName and collects every --worker-dir it names. A process
// is a candidate worker only when its argv contains the "worker" literal,
// the "--name" flag, and the machine name itself.
func ParseWorkerCmdline(argv []string, machineName string) ([]string, bool) {
	hasWorker, hasName, hasMachine := false, false, false
	for _, tok := range argv {
		if tok == "worker" {
			hasWorker = true
		}
		if tok == "--name" || strings.HasPrefix(tok, "--name=") {
			hasName = true
		}
		if tok == machineName {
			hasMachine = true
		}
	}
	if !hasWorker || !hasName || !hasMachine {
		return nil, false
	}
	var dirs []string
	seen := map[string]bool{}
	for i, tok := range argv {
		val := ""
		if tok == "--worker-dir" {
			if i+1 < len(argv) && !strings.HasPrefix(argv[i+1], "-") {
				val = argv[i+1]
			}
		} else if v, ok := strings.CutPrefix(tok, "--worker-dir="); ok {
			val = v
		} else {
			continue
		}
		if val != "" && !seen[val] {
			seen[val] = true
			dirs = append(dirs, val)
		}
	}
	if len(dirs) == 0 {
		return nil, false
	}
	return dirs, true
}

// DiskCheckout is the verified shape of one candidate worker checkout: the
// facts git would report, already computed, so the match rule stays a pure
// decision.
type DiskCheckout struct {
	// Path is the checkout root; IsGitRepo marks whether it is one at all.
	Path      string
	IsGitRepo bool
	// BaseAncestor: source_base_sha is an ancestor of HEAD.
	BaseAncestor bool
	// HeadIsBase: HEAD equals the base — no worker changes landed.
	HeadIsBase bool
	// ChangedPaths is `git diff --name-only base HEAD`.
	ChangedPaths []string
	// OnDiskSHA maps every changed path to its on-disk sha256.
	OnDiskSHA map[string]string
	// GitBlobSHA maps every changed path to the sha256 of `git show HEAD:path`.
	GitBlobSHA map[string]string
	// HeadSHA and TreeSHA are the checkout's HEAD^{commit} / HEAD^{tree}.
	HeadSHA string
	TreeSHA string
	// SourceBase is the dispatched source_base_sha.
	SourceBase string
}

// MatchDiskRoot turns a candidate checkout into a manifest when every rule
// holds: a git repo, base strictly behind HEAD, a non-empty diff, and every
// changed path's on-disk bytes identical to the committed blob. A missing
// path (deleted at HEAD, or unreadable) fails closed — empty blob hash.
func MatchDiskRoot(c DiskCheckout) (*Manifest, string) {
	if !c.IsGitRepo {
		return nil, fmt.Sprintf("%s is not a git repository", c.Path)
	}
	if !isSHA(c.SourceBase) {
		return nil, fmt.Sprintf("%s source base is not a commit SHA", c.Path)
	}
	if !c.BaseAncestor {
		return nil, fmt.Sprintf("%s HEAD is not descended from the dispatched base", c.Path)
	}
	if c.HeadIsBase {
		return nil, fmt.Sprintf("%s HEAD equals the dispatched base; no worker changes", c.Path)
	}
	if len(c.ChangedPaths) == 0 {
		return nil, fmt.Sprintf("%s has no changed paths after the base", c.Path)
	}
	paths := append([]string(nil), c.ChangedPaths...)
	sort.Strings(paths)
	files := make([]ManifestFile, 0, len(paths))
	for _, p := range paths {
		disk, diskOK := c.OnDiskSHA[p]
		blob, blobOK := c.GitBlobSHA[p]
		if !diskOK || disk == "" {
			return nil, fmt.Sprintf("%s: changed path %q is missing on disk", c.Path, p)
		}
		if !blobOK || blob == "" {
			return nil, fmt.Sprintf("%s: changed path %q is missing at HEAD", c.Path, p)
		}
		if disk != blob {
			return nil, fmt.Sprintf("%s: changed path %q is dirty against HEAD", c.Path, p)
		}
		files = append(files, ManifestFile{Path: p, SHA256: disk})
	}
	if !isSHA(c.HeadSHA) || !isSHA(c.TreeSHA) {
		return nil, fmt.Sprintf("%s lacks a resolvable HEAD^{commit}/HEAD^{tree}", c.Path)
	}
	return &Manifest{
		CheckoutPath: c.Path,
		SourceBase:   c.SourceBase,
		HeadSHA:      c.HeadSHA,
		TreeSHA:      c.TreeSHA,
		Files:        files,
		Source:       "disk",
	}, ""
}

// VerifyDisk is the FINISHED gate over every discovered checkout. Exactly
// one matching root verifies from disk. Zero matches means the caller must
// fall back to the stream; more than one is an ambiguous worker state and
// must fail WITHOUT a stream fetch. The second return is true only when
// the stream fallback remains permitted.
func VerifyDisk(checkouts []DiskCheckout) (Result, bool) {
	var matches []*Manifest
	var reasons []string
	for _, c := range checkouts {
		if m, reason := MatchDiskRoot(c); m != nil {
			matches = append(matches, m)
		} else if reason != "" {
			reasons = append(reasons, reason)
		}
	}
	switch len(matches) {
	case 1:
		return VerifyFinished(matches[0], nil), false
	case 0:
		if len(reasons) > 0 {
			joined := strings.Join(reasons, "; ")
			return Result{
				Success: false,
				Error:   fmt.Sprintf("%s: no matching worker checkout (%s)", MissingArtifactEgress, joined),
			}, true
		}
		return Result{
			Success: false,
			Error:   fmt.Sprintf("%s: no worker checkout discovered", MissingArtifactEgress),
		}, true
	default:
		paths := make([]string, 0, len(matches))
		for _, m := range matches {
			paths = append(paths, m.CheckoutPath)
		}
		sort.Strings(paths)
		return Result{
			Success: false,
			Error: fmt.Sprintf(
				"%s: ambiguous worker checkouts %s; refusing the stream fallback",
				MissingArtifactEgress, strings.Join(paths, ", "),
			),
		}, false
	}
}

// HashesMatch reports whether manifest files and snapshot texts describe
// exactly the same set of paths, with every recomputed hash matching. Set
// equality, not subset: a manifest naming an unsnapshotted file fails.
func HashesMatch(files []ManifestFile, texts map[string]string) bool {
	if len(files) != len(texts) {
		return false
	}
	for _, f := range files {
		content, ok := texts[f.Path]
		if !ok {
			return false
		}
		if SHA256Hex(content) != f.SHA256 {
			return false
		}
	}
	return true
}

// StreamURL builds the one read-only endpoint artifact recovery may touch.
func StreamURL(apiBase, agentID, runID string) string {
	return fmt.Sprintf("%s/v1/agents/%s/runs/%s/stream", strings.TrimRight(apiBase, "/"), agentID, runID)
}

// RecoveryReceipt is the receipt slice recovery is allowed to read: it uses
// only the cloud ids and the dispatched base, and never recreates work.
type RecoveryReceipt struct {
	CloudAgentID string
	CloudRunID   string
	SourceBase   string
}

// FetchStream is the read-only stream getter recovery uses.
type FetchStream func(url string) ([]byte, error)

// RecoverArtifact rebuilds a manifest for an existing receipt. A receipt
// missing its cloud ids fails closed before any HTTP happens; the single
// GET is the only remote call permitted.
func RecoverArtifact(receipt RecoveryReceipt, apiBase string, fetch FetchStream) Result {
	if receipt.CloudAgentID == "" || receipt.CloudRunID == "" {
		return Result{
			Success: false,
			Error:   fmt.Sprintf("%s: receipt lacks cloud ids; no recovery fetch attempted", MissingArtifactEgress),
		}
	}
	if fetch == nil {
		return Result{
			Success: false,
			Error:   fmt.Sprintf("%s: no stream fetcher configured", MissingArtifactEgress),
		}
	}
	body, err := fetch(StreamURL(apiBase, receipt.CloudAgentID, receipt.CloudRunID))
	if err != nil {
		return Result{Success: false, Error: fmt.Sprintf("%s: stream fetch failed: %v", MissingArtifactEgress, err)}
	}
	return VerifyStream(body, receipt.SourceBase)
}
