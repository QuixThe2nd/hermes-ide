// Command injectionlabels is the executable form of the canonical Go twin:
// it reads one request as JSON on stdin and writes the rendered result to
// stdout.
//
// This is the bridge the Python tree executes: the contract tests in
// tests/gateway/test_context_injection_labels.py pipe every shared-table
// record (testdata/labels.json) through this binary and assert the Python
// twins — gateway/context_injection_labels.py for headings and
// classification, agent/context_injection_cards.py for the card payloads —
// produce the identical output, so the Python modules (the ones the
// runtime renders with) are pinned against the RUNNING Go implementation,
// not merely against the table. Production never shells out to it:
// deployments (pip, docker) carry no Go toolchain, the same reason every
// other internal/* twin is executed by tests rather than at runtime.
//
// Request shapes (one JSON object, "op" selects):
//
//	{"op": "heading", "kinds": ["skill"], "memory_sourced": false,
//	 "recalled_count": 0, "meta": {"skill": "research"}}
//	 → prints the heading lines as a JSON array.
//
//	{"op": "classify", "text": "[Continuing toward your standing goal]…"}
//	 → prints {"kind": "goal", "detail": ""}.
//
//	{"op": "steer_card", "text": "…"}        → prints the Card JSON.
//	{"op": "spillover_card", "stub": "…"}    → prints the Card JSON.
//	{"op": "subdirectory_card", "hints": "…"} → prints the Card JSON.
//	{"op": "compaction_card", "summary": "…"} → prints the Card JSON.
//	{"op": "assembled_card", "kind": "skill", "detail": "research",
//	 "content": "…"}                          → prints the Card JSON.
//
//	{"op": "session_card", "sections": [{"kind": "identity",
//	 "detail": "soul"}], "ephemeral": false, "tool_count": 2}
//	 → prints {"card": {…}, "sha": "…"} — the inventory card and its
//	   sha-gating key.
//
//	{"op": "compaction_summary", "before": [{"role": "user",
//	 "content": "…"}], "after": […]}
//	 → prints {"summary": "…", "changed": true} (summary "" when the
//	   flagged row carried no content).
//
//	{"op": "spilled_stub", "text": "…"}      → prints {"stub": "…",
//	 "found": true}.
//	{"op": "spilled_stub", "parts": [{"type": "text", "text": "…"}]}
//	 → same shape, detection over multimodal parts.
package main

import (
	"encoding/json"
	"fmt"
	"github.com/QuixThe2nd/hermes-ide/internal/injectionlabels"
	"io"
	"os"
)

func main() {
	var request map[string]any
	if err := json.NewDecoder(os.Stdin).Decode(&request); err != nil {
		fmt.Fprintf(os.Stderr, "injectionlabels: unreadable request: %v\n", err)
		os.Exit(2)
	}
	if err := run(request, os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "injectionlabels: %v\n", err)
		os.Exit(2)
	}
}

func run(request map[string]any, stdout io.Writer) error {
	encode := func(v any) error {
		return json.NewEncoder(stdout).Encode(v)
	}
	switch op, _ := request["op"].(string); op {
	case "heading":
		return encode(injectionlabels.HeadingLines(
			stringValues(request["kinds"]),
			boolValue(request["memory_sourced"]),
			intValue(request["recalled_count"]),
			stringMap(request["meta"]),
		))
	case "classify":
		kind, detail := injectionlabels.ClassifyUserMessage(stringValue(request["text"]))
		return encode(map[string]string{"kind": kind, "detail": detail})
	case "steer_card":
		return encode(injectionlabels.SteerCard(stringValue(request["text"])))
	case "spillover_card":
		return encode(injectionlabels.SpilloverCard(stringValue(request["stub"])))
	case "subdirectory_card":
		return encode(injectionlabels.SubdirectoryCard(stringValue(request["hints"])))
	case "compaction_card":
		return encode(injectionlabels.CompactionCard(stringValue(request["summary"])))
	case "assembled_card":
		return encode(injectionlabels.AssembledUserMessageCard(
			stringValue(request["kind"]), stringValue(request["detail"]),
			stringValue(request["content"]),
		))
	case "session_card":
		card, sha := injectionlabels.SessionInventoryCard(
			sectionsFromRequest(request["sections"]),
			boolValue(request["ephemeral"]),
			intValue(request["tool_count"]),
		)
		return encode(map[string]any{"card": card, "sha": sha})
	case "compaction_summary":
		summary, changed := injectionlabels.NewCompactionSummary(
			messagesFromRequest(request["before"]),
			messagesFromRequest(request["after"]),
		)
		return encode(map[string]any{"summary": summary, "changed": changed})
	case "spilled_stub":
		if parts, ok := request["parts"]; ok && parts != nil {
			stub, found := injectionlabels.SpilledStubFromParts(partsFromRequest(parts))
			return encode(map[string]any{"stub": stub, "found": found})
		}
		text := stringValue(request["text"])
		if injectionlabels.IsSpilledStub(text) {
			return encode(map[string]any{"stub": text, "found": true})
		}
		return encode(map[string]any{"stub": "", "found": false})
	default:
		return fmt.Errorf("unknown op %q", op)
	}
}

func sectionsFromRequest(v any) []injectionlabels.Section {
	rows, _ := v.([]any)
	sections := make([]injectionlabels.Section, 0, len(rows))
	for _, row := range rows {
		m, _ := row.(map[string]any)
		sections = append(sections, injectionlabels.Section{
			Kind:   stringValue(m["kind"]),
			Detail: stringValue(m["detail"]),
		})
	}
	return sections
}

func messagesFromRequest(v any) []injectionlabels.Message {
	rows, _ := v.([]any)
	messages := make([]injectionlabels.Message, 0, len(rows))
	for _, row := range rows {
		m, _ := row.(map[string]any)
		msg := injectionlabels.Message{
			Role:              stringValue(m["role"]),
			CompressedSummary: boolValue(m[injectionlabels.CompressedSummaryMetadataKey]),
		}
		if content, present := m["content"]; present && content != nil {
			rendered := stringValue(content)
			msg.Content = &rendered
		}
		messages = append(messages, msg)
	}
	return messages
}

func partsFromRequest(v any) []injectionlabels.TextPart {
	rows, _ := v.([]any)
	parts := make([]injectionlabels.TextPart, 0, len(rows))
	for _, row := range rows {
		m, _ := row.(map[string]any)
		parts = append(parts, injectionlabels.TextPart{
			Type: stringValue(m["type"]),
			Text: stringValue(m["text"]),
		})
	}
	return parts
}

func stringValue(v any) string {
	s, _ := v.(string)
	return s
}

func stringValues(v any) []string {
	items, _ := v.([]any)
	out := make([]string, 0, len(items))
	for _, item := range items {
		s, _ := item.(string)
		out = append(out, s)
	}
	return out
}

func stringMap(v any) map[string]string {
	m, _ := v.(map[string]any)
	out := make(map[string]string, len(m))
	for k, val := range m {
		out[k] = stringValue(val)
	}
	return out
}

func boolValue(v any) bool {
	b, _ := v.(bool)
	return b
}

func intValue(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case float64:
		return int(n)
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	}
	return 0
}
