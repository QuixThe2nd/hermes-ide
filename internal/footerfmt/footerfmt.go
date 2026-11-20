// Package footerfmt holds the pure formatting rules of the gateway
// runtime-metadata footer. It is the Go twin of
// gateway/runtime_footer.py — same rules, independently tested. Production
// Python never calls it; deployments (pip, docker) carry no Go toolchain. It
// exists so the footer format rules have a second, line-balance-accounted
// implementation (see internal/citationstrip for the pattern).
//
// Rules (shared with the Python twin):
//   - ShortModel drops a trailing vendor path segment ("openai/gpt-5.4" →
//     "gpt-5.4") and then the Anthropic maker prefix ("claude-opus-5-5" →
//     "opus-5-5"). Names without either prefix are unchanged: "gpt-5.4",
//     "gemini-3.1-pro", and a bare "claude" stay as they are.
//   - The reasoning effort renders as a parenthesized shorthand riding the
//     model field: "medium" → "med.", "minimal" → "min.", every other level
//     ("low", "high", "xhigh", "off") verbatim. No model → no effort.
//   - The tool-time segment renders "tools 0" when the turn made zero tool
//     calls or the measured time is exactly zero; otherwise "tools " plus the
//     human-friendly duration. An unmeasured (nil) or negative time renders
//     nothing — the segment is omitted, never "tools ?s".
package footerfmt

import (
	"fmt"
	"strings"
)

const makerPrefix = "claude-"

var effortShort = map[string]string{
	"medium":  "med.",
	"minimal": "min.",
}

// ShortModel shortens a model name for the footer: vendor path segment and
// Anthropic maker prefix dropped. The empty string stays empty.
func ShortModel(model string) string {
	if model == "" {
		return ""
	}
	short := model
	if i := strings.LastIndex(short, "/"); i >= 0 {
		short = short[i+1:]
	}
	if len(short) > len(makerPrefix) && strings.HasPrefix(short, makerPrefix) {
		return short[len(makerPrefix):]
	}
	return short
}

// EffortShorthand maps a reasoning effort to its footer shorthand. Unknown
// levels pass through verbatim; the empty effort stays empty.
func EffortShorthand(effort string) string {
	if effort == "" {
		return ""
	}
	if short, ok := effortShort[effort]; ok {
		return short
	}
	return effort
}

// ModelField renders the leading model segment, optionally carrying the
// effort shorthand: "opus-5-5 (med.)". Without an effort it is the bare
// short model; without a model it is "" (and then carries no effort either).
func ModelField(model, effort string) string {
	name := ShortModel(model)
	if name == "" {
		return ""
	}
	if tag := EffortShorthand(effort); tag != "" {
		return name + " (" + tag + ")"
	}
	return name
}

// FormatDuration humanizes a duration the way the footer does:
// 0.4s, 12s, 2m05s, 1h23m. A negative input renders "" (omitted).
func FormatDuration(seconds float64) string {
	if seconds < 0 {
		return ""
	}
	if seconds < 10 {
		return fmt.Sprintf("%.1fs", seconds)
	}
	if seconds < 60 {
		return fmt.Sprintf("%.0fs", seconds)
	}
	total := int(seconds)
	m, s := total/60, total%60
	if m < 60 {
		return fmt.Sprintf("%dm%02ds", m, s)
	}
	return fmt.Sprintf("%dh%02dm", m/60, m%60)
}

// ToolsField renders the tool-time segment. A nil toolTime (unmeasured) or a
// negative one renders "" (segment omitted). Zero tool calls — or an exactly
// zero measurement — render "tools 0"; anything else is "tools " plus
// FormatDuration. A nil toolCalls means the count was not measured and never
// forces the zero form on its own.
func ToolsField(toolTime *float64, toolCalls *int) string {
	if toolTime == nil || *toolTime < 0 {
		return ""
	}
	if *toolTime == 0 || (toolCalls != nil && *toolCalls == 0) {
		return "tools 0"
	}
	return "tools " + FormatDuration(*toolTime)
}
