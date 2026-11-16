package cursoregress

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

const testSourceBase = "0000000000000000000000000000000000000000"

func shaOf(content string) string { return SHA256Hex(content) }

func editEvent(rel, content string, claimedSHA string) string {
	event := map[string]any{
		"callId": "call-1",
		"name":   "edit_file",
		"status": "completed",
		"args":   map[string]any{"path": "/workspace/" + rel},
		"result": map[string]any{
			"success": map[string]any{
				"path":                 "/workspace/" + rel,
				"afterFullFileContent": content,
			},
		},
	}
	if claimedSHA != "" {
		event["result"].(map[string]any)["success"].(map[string]any)["sha256"] = claimedSHA
	}
	raw, _ := json.Marshal(event)
	return string(raw)
}

func stream(events ...string) []byte {
	body := ""
	for _, e := range events {
		body += "data: " + e + "\n\n"
	}
	return []byte(body)
}

func completeStream(rel, content string) []byte {
	turnEnded, _ := json.Marshal(map[string]any{"type": "turn-ended"})
	return stream(editEvent(rel, content, ""), string(turnEnded))
}

// 1. FINISHED without verified hashes is not success and carries the marker.
func TestFinishedWithoutVerifiedHashesFails(t *testing.T) {
	result := VerifyFinished(nil, nil)
	if result.Success {
		t.Fatalf("FINISHED with no artifact must not be success: %+v", result)
	}
	if !strings.Contains(result.Error, MissingArtifactEgress) {
		t.Fatalf("error %q must contain %q", result.Error, MissingArtifactEgress)
	}
	if result.Manifest != nil {
		t.Fatalf("failed verification must not attach a manifest: %+v", result.Manifest)
	}

	empty := VerifyStream(nil, testSourceBase)
	if empty.Success {
		t.Fatalf("empty stream must not be success: %+v", empty)
	}
	if !strings.Contains(empty.Error, MissingArtifactEgress) {
		t.Fatalf("error %q must contain %q", empty.Error, MissingArtifactEgress)
	}
}

// 2. A truncated body (no turn-ended) is not complete.
func TestTruncatedBodyNotComplete(t *testing.T) {
	cases := map[string][]byte{
		"empty body":   nil,
		"no newline":   []byte("data: {\"type\":\"turn-ended\"}"),
		"wrong events": completeStream("src/app.py", "x = 1\n")[:len(completeStream("src/app.py", "x = 1\n"))-40],
	}
	for name, body := range cases {
		if complete, reason := StreamBodyIsComplete(body); complete {
			t.Fatalf("%s: body must not be complete", name)
		} else if reason == "" {
			t.Fatalf("%s: incomplete body must carry a reason", name)
		}
		result := VerifyStream(body, testSourceBase)
		if result.Success {
			t.Fatalf("%s: truncated stream must not be success", name)
		}
		if !strings.Contains(result.Error, MissingArtifactEgress) {
			t.Fatalf("%s: error %q must contain %q", name, result.Error, MissingArtifactEgress)
		}
	}

	// Snapshot-only body with no turn-ended is still truncated.
	snapshotOnly := stream(editEvent("src/app.py", "x = 1\n", ""))
	if complete, _ := StreamBodyIsComplete(snapshotOnly); complete {
		t.Fatalf("snapshot without turn-ended must not be complete")
	}
}

// 3. A complete edit_file snapshot plus turn-ended yields recomputed sha256.
func TestCompleteSnapshotRecomputesSHA(t *testing.T) {
	content := "print('worker result')\n"
	result := VerifyStream(completeStream("src/main.py", content), testSourceBase)
	if !result.Success {
		t.Fatalf("complete stream must verify: %+v", result)
	}
	m := result.Manifest
	if m.Source != "stream" {
		t.Fatalf("source = %q, want stream", m.Source)
	}
	if len(m.Files) != 1 {
		t.Fatalf("files = %+v, want exactly the snapshot file", m.Files)
	}
	if m.Files[0].Path != "src/main.py" {
		t.Fatalf("path = %q, want workspace prefix stripped", m.Files[0].Path)
	}
	if got, want := m.Files[0].SHA256, shaOf(content); got != want {
		t.Fatalf("sha256 = %s, want recomputed %s", got, want)
	}
	if m.TreeSHA != CanonicalTreeSHA(m.Files) {
		t.Fatalf("tree sha %s must hash the canonical file list", m.TreeSHA)
	}
	if m.SourceBase != testSourceBase {
		t.Fatalf("source base = %q, want %q", m.SourceBase, testSourceBase)
	}
}

// 4. A claimed sha256 inside an event that differs from the bytes is ignored.
func TestClaimedSHAInEventIgnored(t *testing.T) {
	content := "x = 1\n"
	lie := strings.Repeat("deadbeef", 5)
	body := stream(editEvent("src/app.py", content, lie), fmt.Sprintf(`{"type":"turn-ended"}`))
	result := VerifyStream(body, testSourceBase)
	if !result.Success {
		t.Fatalf("complete stream must verify regardless of claimed hashes: %+v", result)
	}
	if got := result.Manifest.Files[0].SHA256; got != shaOf(content) {
		t.Fatalf("sha256 = %s, want recomputed %s (claimed %s must be ignored)", got, shaOf(content), lie)
	}

	snapshots := ExtractStreamSnapshots(body)
	if got := snapshots["src/app.py"]; got != content {
		t.Fatalf("snapshot bytes = %q, want %q", got, content)
	}
}

// 5. Artifact recovery never issues anything but a read-only stream GET.
func TestRecoveryRefusesNonGET(t *testing.T) {
	if err := VerifyRecoveryRequest("POST", "https://api.cursor.com/v1/agents/a/runs/r/stream"); err == nil {
		t.Fatalf("POST during recovery must be refused")
	}
	if err := VerifyRecoveryRequest("PUT", "https://api.cursor.com/v1/agents/a/runs/r"); err == nil {
		t.Fatalf("PUT during recovery must be refused")
	}
	if err := VerifyRecoveryRequest("GET", "https://api.cursor.com/v1/agents/a/runs/r/results"); err == nil {
		t.Fatalf("GET to a non-stream endpoint must be refused")
	}
	if err := VerifyRecoveryRequest("GET", "https://api.cursor.com/v1/agents/a/runs/r/stream"); err != nil {
		t.Fatalf("read-only stream GET must be allowed: %v", err)
	}
}

// 6. A disk manifest requires source_base, head, tree, and per-file sha256.
func TestDiskManifestRequiresFullIdentity(t *testing.T) {
	content := "print('base')\n"
	head := "1111111111111111111111111111111111111111"
	tree := "2222222222222222222222222222222222222222"
	base := &Manifest{
		CheckoutPath: "/tmp/worker",
		SourceBase:   testSourceBase,
		HeadSHA:      head,
		TreeSHA:      tree,
		Files:        []ManifestFile{{Path: "src/main.py", SHA256: shaOf(content)}},
		Source:       "disk",
	}
	if err := base.Validate(); err != nil {
		t.Fatalf("complete disk manifest must validate: %v", err)
	}
	if res := VerifyFinished(base, nil); !res.Success {
		t.Fatalf("complete disk manifest must verify: %+v", res)
	}

	mutations := map[string]func(m *Manifest){
		"branch name as source base": func(m *Manifest) { m.SourceBase = "main" },
		"missing checkout path":      func(m *Manifest) { m.CheckoutPath = "" },
		"missing head":               func(m *Manifest) { m.HeadSHA = "" },
		"missing tree":               func(m *Manifest) { m.TreeSHA = "" },
		"no files":                   func(m *Manifest) { m.Files = nil },
		"file without sha256":        func(m *Manifest) { m.Files[0].SHA256 = "" },
		"unknown source":             func(m *Manifest) { m.Source = "carrier-pigeon" },
	}
	for name, mutate := range mutations {
		mutated := *base
		mutated.Files = append([]ManifestFile(nil), base.Files...)
		mutate(&mutated)
		if err := mutated.Validate(); err == nil {
			t.Fatalf("%s: manifest must fail validation", name)
		}
		result := VerifyFinished(&mutated, nil)
		if result.Success {
			t.Fatalf("%s: invalid manifest must not be success", name)
		}
		if !strings.Contains(result.Error, MissingArtifactEgress) {
			t.Fatalf("%s: error %q must contain %q", name, result.Error, MissingArtifactEgress)
		}
	}
}

// Stream snapshots take the last event per path and strip /workspace/.
func TestSnapshotVariantsAndLastEventWins(t *testing.T) {
	valueVariant, _ := json.Marshal(map[string]any{
		"toolCall": map[string]any{
			"result": map[string]any{
				"value": map[string]any{"afterFullFileContent": "value variant\n"},
			},
		},
		"result": map[string]any{
			"success": map[string]any{"path": "/workspace/src/variant.go"},
		},
	})
	body := stream(
		editEvent("src/app.py", "first\n", ""),
		editEvent("src/app.py", "second\n", ""),
		string(valueVariant),
	)
	texts := ExtractStreamSnapshots(body)
	if got := texts["src/app.py"]; got != "second\n" {
		t.Fatalf("later event must win per path, got %q", got)
	}
	if got := texts["src/variant.go"]; got != "value variant\n" {
		t.Fatalf("toolCall.result.value variant must be collected, got %q", got)
	}

	turnEnded, _ := json.Marshal(map[string]any{"type": "turn-ended"})
	result := VerifyStream(append(body, []byte("data: "+string(turnEnded)+"\n\n")...), testSourceBase)
	if !result.Success {
		t.Fatalf("multi-file stream must verify: %+v", result)
	}
	if len(result.Manifest.Files) != 2 {
		t.Fatalf("files = %+v, want both snapshot paths", result.Manifest.Files)
	}
}

func TestStreamFramingRules(t *testing.T) {
	// Non-data lines are ignored when hunting the turn-ended event, and an
	// empty data payload is not an event.
	framed := []byte(strings.Join([]string{
		": keep-alive comment",
		`event: turn`,
		`data: `,
		`data: {"type":"turn-ended"}`,
		"",
	}, "\n"))
	if complete, _ := StreamBodyIsComplete(framed); !complete {
		t.Fatalf("non-data lines must not hide the turn-ended event")
	}

	// A stream whose only event is turn-ended has no snapshots to verify.
	result := VerifyStream(framed, testSourceBase)
	if result.Success {
		t.Fatalf("turn-ended without snapshots must not verify")
	}
	if !strings.Contains(result.Error, "no full-file snapshots") {
		t.Fatalf("error %q must name the missing snapshots", result.Error)
	}

	// A branch name as the dispatched base never reaches stream verification.
	result = VerifyStream(completeStream("src/app.py", "x = 1\n"), "main")
	if result.Success {
		t.Fatalf("branch-name source base must not verify")
	}
	if !strings.Contains(result.Error, MissingArtifactEgress) {
		t.Fatalf("error %q must contain %q", result.Error, MissingArtifactEgress)
	}
}
