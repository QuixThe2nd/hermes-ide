package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"testing"
)

// cardJSON spells one per-event Card expectation without hand-counting
// characters: injected_chars is len(content), the same derivation the
// builder performs.
func cardJSON(kind, content, source string) string {
	return fmt.Sprintf(`{"kinds":[%q],"content":%q,"injected_chars":%d,`+
		`"sources":[%q]}`, kind, content, len(content), source)
}

// One op per request shape: every request the Python contract test can
// send must round-trip through run() to the documented output form. The
// shared-table VALUES are pinned by the package tests and the Python
// contract tests; these pin the cmd's request decoding itself.
func TestRunDecodesEveryOpShape(t *testing.T) {
	cases := []struct {
		name    string
		request string
		want    string
	}{
		{
			"heading",
			`{"op":"heading","kinds":["skill"],"memory_sourced":false,` +
				`"recalled_count":0,"meta":{"skill":"research"}}`,
			`["📚 Injecting skill (research)"]`,
		},
		{
			"continuation",
			`{"op":"continuation","kinds":["spillover"],"meta":{"spillover":"result.txt"},` +
				`"default":"📦 Large tool result saved to disk (result.txt)"}`,
			`"📄 Large tool result saved to disk (continued)"`,
		},
		{
			"classify",
			`{"op":"classify","text":"[Continuing toward your standing goal]\nGoal: x"}`,
			`{"kind":"goal","detail":""}`,
		},
		{
			"steer_card",
			`{"op":"steer_card","text":"turn left"}`,
			cardJSON("steer", "turn left", "user"),
		},
		{
			"spillover_card",
			`{"op":"spillover_card","stub":"<persisted-output>/tmp/s</persisted-output>"}`,
			cardJSON("spillover", "<persisted-output>/tmp/s</persisted-output>",
				"tool_result"),
		},
		{
			"subdirectory_card",
			`{"op":"subdirectory_card","hints":"[Subdirectory context discovered: pkg/AGENTS.md]"}`,
			cardJSON("subdirectory",
				"[Subdirectory context discovered: pkg/AGENTS.md]", "tool_result"),
		},
		{
			"compaction_card",
			`{"op":"compaction_card","summary":"[CONTEXT SUMMARY]: story"}`,
			cardJSON("compaction", "[CONTEXT SUMMARY]: story", "session"),
		},
		{
			"assembled_card",
			`{"op":"assembled_card","kind":"skill","detail":"research",` +
				`"content":"skill body"}`,
			`{"kinds":["skill"],"content":"skill body","injected_chars":10,` +
				`"sources":["session"],"meta":{"skill":"research"}}`,
		},
		{
			"compaction_summary_silent",
			`{"op":"compaction_summary","before":[{"role":"user","content":"old"}],` +
				`"after":[{"role":"user","content":"old"}]}`,
			`{"summary":"","changed":false}`,
		},
		{
			"compaction_summary_new",
			`{"op":"compaction_summary","before":[],` +
				`"after":[{"role":"user","content":"[CONTEXT SUMMARY]: x"}]}`,
			`{"summary":"[CONTEXT SUMMARY]: x","changed":true}`,
		},
		{
			"spilled_stub_string",
			`{"op":"spilled_stub","text":"<persisted-output>x</persisted-output>"}`,
			`{"stub":"<persisted-output>x</persisted-output>","found":true}`,
		},
		{
			"spilled_stub_parts",
			`{"op":"spilled_stub","parts":[{"type":"text","text":"clean"},` +
				`{"type":"text","text":"<persisted-output>y"}]}`,
			`{"stub":"<persisted-output>y","found":true}`,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var request map[string]any
			if err := json.Unmarshal([]byte(c.request), &request); err != nil {
				t.Fatalf("decode request: %v", err)
			}
			var stdout bytes.Buffer
			if err := run(request, &stdout); err != nil {
				t.Fatalf("run: %v", err)
			}
			assertJSONEquals(t, stdout.String(), c.want, c.name)
		})
	}
}

// TestSessionCardOpDerivesRealSha pins the sha-gating key the cmd emits:
// the sha256 hex of the composed content — the same derivation the package
// tests pin from the shared table. tool_count arrives as a plain int when
// a Go caller builds the request, and as a float64 after a JSON
// round-trip; both must count.
func TestSessionCardOpDerivesRealSha(t *testing.T) {
	for _, toolCount := range []any{2, float64(2)} {
		request := map[string]any{
			"op": "session_card",
			"sections": []any{
				map[string]any{"kind": "identity", "detail": "soul"},
				map[string]any{"kind": "timestamp"},
			},
			"ephemeral":  true,
			"tool_count": toolCount,
		}
		var stdout bytes.Buffer
		if err := run(request, &stdout); err != nil {
			t.Fatalf("run(tool_count=%v): %v", toolCount, err)
		}
		var out struct {
			Card struct {
				Content string `json:"content"`
			} `json:"card"`
			SHA string `json:"sha"`
		}
		if err := json.Unmarshal(stdout.Bytes(), &out); err != nil {
			t.Fatalf("decode output %q: %v", stdout.String(), err)
		}
		content := "identity (soul)\ntimestamp\n" +
			"session context (ephemeral, per-request)\n2 tool schemas"
		if out.Card.Content != content {
			t.Errorf("content = %q, want %q", out.Card.Content, content)
		}
		sum := sha256.Sum256([]byte(content))
		if want := hex.EncodeToString(sum[:]); out.SHA != want {
			t.Errorf("sha = %s, want %s", out.SHA, want)
		}
	}
}

// TestUnknownOpFailsClosed proves an unrecognized op is an error, not a
// silent empty success the Python bridge could mistake for parity.
func TestUnknownOpFailsClosed(t *testing.T) {
	var stdout bytes.Buffer
	err := run(map[string]any{"op": "mystery"}, &stdout)
	if err == nil {
		t.Fatal("unknown op succeeded")
	}
	if stdout.Len() != 0 {
		t.Errorf("unknown op wrote %q before failing", stdout.String())
	}
}

func assertJSONEquals(t *testing.T, got, want, name string) {
	t.Helper()
	var g, w any
	if err := json.Unmarshal([]byte(got), &g); err != nil {
		t.Fatalf("%s: decode output %q: %v", name, got, err)
	}
	if err := json.Unmarshal([]byte(want), &w); err != nil {
		t.Fatalf("%s: decode expectation %q: %v", name, want, err)
	}
	gj, _ := json.Marshal(g)
	wj, _ := json.Marshal(w)
	if string(gj) != string(wj) {
		t.Errorf("%s: output = %s, want %s", name, gj, wj)
	}
}
