// Package injectionlabels is the Go twin of the gateway context-injection
// heading-label contract in gateway/context_injection_labels.py: the label
// table that maps an injected segment's *kind* to the sentence-cased heading
// the progress card renders, the generic heading for everything the display
// layer cannot classify, and the memory heading with its optional discrete
// recall count.
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
//   - Duplicate OUTPUT lines render once, whatever kinds produced them.
package injectionlabels

import (
	"fmt"
	"slices"
)

// GenericLabel is the heading for every kind the display layer cannot
// classify: unknown kinds, blank kinds, and legacy payloads without kinds.
// Mirrors CONTEXT_INJECTION_GENERIC_LABEL.
const GenericLabel = "🧠 Injecting context"

// KindMemory is the special-cased memory kind: it keeps its dedicated
// heading rather than a table label. Mirrors KIND_MEMORY.
const KindMemory = "memory"

// KindLabels maps a known injection kind to its heading line. Mirrors
// CONTEXT_INJECTION_KIND_LABELS; a kind absent from the table renders
// GenericLabel.
var KindLabels = map[string]string{
	"message_id":     "📎 Injecting message id",
	"restart":        "🔁 Injecting restart notice",
	"voice":          "🎙️ Injecting voice status",
	"session_reset":  "🆕 Injecting session reset",
	"intro":          "👋 Injecting intro",
	"surface_switch": "🔀 Injecting surface switch",
	"plugin":         "🧩 Injecting plugin context",
}

// MemoryHeading renders the memory heading. A positive recalledCount is
// spoken discretely and pluralized ("1 memory", "3 memories"); anything
// else — zero, negative, or unknown — renders the plain form, mirroring the
// Python twin's guarded int check (bools and junk shapes never count).
func MemoryHeading(recalledCount int) string {
	if recalledCount > 0 {
		if recalledCount == 1 {
			return "🧠 Injecting 1 memory"
		}
		return fmt.Sprintf("🧠 Injecting %d memories", recalledCount)
	}
	return "🧠 Injecting memory"
}

// HeadingLines renders the ordered heading lines for an injection payload's
// kinds. Mirrors
// gateway.context_injection_labels.context_injection_heading_lines:
//
//   - nil/empty kinds is the legacy shape: the memory heading when the
//     payload was memory-sourced, else the generic label.
//   - the memory kind keeps its dedicated (optionally counted) heading.
//   - known kinds render their table label; unknown or blank kinds render
//     the generic label.
//   - duplicate output lines render once.
func HeadingLines(kinds []string, memorySourced bool, recalledCount int) []string {
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
				line = label
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
