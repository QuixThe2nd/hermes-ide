// Package modelcmd is the canonical pure-logic core of the /model slash
// command: argument/flag parsing, the no-argument listing text, and the
// interactive picker's provider rows.
//
// hermes_cli/modelcmd.py is the Python twin the runtime calls — repo
// deployments (pip, docker) carry no Go toolchain, the same arrangement as
// internal/servingidentity and internal/generations. The contract test
// (tests/hermes_cli/test_modelcmd_contract.py) pipes every shared-table
// record (testdata/cases.json) through the RUNNING Go binary
// (cmd/modelcmd) when a toolchain exists and asserts the twin renders the
// identical output, so the two implementations cannot drift.
//
// Parsing is a faithful port of hermes_cli/model_switch.py
// parse_model_switch_args (the ONE parser every surface shares): flag
// names, the Unicode-dash normalization for mobile keyboards, the
// trailing-bare-value-flag rule, the error codes and their canonical copy,
// and the scope precedence once > session > global > default. The listing
// layout mirrors the gateway's no-arg /model reply; provider and model
// identifiers are never translated — only the label strings carried in the
// record are localized, and they arrive pre-rendered from the caller.
package modelcmd

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

// Flag-error codes, shared with the Python parser's MODEL_SWITCH_ERR_*.
const (
	ErrOnceWithGlobal     = "once_with_global"
	ErrOnceRequiresTarget = "once_requires_target"
	ErrBadReasoning       = "bad_reasoning"
)

// Canonical error copy. Surfaces may prepend their own decoration ("❌ "
// in the gateway, "  ✗ " in the CLI) but must not change the sentence.
var errorText = map[string]string{
	ErrOnceWithGlobal:     "/model --once cannot be combined with --global",
	ErrOnceRequiresTarget: "/model --once requires a model or provider.",
	ErrBadReasoning:       "/model --reasoning takes none, minimal, low, medium, high, xhigh, max or ultra.",
}

// FlagError is one parse error: a stable code plus its canonical copy.
type FlagError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Parsed is the wire shape of one parsed /model command: the same fields
// hermes_cli.model_switch.ModelSwitchRequest carries.
type Parsed struct {
	Raw              string      `json:"raw"`
	Target           string      `json:"target"`
	ExplicitProvider string      `json:"explicit_provider"`
	ReasoningEffort  string      `json:"reasoning_effort"`
	IsGlobal         bool        `json:"is_global"`
	IsSession        bool        `json:"is_session"`
	IsOnce           bool        `json:"is_once"`
	ForceRefresh     bool        `json:"force_refresh"`
	Scope            string      `json:"scope"`
	Errors           []FlagError `json:"errors"`
}

// dashRE matches one Unicode dash (U+2012..U+2015) directly before a
// flag keyword: mobile keyboards auto-convert "--" to an en/em dash.
var dashRE = regexp.MustCompile(`[‒–—―](provider|reasoning|global|session|refresh|once)`)

// validReasoning is the accepted --reasoning vocabulary: the canonical
// effort ladder plus the disable words parse_reasoning_effort accepts.
var validReasoning = map[string]bool{
	"none": true, "false": true, "disabled": true,
	"minimal": true, "low": true, "medium": true, "high": true,
	"xhigh": true, "max": true, "ultra": true,
}

// Parse tokenizes a /model argument string and validates flag conflicts.
//
// Boolean flags: --global, --session, --refresh, --once. Value flags:
// --provider <slug>, --reasoning <level>. A value flag as the FINAL token
// has no value to consume and stays part of the model text (the historical
// parser never required shell quoting, and model IDs may contain colons
// and slashes). Targets pass through untouched for switch_model.
func Parse(raw string) Parsed {
	normalized := dashRE.ReplaceAllString(raw, "--$1")
	tokens := strings.Fields(normalized)

	p := Parsed{Raw: raw, Scope: "default", Errors: []FlagError{}}
	var filtered []string
	for i := 0; i < len(tokens); i++ {
		tok := tokens[i]
		switch {
		case tok == "--global":
			p.IsGlobal = true
		case tok == "--session":
			p.IsSession = true
		case tok == "--refresh":
			p.ForceRefresh = true
		case tok == "--once":
			p.IsOnce = true
		case (tok == "--provider" || tok == "--reasoning") && i+1 < len(tokens):
			i++
			if tok == "--provider" {
				p.ExplicitProvider = tokens[i]
			} else {
				p.ReasoningEffort = tokens[i]
			}
		default:
			filtered = append(filtered, tok)
		}
	}
	p.Target = strings.Join(filtered, " ")

	var codes []string
	if p.IsOnce && p.IsGlobal {
		codes = append(codes, ErrOnceWithGlobal)
	}
	if p.IsOnce && p.Target == "" && p.ExplicitProvider == "" {
		codes = append(codes, ErrOnceRequiresTarget)
	}
	if p.ReasoningEffort != "" && !validReasoning[strings.ToLower(strings.TrimSpace(p.ReasoningEffort))] {
		codes = append(codes, ErrBadReasoning)
	}
	for _, code := range codes {
		p.Errors = append(p.Errors, FlagError{Code: code, Message: errorText[code]})
	}
	// First matching flag wins: once > session > global > default.
	switch {
	case p.IsOnce:
		p.Scope = "once"
	case p.IsSession:
		p.Scope = "session"
	case p.IsGlobal:
		p.Scope = "global"
	}
	return p
}

// ListingProvider is one provider row of the no-arg listing. Models is the
// (possibly capped) model-id list shown; TotalModels is the provider's full
// count, so hidden models surface as "+N more".
type ListingProvider struct {
	Name        string   `json:"name"`
	Slug        string   `json:"slug"`
	IsCurrent   bool     `json:"is_current"`
	Models      []string `json:"models"`
	TotalModels int      `json:"total_models"`
	APIURL      string   `json:"api_url"`
}

// ListingRecord is everything the no-arg listing needs. Labels carry the
// pre-localized template strings (agent.i18n on the Python side); the
// format placeholders are {model}, {provider}, {count}.
type ListingRecord struct {
	CurrentModel         string            `json:"current_model"`
	CurrentProviderLabel string            `json:"current_provider_label"`
	Labels               map[string]string `json:"labels"`
	Providers            []ListingProvider `json:"providers"`
}

// English label defaults for records built without a Labels map.
var defaultListingLabels = map[string]string{
	"current_label":         "Current: `{model}` on {provider}",
	"current_tag":           " (current)",
	"more_models_suffix":    " (+{count} more)",
	"usage_switch_model":    "`/model <name>` — switch model",
	"usage_switch_provider": "`/model <name> --provider <slug>` — switch provider",
	"usage_persist":         "`/model <name> --global` — persist",
}

func label(labels map[string]string, key string) string {
	if v, ok := labels[key]; ok && v != "" {
		return v
	}
	return defaultListingLabels[key]
}

// format substitutes {key} placeholders in tmpl. Unknown placeholders are
// left verbatim (str.format raises; the twin falls back to the raw string,
// and leaving the token is equally inert on the Go side).
func format(tmpl string, vars map[string]string) string {
	out := tmpl
	keys := make([]string, 0, len(vars))
	for k := range vars {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		out = strings.ReplaceAll(out, "{"+k+"}", vars[k])
	}
	return out
}

// RenderListing lays out the no-arg /model reply: the current-model line,
// one block per provider (name + --provider slug + current tag, the shown
// models with the "+N more" tail, or the endpoint when no models are
// listed), then the three usage lines.
func RenderListing(rec ListingRecord) string {
	var lines []string
	lines = append(lines, format(label(rec.Labels, "current_label"), map[string]string{
		"model":    rec.CurrentModel,
		"provider": rec.CurrentProviderLabel,
	}))
	lines = append(lines, "")
	for _, p := range rec.Providers {
		tag := ""
		if p.IsCurrent {
			tag = label(rec.Labels, "current_tag")
		}
		lines = append(lines, fmt.Sprintf("**%s** `--provider %s`%s:", p.Name, p.Slug, tag))
		if len(p.Models) > 0 {
			quoted := make([]string, len(p.Models))
			for i, m := range p.Models {
				quoted[i] = "`" + m + "`"
			}
			extra := ""
			if hidden := p.TotalModels - len(p.Models); hidden > 0 {
				extra = format(label(rec.Labels, "more_models_suffix"), map[string]string{
					"count": fmt.Sprintf("%d", hidden),
				})
			}
			lines = append(lines, fmt.Sprintf("  %s%s", strings.Join(quoted, ", "), extra))
		} else if p.APIURL != "" {
			lines = append(lines, fmt.Sprintf("  `%s`", p.APIURL))
		}
		lines = append(lines, "")
	}
	lines = append(lines,
		label(rec.Labels, "usage_switch_model"),
		label(rec.Labels, "usage_switch_provider"),
		label(rec.Labels, "usage_persist"))
	return strings.Join(lines, "\n")
}

// Picker caps: Discord select menus hold at most 25 options and the picker
// view reads at most 5 rows, so providers beyond the cap can never be
// reached. The current provider always survives the cap.
const (
	DefaultMaxPickerProviders = 25
	DefaultMaxPickerModels    = 50
)

// PickerOptions trims raw provider rows into what the interactive picker
// can actually render, working on arbitrary JSON rows so unknown fields
// pass through untouched:
//
//   - rows without any model AND without an api_url are dropped (a provider
//     the picker cannot offer is noise);
//   - each row's model list is capped at maxModels (total_models keeps the
//     full count so "+N more" stays truthful);
//   - is_current is set exactly on the row matching currentProvider
//     (slug-normalized) and cleared everywhere else;
//   - at most maxProviders rows survive, the current provider always among
//     them (the last non-current rows are dropped first).
func PickerOptions(providers []map[string]any, currentProvider string, maxProviders, maxModels int) []map[string]any {
	if maxProviders <= 0 {
		maxProviders = DefaultMaxPickerProviders
	}
	if maxModels <= 0 {
		maxModels = DefaultMaxPickerModels
	}
	current := strings.ToLower(strings.TrimSpace(currentProvider))

	var rows []map[string]any
	for _, p := range providers {
		models := stringSlice(p["models"])
		if len(models) == 0 && strings.TrimSpace(stringValue(p["api_url"])) == "" {
			continue
		}
		if len(models) > maxModels {
			p["models"] = toAnySlice(models[:maxModels])
		}
		p["is_current"] = strings.ToLower(stringValue(p["slug"])) == current
		rows = append(rows, p)
	}

	if len(rows) > maxProviders {
		currentIdx := -1
		for i, r := range rows {
			if r["is_current"] == true {
				currentIdx = i
				break
			}
		}
		keep := rows[:maxProviders]
		if currentIdx >= maxProviders {
			// Swap the current provider in for the last kept slot so the
			// active route is always pickable.
			keep[maxProviders-1], rows[currentIdx] = rows[currentIdx], keep[maxProviders-1]
		}
		rows = keep
	}
	return rows
}

// stringValue coerces a JSON-decoded scalar to a string ("" for anything
// that is not one).
func stringValue(v any) string {
	s, _ := v.(string)
	return s
}

// stringSlice coerces a JSON-decoded array of scalars to []string,
// skipping non-string entries.
func stringSlice(v any) []string {
	arr, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, e := range arr {
		if s, ok := e.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// toAnySlice re-boxes a []string as []any for JSON re-encoding.
func toAnySlice(in []string) []any {
	out := make([]any, len(in))
	for i, s := range in {
		out[i] = s
	}
	return out
}
