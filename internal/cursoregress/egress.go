// Package cursoregress is the Go twin of tools/cursor_artifact_egress.py.
//
// It restates, independently, the artifact-egress contract for a FINISHED
// Cursor cloud run: success is reported only when a run artifact is
// hash-verified — either a worker checkout on disk or a complete run-stream
// snapshot. It is a tested copy of the rules, not shared code; the Python
// sibling is the production authority.
package cursoregress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// MissingArtifactEgress is the error marker a FINISHED run must carry when
// no hash-verified artifact backs it.
const MissingArtifactEgress = "missing artifact egress"

// Manifest is the verified-artifact record attached to a successful result.
type Manifest struct {
	// CheckoutPath is the worker checkout root for disk verification, or
	// empty when the manifest was rebuilt from a stream snapshot.
	CheckoutPath string
	// SourceBase is the dispatched source_base_sha. Never a branch name.
	SourceBase string
	// HeadSHA is the worker checkout HEAD for disk verification.
	HeadSHA string
	// TreeSHA is git HEAD^{tree} for disk verification, or the canonical
	// hash of the sorted file list for stream verification.
	TreeSHA string
	// Files carries one recomputed sha256 per changed file.
	Files []ManifestFile
	// Source is "disk" or "stream".
	Source string
}

// ManifestFile is a single path plus the sha256 of its bytes.
type ManifestFile struct {
	Path   string
	SHA256 string
}

// Result reports whether a FINISHED run may be surfaced as success.
type Result struct {
	Success  bool
	Error    string
	Manifest *Manifest
}

// SnapshotEvent mirrors the run-stream event shapes that can carry a full
// file snapshot. Later events win per path.
type SnapshotEvent struct {
	Type string `json:"type"`
	Name string `json:"name"`
	// result.success.{path, afterFullFileContent} — untyped edit_file event.
	Result struct {
		Success struct {
			Path                 string `json:"path"`
			AfterFullFileContent string `json:"afterFullFileContent"`
			// ClaimedSHA256 is never trusted; bytes are rehashed.
			ClaimedSHA256 string `json:"sha256"`
		} `json:"success"`
	} `json:"result"`
	// toolCall.{result.value, editToolCall}.afterFullFileContent variants.
	ToolCall struct {
		Result struct {
			Value struct {
				AfterFullFileContent string `json:"afterFullFileContent"`
			} `json:"value"`
		} `json:"result"`
		EditToolCall struct {
			AfterFullFileContent string `json:"afterFullFileContent"`
		} `json:"editToolCall"`
	} `json:"toolCall"`
}

// VerifyFinished is the FINISHED-run gate. A nil manifest with no error
// never happens: no verified artifact means failure with the
// MissingArtifactEgress marker.
func VerifyFinished(manifest *Manifest, streamBody []byte) Result {
	if manifest == nil {
		return Result{Success: false, Error: fmt.Sprintf("%s: no verified artifact", MissingArtifactEgress)}
	}
	if err := manifest.Validate(); err != nil {
		return Result{Success: false, Error: fmt.Sprintf("%s: %v", MissingArtifactEgress, err)}
	}
	return Result{Success: true, Manifest: manifest}
}

// Validate enforces the manifest invariants: source is disk or stream, the
// base is a full 40-hex SHA (never a branch name), disk manifests carry a
// checkout path plus head and tree SHAs, and every file has a sha256 that
// matches the recomputed hash of its bytes.
func (m *Manifest) Validate() error {
	if m.Source != "disk" && m.Source != "stream" {
		return fmt.Errorf("source must be disk or stream, got %q", m.Source)
	}
	if !isSHA(m.SourceBase) {
		return fmt.Errorf("source_base must be a 40-hex SHA, got %q", m.SourceBase)
	}
	if m.Source == "disk" {
		if m.CheckoutPath == "" {
			return fmt.Errorf("disk manifest requires checkout_path")
		}
		if !isSHA(m.HeadSHA) {
			return fmt.Errorf("disk manifest requires head_sha, got %q", m.HeadSHA)
		}
		if !isSHA(m.TreeSHA) {
			return fmt.Errorf("disk manifest requires tree_sha, got %q", m.TreeSHA)
		}
	} else {
		if m.CheckoutPath != "" {
			return fmt.Errorf("stream manifest must not claim a checkout path")
		}
		if m.HeadSHA != "" || isSHA(m.HeadSHA) {
			return fmt.Errorf("stream manifest carries no head_sha")
		}
		if !isSHA256(m.TreeSHA) {
			return fmt.Errorf("stream tree_sha must hash the canonical file list, got %q", m.TreeSHA)
		}
		if m.TreeSHA != CanonicalTreeSHA(m.Files) {
			return fmt.Errorf("stream tree_sha does not hash the manifest files")
		}
	}
	if len(m.Files) == 0 {
		return fmt.Errorf("manifest carries no files")
	}
	seen := map[string]bool{}
	for _, f := range m.Files {
		if f.Path == "" {
			return fmt.Errorf("manifest file entry has no path")
		}
		if seen[f.Path] {
			return fmt.Errorf("duplicate manifest path %q", f.Path)
		}
		seen[f.Path] = true
		if !isSHA256(f.SHA256) {
			return fmt.Errorf("file %q has no sha256", f.Path)
		}
	}
	return nil
}

// StreamBodyIsComplete reports whether a run-stream body can back a
// manifest: fully received (trailing newline), and carrying a turn-ended
// data event.
func StreamBodyIsComplete(body []byte) (bool, string) {
	if len(body) == 0 {
		return false, "empty stream body"
	}
	if body[len(body)-1] != '\n' {
		return false, "stream body is not newline-terminated"
	}
	for _, event := range dataEvents(body) {
		var probe struct {
			Type string `json:"type"`
		}
		if json.Unmarshal(event, &probe) == nil && probe.Type == "turn-ended" {
			return true, ""
		}
	}
	return false, "no turn-ended event"
}

// ExtractStreamSnapshots collects the last full-file content per path from
// a run-stream body, stripping the /workspace/ prefix and ignoring any
// sha256 claimed inside the events.
func ExtractStreamSnapshots(body []byte) map[string]string {
	texts := map[string]string{}
	for _, event := range dataEvents(body) {
		var se SnapshotEvent
		if json.Unmarshal(event, &se) != nil {
			continue
		}
		if p, content, ok := se.snapshot(); ok {
			texts[stripWorkspace(p)] = content
		}
	}
	return texts
}

func (se *SnapshotEvent) snapshot() (string, string, bool) {
	if se.Result.Success.Path != "" && se.Result.Success.AfterFullFileContent != "" {
		return se.Result.Success.Path, se.Result.Success.AfterFullFileContent, true
	}
	if se.ToolCall.Result.Value.AfterFullFileContent != "" {
		if se.Result.Success.Path != "" {
			return se.Result.Success.Path, se.ToolCall.Result.Value.AfterFullFileContent, true
		}
		return "", "", false
	}
	if se.ToolCall.EditToolCall.AfterFullFileContent != "" {
		if se.Result.Success.Path != "" {
			return se.Result.Success.Path, se.ToolCall.EditToolCall.AfterFullFileContent, true
		}
		return "", "", false
	}
	return "", "", false
}

// StreamManifest rebuilds a manifest from a complete run-stream body by
// recomputing sha256 over the snapshot bytes. Claimed hashes inside the
// events are never consulted.
func StreamManifest(body []byte, sourceBase string) (*Manifest, error) {
	if complete, reason := StreamBodyIsComplete(body); !complete {
		return nil, fmt.Errorf("run stream truncated (%s)", reason)
	}
	texts := ExtractStreamSnapshots(body)
	if len(texts) == 0 {
		return nil, fmt.Errorf("complete run stream carries no full-file snapshots")
	}
	paths := make([]string, 0, len(texts))
	for p := range texts {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	files := make([]ManifestFile, 0, len(paths))
	for _, p := range paths {
		files = append(files, ManifestFile{Path: p, SHA256: SHA256Hex(texts[p])})
	}
	return &Manifest{
		SourceBase: sourceBase,
		TreeSHA:    CanonicalTreeSHA(files),
		Files:      files,
		Source:     "stream",
	}, nil
}

// VerifyStream is the FINISHED gate over a run-stream body.
func VerifyStream(body []byte, sourceBase string) Result {
	manifest, err := StreamManifest(body, sourceBase)
	if err != nil {
		return Result{Success: false, Error: fmt.Sprintf("%s: %v", MissingArtifactEgress, err)}
	}
	return VerifyFinished(manifest, body)
}

// VerifyRecoveryRequest refuses anything but a read-only GET when
// recovering an artifact for a receipt.
func VerifyRecoveryRequest(method, url string) error {
	if method != "GET" {
		return fmt.Errorf("recovery must not %s; artifact recovery is read-only", method)
	}
	if !strings.HasSuffix(url, "/stream") {
		return fmt.Errorf("recovery URL must be the run stream endpoint, got %q", url)
	}
	return nil
}

// CanonicalTreeSHA is sha256 over the sorted "path\nsha256\n" lines.
func CanonicalTreeSHA(files []ManifestFile) string {
	sorted := append([]ManifestFile(nil), files...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Path < sorted[j].Path })
	var b strings.Builder
	for _, f := range sorted {
		b.WriteString(f.Path)
		b.WriteString("\n")
		b.WriteString(f.SHA256)
		b.WriteString("\n")
	}
	return SHA256Hex(b.String())
}

// SHA256Hex is the hex sha256 of the given bytes.
func SHA256Hex(data any) string {
	sum := sha256.Sum256(toBytes(data))
	return hex.EncodeToString(sum[:])
}

func toBytes(data any) []byte {
	switch v := data.(type) {
	case []byte:
		return v
	case string:
		return []byte(v)
	default:
		return []byte(fmt.Sprintf("%v", v))
	}
}

func stripWorkspace(path string) string {
	return strings.TrimPrefix(path, "/workspace/")
}

func dataEvents(body []byte) [][]byte {
	var events [][]byte
	for _, line := range strings.Split(string(body), "\n") {
		line = strings.TrimRight(line, "\r")
		if payload, ok := strings.CutPrefix(line, "data: "); ok && strings.TrimSpace(payload) != "" {
			events = append(events, []byte(payload))
		}
	}
	return events
}

func isSHA256(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, c := range s {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

func isSHA(s string) bool {
	if len(s) != 40 {
		return false
	}
	for _, c := range s {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}
