// Package injectionlabels is the Go twin of the gateway context-injection
// heading-label contract in gateway/context_injection_labels.py: the label
// table that maps an injected segment's *kind* to the sentence-cased heading
// the progress card renders, the generic heading for everything the display
// layer cannot classify, the memory heading with its optional discrete
// recall count, the meta suffix that names WHAT a kind carried (the skill's
// name, the goal's title), and the anchored classifier for user messages
// that were assembled rather than typed (standing-goal continuations, skill
// invocations).
//
// This is the canonical statement of the contract; the Python module is what
// the runtime renders with (executed by tests, not imported at runtime —
// deployments (pip, docker) carry no Go toolchain, the same arrangement as
// every other internal/* twin). The shared table
// (testdata/labels.json) is asserted from BOTH sides: the Go tests here and
// the Python contract test (tests/gateway/test_context_injection_labels.py)
// read the same bytes, so the twins cannot drift.
//
// Contract (shared with the Python twin):
//   - Known kinds render exactly their table label; the kind set is additive
//     display metadata that never changes the API-bound bytes.
//   - Anything else — an unknown kind, a blank kind, or a legacy payload
//     without kinds — renders the generic heading. Unclassifiable bytes are
//     never given a specific label they may not deserve.
//   - Memory is special-cased: it keeps its dedicated heading (with the
//     discrete recall count pluralized when a positive count is known)
//     rather than a table label.
//   - A legacy payload (no kinds) derives from memory-sourced alone, exactly
//     as the pre-kinds formatter did.
//   - A kind with a non-empty meta detail renders "label (detail)", or the
//     kind's detail template when it has one (the detail names WHAT was
//     carried in place of the generic tail); the detail never changes the
//     label bytes themselves. Memory ignores meta (its count already
//     speaks), and meta on an unclassifiable kind is dropped with the label.
//   - Duplicate OUTPUT lines render once, whatever kinds produced them.
package injectionlabels

import (
	"fmt"
	"slices"
	"strings"
)

// GenericLabel is the heading for every kind the display layer cannot
// classify: unknown kinds, blank kinds, and legacy payloads without kinds.
// Mirrors CONTEXT_INJECTION_GENERIC_LABEL.
const GenericLabel = "🧠 Injecting context"

// KindMemory is the special-cased memory kind: it keeps its dedicated
// heading rather than a table label. Mirrors KIND_MEMORY.
const KindMemory = "memory"

// SpilloverContinuationLabel distinguishes later chunks of one persisted-output
// card from a separate card. Mirrors CONTEXT_INJECTION_SPILLOVER_CONTINUATION_LABEL.
const SpilloverContinuationLabel = "📄 Large tool result saved to disk (continued)"

// KindLabels maps a known injection kind to its heading line. Mirrors
// CONTEXT_INJECTION_KIND_LABELS; a kind absent from the table renders
// GenericLabel. The kinds split by surface:
//
//   - gateway envelope (message_id, restart, session_reset, intro,
//     surface_switch, plugin, process, code_delta) — bytes added around the
//     clean user message;
//   - session — the once-per-session inventory of what the system prompt
//     carries (identity, context files, skills index, workspace, …);
//   - goal / skill — the current user message was assembled by the
//     standing-goal loop or a skill slash command, not typed by the user;
//   - compaction — a conversation summary replaced pruned history;
//   - steer — a mid-turn user steering row landed after a tool result;
//   - subdirectory / spillover — context appended inside a tool result
//     (directory AGENTS.md hints, persisted-output stubs).
var KindLabels = map[string]string{
	"message_id":     "📎 Injecting message id",
	"restart":        "🔁 Injecting restart notice",
	"session_reset":  "🆕 Injecting session reset",
	"intro":          "👋 Injecting intro",
	"surface_switch": "🔀 Injecting surface switch",
	"plugin":         "🧩 Injecting plugin context",
	"process":        "🖥️ Injecting process info",
	"code_delta":     "📝 Injecting code delta",
	"session":        "🗺️ Injecting session context",
	"goal":           "🎯 Injecting standing goal",
	"skill":          "📚 Injecting skill",
	"compaction":     "🗜️ Injecting conversation summary",
	"steer":          "🧭 Injecting steering update",
	"subdirectory":   "🗂️ Injecting directory context",
	"spillover":      "📦 Large tool result saved to disk",
}

// KindDetailTemplates maps a kind to the heading template used when its meta
// carries a detail: the detail REPLACES the generic tail of the label
// ("🗂️ Injecting pkg/AGENTS.md" names the loaded file, where
// "🗂️ Injecting directory context (pkg/AGENTS.md)" buries it). Kinds absent
// from this map keep the "label (detail)" suffix of FormatHeading; a blank
// or absent detail falls back to the table label either way. Mirrors
// CONTEXT_INJECTION_KIND_DETAIL_TEMPLATES; the shared table
// (testdata/labels.json, "kind_detail_templates") pins both byte-for-byte.
var KindDetailTemplates = map[string]string{
	"subdirectory": "🗂️ Injecting {detail}",
}

// MemoryHeading renders the memory heading. A positive recalledCount is
// spoken discretely and pluralized ("1 memory", "3 memories"); anything
// else — zero, negative, or unknown — renders the plain form, mirroring the
// Python twin's guarded int check (bools and junk shapes never count).
func MemoryHeading(recalledCount int) string {
	switch {
	case recalledCount == 1:
		return "🧠 Injecting 1 memory"
	case recalledCount > 1:
		return fmt.Sprintf("🧠 Injecting %d memories", recalledCount)
	default:
		return "🧠 Injecting memory"
	}
}

// FormatHeading renders one table label with its optional meta detail:
// "label (detail)". Blank detail renders the bare label, so a producer
// without a name never renders empty parentheses.
func FormatHeading(label, detail string) string {
	if detail == "" {
		return label
	}
	return label + " (" + detail + ")"
}

// FormatKindHeading renders one kind's heading: the kind's detail template
// when it has one and a detail was carried, else the plain
// "label (detail)" suffix. The detail substitutes verbatim — it is never
// passed as a format string, so a path containing a percent sign or braces
// renders as-is. Mirrors _format_kind_heading in the Python twin.
func FormatKindHeading(kind, label, detail string) string {
	if template, ok := KindDetailTemplates[kind]; ok && detail != "" {
		return strings.Replace(template, "{detail}", detail, 1)
	}
	return FormatHeading(label, detail)
}

// ContinuationHeading labels later chunks of a lone spillover card without
// making them look like separate injections. Metadata details belong to the
// primary heading; every other kind shape retains its existing heading.
func ContinuationHeading(kinds []string, meta map[string]string, defaultHeading string) string {
	if len(kinds) == 1 && kinds[0] == "spillover" {
		return SpilloverContinuationLabel
	}
	return defaultHeading
}

// HeadingLines renders the ordered heading lines for an injection payload's
// kinds. Mirrors
// gateway.context_injection_labels.context_injection_heading_lines:
//
//   - nil/empty kinds is the legacy shape: the memory heading when the
//     payload was memory-sourced, else the generic label.
//   - the memory kind keeps its dedicated (optionally counted) heading;
//     meta never applies to it.
//   - known kinds render their table label, suffixed with the kind's meta
//     detail when one was carried (or through the kind's detail template
//     when it has one); unknown or blank kinds render the generic label
//     (their meta is dropped with the label).
//   - duplicate output lines render once.
func HeadingLines(kinds []string, memorySourced bool, recalledCount int, meta map[string]string) []string {
	if len(kinds) == 0 {
		if memorySourced {
			return []string{MemoryHeading(recalledCount)}
		}
		return []string{GenericLabel}
	}
	var lines []string
	for _, kind := range kinds {
		var line string
		switch {
		case kind == KindMemory:
			line = MemoryHeading(recalledCount)
		case kind != "":
			if label, ok := KindLabels[kind]; ok {
				line = FormatKindHeading(kind, label, meta[kind])
			} else {
				line = GenericLabel
			}
		default:
			line = GenericLabel
		}
		if !slices.Contains(lines, line) {
			lines = append(lines, line)
		}
	}
	return lines
}

// Anchors for ClassifyUserMessage — the exact bytes the goal loop and the
// skill-command builder emit, mirrored from hermes_cli/goals.py
// (CONTINUATION_PROMPT_TEMPLATE and friends) and agent/skill_commands.py
// (build_skill_invocation_message). Prefix/substring anchors, not a parser:
// the note bytes are load-bearing prompt scaffolding and stable by
// contract.
const (
	goalContinuationPrefix = "[Continuing toward your standing goal"
	skillInvocationOpen    = `[IMPORTANT: The user has invoked the "`
)

// ClassifyUserMessage anchors a clean user message to the subsystem that
// assembled it. Returns ("goal", ""), ("skill", "<skill name>"), or ("", "")
// for a message the user typed themselves. Goal beats skill ordering: the
// goal-continuation templates never embed the skill note, so the prefix
// check is unambiguous. Mirrors classify_user_message_injection.
func ClassifyUserMessage(text string) (kind, detail string) {
	if strings.HasPrefix(text, goalContinuationPrefix) {
		return "goal", ""
	}
	if i := strings.Index(text, skillInvocationOpen); i >= 0 {
		rest := text[i+len(skillInvocationOpen):]
		if j := strings.IndexByte(rest, '"'); j > 0 {
			return "skill", rest[:j]
		}
	}
	return "", ""
}
