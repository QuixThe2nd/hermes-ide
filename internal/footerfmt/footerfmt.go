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
//   - The overhead remainder splits into measured phase categories
//     (PhaseTail): mem, setup, compress, save render in that fixed order
//     after tools, each only at/above 0.1s but always subtracted from the
//     residual; the residual "other" renders only at/above 0.1s. With no
//     measured phase the legacy single "other" segment renders instead.
package footerfmt

import (
	"fmt"
	"math"
	"strings"
)

const makerPrefix = "claude-"

// phaseMinSeconds mirrors _PHASE_MIN_SECONDS: categories and the residual
// "other" render only once they reach this threshold.
const phaseMinSeconds = 0.1

// phaseOrder mirrors _PHASE_ORDER: the fixed render order of the measured
// phase categories after the tools segment.
var phaseOrder = []string{"mem", "setup", "compress", "save"}

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

// PhaseTail renders the overhead remainder as measured phase categories.
//
// A nil overhead (unmeasured) renders "" — segment omitted. A nil or empty
// phases map keeps the legacy single "other" render for older callers. With
// measured phases, each category in phaseOrder renders "key duration" only
// once it reaches phaseMinSeconds, but every measured value (even negative
// ones, clamped to zero) is subtracted from the overhead first, so
// sub-threshold time shrinks the residual instead of hiding inside it. The
// residual "other" renders last, again only at/above the threshold.
// Unknown phase keys are ignored, exactly like the Python twin.
func PhaseTail(overhead *float64, phases map[string]float64) string {
	if overhead == nil {
		return ""
	}
	if len(phases) == 0 {
		return labeled("other", *overhead)
	}
	var segments []string
	attributed := 0.0
	for _, key := range phaseOrder {
		seconds, ok := phases[key]
		if !ok {
			continue
		}
		attributed += math.Max(0, seconds)
		if seconds >= phaseMinSeconds {
			segments = append(segments, labeled(key, seconds))
		}
	}
	residual := math.Max(0, *overhead-attributed)
	if residual >= phaseMinSeconds {
		segments = append(segments, labeled("other", residual))
	}
	return strings.Join(segments, " · ")
}

// labeled renders "prefix duration", or "" when the duration itself renders
// empty (negative input) — the _labeled twin.
func labeled(prefix string, seconds float64) string {
	duration := FormatDuration(seconds)
	if duration == "" {
		return ""
	}
	return prefix + " " + duration
}

// LineInput carries the fields a gateway turn measures for the footer line.
// Pointer fields distinguish "unmeasured" (nil) from a real zero, mirroring
// the Python keyword arguments of format_runtime_footer.
type LineInput struct {
	Model         string
	Effort        string
	ContextTokens int
	ContextLength int
	TurnTime      *float64
	APITime       *float64
	ToolTime      *float64
	ToolCalls     *int
	APICalls      int
	Phases        map[string]float64
}

// ContextPct renders the context-percentage field: 0–100, rounded, with "%".
// A non-positive window (or negative tokens) omits the field — the Python
// twin never renders "?%".
func ContextPct(tokens, window int) string {
	if window <= 0 || tokens < 0 {
		return ""
	}
	pct := int(math.Round(float64(tokens) / float64(window) * 100))
	if pct < 0 {
		pct = 0
	}
	if pct > 100 {
		pct = 100
	}
	return fmt.Sprintf("%d%%", pct)
}

// APICallsField renders the call-count field: "1 call", "3 calls". Zero or
// negative counts omit the field, matching api_calls_field.
func APICallsField(calls int) string {
	if calls <= 0 {
		return ""
	}
	if calls == 1 {
		return "1 call"
	}
	return fmt.Sprintf("%d calls", calls)
}

// overhead derives the remainder from the turn/api/tool triple. A nil member
// means unmeasured and yields nil — the phase tail then renders nothing.
func overhead(turn, api, tool *float64) *float64 {
	if turn == nil || api == nil || tool == nil {
		return nil
	}
	v := math.Max(0, *turn-*api-*tool)
	return &v
}

// FooterLine composes the full runtime-metadata footer in field order:
// model (with effort), context %, turn/api/tool durations, the measured
// phase tail, then the call count. Fields without data are skipped and the
// non-empty segments join with " · " — the format_runtime_footer twin over
// the default field set.
func FooterLine(in LineInput) string {
	var segments []string
	add := func(s string) {
		if s != "" {
			segments = append(segments, s)
		}
	}
	add(ModelField(in.Model, in.Effort))
	add(ContextPct(in.ContextTokens, in.ContextLength))
	if in.TurnTime != nil {
		add(FormatDuration(*in.TurnTime))
	}
	if in.APITime != nil {
		add(labeled("api", *in.APITime))
	}
	add(ToolsField(in.ToolTime, in.ToolCalls))
	add(PhaseTail(overhead(in.TurnTime, in.APITime, in.ToolTime), in.Phases))
	add(APICallsField(in.APICalls))
	add(ToolCallsField(in.ToolCalls))
	return strings.Join(segments, " · ")
}

// ToolCallsField renders the tool-call-count field: "1 tool", "9 tools".
// An unmeasured (nil) count or a non-positive one omits the field —
// tool_calls_field twin. It never forces ToolsField's zero form; that is
// ToolsField's own rule.
func ToolCallsField(calls *int) string {
	if calls == nil || *calls <= 0 {
		return ""
	}
	if *calls == 1 {
		return "1 tool"
	}
	return fmt.Sprintf("%d tools", *calls)
}

// ParsePhases mirrors phase_times_from_result: pull the measured phase
// seconds out of a result-shaped map with "<key>_time" keys, ignoring
// non-numeric values. Returns nil when nothing was measured so callers keep
// the legacy single-"other" render.
func ParsePhases(result map[string]float64) map[string]float64 {
	phases := make(map[string]float64)
	for _, key := range phaseOrder {
		if v, ok := result[key+"_time"]; ok {
			phases[key] = v
		}
	}
	if len(phases) == 0 {
		return nil
	}
	return phases
}
